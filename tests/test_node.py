"""Noden testit: oikea palvelin portissa 0, allekirjoitetut pyynnöt urllibillä."""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from jarvis_core import protokolla
from jarvis_node import kyvyt as kyvyt_mod
from jarvis_node.node import NodePalvelin

TOKEN = "testitoken-" + "a" * 40
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ValeJarjestelma:
    def cpu_ytimet(self) -> int:
        return 6

    def cpu_pct(self) -> float:
        return 12.5

    def muisti(self) -> tuple[int, int]:
        return 32000, 20000

    def gpu(self) -> dict:
        return {"nimi": "RTX Vale", "pct": 40.0, "vram_mb": 8192, "vram_vapaa_mb": 5000}

    def peli_aktiivinen(self, pelit=None):
        return None


def pyynto(node: NodePalvelin, method: str, path: str, body: dict | None = None,
           allekirjoita: bool = True, token: str = TOKEN) -> tuple[int, bytes, dict]:
    raw = json.dumps(body).encode() if body is not None else b""
    headers = {"Content-Type": "application/json"}
    if allekirjoita:
        headers.update(protokolla.allekirjoita(token, method, path, raw, "testi"))
    req = urllib.request.Request(f"http://127.0.0.1:{node.portti}{path}",
                                 data=raw if method == "POST" else None,
                                 method=method, headers=headers)
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def pj(node, method, path, body=None, **kw):
    status, raw, _ = pyynto(node, method, path, body, **kw)
    return status, json.loads(raw)


def odota_valmis(node, tyo_id, aikaraja=10.0) -> dict:
    loppu = time.time() + aikaraja
    while time.time() < loppu:
        _, tila = pj(node, "GET", f"/tyo/{tyo_id}")
        if tila["tila"] in ("valmis", "epaonnistui", "peruttu"):
            return tila
        time.sleep(0.05)
    raise AssertionError(f"työ {tyo_id} ei valmistunut")


def kaynnista(config: dict | None = None, kyvyt=None) -> NodePalvelin:
    conf = {"node_id": "testi", "nimi": "Testinode", "prioriteetti": 3,
            "kuuntele": ["127.0.0.1"], "portti": 0, "palvelut": {}}
    conf.update(config or {})
    return NodePalvelin(conf, TOKEN, kyvyt=kyvyt, jarjestelma=ValeJarjestelma()).start()


# --------------------------------------------------------------------------
# valekyvyt


class GpuSeuranta:
    def __init__(self) -> None:
        self.lukko = threading.Lock()
        self.nyt = 0
        self.maks = 0
        self.alkujarjestys: list[int] = []

    def kyky(self, args, ctx):
        with self.lukko:
            self.nyt += 1
            self.maks = max(self.maks, self.nyt)
            self.alkujarjestys.append(args["n"])
        ctx.progress("hommissa", 50)
        time.sleep(0.15)
        with self.lukko:
            self.nyt -= 1
        return {"n": args["n"]}


def odota_perumista(args, ctx):
    loppu = time.time() + 5
    while time.time() < loppu:
        if ctx.peruttu():
            raise kyvyt_mod.Peruttu()
        time.sleep(0.02)
    return {"ei": "peruttu"}


def tee_tiedosto(args, ctx):
    return {"tiedostot": [ctx.tallenna_tiedosto("kuva.png", b"\x89PNGvaledata")]}


def kaatuu(args, ctx):
    raise ValueError("pieleen")


@pytest.fixture
def seuranta():
    return GpuSeuranta()


@pytest.fixture
def node(seuranta):
    kyvyt = {
        "kaiku": (lambda a, c: {"kaiku": a.get("x")}, {"gpu": False, "vram_tarve_mb": 0,
                                                        "kuvaus": "kaiku"}),
        "gpu.testi": (seuranta.kyky, {"gpu": True, "vram_tarve_mb": 1000, "kuvaus": "gpu"}),
        "gpu.peruttava": (odota_perumista, {"gpu": True, "vram_tarve_mb": 0, "kuvaus": ""}),
        "tiedosto": (tee_tiedosto, {"gpu": False, "vram_tarve_mb": 0, "kuvaus": ""}),
        "kaatuu": (kaatuu, {"gpu": False, "vram_tarve_mb": 0, "kuvaus": ""}),
        "jarjestelma.tila": (lambda a, c: c.syke(), {"gpu": False, "vram_tarve_mb": 0,
                                                      "kuvaus": ""}),
    }
    n = kaynnista(kyvyt=kyvyt)
    yield n
    n.stop()


