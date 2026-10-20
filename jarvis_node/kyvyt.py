"""Noden kyvyt: Ollama (llm.*), ComfyUI (comfy.aja), palveluiden käynnistys ja tila.

Kyky = `fn(args: dict, ctx) -> dict`. ctx tarjoaa `.progress(viesti, pct)`,
`.peruttu()`, `.tallenna_tiedosto(nimi, data)`, `.syke()` ja `.aikaraja_s`.

HTTP-kutsut kulkevat moduulitason funktioiden `http_json`/`http_bytes` kautta,
jotta testit voivat korvata ne (monkeypatch). Samoin `aja_komento`,
`kaynnista_irti` ja `_nuku`.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

log = logging.getLogger("jarvis_node.kyvyt")

Kyky = Callable[[dict, Any], dict]

COMFY_VRAM_MB = 6000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

# Ei välityspalvelinta paikallisiin palveluihin
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Peruttu(Exception):
    """Kyky keskeytettiin peruutuksen takia."""


# --------------------------------------------------------------------------
# HTTP (injektoitavat)


def http_bytes(method: str, url: str, data: bytes | None = None,
               headers: dict[str, str] | None = None, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with _opener.open(req, timeout=timeout) as vastaus:
            return vastaus.read()
    except urllib.error.HTTPError as exc:
        teksti = exc.read()[:500].decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {teksti}") from exc


def http_json(method: str, url: str, data: Any = None, timeout: float = 30.0) -> Any:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else None
    raw = http_bytes(method, url, body, headers, timeout)
    return json.loads(raw) if raw.strip() else None


def _nuku(s: float) -> None:
    time.sleep(s)


# --------------------------------------------------------------------------
# Palvelut: tila, käynnistys, sammutus


def _palvelut(config: dict) -> dict[str, dict]:
    return dict(config.get("palvelut") or {})


def palvelu_url(palvelu: dict, polku: str = "") -> str:
    return f"http://127.0.0.1:{int(palvelu['portti'])}{polku}"


def terveys_ok(palvelu: dict, timeout: float = 1.5) -> bool | None:
    """True/False terveys-URL:n perusteella; None jos palvelulla ei ole porttia."""
    if not palvelu.get("portti"):
        return None
    try:
        http_bytes("GET", palvelu_url(palvelu, palvelu.get("terveys") or "/"), timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - mikä tahansa virhe = ei vastaa
        return False


def palveluiden_tila(config: dict) -> dict[str, dict]:
    """{nimi: {"kaynnissa","portti","tiedot"}}; terveystarkistukset rinnakkain."""
    palvelut = _palvelut(config)
    tulos: dict[str, dict] = {}

    def tarkista(nimi: str, p: dict) -> None:
        tulos[nimi] = {"kaynnissa": terveys_ok(p),
                       "portti": int(p["portti"]) if p.get("portti") else None,
                       "tiedot": {}}

    saikeet = [threading.Thread(target=tarkista, args=(n, p), daemon=True)
               for n, p in palvelut.items()]
    for s in saikeet:
        s.start()
    for s in saikeet:
        s.join(3.0)
    for nimi, p in palvelut.items():
        tulos.setdefault(nimi, {"kaynnissa": None, "portti": p.get("portti"), "tiedot": {}})
    return tulos


def ollama_ladattu(config: dict) -> list[dict]:
    """Ollaman muistissa olevat mallit (/api/ps)."""
    p = _palvelut(config).get("ollama")
    if not p or not p.get("portti"):
        return []
    try:
        data = http_json("GET", palvelu_url(p, "/api/ps"), timeout=2.0) or {}
    except Exception:  # noqa: BLE001
        return []
    return [{"nimi": str(m.get("name") or m.get("model") or ""),
             "vram_mb": int((m.get("size_vram") or 0) // 1048576),
             "vanhenee": m.get("expires_at")}
            for m in data.get("models") or []]


def aja_komento(argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                              timeout=timeout,
                              creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        return 124, "aikakatkaisu"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def kaynnista_irti(palvelu: dict) -> int:
    """Käynnistä palvelun ohjelma irrotettuna; palauttaa pid:n."""
    exe = os.path.expandvars(str(palvelu["kaynnistys"]))
    argumentit = os.path.expandvars(str(palvelu.get("argumentit") or ""))
    cwd = os.path.expandvars(str(palvelu["kansio"])) if palvelu.get("kansio") else None
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (palvelu.get("ymparisto") or {}).items()})
    loki = palvelu.get("loki")
    ulos: Any = subprocess.DEVNULL
    if loki:
        ulos = open(os.path.expandvars(str(loki)), "ab")  # noqa: SIM115 - lapsi perii kahvan
    try:
        if os.name == "nt":
            proc = subprocess.Popen(
                f'"{exe}" {argumentit}'.strip(), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=ulos, stderr=subprocess.STDOUT, close_fds=True,
                creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        else:
            proc = subprocess.Popen([exe, *shlex.split(argumentit)], cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=ulos,
                                    stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        if loki:
            ulos.close()
    return proc.pid


def _ps_lainaus(teksti: str) -> str:
    return "'" + str(teksti).replace("'", "''") + "'"


def kaynnista_palvelu(nimi: str, palvelu: dict) -> str:
    """Windows: ajastettu tehtävä tai irrotettu prosessi. Muualla irrotettu prosessi."""
    tehtava = palvelu.get("tehtava")
    if os.name == "nt" and tehtava:
        koodi, ulos = aja_komento(["schtasks", "/Run", "/TN", str(tehtava)])
        if koodi != 0:
            raise RuntimeError(f"schtasks /Run {tehtava} epäonnistui: {ulos[:300]}")
        return f"{nimi}: ajastettu tehtävä '{tehtava}' käynnistetty"
    if not palvelu.get("kaynnistys"):
        raise RuntimeError(f"palvelulle {nimi} ei ole käynnistystapaa tällä koneella")
    pid = kaynnista_irti(palvelu)
    return f"{nimi}: käynnistetty (pid {pid})"


def sammuta_palvelu(nimi: str, palvelu: dict) -> str:
    tunniste = str(palvelu.get("tunniste") or "")
    if not tunniste:
        raise RuntimeError(f"palvelulla {nimi} ei ole tunnistetta sammutusta varten")
    if os.name == "nt":
        if palvelu.get("tehtava"):
            aja_komento(["schtasks", "/End", "/TN", str(palvelu["tehtava"])])
        skripti = (
            "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
            f"{_ps_lainaus('*' + tunniste + '*')} -and $_.ProcessId -ne $PID "
            f"-and $_.ProcessId -ne {os.getpid()} }}; "
            "foreach ($x in $p) { Stop-Process -Id $x.ProcessId -Force "
            "-ErrorAction SilentlyContinue }; \"SAMMUTETTU $(@($p).Count)\"")
        koodi, ulos = aja_komento(["powershell", "-NoProfile", "-NonInteractive",
                                   "-Command", skripti])
    else:
        koodi, ulos = aja_komento(["pkill", "-f", tunniste])
        # pkill: 1 = ei osumia
        if koodi == 1:
            koodi, ulos = 0, "ei käynnissä"
    if koodi != 0:
        raise RuntimeError(f"sammutus epäonnistui: {ulos[:300]}")
    return f"{nimi}: {ulos.strip() or 'sammutettu'}"


def _odota_tila(palvelu: dict, haluttu: bool, odota_s: float, ctx: Any, nimi: str) -> bool:
    """Odota kunnes terveys == haluttu tai aika loppuu. Palauttaa lopputilan."""
    alku = time.monotonic()
    while True:
        tila = terveys_ok(palvelu)
        if tila is None or tila == haluttu:
            return bool(tila)
        kulunut = time.monotonic() - alku
        if kulunut >= odota_s or ctx.peruttu():
            return bool(tila)
        teko = "käynnistyy" if haluttu else "sammuu"
        ctx.progress(f"{nimi} {teko}… ({int(kulunut)} s)",
                     min(95, 10 + int(85 * kulunut / max(odota_s, 1))))
        _nuku(1.0)


def _palvelu_kyky(config: dict, kaynnista: bool) -> Kyky:
    def fn(args: dict, ctx: Any) -> dict:
        nimi = str(args.get("nimi") or "")
        palvelu = _palvelut(config).get(nimi)
        if palvelu is None:
            raise ValueError(f"tuntematon palvelu '{nimi}' (tunnetut: "
                             f"{', '.join(_palvelut(config)) or '-'})")
        if kaynnista:
            if terveys_ok(palvelu):
                return {"viesti": f"{nimi} on jo käynnissä", "kaynnissa": True}
            viesti = kaynnista_palvelu(nimi, palvelu)
            odota = float(palvelu.get("odota_s") or 60)
        else:
            viesti = sammuta_palvelu(nimi, palvelu)
            odota = 15.0
        if getattr(ctx, "aikaraja_s", None):
            odota = min(odota, float(ctx.aikaraja_s))
        ctx.progress(viesti, 10)
        tila = _odota_tila(palvelu, kaynnista, odota, ctx, nimi)
        if palvelu.get("portti") is None:
            tila = kaynnista
        return {"viesti": viesti, "kaynnissa": tila}

    return fn


# --------------------------------------------------------------------------
# Ollama


def _ollama_kyvyt(config: dict) -> dict[str, tuple[Kyky, dict]]:
    p = _palvelut(config)["ollama"]

    def aikaraja(ctx: Any) -> float:
        return float(getattr(ctx, "aikaraja_s", None) or 600)

    def chat(args: dict, ctx: Any) -> dict:
        malli = args.get("malli") or p.get("oletus_malli")
        if not malli:
            raise ValueError("malli puuttuu")
        data: dict[str, Any] = {"model": malli, "messages": list(args.get("viestit") or []),
                                "stream": False}
        if args.get("asetukset"):
            data["options"] = args["asetukset"]
        if "keep_alive" in args:
            data["keep_alive"] = args["keep_alive"]
        ctx.progress(f"Ollama ({malli}) vastaa…", 10)
        vastaus = http_json("POST", palvelu_url(p, "/api/chat"), data, timeout=aikaraja(ctx)) or {}
        maara, kesto = vastaus.get("eval_count"), vastaus.get("eval_duration")
        tps = round(maara / (kesto / 1e9), 2) if maara and kesto else None
        return {"vastaus": str((vastaus.get("message") or {}).get("content") or ""),
                "malli": str(vastaus.get("model") or malli), "tokenit_s": tps}

    def vision(args: dict, ctx: Any) -> dict:
        malli = args.get("malli") or p.get("vision_malli") or p.get("oletus_malli")
        if not malli:
            raise ValueError("malli puuttuu")
        viesti = {"role": "user", "content": str(args.get("kysymys") or ""),
                  "images": list(args.get("kuvat_b64") or [])}
        ctx.progress(f"Ollama ({malli}) katsoo kuvaa…", 10)
        vastaus = http_json("POST", palvelu_url(p, "/api/chat"),
                            {"model": malli, "messages": [viesti], "stream": False},
                            timeout=aikaraja(ctx)) or {}
        return {"vastaus": str((vastaus.get("message") or {}).get("content") or ""),
                "malli": str(vastaus.get("model") or malli)}

    def mallit(args: dict, ctx: Any) -> dict:
        data = http_json("GET", palvelu_url(p, "/api/tags"), timeout=10.0) or {}
        return {"mallit": [{"nimi": str(m.get("name") or m.get("model") or ""),
                            "koko_mb": int((m.get("size") or 0) // 1048576)}
                           for m in data.get("models") or []]}

    # Ollama jonottaa ja hallitsee VRAMin itse -> ei noden GPU-jonoon
    return {
        "llm.chat": (chat, {"gpu": False, "vram_tarve_mb": 0,
                            "kuvaus": "Keskustelu paikallisella Ollama-mallilla"}),
        "llm.vision": (vision, {"gpu": False, "vram_tarve_mb": 0,
                                "kuvaus": "Kuvan tulkinta Ollama-visiomallilla"}),
        "llm.mallit": (mallit, {"gpu": False, "vram_tarve_mb": 0,
                                "kuvaus": "Ollaman asennetut mallit"}),
    }


# --------------------------------------------------------------------------
# ComfyUI


def _multipart(kentat: dict[str, str], tiedosto: tuple[str, str, bytes]) -> tuple[bytes, str]:
    raja = "----jarvis" + uuid.uuid4().hex
    osat: list[bytes] = []
    for nimi, arvo in kentat.items():
        osat.append(f'--{raja}\r\nContent-Disposition: form-data; name="{nimi}"\r\n\r\n'
                    f"{arvo}\r\n".encode())
    kentta, tiedostonimi, data = tiedosto
    osat.append(f'--{raja}\r\nContent-Disposition: form-data; name="{kentta}"; '
                f'filename="{tiedostonimi}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                .encode() + data + b"\r\n")
    osat.append(f"--{raja}--\r\n".encode())
    return b"".join(osat), f"multipart/form-data; boundary={raja}"


def _korvaa_nimet(workflow: dict, vaihdot: dict[str, str]) -> None:
    """Jos ComfyUI tallensi latauksen eri nimellä, päivitä viittaukset workflowssa."""
    if not vaihdot:
        return
    for solmu in workflow.values():
        syotteet = solmu.get("inputs") if isinstance(solmu, dict) else None
        if not isinstance(syotteet, dict):
            continue
        for avain, arvo in list(syotteet.items()):
            if isinstance(arvo, str) and arvo in vaihdot:
                syotteet[avain] = vaihdot[arvo]


def _comfy_virhe(status: dict) -> str:
    for viesti in status.get("messages") or []:
        if isinstance(viesti, list) and len(viesti) == 2 and viesti[0] == "execution_error":
            d = viesti[1] or {}
            return (f"ComfyUI-virhe solmussa {d.get('node_type') or d.get('node_id')}: "
                    f"{d.get('exception_message') or 'tuntematon'}").strip()
    return "ComfyUI-ajo epäonnistui"


def _comfy_kyky(config: dict) -> Kyky:
    p = _palvelut(config)["comfyui"]

    def fn(args: dict, ctx: Any) -> dict:
        import base64

        base = palvelu_url(p)
        workflow = args.get("workflow")
        if not isinstance(workflow, dict) or not workflow:
            raise ValueError("workflow puuttuu")
        if terveys_ok(p) is False:
            # Kone herätettiin juuri jonotyötä varten: ComfyUI ei ole vielä päällä.
            ctx.progress(kaynnista_palvelu("comfyui", p), 1)
            if not _odota_tila(p, True, float(p.get("odota_s") or 300), ctx, "ComfyUI"):
                raise RuntimeError("ComfyUI ei käynnistynyt")
        workflow = json.loads(json.dumps(workflow))  # oma kopio
        vaihdot: dict[str, str] = {}
        lataukset = list(args.get("lataukset") or [])
        for i, lataus in enumerate(lataukset):
            nimi = str(lataus.get("nimi") or f"jarvis_{i}.png")
            data = base64.b64decode(lataus.get("b64") or "")
            ctx.progress(f"Ladataan {nimi} ComfyUI:hin", 2)
            body, ctype = _multipart({"overwrite": "true", "type": "input"}, ("image", nimi, data))
            vastaus = json.loads(http_bytes("POST", base + "/upload/image", body,
                                            {"Content-Type": ctype}, timeout=120.0) or b"{}")
            uusi = str(vastaus.get("name") or nimi)
            if vastaus.get("subfolder"):
                uusi = f"{vastaus['subfolder']}/{uusi}"
            if uusi != nimi:
                vaihdot[nimi] = uusi
        _korvaa_nimet(workflow, vaihdot)

        client_id = uuid.uuid4().hex
        vastaus = http_json("POST", base + "/prompt",
                            {"prompt": workflow, "client_id": client_id}, timeout=60.0) or {}
        prompt_id = vastaus.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI hylkäsi workflown: {json.dumps(vastaus)[:500]}")
        ctx.progress("ComfyUI-jonossa", 5)

        raja = float(getattr(ctx, "aikaraja_s", None) or 1800)
        alku = time.monotonic()
        while True:
            if ctx.peruttu():
                try:
                    http_json("POST", base + "/queue", {"delete": [prompt_id]}, timeout=5.0)
                    http_json("POST", base + "/interrupt", {}, timeout=5.0)
                except Exception:  # noqa: BLE001
                    log.warning("ComfyUI-keskeytys epäonnistui")
                raise Peruttu("peruttu")
            historia = http_json("GET", f"{base}/history/{prompt_id}", timeout=15.0) or {}
            kohde = historia.get(prompt_id) if isinstance(historia, dict) else None
            if kohde:
                status = kohde.get("status") or {}
                if status.get("status_str") == "error":
                    raise RuntimeError(_comfy_virhe(status))
                if status.get("completed", True) or kohde.get("outputs"):
                    break
            kulunut = time.monotonic() - alku
            if kulunut > raja:
                raise TimeoutError(f"ComfyUI ei valmistunut {int(raja)} s:ssa")
            # asymptoottinen arvio: 5 → 90 %
            ctx.progress(f"ComfyUI työstää… ({int(kulunut)} s)",
                         int(5 + 85 * (1 - math.exp(-kulunut / 60.0))))
            _nuku(1.0)

        ulostulot: list[dict] = []
        for solmu in (kohde.get("outputs") or {}).values():
            for avain in ("images", "gifs", "videos", "audio", "files"):
                for e in solmu.get(avain) or []:
                    if isinstance(e, dict) and e.get("filename"):
                        ulostulot.append(e)
        valitut = [e for e in ulostulot if (e.get("type") or "output") == "output"] or ulostulot
        tiedostot = []
        for i, e in enumerate(valitut):
            ctx.progress(f"Haetaan tulos {i + 1}/{len(valitut)}", 92)
            kysely = urllib.parse.urlencode({"filename": e["filename"],
                                             "subfolder": e.get("subfolder") or "",
                                             "type": e.get("type") or "output"})
            data = http_bytes("GET", f"{base}/view?{kysely}", timeout=120.0)
            tiedostot.append(ctx.tallenna_tiedosto(str(e["filename"]), data))
        return {"tiedostot": tiedostot, "prompt_id": prompt_id}

    return fn


# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Wake-on-LAN: lähiverkon node herättää sammuneen koneen aivojen puolesta


def laheta_wol(mac: str, lan: str = "") -> bool:
    """Taikapaketti lähiverkon broadcastiin (portit 9 ja 7). Injektoitava."""
    puhdas = mac.replace(":", "").replace("-", "").strip()
    if len(puhdas) != 12:
        raise ValueError(f"virheellinen MAC '{mac}'")
    paketti = bytes.fromhex("FF" * 6 + puhdas * 16)
    kohteet = ["255.255.255.255"]
    if lan.count(".") == 3:
        kohteet.append(lan.rsplit(".", 1)[0] + ".255")
    lahti = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for kohde in kohteet:
            for portti in (9, 7):
                try:
                    sock.sendto(paketti, (kohde, portti))
                    lahti = True
                except OSError:
                    pass
    return lahti


def _herata_kyky(args: dict, ctx: Any) -> dict:  # noqa: ARG001
    lahti = laheta_wol(str(args.get("mac") or ""), str(args.get("lan") or ""))
    return {"lahetetty": lahti}


# --------------------------------------------------------------------------
# Hereilläpito: verkosta herätetty Windows nukahtaa ~2 min päästä uudelleen, jos
# kukaan ei koske hiireen ("unattended sleep": pitkä kuvatyö kaatui, kun
# työkone nukahti kesken). Node pyytää koneen pysymään hereillä samalla
# tavalla kuin mediasoittimet (SetThreadExecutionState), niin kauan kuin
# töitä on tullut viimeisen HEREILLA_MIN minuutin aikana. Asetuksiin ei kosketa.

HEREILLA_MIN = 15.0
_hereilla_asti = 0.0
_hereilla_lukko = threading.Lock()
_hereilla_saie: threading.Thread | None = None
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _aseta_suoritustila(hereilla: bool) -> None:
    """Windowsin virtapyyntö päälle/pois. Injektoitava testeissä; muualla no-op."""
    if os.name != "nt":
        return
    import ctypes

    tila = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if hereilla else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(tila))


def pida_hereilla(minuutit: float = HEREILLA_MIN, kello: Callable[[], float] = time.time) -> float:
    """Pidennä hereilläoloa vähintään `minuutit` tästä hetkestä. Palauttaa takarajan."""
    global _hereilla_asti
    with _hereilla_lukko:
        _hereilla_asti = max(_hereilla_asti, kello() + max(0.0, float(minuutit)) * 60.0)
        return _hereilla_asti


def hereilla_jaljella_s(kello: Callable[[], float] = time.time) -> float:
    return max(0.0, _hereilla_asti - kello())


def kaynnista_hereillapito(vali_s: float = 30.0) -> None:
    """Oma säie: virtapyyntö on säiekohtainen, joten sama säie asettaa ja purkaa sen."""
    global _hereilla_saie
    if _hereilla_saie and _hereilla_saie.is_alive():
        return

    def silmukka() -> None:
        paalla = False
        while True:
            haluttu = hereilla_jaljella_s() > 0
            if haluttu or paalla:
                try:
                    _aseta_suoritustila(haluttu)
                except Exception:  # noqa: BLE001
                    log.debug("Hereilläpito epäonnistui", exc_info=True)
                paalla = haluttu
            time.sleep(vali_s)

    _hereilla_saie = threading.Thread(target=silmukka, name="node-hereilla", daemon=True)
    _hereilla_saie.start()


def _hereilla_kyky(args: dict, ctx: Any) -> dict:  # noqa: ARG001
    minuutit = min(240.0, float(args.get("minuutit") or HEREILLA_MIN))
    asti = pida_hereilla(minuutit)
    return {"hereilla_asti": asti, "minuutit": minuutit}


def rakenna_kyvyt(config: dict) -> dict[str, tuple[Kyky, dict]]:
    """Kyvyt node.json:n palveluiden mukaan: {nimi: (fn, meta)}."""
    palvelut = _palvelut(config)
    kyvyt: dict[str, tuple[Kyky, dict]] = {
        "jarjestelma.tila": (lambda args, ctx: ctx.syke(),
                             {"gpu": False, "vram_tarve_mb": 0,
                              "kuvaus": "Koneen kuorma, muisti, GPU ja palvelut"}),
        "jarjestelma.pida_hereilla": (_hereilla_kyky,
                                      {"gpu": False, "vram_tarve_mb": 0,
                                       "kuvaus": "Pidä kone hereillä N minuuttia (ei nukahda kesken töiden)"}),
        "laite.herata": (_herata_kyky,
                         {"gpu": False, "vram_tarve_mb": 0,
                          "kuvaus": "Lähetä Wake-on-LAN-paketti lähiverkon koneelle (mac, lan)"}),
    }
    if "ollama" in palvelut:
        kyvyt.update(_ollama_kyvyt(config))
    if "comfyui" in palvelut:
        kyvyt["comfy.aja"] = (_comfy_kyky(config),
                              {"gpu": True, "vram_tarve_mb": COMFY_VRAM_MB,
                               "kuvaus": "Aja ComfyUI-workflow ja palauta tulostiedostot"})
    if palvelut:
        kyvyt["palvelu.kaynnista"] = (_palvelu_kyky(config, True),
                                      {"gpu": False, "vram_tarve_mb": 0,
                                       "kuvaus": "Käynnistä noden palvelu",
                                       "palvelut": sorted(palvelut)})
        kyvyt["palvelu.sammuta"] = (_palvelu_kyky(config, False),
                                    {"gpu": False, "vram_tarve_mb": 0,
                                     "kuvaus": "Sammuta noden palvelu",
                                     "palvelut": sorted(palvelut)})
    return kyvyt
