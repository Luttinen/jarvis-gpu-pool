"""Jarvis node: stdlib-HTTP-palvelin, joka suorittaa kykyjä allekirjoitetuista pyynnöistä.

Käyttö:  python node.py [--config node.json]
Koordinaattorista säikeenä:  NodePalvelin(config, token).start()

Rajapinta (ks. README.md): /health (ilman authia), /manifest, /syke,
POST /call, GET /tyo/<id>, POST /tyo/<id>/peru, GET /f/<id>.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Upotetun Pythonin ._pth estää skriptikansion lisäämisen -> repojuuri käsin
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_core.protokolla import (  # noqa: E402
    NODE_PORTTI,
    VERSIO,
    Tarkistaja,
    lue_token,
    sallittu_osoite,
)
from jarvis_node import kyvyt as kyvyt_mod  # noqa: E402
from jarvis_node.jarjestelma import OLETUS_PELIT, Jarjestelma  # noqa: E402

log = logging.getLogger("jarvis_node")

MAKS_KEHO = 200 * 1024 * 1024
SIIVOUS_S = 3600.0
TILAT_VALMIS = ("valmis", "epaonnistui", "peruttu")


class Tyo:
    def __init__(self, kyky: str, args: dict, gpu: bool, aikaraja_s: float | None) -> None:
        self.tyo_id = uuid.uuid4().hex
        self.kyky = kyky
        self.args = args
        self.gpu = gpu
        self.aikaraja_s = aikaraja_s
        self.tila = "jonossa"
        self.pct = 0
        self.viesti = ""
        self.tulos: dict | None = None
        self.virhe: str | None = None
        self.luotu = time.time()
        self.alku: float | None = None
        self.loppu: float | None = None
        self.peru = threading.Event()

    def aikaraja_ylitetty(self) -> bool:
        return bool(self.aikaraja_s and time.time() - self.luotu > self.aikaraja_s)

    def julkinen(self) -> dict:
        kesto = (int((self.loppu - self.alku) * 1000)
                 if self.alku is not None and self.loppu is not None else None)
        return {"tyo_id": self.tyo_id, "kyky": self.kyky, "tila": self.tila, "pct": self.pct,
                "viesti": self.viesti, "tulos": self.tulos, "virhe": self.virhe,
                "kesto_ms": kesto}


class Konteksti:
    """Kyvylle annettava ctx."""

    def __init__(self, palvelin: NodePalvelin, tyo: Tyo) -> None:
        self._palvelin = palvelin
        self._tyo = tyo
        self.aikaraja_s = tyo.aikaraja_s

    def progress(self, viesti: str, pct: int | float | None = None) -> None:
        with self._palvelin._lukko:
            self._tyo.viesti = str(viesti)
            if pct is not None:
                self._tyo.pct = max(0, min(100, int(pct)))

    def peruttu(self) -> bool:
        return self._tyo.peru.is_set() or self._tyo.aikaraja_ylitetty()

    def tallenna_tiedosto(self, nimi: str, data: bytes) -> dict:
        return self._palvelin.tallenna_tiedosto(nimi, data)

    def syke(self) -> dict:
        return self._palvelin.syke()


class _Palvelin4(ThreadingHTTPServer):
    daemon_threads = True
    # Windowsissa SO_REUSEADDR sallisi kaksi nodea samaan porttiin
    allow_reuse_address = os.name != "nt"


class _Palvelin6(_Palvelin4):
    address_family = socket.AF_INET6


def _tee_kasittelija(palvelin: NodePalvelin) -> type[BaseHTTPRequestHandler]:
    class Kasittelija(BaseHTTPRequestHandler):
        server_version = "JarvisNode/" + VERSIO
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # pythonw: stderr puuttuu -> vain lokiin
            log.debug("%s %s", self.client_address[0], format % args)

        def do_GET(self) -> None:  # noqa: N802
            palvelin._kasittele(self, "GET")

        def do_POST(self) -> None:  # noqa: N802
            palvelin._kasittele(self, "POST")

    return Kasittelija


class NodePalvelin:
    def __init__(self, config: dict, token: str, kyvyt: dict | None = None,
                 jarjestelma: Any = None) -> None:
        self.config = dict(config)
        self.node_id = str(self.config.get("node_id") or socket.gethostname())
        self._tarkistaja = Tarkistaja(token)
        self.kyvyt = kyvyt if kyvyt is not None else kyvyt_mod.rakenna_kyvyt(self.config)
        self.jarjestelma = jarjestelma or Jarjestelma()
        self.siivous_s = float(self.config.get("siivous_s") or SIIVOUS_S)
        self._lukko = threading.RLock()
        self._tyot: dict[str, Tyo] = {}
        self._tiedostot: dict[str, dict] = {}
        self._gpu_jono: queue.Queue[Tyo | None] = queue.Queue()
        self._seis = threading.Event()
        self._palvelimet: list[ThreadingHTTPServer] = []
        self._saikeet: list[threading.Thread] = []
        self._sitomatta: list[str] = []
        self.portti: int = int(self.config.get("portti", NODE_PORTTI))
        kansio = self.config.get("tiedosto_kansio")
        self._oma_kansio = not kansio
        self._kansio = Path(kansio) if kansio else Path(tempfile.mkdtemp(prefix="jarvis_node_"))
        self._kansio.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ elinkaari

    def _kuuntele(self) -> list[str]:
        k = self.config.get("kuuntele") or ["127.0.0.1"]
        return [k] if isinstance(k, str) else [str(x) for x in k]

    def _sido(self, osoite: str) -> bool:
        luokka = _Palvelin6 if ":" in osoite else _Palvelin4
        try:
            srv = luokka((osoite, self.portti), _tee_kasittelija(self))
        except OSError as exc:
            log.warning("Osoitteeseen %s:%s ei voitu sitoa: %s", osoite, self.portti, exc)
            return False
        if self.portti == 0:
            self.portti = int(srv.server_address[1])
        s = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                             name=f"node-http-{osoite}", daemon=True)
        s.start()
        self._palvelimet.append(srv)
        self._saikeet.append(s)
        log.info("Node %s kuuntelee %s:%s", self.node_id, osoite, self.portti)
        return True

    def start(self) -> NodePalvelin:
        self._seis.clear()
        for osoite in self._kuuntele():
            if not self._sido(osoite):
                self._sitomatta.append(osoite)
        if not self._palvelimet:
            raise OSError(f"node ei saanut sidottua yhtään osoitetta: {self._kuuntele()}")
        for kohde, nimi in ((self._gpu_silmukka, "node-gpu"), (self._huolto, "node-huolto")):
            s = threading.Thread(target=kohde, name=nimi, daemon=True)
            s.start()
            self._saikeet.append(s)
        kyvyt_mod.kaynnista_hereillapito()
        return self

    def stop(self) -> None:
        self._seis.set()
        self._gpu_jono.put(None)
        with self._lukko:
            for tyo in self._tyot.values():
                tyo.peru.set()
        for srv in self._palvelimet:
            try:
                srv.shutdown()
                srv.server_close()
            except OSError:
                pass
        self._palvelimet.clear()
        if self._oma_kansio:
            shutil.rmtree(self._kansio, ignore_errors=True)

    def _huolto(self) -> None:
        """Siivous ja sitomatta jääneiden osoitteiden uusintayritys (esim. Tailscale myöhässä)."""
        while not self._seis.wait(30.0):
            self.siivoa()
            for osoite in list(self._sitomatta):
                if self._sido(osoite):
                    self._sitomatta.remove(osoite)

    # ------------------------------------------------------------------ työt

    def _gpu_silmukka(self) -> None:
        while not self._seis.is_set():
            try:
                tyo = self._gpu_jono.get(timeout=0.5)
            except queue.Empty:
                continue
            if tyo is None:
                break
            self._aja(tyo)

    def _aja(self, tyo: Tyo) -> None:
        with self._lukko:
            if tyo.tila != "jonossa":
                return
            tyo.tila = "kaynnissa"
            tyo.alku = time.time()
        # Työn aikana ja HEREILLA_MIN sen jälkeen kone ei nukahda (verkkoherätys).
        kyvyt_mod.pida_hereilla()
        fn, _meta = self.kyvyt[tyo.kyky]
        tulos: Any = None
        virhe: str | None = None
        try:
            tulos = fn(dict(tyo.args), Konteksti(self, tyo))
        except kyvyt_mod.Peruttu:
            virhe = "peruttu"
        except Exception as exc:  # noqa: BLE001 - kyky kaatui
            log.exception("Kyky %s kaatui", tyo.kyky)
            virhe = f"{type(exc).__name__}: {exc}"
        with self._lukko:
            tyo.loppu = time.time()
            if tyo.peru.is_set():
                tyo.tila, tyo.virhe = "peruttu", "peruttu"
            elif virhe is not None:
                tyo.tila = "epaonnistui"
                tyo.virhe = f"AIKARAJA: {virhe}" if tyo.aikaraja_ylitetty() else virhe
            else:
                tyo.tila, tyo.pct = "valmis", 100
                tyo.tulos = tulos if isinstance(tulos, dict) else {"arvo": tulos}
        kyvyt_mod.pida_hereilla()  # pitkä työ: valveillaolo lasketaan sen lopusta
        log.info("Työ %s (%s): %s %.1f s", tyo.tyo_id[:8], tyo.kyky, tyo.tila,
                 (tyo.loppu or 0) - (tyo.alku or 0))

    def lisaa_tyo(self, kyky: str, args: dict, aikaraja_s: float | None = None) -> Tyo:
        _fn, meta = self.kyvyt[kyky]
        tyo = Tyo(kyky, args, bool(meta.get("gpu")), aikaraja_s)
        with self._lukko:
            self._tyot[tyo.tyo_id] = tyo
        if tyo.gpu:
            self._gpu_jono.put(tyo)
        else:
            threading.Thread(target=self._aja, args=(tyo,), name=f"tyo-{kyky}",
                             daemon=True).start()
        return tyo

    def peru(self, tyo_id: str) -> bool:
        with self._lukko:
            tyo = self._tyot.get(tyo_id)
            if tyo is None:
                return False
            tyo.peru.set()
            if tyo.tila == "jonossa":
                tyo.tila, tyo.virhe, tyo.loppu = "peruttu", "peruttu", time.time()
            elif tyo.tila == "kaynnissa":
                tyo.viesti = "peruutetaan…"
        return True

    def tyo(self, tyo_id: str) -> dict | None:
        with self._lukko:
            tyo = self._tyot.get(tyo_id)
            return tyo.julkinen() if tyo else None

    # ------------------------------------------------------------------ tiedostot

    def tallenna_tiedosto(self, nimi: str, data: bytes) -> dict:
        tid = uuid.uuid4().hex
        nimi = Path(str(nimi)).name or "tiedosto"
        polku = self._kansio / (tid + Path(nimi).suffix)
        polku.write_bytes(data)
        tieto = {"id": tid, "nimi": nimi, "koko": len(data),
                 "sha256": hashlib.sha256(data).hexdigest()}
        with self._lukko:
            self._tiedostot[tid] = dict(tieto, polku=str(polku), luotu=time.time())
        return tieto

    def siivoa(self) -> None:
        raja = time.time() - self.siivous_s
        with self._lukko:
            for tid in [t for t, x in self._tyot.items()
                        if x.tila in TILAT_VALMIS and (x.loppu or x.luotu) < raja]:
                del self._tyot[tid]
            vanhat = [t for t, x in self._tiedostot.items() if x["luotu"] < raja]
            poistettavat = [self._tiedostot.pop(t)["polku"] for t in vanhat]
        for polku in poistettavat:
            try:
                os.remove(polku)
            except OSError:
                pass

    # ------------------------------------------------------------------ tila

    def manifest(self) -> dict:
        j = self.jarjestelma
        gpu = j.gpu() or {}
        ram_mb, _ = j.muisti()
        return {
            "node_id": self.node_id,
            "nimi": str(self.config.get("nimi") or self.node_id),
            "os": str(self.config.get("os") or ("windows" if os.name == "nt" else sys.platform)),
            "prioriteetti": int(self.config.get("prioriteetti", 1)),
            "versio": VERSIO,
            "laitteisto": {"gpu": gpu.get("nimi"), "vram_mb": int(gpu.get("vram_mb") or 0),
                           "ram_mb": int(ram_mb), "cpu_ytimet": int(j.cpu_ytimet())},
            "kyvyt": [{"nimi": nimi, "gpu": bool(meta.get("gpu")),
                       "vram_tarve_mb": int(meta.get("vram_tarve_mb") or 0),
                       "kuvaus": str(meta.get("kuvaus") or ""),
                       **{k: v for k, v in meta.items()
                          if k not in ("gpu", "vram_tarve_mb", "kuvaus")}}
                      for nimi, (_fn, meta) in self.kyvyt.items()],
            "palvelut": sorted((self.config.get("palvelut") or {}).keys()),
        }

    def syke(self) -> dict:
        j = self.jarjestelma
        tulokset: dict[str, Any] = {}

        # hitaat osat rinnakkain
        def hae(nimi: str, f: Any) -> None:
            try:
                tulokset[nimi] = f()
            except Exception as exc:  # noqa: BLE001
                log.debug("syke %s: %s", nimi, exc)

        osat = {
            "palvelut": lambda: kyvyt_mod.palveluiden_tila(self.config),
            "ollama": lambda: kyvyt_mod.ollama_ladattu(self.config),
            "gpu": j.gpu,
            "peli": lambda: j.peli_aktiivinen(list(self.config.get("pelit") or OLETUS_PELIT)),
            "cpu": j.cpu_pct,
            "joutilas": getattr(j, "kayttaja_joutilas_s", lambda: None),
        }
        saikeet = [threading.Thread(target=hae, args=(n, f), daemon=True) for n, f in osat.items()]
        for s in saikeet:
            s.start()
        ram_mb, ram_vapaa = j.muisti()
        for s in saikeet:
            s.join(8.0)
        gpu = tulokset.get("gpu") or {}
        with self._lukko:
            kaynnissa = sum(1 for t in self._tyot.values() if t.tila == "kaynnissa")
            jonossa = sum(1 for t in self._tyot.values() if t.tila == "jonossa")
        return {
            "ts": time.time(),
            "cpu_pct": float(tulokset.get("cpu") or 0.0),
            "ram_mb": int(ram_mb), "ram_vapaa_mb": int(ram_vapaa),
            "gpu_nimi": gpu.get("nimi"),
            "gpu_pct": gpu.get("pct"),
            "vram_mb": int(gpu.get("vram_mb") or 0),
            "vram_vapaa_mb": int(gpu.get("vram_vapaa_mb") or 0),
            "palvelut": tulokset.get("palvelut") or {},
            "ollama_ladattu": tulokset.get("ollama") or [],
            "tyot_kaynnissa": kaynnissa, "tyot_jonossa": jonossa,
            "peli_aktiivinen": tulokset.get("peli"),
            "kayttaja_joutilas_s": tulokset.get("joutilas"),
            "hereilla_s": round(kyvyt_mod.hereilla_jaljella_s()),
        }

    # ------------------------------------------------------------------ HTTP

    def _laheta_json(self, h: BaseHTTPRequestHandler, status: int, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        try:
            h.wfile.write(raw)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # The /syke poller hangs up before reading: dozens of "käsittely
            # kaatui" tracebacks an hour for a client that already left (2026-10-18).
            log.debug("Asiakas sulki yhteyden kesken vastauksen (%s)", h.path)

    def _laheta_tiedosto(self, h: BaseHTTPRequestHandler, tid: str) -> None:
        with self._lukko:
            tieto = self._tiedostot.get(tid)
        if not tieto or not os.path.exists(tieto["polku"]):
            self._laheta_json(h, 404, {"virhe": "EI_TIEDOSTOA"})
            return
        tyyppi = mimetypes.guess_type(tieto["nimi"])[0] or "application/octet-stream"
        h.send_response(200)
        h.send_header("Content-Type", tyyppi)
        h.send_header("Content-Length", str(tieto["koko"]))
        h.send_header("Content-Disposition", f'attachment; filename="{tieto["nimi"]}"')
        h.end_headers()
        with open(tieto["polku"], "rb") as f:
            shutil.copyfileobj(f, h.wfile)

    def _kasittele(self, h: BaseHTTPRequestHandler, method: str) -> None:
        try:
            self._reitita(h, method)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("Pyynnön %s %s käsittely kaatui", method, h.path)
            try:
                self._laheta_json(h, 500, {"virhe": "KAATUI", "viesti": str(exc)})
            except OSError:
                pass

    def _reitita(self, h: BaseHTTPRequestHandler, method: str) -> None:
        polku = urlsplit(h.path).path
        if method == "GET" and polku == "/health":
            self._laheta_json(h, 200, {"ok": True, "node_id": self.node_id, "versio": VERSIO})
            return
        try:
            pituus = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            pituus = -1
        if pituus < 0 or pituus > MAKS_KEHO:
            h.close_connection = True
            self._laheta_json(h, 413, {"virhe": "LIIAN_SUURI"})
            return
        body = h.rfile.read(pituus) if pituus else b""
        peer = str(h.client_address[0])
        if not sallittu_osoite(peer):
            log.warning("Hylätty osoite %s (%s %s)", peer, method, polku)
            self._laheta_json(h, 403, {"virhe": "EI_LUPAA", "syy": "osoite ei ole sallittu"})
            return
        ok, syy = self._tarkistaja.tarkista(method, h.path, h.headers, body)
        if not ok:
            log.warning("Hylätty pyyntö %s %s %s: %s", peer, method, polku, syy)
            self._laheta_json(h, 403, {"virhe": "EI_LUPAA", "syy": syy})
            return

        osat = [o for o in polku.split("/") if o]
        if method == "GET" and polku == "/manifest":
            self._laheta_json(h, 200, self.manifest())
        elif method == "GET" and polku == "/syke":
            self.siivoa()
            self._laheta_json(h, 200, self.syke())
        elif method == "POST" and polku == "/call":
            self._kutsu(h, body)
        elif method == "GET" and len(osat) == 2 and osat[0] == "tyo":
            tila = self.tyo(osat[1])
            if tila is None:
                self._laheta_json(h, 404, {"virhe": "EI_TYOTA"})
            else:
                self._laheta_json(h, 200, tila)
        elif method == "POST" and len(osat) == 3 and osat[0] == "tyo" and osat[2] == "peru":
            if self.peru(osat[1]):
                self._laheta_json(h, 200, {"ok": True})
            else:
                self._laheta_json(h, 404, {"virhe": "EI_TYOTA"})
        elif method == "GET" and len(osat) == 2 and osat[0] == "f":
            self._laheta_tiedosto(h, osat[1])
        else:
            self._laheta_json(h, 404, {"virhe": "EI_REITTIA"})

    def _kutsu(self, h: BaseHTTPRequestHandler, body: bytes) -> None:
        try:
            pyynto = json.loads(body or b"{}")
        except ValueError:
            self._laheta_json(h, 400, {"virhe": "VIRHEELLINEN_JSON"})
            return
        kyky = str(pyynto.get("kyky") or "")
        if kyky not in self.kyvyt:
            self._laheta_json(h, 404, {"virhe": "EI_KYKYA"})
            return
        args = pyynto.get("args") or {}
        if not isinstance(args, dict):
            self._laheta_json(h, 400, {"virhe": "VIRHEELLISET_ARGUMENTIT"})
            return
        aikaraja = pyynto.get("aikaraja_s")
        aikaraja_s = float(aikaraja) if isinstance(aikaraja, (int, float)) and aikaraja > 0 else None
        self.siivoa()
        tyo = self.lisaa_tyo(kyky, args, aikaraja_s)
        self._laheta_json(h, 202, {"tyo_id": tyo.tyo_id})


# --------------------------------------------------------------------------
# CLI


def _lokitus(kansio: Path) -> None:
    juuri = logging.getLogger()
    juuri.setLevel(logging.INFO)
    kasittelija = RotatingFileHandler(kansio / "node.log", maxBytes=1024 * 1024, backupCount=2,
                                      encoding="utf-8")
    kasittelija.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    juuri.addHandler(kasittelija)
    if sys.stderr is not None:
        juuri.addHandler(logging.StreamHandler())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis node")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent.parent
                                                 / "node.json"))
    a = parser.parse_args(argv)
    config_polku = Path(a.config).resolve()
    kansio = config_polku.parent
    _lokitus(kansio)
    try:
        config = json.loads(config_polku.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.error("Konfiguraatiota %s ei voitu lukea: %s", config_polku, exc)
        return 2
    token = lue_token(kansio / str(config.get("token_tiedosto") or "node_token.txt"))
    if not token:
        log.error("Token puuttuu: kaikki allekirjoitetut pyynnöt hylätään")
    palvelin = NodePalvelin(config, token)
    try:
        palvelin.start()
    except OSError as exc:
        log.error("Node ei käynnistynyt: %s", exc)
        return 1
    log.info("Jarvis node %s v%s käynnissä, kyvyt: %s", palvelin.node_id, VERSIO,
             ", ".join(palvelin.kyvyt))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        palvelin.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