# --------------------------------------------------------------------------
# perusreitit ja turva


def test_health_ilman_authia(node):
    status, data = pj(node, "GET", "/health", allekirjoita=False)
    assert status == 200
    assert data == {"ok": True, "node_id": "testi", "versio": protokolla.VERSIO}


def test_ilman_allekirjoitusta_403(node):
    for path in ("/manifest", "/syke"):
        status, data = pj(node, "GET", path, allekirjoita=False)
        assert status == 403
        assert data["virhe"] == "EI_LUPAA"
    status, _ = pj(node, "POST", "/call", {"kyky": "kaiku"}, allekirjoita=False)
    assert status == 403


def test_vaara_token_ja_toisto_hylataan(node):
    status, _ = pj(node, "GET", "/manifest", token="vaara")
    assert status == 403
    # sama nonce kahdesti -> toinen hylätään
    headers = protokolla.allekirjoita(TOKEN, "GET", "/manifest", b"", "testi")
    for odotettu in (200, 403):
        req = urllib.request.Request(f"http://127.0.0.1:{node.portti}/manifest", headers=headers)
        try:
            with _opener.open(req, timeout=5) as r:
                assert r.status == odotettu
        except urllib.error.HTTPError as exc:
            assert exc.code == odotettu


def test_vieras_osoite_hylataan(node, monkeypatch):
    from jarvis_node import node as node_mod

    monkeypatch.setattr(node_mod, "sallittu_osoite", lambda ip: False)
    status, data = pj(node, "GET", "/manifest")
    assert status == 403 and data["virhe"] == "EI_LUPAA"


def test_manifest(node):
    status, m = pj(node, "GET", "/manifest")
    assert status == 200
    assert m["node_id"] == "testi" and m["nimi"] == "Testinode" and m["prioriteetti"] == 3
    assert m["versio"] == protokolla.VERSIO
    assert m["laitteisto"] == {"gpu": "RTX Vale", "vram_mb": 8192, "ram_mb": 32000,
                               "cpu_ytimet": 6}
    kyvyt = {k["nimi"]: k for k in m["kyvyt"]}
    assert kyvyt["gpu.testi"]["gpu"] is True and kyvyt["gpu.testi"]["vram_tarve_mb"] == 1000
    assert m["palvelut"] == []


def test_syke(node):
    status, s = pj(node, "GET", "/syke")
    assert status == 200
    assert s["cpu_pct"] == 12.5 and s["ram_mb"] == 32000 and s["ram_vapaa_mb"] == 20000
    assert s["gpu_nimi"] == "RTX Vale" and s["gpu_pct"] == 40.0
    assert s["vram_mb"] == 8192 and s["vram_vapaa_mb"] == 5000
    assert s["palvelut"] == {} and s["ollama_ladattu"] == []
    assert s["tyot_kaynnissa"] == 0 and s["tyot_jonossa"] == 0
    assert s["peli_aktiivinen"] is None
    assert isinstance(s["ts"], float)


def test_tuntematon_kyky_ja_tyo(node):
    status, data = pj(node, "POST", "/call", {"kyky": "ei.ole", "args": {}})
    assert status == 404 and data == {"virhe": "EI_KYKYA"}
    status, _ = pj(node, "GET", "/tyo/eiole")
    assert status == 404


# --------------------------------------------------------------------------
# työt


def test_call_ja_pollaus(node):
    status, data = pj(node, "POST", "/call", {"kyky": "kaiku", "args": {"x": 5}, "aikaraja_s": 30})
    assert status == 202
    tila = odota_valmis(node, data["tyo_id"])
    assert tila["tila"] == "valmis" and tila["tulos"] == {"kaiku": 5}
    assert tila["pct"] == 100 and tila["virhe"] is None
    assert isinstance(tila["kesto_ms"], int) and tila["kyky"] == "kaiku"


