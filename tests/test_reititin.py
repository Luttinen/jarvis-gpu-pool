"""Testit reitittimelle vale-HTTP:llä (vale-nodet tarkistavat allekirjoituksen)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from jarvis_core import protokolla as p
from jarvis_core import reititin as r
from jarvis_core.reititin import NodeRekisteri, NodeVirhe

TOKEN = "f" * 64
REPO = Path(__file__).resolve().parent.parent


class Kello:
    """Jäädytetty kello; nukku() siirtää sitä eteenpäin."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def nukku(self, s: float) -> None:
        self.t += s


class ValeNode:
    """Dict-pohjainen vale-node: /syke, /manifest, /call, /tyo/<id>, /f/<id>."""

    def __init__(self, node_id: str, kyvyt: dict | None = None, prioriteetti: int = 1,
                 token: str = TOKEN, **syke) -> None:
        self.node_id = node_id
        self.tarkistaja = p.Tarkistaja(token)
        # kyvyt: nimi -> {"gpu": bool, "vaiheet": [(viesti, pct)], "tulos": {}, "ajo": fn,
        #                  "jumi": bool, "epaonnistuu": str}
        self.kyvyt = kyvyt or {}
        self.prioriteetti = prioriteetti
        self.versio = "0.1.0"
        self.syke = {
            "ts": 0.0, "cpu_pct": 5.0, "ram_mb": 16000, "ram_vapaa_mb": 8000,
            "gpu_nimi": "RTX", "gpu_pct": 0.0, "vram_mb": 8192, "vram_vapaa_mb": 8000,
            "palvelut": {"ollama": {"kaynnissa": True, "portti": 11434, "tiedot": {}}},
            "ollama_ladattu": [], "tyot_kaynnissa": 0, "tyot_jonossa": 0,
            "peli_aktiivinen": None,
        }
        self.syke.update(syke)
        self.paalla = True
        self.katkaise_tyot = False  # /tyo-pyynnöt kaatuvat yhteysvirheeseen
        self.tyot: dict[str, dict] = {}
        self.tiedostot: dict[str, bytes] = {}
        self.pyynnot: list[tuple[str, str]] = []
        self.hylatyt: list[str] = []
        self._lukko = threading.Lock()
        self._n = 0

    def manifest(self) -> dict:
        return {
            "node_id": self.node_id, "nimi": self.node_id.upper(), "os": "windows",
            "prioriteetti": self.prioriteetti, "versio": self.versio,
            "laitteisto": {"gpu": "RTX", "vram_mb": 8192, "ram_mb": 16000, "cpu_ytimet": 8},
            "kyvyt": [{"nimi": n, "vram_tarve_mb": 0, "gpu": bool(k.get("gpu")), "kuvaus": ""}
                      for n, k in self.kyvyt.items()],
            "palvelut": ["ollama"],
        }

    @staticmethod
    def _vastaa(status: int, data) -> tuple[int, bytes]:
        return status, json.dumps(data).encode("utf-8")

    def kasittele(self, method: str, path: str, headers: dict, body: bytes) -> tuple[int, bytes]:
        ok, syy = self.tarkistaja.tarkista(method, path, headers, body)
        with self._lukko:
            self.pyynnot.append((method, path))
        if not ok:
            self.hylatyt.append(syy)
            return self._vastaa(401, {"virhe": "EI_LUPAA", "syy": syy})
        if method == "GET" and path == "/syke":
            return self._vastaa(200, dict(self.syke, ts=time.time()))
        if method == "GET" and path == "/manifest":
            return self._vastaa(200, self.manifest())
        if method == "POST" and path == "/call":
            return self._call(json.loads(body))
        if path.startswith("/tyo/"):
            if self.katkaise_tyot:
                raise OSError("yhteys katkesi")
            osat = path.split("/")
            tyo = self.tyot.get(osat[2])
            if tyo is None:
                return self._vastaa(404, {"virhe": "EI_TYOTA"})
            if method == "POST" and len(osat) == 4 and osat[3] == "peru":
                tyo["tila"] = "peruttu"
                return self._vastaa(200, {"ok": True})
            return self._vastaa(200, self._edista(tyo))
        if method == "GET" and path.startswith("/f/"):
            data = self.tiedostot.get(path[3:])
            return (200, data) if data is not None else self._vastaa(404, {"virhe": "EI_TIEDOSTOA"})
        return self._vastaa(404, {"virhe": "EI_POLKUA"})

    def _call(self, pyynto: dict) -> tuple[int, bytes]:
        kyky = self.kyvyt.get(pyynto["kyky"])
        if kyky is None:
            return self._vastaa(404, {"virhe": "EI_KYKYA"})
        with self._lukko:
            self._n += 1
            tyo_id = f"{self.node_id}-{self._n}"
        tyo = {"tyo_id": tyo_id, "kyky": pyynto["kyky"], "tila": "jonossa", "pct": 0,
               "viesti": "", "tulos": None, "virhe": None, "kesto_ms": None,
               "_vaihe": 0, "_kyky": kyky, "_args": pyynto["args"]}
        self.tyot[tyo_id] = tyo
        if "ajo" in kyky:
            def saie() -> None:
                tyo["tila"] = "kaynnissa"
                try:
                    tyo["tulos"] = kyky["ajo"](pyynto["args"], self)
                    tyo["tila"] = "valmis"
                except Exception as e:  # noqa: BLE001
                    tyo["virhe"] = str(e)
                    tyo["tila"] = "epaonnistui"
            threading.Thread(target=saie, daemon=True).start()
        return self._vastaa(202, {"tyo_id": tyo_id})

    def _edista(self, tyo: dict) -> dict:
        kyky = tyo["_kyky"]
        if "ajo" not in kyky and tyo["tila"] in ("jonossa", "kaynnissa"):
            vaiheet = kyky.get("vaiheet", [])
            if kyky.get("jumi"):
                tyo.update(tila="kaynnissa", viesti="jumissa", pct=1)
            elif tyo["_vaihe"] < len(vaiheet):
                viesti, pct = vaiheet[tyo["_vaihe"]]
                tyo.update(tila="kaynnissa", viesti=viesti, pct=pct)
                tyo["_vaihe"] += 1
            elif kyky.get("epaonnistuu"):
                tyo.update(tila="epaonnistui", virhe=kyky["epaonnistuu"])
            else:
                tulos = kyky.get("tulos", {"args": tyo["_args"], "node": self.node_id})
                tyo.update(tila="valmis", pct=100, viesti="valmis", tulos=tulos, kesto_ms=5)
        return {k: v for k, v in tyo.items() if not k.startswith("_")}


