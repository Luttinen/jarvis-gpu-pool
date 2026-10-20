"""Aivojen reititin: tuntee nodet, pollaa niiden sykkeen ja jakaa työt.

Aivot (koordinaattori) eivät tiedä IP-osoitteita vaan kykyjä: "tarvitsen `comfy.aja`"
→ reititin valitsee elossa olevan noden, jolla kyky on ja muistia riittää.
Kaikki pyynnöt allekirjoitetaan `protokolla.allekirjoita`:lla. Vain stdlib.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from . import protokolla

KOODIT = ("EI_NODEA", "EI_KYKYA", "AIKARAJA", "KAATUI", "YHTEYS", "EI_LUPAA")
UUSITTAVAT = ("YHTEYS", "KAATUI")
GPU_RINNAKKAIN = 1
KEVYT_RINNAKKAIN = 4

Http = Callable[[str, str, dict, bytes, float], tuple[int, bytes]]


class NodeVirhe(Exception):
    """Reitityksen tai noden virhe; `koodi` on yksi KOODIT-arvoista."""

    def __init__(self, koodi: str, viesti: str = "", node_id: str | None = None) -> None:
        super().__init__(f"{koodi}: {viesti}" if viesti else koodi)
        self.koodi = koodi
        self.viesti = viesti
        self.node_id = node_id


@dataclass
class Node:
    node_id: str
    url: str
    tila: str = "tuntematon"  # tuntematon|elossa|epavarma|poissa
    manifest: dict | None = None
    syke: dict | None = None
    viimeksi_ok: float | None = None
    virhe: str = ""
    # sisäinen kirjanpito tilasiirtymiä varten
    viimeisin_onnistui: bool | None = field(default=None, repr=False)
    epaonnistumiset: int = field(default=0, repr=False)

    def _kyky(self, kyky: str) -> dict | None:
        for k in (self.manifest or {}).get("kyvyt") or []:
            if isinstance(k, dict) and k.get("nimi") == kyky:
                return k
        return None

    def tukee(self, kyky: str) -> bool:
        return self._kyky(kyky) is not None

    def on_gpu_kyky(self, kyky: str) -> bool:
        k = self._kyky(kyky)
        return bool(k and k.get("gpu"))

    def vram_vapaa(self) -> int:
        return int((self.syke or {}).get("vram_vapaa_mb") or 0)

    def kuorma(self) -> int:
        s = self.syke or {}
        return int(s.get("tyot_kaynnissa") or 0) + int(s.get("tyot_jonossa") or 0)

    def prioriteetti(self) -> int:
        try:
            return int((self.manifest or {}).get("prioriteetti", 100))
        except (TypeError, ValueError):
            return 100

    def peli(self) -> str | None:
        return (self.syke or {}).get("peli_aktiivinen") or None


def _urllib_http(method: str, url: str, headers: dict, body: bytes,
                 timeout: float) -> tuple[int, bytes]:
    """Oletus-HTTP: ei välityspalvelimia (tailnet/LAN suoraan)."""
    req = urllib.request.Request(url, data=body if body or method != "GET" else None,
                                 headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as vastaus:
            return vastaus.status, vastaus.read()
    except urllib.error.HTTPError as e:
        try:
            runko = e.read()
        except OSError:
            runko = b""
        return e.code, runko
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from e


def _polku(url: str) -> str:
    osat = urlsplit(url)
    return (osat.path or "/") + (f"?{osat.query}" if osat.query else "")


class NodeRekisteri:
    """Nodejen tila, valinta ja työkutsut."""

    def __init__(self, token: str, nodet: list[tuple[str, str]], lahettaja: str = "aivot",
                 kello: Callable[[], float] = time.monotonic, http: Http | None = None,
                 epavarma_s: float = 25, poissa_s: float = 60,
                 nukku: Callable[[float], None] = time.sleep,
                 kysely_vali_s: float = 1.0) -> None:
        self.token = token or ""
        self.lahettaja = lahettaja
        self.kello = kello
        self.http: Http = http or _urllib_http
        self.epavarma_s = epavarma_s
        self.poissa_s = poissa_s
        self.nukku = nukku
        self.kysely_vali_s = kysely_vali_s
        self._nodet: dict[str, Node] = {
            nid: Node(node_id=nid, url=url.rstrip("/")) for nid, url in nodet
        }
        self._lukko = threading.RLock()
        self._saie: threading.Thread | None = None
        self._seis = threading.Event()

    # ---------- HTTP ----------

    def _pyynto(self, node: Node, method: str, path: str, data: Any = None,
                timeout: float = 10.0) -> tuple[int, bytes]:
        url = node.url + path
        body = b"" if data is None else json.dumps(data).encode("utf-8")
        headers = protokolla.allekirjoita(self.token, method, _polku(url), body, self.lahettaja)
        if data is not None:
            headers["Content-Type"] = "application/json"
        return self.http(method, url, headers, body, timeout)

    def _json(self, node: Node, method: str, path: str, data: Any = None,
              timeout: float = 10.0, sallitut: tuple[int, ...] = (200,)) -> dict:
        try:
            status, raaka = self._pyynto(node, method, path, data, timeout)
        except OSError as e:
            raise NodeVirhe("YHTEYS", f"{node.node_id}: {e}", node.node_id) from e
        try:
            vastaus = json.loads(raaka.decode("utf-8")) if raaka else {}
        except (ValueError, UnicodeDecodeError):
            vastaus = {}
        if not isinstance(vastaus, dict):
            vastaus = {}
        if status in sallitut:
            return vastaus
        virhe = str(vastaus.get("virhe") or "")
        if status in (401, 403):
            raise NodeVirhe("EI_LUPAA", f"{node.node_id}: {virhe or status}", node.node_id)
        if status == 404 and virhe == "EI_KYKYA":
            raise NodeVirhe("EI_KYKYA", f"{node.node_id}: kyky puuttuu", node.node_id)
        raise NodeVirhe("KAATUI", f"{node.node_id}: HTTP {status} {virhe}".strip(), node.node_id)

    # ---------- tila ----------

    def _laske_tila(self, node: Node, nyt: float) -> str:
        if node.viimeksi_ok is None:
            return "poissa" if node.epaonnistumiset > 0 else "tuntematon"
        ika = nyt - node.viimeksi_ok
        if ika > self.poissa_s:
            return "poissa"
        if ika > self.epavarma_s or node.viimeisin_onnistui is False:
            return "epavarma"
        return "elossa"

    def _paivita_tilat(self) -> None:
        nyt = self.kello()
        with self._lukko:
            for node in self._nodet.values():
                node.tila = self._laske_tila(node, nyt)

    def _merkitse_virhe(self, node: Node, virhe: str) -> None:
        with self._lukko:
            node.viimeisin_onnistui = False
            node.epaonnistumiset += 1
            node.virhe = virhe
            node.tila = self._laske_tila(node, self.kello())

    def _pollaa(self, node: Node) -> None:
        try:
            syke = self._json(node, "GET", "/syke", timeout=5.0)
            hae_manifest = (
                node.manifest is None
                or node.viimeisin_onnistui is False
                or (syke.get("versio") and syke.get("versio") != node.manifest.get("versio"))
            )
            manifest = self._json(node, "GET", "/manifest", timeout=5.0) if hae_manifest else None
        except NodeVirhe as e:
            self._merkitse_virhe(node, e.viesti or e.koodi)
            return
        with self._lukko:
            node.syke = syke
            if manifest is not None:
                node.manifest = manifest
            node.viimeksi_ok = self.kello()
            node.viimeisin_onnistui = True
            node.virhe = ""
            node.tila = self._laske_tila(node, node.viimeksi_ok)

    def paivita(self) -> None:
        """Pollaa kaikki nodet rinnakkain."""
        nodet = list(self._nodet.values())
        if not nodet:
            return
        with ThreadPoolExecutor(max_workers=len(nodet)) as pool:
            list(pool.map(self._pollaa, nodet))
        self._paivita_tilat()

    def kaynnista(self, vali_s: float = 10.0) -> None:
        if self._saie and self._saie.is_alive():
            return
        self._seis.clear()

        def silmukka() -> None:
            while not self._seis.is_set():
                try:
                    self.paivita()
                except Exception:  # noqa: BLE001 - taustasäie ei saa kuolla
                    pass
                self._seis.wait(vali_s)

        self._saie = threading.Thread(target=silmukka, name="jarvis-reititin", daemon=True)
        self._saie.start()

    def pysayta(self) -> None:
        self._seis.set()
        if self._saie:
            self._saie.join(timeout=5)
        self._saie = None

    def nodet(self) -> list[Node]:
        self._paivita_tilat()
        with self._lukko:
            return list(self._nodet.values())

    def tila(self) -> list[dict]:
        """JSON-sarjallistettava yhteenveto (/api/nodet)."""
        nyt = self.kello()
        tulos = []
        for node in self.nodet():
            s = node.syke or {}
            m = node.manifest or {}
            laitteisto = m.get("laitteisto") or {}
            rivi = {
                "node_id": node.node_id,
                "nimi": m.get("nimi") or node.node_id,
                "url": node.url,
                "tila": node.tila,
                "virhe": node.virhe,
                "viimeksi_ok_s_sitten": (
                    None if node.viimeksi_ok is None else round(nyt - node.viimeksi_ok, 1)
                ),
                "gpu": s.get("gpu_nimi") or laitteisto.get("gpu"),
                "vram_vapaa_mb": s.get("vram_vapaa_mb"),
                "vram_mb": s.get("vram_mb") or laitteisto.get("vram_mb"),
                "cpu_pct": s.get("cpu_pct"),
                "palvelut": s.get("palvelut") or {},
                "ollama_ladattu": s.get("ollama_ladattu") or [],
                "tyot": {"kaynnissa": int(s.get("tyot_kaynnissa") or 0),
                         "jonossa": int(s.get("tyot_jonossa") or 0)},
                "peli_aktiivinen": s.get("peli_aktiivinen"),
                "prioriteetti": m.get("prioriteetti"),
                "kyvyt": [k.get("nimi") for k in m.get("kyvyt") or [] if isinstance(k, dict)],
            }
            tulos.append(json.loads(json.dumps(rivi, default=str)))
        return tulos

    # ---------- valinta ----------

    @staticmethod
    def _vram_riittaa(node: Node, tarve: int) -> bool:
        s = node.syke or {}
        if tarve <= 0 or not s.get("vram_mb") or s.get("vram_vapaa_mb") is None:
            return True
        return node.vram_vapaa() >= tarve

    def _ehdokkaat(self, kyky: str, vram_tarve_mb: int = 0,
                   ohita: set[str] | None = None) -> list[Node]:
        ohita = ohita or set()
        return [n for n in self.nodet()
                if n.node_id not in ohita and n.tila == "elossa" and n.tukee(kyky)
                and self._vram_riittaa(n, vram_tarve_mb)]

    @staticmethod
    def _jarjestys(node: Node) -> tuple:
        return (node.peli() is not None, node.kuorma(), node.prioriteetti(), -node.vram_vapaa())

    def valitse(self, kyky: str, vram_tarve_mb: int = 0, ohita: set[str] | None = None,
                suosi: str | None = None) -> Node | None:
        ehdokkaat = self._ehdokkaat(kyky, vram_tarve_mb, ohita)
        if not ehdokkaat:
            return None
        for n in ehdokkaat:
            if n.node_id == suosi:
                return n
        return min(ehdokkaat, key=self._jarjestys)

    def _ei_sopivaa(self, kyky: str) -> NodeVirhe:
        if any(n.tukee(kyky) for n in self.nodet()):
            return NodeVirhe("EI_NODEA", f"ei sopivaa elossa olevaa nodea kyvylle {kyky}")
        return NodeVirhe("EI_KYKYA", f"millään nodella ei ole kykyä {kyky}")

    # ---------- kutsut ----------

    def kutsu(self, kyky: str, args: dict | None = None, aikaraja_s: float = 300,
              progress: Callable[[str, int], Any] | None = None, node: str | None = None,
              vram_tarve_mb: int = 0) -> dict:
        """Aja kyky nodella ja odota tulos. Palauttaa tuloksen + "_node"."""
        if node is not None:
            with self._lukko:
                kohde = self._nodet.get(node)
            if kohde is None:
                raise NodeVirhe("EI_NODEA", f"tuntematon node {node}")
            if kohde.manifest is not None and not kohde.tukee(kyky):
                raise NodeVirhe("EI_KYKYA", f"{node}: ei kykyä {kyky}", node)
        else:
            kohde = self.valitse(kyky, vram_tarve_mb)
            if kohde is None:
                raise self._ei_sopivaa(kyky)
        nid = kohde.node_id
        raja = self.kello() + aikaraja_s
        try:
            vastaus = self._json(kohde, "POST", "/call",
                                 {"kyky": kyky, "args": args or {},
                                  "aikaraja_s": int(aikaraja_s)},
                                 sallitut=(200, 202))
        except NodeVirhe as e:
            if e.koodi == "YHTEYS":
                self._merkitse_virhe(kohde, e.viesti)
            raise
        tyo_id = str(vastaus.get("tyo_id") or "")
        if not tyo_id:
            raise NodeVirhe("KAATUI", f"{nid}: /call ei palauttanut tyo_id:tä", nid)
        polku = f"/tyo/{quote(tyo_id, safe='')}"
        edellinen: tuple | None = None
        while True:
            try:
                tyo = self._json(kohde, "GET", polku)
            except NodeVirhe as e:
                if e.koodi == "YHTEYS":
                    self._merkitse_virhe(kohde, e.viesti)
                raise
            viesti, pct = str(tyo.get("viesti") or ""), int(tyo.get("pct") or 0)
            if progress and (viesti, pct) != edellinen:
                edellinen = (viesti, pct)
                try:
                    progress(viesti, pct)
                except Exception:  # noqa: BLE001 - kutsujan callback ei kaada työtä
                    pass
            tila = tyo.get("tila")
            if tila == "valmis":
                tulos = dict(tyo.get("tulos") or {})
                tulos["_node"] = nid
                return tulos
            if tila in ("epaonnistui", "peruttu"):
                raise NodeVirhe("KAATUI", f"{nid}: {tyo.get('virhe') or tila}", nid)
            if self.kello() >= raja:
                try:
                    self._json(kohde, "POST", polku + "/peru", {}, timeout=5.0)
                except NodeVirhe:
                    pass
                raise NodeVirhe("AIKARAJA", f"{nid}: {kyky} ei valmistunut {aikaraja_s} s:ssa",
                                nid)
            self.nukku(self.kysely_vali_s)

    def rinnakkain(self, tehtavat: list[dict], progress: Callable[[str, int], Any] | None = None,
                   aikaraja_s: float = 900) -> list[dict | NodeVirhe]:
        """Jaa tehtävät kaikille kykeneville nodeille samanaikaisesti."""
        if not tehtavat:
            return []
        raja = self.kello() + aikaraja_s
        ehto = threading.Condition()
        varatut: dict[tuple[str, str], int] = {}
        kaikki: dict[str, int] = {}
        n = len(tehtavat)

        def varaa(kyky: str, tarve: int, ohita: set[str]) -> tuple[str, str] | NodeVirhe:
            with ehto:
                while True:
                    ehdokkaat = self._ehdokkaat(kyky, tarve, ohita)
                    if not ehdokkaat:
                        return self._ei_sopivaa(kyky)
                    vapaat = []
                    for node in ehdokkaat:
                        luokka = "gpu" if node.on_gpu_kyky(kyky) else "kevyt"
                        katto = GPU_RINNAKKAIN if luokka == "gpu" else KEVYT_RINNAKKAIN
                        if varatut.get((node.node_id, luokka), 0) < katto:
                            vapaat.append((node, luokka))
                    if vapaat:
                        node, luokka = min(vapaat, key=lambda p: (
                            kaikki.get(p[0].node_id, 0), self._jarjestys(p[0])))
                        avain = (node.node_id, luokka)
                        varatut[avain] = varatut.get(avain, 0) + 1
                        kaikki[node.node_id] = kaikki.get(node.node_id, 0) + 1
                        return avain
                    if self.kello() >= raja:
                        return NodeVirhe("AIKARAJA", f"ei vapaata nodea kyvylle {kyky}")
                    ehto.wait(0.2)

        def vapauta(avain: tuple[str, str]) -> None:
            with ehto:
                varatut[avain] -= 1
                kaikki[avain[0]] -= 1
                ehto.notify_all()

        def aja(i: int) -> dict | NodeVirhe:
            tehtava = tehtavat[i]
            kyky = str(tehtava.get("kyky") or "")
            args = tehtava.get("args") or {}
            tarve = int(tehtava.get("vram_tarve_mb") or 0)
            ohita: set[str] = set()
            virhe: NodeVirhe | None = None
            for _yritys in range(2):
                avain = varaa(kyky, tarve, ohita)
                if isinstance(avain, NodeVirhe):
                    return virhe or avain
                nid = avain[0]

                def raportti(viesti: str, pct: int, nid: str = nid) -> None:
                    if progress:
                        progress(f"[{i + 1}/{n}] {nid}: {viesti}", pct)

                try:
                    jaljella = max(1.0, raja - self.kello())
                    aikaraja = min(float(tehtava.get("aikaraja_s") or jaljella), jaljella)
                    return self.kutsu(kyky, args, aikaraja, raportti, node=nid,
                                      vram_tarve_mb=tarve)
                except NodeVirhe as e:
                    e.node_id = e.node_id or nid
                    virhe = e
                    if e.koodi not in UUSITTAVAT:
                        return e
                    ohita.add(nid)
                finally:
                    vapauta(avain)
            return virhe or NodeVirhe("KAATUI", "tuntematon virhe")

        with ThreadPoolExecutor(max_workers=min(32, n)) as pool:
            return list(pool.map(aja, range(n)))

    def hae_tiedosto(self, node_id: str, tiedosto_id: str) -> bytes:
        with self._lukko:
            node = self._nodet.get(node_id)
        if node is None:
            raise NodeVirhe("EI_NODEA", f"tuntematon node {node_id}")
        try:
            status, raaka = self._pyynto(node, "GET", f"/f/{quote(tiedosto_id, safe='')}",
                                         timeout=120.0)
        except OSError as e:
            raise NodeVirhe("YHTEYS", f"{node_id}: {e}", node_id) from e
        if status == 200:
            return raaka
        if status in (401, 403):
            raise NodeVirhe("EI_LUPAA", f"{node_id}: HTTP {status}", node_id)
        raise NodeVirhe("KAATUI", f"{node_id}: tiedostoa {tiedosto_id} ei saatu (HTTP {status})",
                        node_id)


def rekisteri_laitteista(laitteet: dict, token: str, lahettaja: str = "aivot") -> NodeRekisteri:
    """Luo rekisterin devices.json:n "laitteet"-dictistä (laitteet joilla node_portti)."""
    nodet: list[tuple[str, str]] = []
    for nid, laite in (laitteet or {}).items():
        if not isinstance(laite, dict) or not laite.get("node_portti"):
            continue
        portti = int(laite["node_portti"])
        if laite.get("paikallinen"):
            isanta = "127.0.0.1"
        else:
            isanta = laite.get("tailscale") or laite.get("lan") or ""
        if not isanta:
            continue
        if ":" in isanta:
            isanta = f"[{isanta}]"
        nodet.append((nid, f"http://{isanta}:{portti}"))
    return NodeRekisteri(token, nodet, lahettaja=lahettaja)


_oletus: NodeRekisteri | None = None


def oletus() -> NodeRekisteri | None:
    return _oletus


def aseta_oletus(r: NodeRekisteri | None) -> None:
    global _oletus
    _oletus = r