def test_kaatuva_kyky(node):
    _, data = pj(node, "POST", "/call", {"kyky": "kaatuu", "args": {}})
    tila = odota_valmis(node, data["tyo_id"])
    assert tila["tila"] == "epaonnistui" and "pieleen" in tila["virhe"]


def test_jarjestelma_tila_kyky(node):
    _, data = pj(node, "POST", "/call", {"kyky": "jarjestelma.tila", "args": {}})
    tila = odota_valmis(node, data["tyo_id"])
    assert tila["tulos"]["gpu_nimi"] == "RTX Vale"


def test_gpu_jono_fifo_yksi_kerrallaan(node, seuranta):
    idt = []
    for n in range(4):
        _, data = pj(node, "POST", "/call", {"kyky": "gpu.testi", "args": {"n": n}})
        idt.append(data["tyo_id"])
    time.sleep(0.05)
    _, s = pj(node, "GET", "/syke")
    assert s["tyot_kaynnissa"] == 1 and s["tyot_jonossa"] >= 2
    tilat = [odota_valmis(node, t) for t in idt]
    assert all(t["tila"] == "valmis" for t in tilat)
    assert seuranta.maks == 1
    assert seuranta.alkujarjestys == [0, 1, 2, 3]


def test_kevyt_ei_odota_gpu_jonoa(node):
    _, gpu = pj(node, "POST", "/call", {"kyky": "gpu.peruttava", "args": {}})
    _, kevyt = pj(node, "POST", "/call", {"kyky": "kaiku", "args": {"x": 1}})
    assert odota_valmis(node, kevyt["tyo_id"], 2.0)["tila"] == "valmis"
    pj(node, "POST", f"/tyo/{gpu['tyo_id']}/peru")


def test_peruutus_kaynnissa_ja_jonossa(node):
    _, a = pj(node, "POST", "/call", {"kyky": "gpu.peruttava", "args": {}})
    _, b = pj(node, "POST", "/call", {"kyky": "gpu.peruttava", "args": {}})
    time.sleep(0.1)
    _, tila_b = pj(node, "GET", f"/tyo/{b['tyo_id']}")
    assert tila_b["tila"] == "jonossa"
    status, data = pj(node, "POST", f"/tyo/{b['tyo_id']}/peru")
    assert status == 200 and data == {"ok": True}
    _, tila_b = pj(node, "GET", f"/tyo/{b['tyo_id']}")
    assert tila_b["tila"] == "peruttu"
    pj(node, "POST", f"/tyo/{a['tyo_id']}/peru")
    alku = time.time()
    assert odota_valmis(node, a["tyo_id"])["tila"] == "peruttu"
    assert time.time() - alku < 2.0
    assert pj(node, "POST", "/tyo/eiole/peru")[0] == 404


def test_tiedosto(node):
    _, data = pj(node, "POST", "/call", {"kyky": "tiedosto", "args": {}})
    tila = odota_valmis(node, data["tyo_id"])
    tieto = tila["tulos"]["tiedostot"][0]
    assert tieto["nimi"] == "kuva.png" and tieto["koko"] == 12 and len(tieto["sha256"]) == 64
    status, raw, headers = pyynto(node, "GET", f"/f/{tieto['id']}")
    assert status == 200 and raw == b"\x89PNGvaledata"
    assert headers["Content-Type"] == "image/png"
    assert pyynto(node, "GET", f"/f/{tieto['id']}", allekirjoita=False)[0] == 403
    assert pyynto(node, "GET", "/f/eiole")[0] == 404


def test_siivous(node):
    _, data = pj(node, "POST", "/call", {"kyky": "tiedosto", "args": {}})
    tila = odota_valmis(node, data["tyo_id"])
    tid = tila["tulos"]["tiedostot"][0]["id"]
    node.siivous_s = -1
    node.siivoa()
    assert pj(node, "GET", f"/tyo/{data['tyo_id']}")[0] == 404
    assert pyynto(node, "GET", f"/f/{tid}")[0] == 404