class ValeHttp:
    """Reitittää pyynnöt vale-nodeille isäntänimen perusteella."""

    def __init__(self, *nodet: ValeNode) -> None:
        self.nodet = {n.node_id: n for n in nodet}

    def __call__(self, method, url, headers, body, timeout):
        osat = urlsplit(url)
        node = self.nodet.get(osat.hostname or "")
        if node is None or not node.paalla:
            raise OSError("yhteys evätty")
        polku = osat.path + (f"?{osat.query}" if osat.query else "")
        return node.kasittele(method, polku, dict(headers), body)


def rekisteri(*nodet: ValeNode, kello: Kello | None = None, token: str = TOKEN,
              **kw) -> NodeRekisteri:
    if kello is not None:
        kw.setdefault("kello", kello)
        kw.setdefault("nukku", kello.nukku)
    else:
        kw.setdefault("nukku", lambda s: time.sleep(0.005))
    return NodeRekisteri(token, [(n.node_id, f"http://{n.node_id}:8791") for n in nodet],
                         http=ValeHttp(*nodet), **kw)


def node(reg: NodeRekisteri, nid: str) -> r.Node:
    return next(n for n in reg.nodet() if n.node_id == nid)


# ---------- tilat ----------

def test_tilasiirtymat_jaadytetylla_kellolla():
    a = ValeNode("a", {"llm.chat": {}})
    kello = Kello()
    reg = rekisteri(a, kello=kello)
    assert node(reg, "a").tila == "tuntematon"
    reg.paivita()
    assert node(reg, "a").tila == "elossa"
    assert node(reg, "a").manifest["node_id"] == "a"
    kello.t += 25
    assert node(reg, "a").tila == "elossa"
    kello.t += 1
    assert node(reg, "a").tila == "epavarma"
    kello.t += 34
    assert node(reg, "a").tila == "epavarma"
    kello.t += 1
    assert node(reg, "a").tila == "poissa"
    reg.paivita()
    assert node(reg, "a").tila == "elossa"
    # manifest haettiin vain kerran, koska välissä ei ollut epäonnistumista
    assert a.pyynnot.count(("GET", "/manifest")) == 1
    assert a.hylatyt == []