def test_useampi_osoite_ja_sitomaton_ohitetaan():
    n = kaynnista({"kuuntele": ["127.0.0.1", "203.0.113.77"]}, kyvyt={})
    try:
        assert len(n._palvelimet) == 1 and n._sitomatta == ["203.0.113.77"]
        assert pj(n, "GET", "/health", allekirjoita=False)[0] == 200
    finally:
        n.stop()


# --------------------------------------------------------------------------
# oikeat kyvyt vale-HTTP:llä


class ValeHttp:
    """Korvaa kyvyt.http_bytes: reitit (method, polku ilman queryä) -> vastaus."""

    def __init__(self) -> None:
        self.kutsut: list[tuple[str, str, bytes | None]] = []
        self.historia_kyselyt = 0

    def __call__(self, method, url, data=None, headers=None, timeout=30.0):
        self.kutsut.append((method, url, data))
        polku = url.split("127.0.0.1:", 1)[1].split("/", 1)[1]
        polku = "/" + polku
        if polku in ("/api/tags",) and method == "GET":
            return json.dumps({"models": [{"name": "llama3:8b", "size": 4 * 1048576 * 1024}]}
                              ).encode()
        if polku == "/system_stats":
            return b"{}"
        if polku == "/api/ps":
            return json.dumps({"models": [{"name": "llama3:8b", "size_vram": 5000 * 1048576,
                                           "expires_at": "2026-10-16T20:00:00Z"}]}).encode()
        if polku == "/api/chat":
            pyynto = json.loads(data)
            assert pyynto["stream"] is False
            return json.dumps({"model": pyynto["model"],
                               "message": {"role": "assistant", "content": "Hei maailma!"},
                               "eval_count": 100, "eval_duration": 2_000_000_000}).encode()
        if polku == "/upload/image":
            assert b'name="image"; filename="syote.png"' in data
            assert b"KUVADATA" in data
            return json.dumps({"name": "syote (1).png", "subfolder": "", "type": "input"}).encode()
        if polku == "/prompt":
            pyynto = json.loads(data)
            assert pyynto["client_id"]
            # latauksen uusi nimi päivitetty workflowhun
            assert pyynto["prompt"]["1"]["inputs"]["image"] == "syote (1).png"
            return json.dumps({"prompt_id": "p123"}).encode()
        if polku == "/history/p123":
            self.historia_kyselyt += 1
            if self.historia_kyselyt < 3:
                return b"{}"
            return json.dumps({"p123": {"status": {"status_str": "success", "completed": True},
                                        "outputs": {"9": {"images": [
                                            {"filename": "tulos_001.png", "subfolder": "jarvis",
                                             "type": "output"}]}}}}).encode()
        if polku.startswith("/view?"):
            assert "filename=tulos_001.png" in polku and "subfolder=jarvis" in polku
            return b"\x89PNGtulos"
        raise RuntimeError(f"tuntematon {method} {url}")


@pytest.fixture
def vale_http(monkeypatch):
    vale = ValeHttp()
    monkeypatch.setattr(kyvyt_mod, "http_bytes", vale)
    monkeypatch.setattr(kyvyt_mod, "_nuku", lambda s: None)
    return vale


PALVELUT = {"ollama": {"portti": 11434, "terveys": "/api/tags", "tunniste": "ollama"},
            "comfyui": {"portti": 8188, "terveys": "/system_stats", "tunniste": "ComfyUI\\main.py"}}


def test_rakenna_kyvyt_palveluiden_mukaan():
    assert set(kyvyt_mod.rakenna_kyvyt({})) == {"jarjestelma.tila", "jarjestelma.pida_hereilla", "laite.herata"}
    vain_ollama = kyvyt_mod.rakenna_kyvyt({"palvelut": {"ollama": PALVELUT["ollama"]}})
    assert {"llm.chat", "llm.vision", "llm.mallit", "palvelu.kaynnista"} <= set(vain_ollama)
    assert "comfy.aja" not in vain_ollama
    kaikki = kyvyt_mod.rakenna_kyvyt({"palvelut": PALVELUT})
    assert kaikki["comfy.aja"][1]["gpu"] is True
    assert kaikki["comfy.aja"][1]["vram_tarve_mb"] == 6000


def test_laite_herata_lahettaa_taikapaketin(monkeypatch):
    lahetetyt = []

    class ValeSoketti:
        def __init__(self, *_a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def setsockopt(self, *_a):
            pass

        def sendto(self, data, osoite):
            lahetetyt.append((data, osoite))

    monkeypatch.setattr(kyvyt_mod.socket, "socket", ValeSoketti)
    fn, meta = kyvyt_mod.rakenna_kyvyt({})["laite.herata"]
    assert meta["gpu"] is False
    tulos = fn({"mac": "00-11-22-33-44-55", "lan": "192.168.1.20"}, None)
    assert tulos == {"lahetetty": True}
    paketti = lahetetyt[0][0]
    assert paketti[:6] == bytes.fromhex("FF" * 6) and paketti[6:12] == bytes.fromhex("001122334455")
    assert ("192.168.1.255", 9) in [o for _, o in lahetetyt]
    with pytest.raises(ValueError):
        fn({"mac": "huono"}, None)


def test_llm_chat_ja_syke_vale_httpllä(vale_http):
    n = kaynnista({"palvelut": PALVELUT})
    try:
        _, data = pj(n, "POST", "/call", {"kyky": "llm.chat", "args": {
            "malli": "llama3:8b", "viestit": [{"role": "user", "content": "moi"}],
            "keep_alive": "10m"}})
        tila = odota_valmis(n, data["tyo_id"])
        assert tila["tila"] == "valmis", tila
        assert tila["tulos"] == {"vastaus": "Hei maailma!", "malli": "llama3:8b",
                                 "tokenit_s": 50.0}
        chat = [json.loads(d) for m, u, d in vale_http.kutsut if u.endswith("/api/chat")][0]
        assert chat["keep_alive"] == "10m"

        _, data = pj(n, "POST", "/call", {"kyky": "llm.mallit", "args": {}})
        assert odota_valmis(n, data["tyo_id"])["tulos"] == {
            "mallit": [{"nimi": "llama3:8b", "koko_mb": 4096}]}

        _, s = pj(n, "GET", "/syke")
        assert s["palvelut"]["ollama"] == {"kaynnissa": True, "portti": 11434, "tiedot": {}}
        assert s["palvelut"]["comfyui"]["kaynnissa"] is True
        assert s["ollama_ladattu"] == [{"nimi": "llama3:8b", "vram_mb": 5000,
                                        "vanhenee": "2026-10-16T20:00:00Z"}]
        _, m = pj(n, "GET", "/manifest")
        assert m["palvelut"] == ["comfyui", "ollama"]
    finally:
        n.stop()


def test_comfy_aja_vale_httpllä(vale_http):
    n = kaynnista({"palvelut": PALVELUT})
    try:
        workflow = {"1": {"class_type": "LoadImage", "inputs": {"image": "syote.png"}},
                    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}}}
        _, data = pj(n, "POST", "/call", {"kyky": "comfy.aja", "args": {
            "workflow": workflow,
            "lataukset": [{"nimi": "syote.png", "b64": base64.b64encode(b"KUVADATA").decode()}]}})
        tila = odota_valmis(n, data["tyo_id"])
        assert tila["tila"] == "valmis", tila
        tiedosto = tila["tulos"]["tiedostot"][0]
        assert tiedosto["nimi"] == "tulos_001.png" and tiedosto["koko"] == 9
        status, raw, _ = pyynto(n, "GET", f"/f/{tiedosto['id']}")
        assert status == 200 and raw == b"\x89PNGtulos"
        assert vale_http.historia_kyselyt == 3
    finally:
        n.stop()


def test_comfy_virhe_raportoidaan(monkeypatch):
    def vale(method, url, data=None, headers=None, timeout=30.0):
        if url.endswith("/prompt"):
            return b'{"prompt_id": "x"}'
        return json.dumps({"x": {"status": {"status_str": "error", "messages": [
            ["execution_error", {"node_type": "KSampler", "exception_message": "OOM"}]]}}}
                          ).encode()

    monkeypatch.setattr(kyvyt_mod, "http_bytes", vale)
    fn, _ = kyvyt_mod.rakenna_kyvyt({"palvelut": PALVELUT})["comfy.aja"]

    class Ctx:
        aikaraja_s = 10

        def progress(self, *a):
            pass

        def peruttu(self):
            return False

    with pytest.raises(RuntimeError, match="KSampler.*OOM"):
        fn({"workflow": {"1": {"inputs": {}}}}, Ctx())