def test_epaonnistunut_syke_epavarma_sitten_poissa():
    a = ValeNode("a", {"llm.chat": {}})
    kello = Kello()
    reg = rekisteri(a, kello=kello)
    reg.paivita()
    a.paalla = False
    kello.t += 5
    reg.paivita()
    n = node(reg, "a")
    assert n.tila == "epavarma" and "yhteys" in n.virhe
    kello.t += 56
    assert node(reg, "a").tila == "poissa"
    # palaa: manifest haetaan uudelleen
    a.paalla = True
    reg.paivita()
    assert node(reg, "a").tila == "elossa" and node(reg, "a").virhe == ""
    assert a.pyynnot.count(("GET", "/manifest")) == 2


def test_ei_koskaan_onnistunut_on_poissa():
    a = ValeNode("a")
    a.paalla = False
    reg = rekisteri(a, kello=Kello())
    reg.paivita()
    assert node(reg, "a").tila == "poissa"


def test_vaara_token_ei_lupaa():
    a = ValeNode("a", {"llm.chat": {}})
    reg = rekisteri(a, kello=Kello(), token="0" * 64)
    reg.paivita()
    n = node(reg, "a")
    assert n.tila == "poissa" and n.manifest is None
    assert a.hylatyt == ["väärä allekirjoitus"]
    with pytest.raises(NodeVirhe) as e:
        reg.kutsu("llm.chat", {}, node="a")
    assert e.value.koodi == "EI_LUPAA"


def test_manifest_haetaan_kun_versio_muuttuu():
    a = ValeNode("a", {"llm.chat": {}})
    reg = rekisteri(a, kello=Kello())
    reg.paivita()
    reg.paivita()
    assert a.pyynnot.count(("GET", "/manifest")) == 1
    a.versio = "0.2.0"
    a.syke["versio"] = "0.2.0"
    reg.paivita()
    assert a.pyynnot.count(("GET", "/manifest")) == 2
    assert node(reg, "a").manifest["versio"] == "0.2.0"


def test_tila_json_sarjallistettava():
    a = ValeNode("a", {"llm.chat": {}, "comfy.aja": {"gpu": True}}, tyot_kaynnissa=1)
    b = ValeNode("b")
    b.paalla = False
    kello = Kello()
    reg = rekisteri(a, b, kello=kello)
    reg.paivita()
    kello.t += 3
    tila = reg.tila()
    json.dumps(tila)
    ta, tb = tila
    assert ta["node_id"] == "a" and ta["tila"] == "elossa" and ta["url"] == "http://a:8791"
    assert ta["viimeksi_ok_s_sitten"] == 3.0
    assert ta["kyvyt"] == ["llm.chat", "comfy.aja"]
    assert ta["tyot"] == {"kaynnissa": 1, "jonossa": 0}
    assert ta["gpu"] == "RTX" and ta["vram_mb"] == 8192 and ta["vram_vapaa_mb"] == 8000
    assert "ollama" in ta["palvelut"] and ta["ollama_ladattu"] == [] and ta["cpu_pct"] == 5.0
    assert tb["tila"] == "poissa" and tb["viimeksi_ok_s_sitten"] is None and tb["kyvyt"] == []


def test_kaynnista_ja_pysayta_saie():
    a = ValeNode("a", {"llm.chat": {}})
    reg = rekisteri(a)
    reg.kaynnista(vali_s=0.01)
    try:
        loppu = time.time() + 5
        while node(reg, "a").tila != "elossa" and time.time() < loppu:
            time.sleep(0.01)
        assert node(reg, "a").tila == "elossa"
    finally:
        reg.pysayta()
    assert reg._saie is None
    maara = len(a.pyynnot)
    time.sleep(0.05)
    assert len(a.pyynnot) == maara


# ---------- valinta ----------

def _kaksi(**kw_b):
    a = ValeNode("a", {"comfy.aja": {"gpu": True}, "llm.chat": {}}, prioriteetti=1)
    b = ValeNode("b", {"comfy.aja": {"gpu": True}, "llm.chat": {}}, prioriteetti=2, **kw_b)
    kello = Kello()
    reg = rekisteri(a, b, kello=kello)
    return a, b, kello, reg


def test_valitse_prioriteetti_ja_kyky():
    a, b, kello, reg = _kaksi()
    reg.paivita()
    assert reg.valitse("llm.chat").node_id == "a"
    assert reg.valitse("ei.ole") is None


def test_valitse_kuorma_voittaa_prioriteetin():
    a, b, kello, reg = _kaksi()
    a.syke.update(tyot_kaynnissa=1, tyot_jonossa=1)
    reg.paivita()
    assert reg.valitse("llm.chat").node_id == "b"


def test_valitse_suurin_vram_tasapelissa():
    a, b, kello, reg = _kaksi()
    b.prioriteetti = 1
    b.syke["vram_vapaa_mb"] = 9000
    reg.paivita()
    assert reg.valitse("llm.chat").node_id == "b"


def test_valitse_vram_tarve():
    a, b, kello, reg = _kaksi(vram_vapaa_mb=7000)
    a.syke["vram_vapaa_mb"] = 2000
    reg.paivita()
    assert reg.valitse("comfy.aja", vram_tarve_mb=6000).node_id == "b"
    assert reg.valitse("comfy.aja", vram_tarve_mb=7500) is None
    assert reg.valitse("comfy.aja").node_id == "a"
    # syke ei tiedä vramia (ei GPU:ta) → tarve ei sulje pois
    a.syke.update(vram_mb=0, vram_vapaa_mb=0, gpu_nimi=None)
    reg.paivita()
    assert reg.valitse("comfy.aja", vram_tarve_mb=7500).node_id == "a"


def test_valitse_peli_aktiivinen_viimeiseksi():
    a, b, kello, reg = _kaksi()
    a.syke["peli_aktiivinen"] = "GTA5.exe"
    b.syke.update(tyot_kaynnissa=3)
    reg.paivita()
    assert reg.valitse("comfy.aja").node_id == "b"
    b.paalla = False
    kello.t += 1
    reg.paivita()
    # pelaava node kelpaa, jos muuta ei ole
    assert reg.valitse("comfy.aja").node_id == "a"


def test_valitse_suosi_ja_ohita():
    a, b, kello, reg = _kaksi()
    reg.paivita()
    assert reg.valitse("llm.chat", suosi="b").node_id == "b"
    assert reg.valitse("llm.chat", ohita={"a"}).node_id == "b"
    assert reg.valitse("llm.chat", ohita={"a", "b"}) is None
    # suosittu ei kelpaa (ohitettu) → normaali järjestys
    assert reg.valitse("llm.chat", suosi="b", ohita={"b"}).node_id == "a"
    assert reg.valitse("llm.chat", suosi="tuntematon").node_id == "a"


def test_valitse_vain_elossa():
    a, b, kello, reg = _kaksi()
    reg.paivita()
    a.paalla = False
    reg.paivita()
    assert node(reg, "a").tila == "epavarma"
    assert reg.valitse("llm.chat").node_id == "b"


# ---------- kutsu ----------

def test_kutsu_progress_ja_tulos():
    a = ValeNode("a", {"comfy.aja": {"gpu": True,
                                     "vaiheet": [("ladataan", 10), ("ladataan", 10),
                                                 ("generoidaan", 50)],
                                     "tulos": {"tiedostot": [{"id": "x1", "nimi": "k.png"}]}}})
    kello = Kello()
    reg = rekisteri(a, kello=kello)
    reg.paivita()
    viestit = []
    tulos = reg.kutsu("comfy.aja", {"workflow": {}}, progress=lambda v, pct: viestit.append((v, pct)))
    assert tulos == {"tiedostot": [{"id": "x1", "nimi": "k.png"}], "_node": "a"}
    assert viestit == [("ladataan", 10), ("generoidaan", 50), ("valmis", 100)]
    assert kello.t == 1003.0  # kolme 1 s odotusta
    assert a.hylatyt == []
    assert ("POST", "/call") in a.pyynnot