def test_palvelu_kaynnista(monkeypatch):
    tila = {"ylhaalla": False}
    kaynnistetty = []

    def vale_http(method, url, data=None, headers=None, timeout=30.0):
        if tila["ylhaalla"]:
            return b"{}"
        raise OSError("ei vastaa")

    def vale_irti(palvelu):
        kaynnistetty.append(palvelu["kaynnistys"])
        tila["ylhaalla"] = True
        return 4321

    monkeypatch.setattr(kyvyt_mod, "http_bytes", vale_http)
    monkeypatch.setattr(kyvyt_mod, "kaynnista_irti", vale_irti)
    monkeypatch.setattr(kyvyt_mod, "_nuku", lambda s: None)
    config = {"palvelut": {"ollama": {"portti": 11434, "terveys": "/api/tags",
                                      "kaynnistys": "ollama", "argumentit": "serve",
                                      "tunniste": "ollama"}}}
    fn, _ = kyvyt_mod.rakenna_kyvyt(config)["palvelu.kaynnista"]

    class Ctx:
        aikaraja_s = 30

        def progress(self, *a):
            pass

        def peruttu(self):
            return False

    tulos = fn({"nimi": "ollama"}, Ctx())
    assert tulos["kaynnissa"] is True and "4321" in tulos["viesti"]
    assert kaynnistetty == ["ollama"]
    with pytest.raises(ValueError):
        fn({"nimi": "eiole"}, Ctx())


def test_comfy_aja_kaynnistaa_comfyuin_ensin(monkeypatch):
    kaynnissa = {"comfy": False}
    kaynnistetty = []

    def vale(method, url, data=None, headers=None, timeout=30.0):
        if url.endswith("/system_stats"):
            if not kaynnissa["comfy"]:
                raise OSError("ei vastaa")
            return b"{}"
        if url.endswith("/prompt"):
            return b'{"prompt_id": "x"}'
        if "/history/" in url:
            return json.dumps({"x": {"status": {"completed": True}, "outputs": {}}}).encode()
        return b"{}"

    def vale_kaynnista(nimi, palvelu):
        kaynnistetty.append(nimi)
        kaynnissa["comfy"] = True
        return "comfyui käynnistetty"

    monkeypatch.setattr(kyvyt_mod, "http_bytes", vale)
    monkeypatch.setattr(kyvyt_mod, "kaynnista_palvelu", vale_kaynnista)
    monkeypatch.setattr(kyvyt_mod, "_nuku", lambda s: None)
    fn, _ = kyvyt_mod.rakenna_kyvyt({"palvelut": PALVELUT})["comfy.aja"]

    class Ctx:
        aikaraja_s = 10

        def progress(self, *a):
            pass

        def peruttu(self):
            return False

        def tallenna_tiedosto(self, nimi, data):
            return {"id": "t", "nimi": nimi}

    assert fn({"workflow": {"1": {"inputs": {}}}}, Ctx())["prompt_id"] == "x"
    assert kaynnistetty == ["comfyui"]


def test_hereillapito(monkeypatch):
    kello = [1000.0]
    monkeypatch.setattr(kyvyt_mod, "_hereilla_asti", 0.0)
    assert kyvyt_mod.hereilla_jaljella_s(lambda: kello[0]) == 0
    asti = kyvyt_mod.pida_hereilla(15, kello=lambda: kello[0])
    assert asti == 1000.0 + 15 * 60
    kyvyt_mod.pida_hereilla(1, kello=lambda: kello[0])  # lyhyempi ei lyhennä
    assert kyvyt_mod.hereilla_jaljella_s(lambda: kello[0]) == 15 * 60
    fn, meta = kyvyt_mod.rakenna_kyvyt({})["jarjestelma.pida_hereilla"]
    assert meta["gpu"] is False
    assert fn({"minuutit": 999}, None)["minuutit"] == 240.0  # katto 4 h