def test_kutsu_progress_poikkeus_ei_kaada():
    a = ValeNode("a", {"x": {"vaiheet": [("v", 1)]}})
    reg = rekisteri(a, kello=Kello())
    reg.paivita()

    def huono(v, pct):
        raise RuntimeError("callback rikki")

    assert reg.kutsu("x", {}, progress=huono)["_node"] == "a"


def test_kutsu_aikaraja_peruu():
    a = ValeNode("a", {"x": {"jumi": True}})
    kello = Kello()
    reg = rekisteri(a, kello=kello)
    reg.paivita()
    with pytest.raises(NodeVirhe) as e:
        reg.kutsu("x", {}, aikaraja_s=5)
    assert e.value.koodi == "AIKARAJA" and e.value.node_id == "a"
    assert a.tyot["a-1"]["tila"] == "peruttu"
    assert ("POST", "/tyo/a-1/peru") in a.pyynnot


def test_kutsu_epaonnistui_on_kaatui():
    a = ValeNode("a", {"x": {"epaonnistuu": "ComfyUI ei vastaa"}})
    reg = rekisteri(a, kello=Kello())
    reg.paivita()
    with pytest.raises(NodeVirhe) as e:
        reg.kutsu("x", {})
    assert e.value.koodi == "KAATUI" and "ComfyUI ei vastaa" in str(e.value)


def test_kutsu_yhteys_katkeaa_merkitsee_epavarmaksi():
    a = ValeNode("a", {"x": {"jumi": True}})
    reg = rekisteri(a, kello=Kello())
    reg.paivita()
    a.katkaise_tyot = True
    with pytest.raises(NodeVirhe) as e:
        reg.kutsu("x", {})
    assert e.value.koodi == "YHTEYS"
    assert node(reg, "a").tila == "epavarma"


def test_kutsu_virhekoodit():
    a = ValeNode("a", {"x": {}})
    b = ValeNode("b", {"x": {}})
    kello = Kello()
    reg = rekisteri(a, b, kello=kello)
    with pytest.raises(NodeVirhe) as e:
        reg.kutsu("x", {})
    assert e.value.koodi == "EI_KYKYA"  # manifesteja ei vielä ole
    reg.paivita()
    with pytest.raises(NodeVirhe) as e:
        reg.kutsu("y", {})
    assert e.value.koodi == "EI_KYKYA"
    with pytest.raises(NodeVirhe) as e:
        reg.kutsu("x", {}, node="z")
    assert e.value.koodi == "EI_NODEA"
    with pytest.raises(NodeVirhe) as e:
        reg.kutsu("y", {}, node="a")
    assert e.value.koodi == "EI_KYKYA"
    kello.t += 100
    with pytest.raises(NodeVirhe) as e:
        reg.kutsu("x", {})
    assert e.value.koodi == "EI_NODEA"  # kyky tunnetaan, mutta nodet poissa
    # nimetty node: kyky poistui nodelta → noden 404 EI_KYKYA
    reg.paivita()
    del b.kyvyt["x"]
    with pytest.raises(NodeVirhe) as e:
        reg.kutsu("x", {}, node="b")
    assert e.value.koodi == "EI_KYKYA"


def test_kutsu_nimetylle_nodelle():
    a = ValeNode("a", {"x": {}}, prioriteetti=1)
    b = ValeNode("b", {"x": {}}, prioriteetti=2)
    reg = rekisteri(a, b, kello=Kello())
    reg.paivita()
    assert reg.kutsu("x", {"k": 1})["_node"] == "a"
    assert reg.kutsu("x", {"k": 1}, node="b") == {"args": {"k": 1}, "node": "b", "_node": "b"}


def test_hae_tiedosto():
    a = ValeNode("a")
    a.tiedostot["kuva-1.png"] = b"\x89PNG data"
    reg = rekisteri(a, kello=Kello())
    assert reg.hae_tiedosto("a", "kuva-1.png") == b"\x89PNG data"
    with pytest.raises(NodeVirhe) as e:
        reg.hae_tiedosto("a", "puuttuu")
    assert e.value.koodi == "KAATUI"
    with pytest.raises(NodeVirhe) as e:
        reg.hae_tiedosto("z", "kuva-1.png")
    assert e.value.koodi == "EI_NODEA"
    a.paalla = False
    with pytest.raises(NodeVirhe) as e:
        reg.hae_tiedosto("a", "kuva-1.png")
    assert e.value.koodi == "YHTEYS"
    assert a.hylatyt == []


# ---------- rinnakkain ----------

def test_rinnakkain_jakaa_kahdelle_nodelle_samanaikaisesti():
    este = threading.Barrier(2, timeout=5)

    def ajo(args, vale):
        este.wait()  # valmistuu vain, jos molemmat ajetaan yhtä aikaa
        return {"i": args["i"], "node": vale.node_id}

    a = ValeNode("a", {"comfy.aja": {"gpu": True, "ajo": ajo}}, prioriteetti=1)
    b = ValeNode("b", {"comfy.aja": {"gpu": True, "ajo": ajo}}, prioriteetti=2)
    reg = rekisteri(a, b, kysely_vali_s=0.01)
    reg.paivita()
    viestit = []
    tulokset = reg.rinnakkain([{"kyky": "comfy.aja", "args": {"i": i}} for i in range(2)],
                              progress=lambda v, pct: viestit.append(v), aikaraja_s=10)
    assert all(isinstance(t, dict) for t in tulokset), tulokset
    assert [t["i"] for t in tulokset] == [0, 1]
    assert {t["_node"] for t in tulokset} == {"a", "b"}
    assert any(v.startswith("[1/2]") for v in viestit)


def _laskuri_ajo(tila: dict, kesto: float = 0.1):
    lukko = threading.Lock()

    def ajo(args, vale):
        with lukko:
            tila["nyt"] += 1
            tila["max"] = max(tila["max"], tila["nyt"])
        time.sleep(kesto)
        with lukko:
            tila["nyt"] -= 1
        return {"i": args["i"]}

    return ajo


def test_rinnakkain_gpu_kyky_yksi_kerrallaan_per_node():
    tila = {"nyt": 0, "max": 0}
    a = ValeNode("a", {"comfy.aja": {"gpu": True, "ajo": _laskuri_ajo(tila, 0.05)}})
    reg = rekisteri(a, kysely_vali_s=0.005)
    reg.paivita()
    tulokset = reg.rinnakkain([{"kyky": "comfy.aja", "args": {"i": i}} for i in range(3)])
    assert [t["i"] for t in tulokset] == [0, 1, 2]
    assert tila["max"] == 1


def test_rinnakkain_kevyt_kyky_nelja_per_node():
    tila = {"nyt": 0, "max": 0}
    a = ValeNode("a", {"llm.chat": {"ajo": _laskuri_ajo(tila, 0.2)}})
    reg = rekisteri(a, kysely_vali_s=0.005)
    reg.paivita()
    tulokset = reg.rinnakkain([{"kyky": "llm.chat", "args": {"i": i}} for i in range(7)])
    assert [t["i"] for t in tulokset] == list(range(7))
    assert tila["max"] == 4


def test_rinnakkain_uusinta_toisella_nodella():
    def kaatuu(args, vale):
        raise RuntimeError("CUDA out of memory")

    def ok(args, vale):
        return {"i": args["i"]}

    a = ValeNode("a", {"comfy.aja": {"gpu": True, "ajo": kaatuu}}, prioriteetti=1)
    b = ValeNode("b", {"comfy.aja": {"gpu": True, "ajo": ok}}, prioriteetti=2)
    reg = rekisteri(a, b, kysely_vali_s=0.005)
    reg.paivita()
    tulokset = reg.rinnakkain([{"kyky": "comfy.aja", "args": {"i": 0}}])
    assert tulokset == [{"i": 0, "_node": "b"}]
    assert ("POST", "/call") in a.pyynnot


def test_rinnakkain_uusinta_kun_yhteys_katkeaa():
    a = ValeNode("a", {"x": {"jumi": True}}, prioriteetti=1)
    b = ValeNode("b", {"x": {}}, prioriteetti=2)
    reg = rekisteri(a, b, kysely_vali_s=0.005)
    reg.paivita()
    a.katkaise_tyot = True
    tulokset = reg.rinnakkain([{"kyky": "x", "args": {"i": 0}}])
    assert tulokset[0]["_node"] == "b"
    assert node(reg, "a").tila == "epavarma"


def test_rinnakkain_virheet_tuloslistassa():
    def kaatuu(args, vale):
        raise RuntimeError("rikki")

    a = ValeNode("a", {"x": {"ajo": kaatuu}, "ok": {}})
    b = ValeNode("b", {"x": {"ajo": kaatuu}})
    reg = rekisteri(a, b, kysely_vali_s=0.005)
    reg.paivita()
    tulokset = reg.rinnakkain([
        {"kyky": "ok", "args": {"n": 1}},
        {"kyky": "x", "args": {}},
        {"kyky": "ei.ole", "args": {}},
    ])
    assert tulokset[0]["_node"] == "a"
    assert isinstance(tulokset[1], NodeVirhe) and tulokset[1].koodi == "KAATUI"
    # kaatui molemmilla: yksi uusinta, ei enempää
    assert a.pyynnot.count(("POST", "/call")) + b.pyynnot.count(("POST", "/call")) == 3
    assert isinstance(tulokset[2], NodeVirhe) and tulokset[2].koodi == "EI_KYKYA"
    assert reg.rinnakkain([]) == []


# ---------- devices.json ----------

def test_rekisteri_laitteista():
    laitteet = json.loads((REPO / "examples" / "devices.example.json").read_text(encoding="utf-8"))
    laitteet = laitteet["laitteet"]
    laitteet["desktop"]["node_portti"] = 8791
    laitteet["gaming-pc"]["node_portti"] = 8791
    laitteet["lan_kone"] = {"lan": "192.168.1.50", "node_portti": 9000}
    laitteet["ipv6"] = {"tailscale": "fd7a:115c:a1e0::5", "node_portti": 8791}
    laitteet["ilman_osoitetta"] = {"node_portti": 8791}
    reg = r.rekisteri_laitteista(laitteet, TOKEN, lahettaja="testi")
    urlit = {n.node_id: n.url for n in reg.nodet()}
    assert urlit == {
        "desktop": "http://127.0.0.1:8791",
        "gaming-pc": "http://100.64.0.20:8791",
        "lan_kone": "http://192.168.1.50:9000",
        "ipv6": "http://[fd7a:115c:a1e0::5]:8791",
    }
    assert reg.lahettaja == "testi"
    assert all(n.tila == "tuntematon" for n in reg.nodet())


def test_oletus():
    vanha = r.oletus()
    try:
        reg = NodeRekisteri(TOKEN, [])
        r.aseta_oletus(reg)
        assert r.oletus() is reg
        r.aseta_oletus(None)
        assert r.oletus() is None
    finally:
        r.aseta_oletus(vanha)


# ---------- oletus-HTTP (urllib) oikeaa palvelinta vastaan ----------

class _Kasittelija(BaseHTTPRequestHandler):
    tarkistaja = p.Tarkistaja(TOKEN)

    def log_message(self, *a):
        pass

    def do_GET(self):
        ok, syy = self.tarkistaja.tarkista("GET", self.path, self.headers, b"")
        if not ok:
            data, status = json.dumps({"virhe": "EI_LUPAA", "syy": syy}).encode(), 401
        elif self.path == "/syke":
            data, status = json.dumps({"ts": 1.0, "vram_mb": 0}).encode(), 200
        elif self.path == "/manifest":
            data, status = json.dumps({"node_id": "oikea", "kyvyt": []}).encode(), 200
        else:
            data, status = b'{"virhe":"EI_POLKUA"}', 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_urllib_http_oikealla_palvelimella():
    palvelin = ThreadingHTTPServer(("127.0.0.1", 0), _Kasittelija)
    threading.Thread(target=palvelin.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{palvelin.server_address[1]}"
        reg = NodeRekisteri(TOKEN, [("oikea", url)])
        reg.paivita()
        assert node(reg, "oikea").tila == "elossa"
        assert node(reg, "oikea").manifest["node_id"] == "oikea"
        status, data = r._urllib_http("GET", url + "/eiole", {}, b"", 5)
        assert status == 401 and b"EI_LUPAA" in data  # HTTPError → status + runko
    finally:
        palvelin.shutdown()
        palvelin.server_close()
    with pytest.raises(OSError):
        r._urllib_http("GET", url + "/syke", {}, b"", 2)
