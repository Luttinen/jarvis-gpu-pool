"""Testit työjonolle: herätys, purku koneen herättyä, aikarajat ja kaatumisesta palautus."""

from __future__ import annotations

from jarvis_core.jono import MAX_YRITYKSET, Jono
from jarvis_core.reititin import Node, NodeVirhe


class Kello:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class ValeRekisteri:
    """NodeRekisterin rajapinta ilman verkkoa: nodet(), valitse(), kutsu()."""

    def __init__(self, *nodet: Node) -> None:
        self._nodet = {n.node_id: n for n in nodet}
        self.kutsut: list[tuple[str, dict, str]] = []
        self.virhe: NodeVirhe | None = None

    def nodet(self) -> list[Node]:
        return list(self._nodet.values())

    def valitse(self, kyky: str, *_a, **_k) -> Node | None:
        ehd = [n for n in self._nodet.values() if n.tila == "elossa" and n.tukee(kyky)]
        return ehd[0] if ehd else None

    def kutsu(self, kyky: str, args: dict, aikaraja_s: float = 300, node: str | None = None,
              **_k) -> dict:
        self.kutsut.append((kyky, args, node or ""))
        if self.virhe is not None:
            raise self.virhe
        return {"vastaus": f"ok {args.get('kehote', '')}", "_node": node}


def _node(nid: str, tila: str, kyvyt: list[str] | None) -> Node:
    manifest = None if kyvyt is None else {"kyvyt": [{"nimi": k} for k in kyvyt]}
    return Node(node_id=nid, url=f"http://{nid}", tila=tila, manifest=manifest)


def _jono(tmp_path, rek, **kw):
    herätetyt: list[str] = []
    ilmoitukset: list[dict] = []
    kw.setdefault("heraata", lambda nid: herätetyt.append(nid) or True)
    j = Jono(tmp_path / "jono.db", rek, ilmoita=ilmoitukset.append, aja_saikeessa=False, **kw)
    return j, herätetyt, ilmoitukset


def test_elossa_oleva_node_ajaa_heti(tmp_path):
    rek = ValeRekisteri(_node("pelikone", "elossa", ["llm.chat"]))
    j, herätetyt, ilmoitukset = _jono(tmp_path, rek)
    tyo = j.lisaa("llm.chat", {"kehote": "hei"}, pyytaja="testi")
    assert tyo["tila"] == "valmis"
    assert tyo["node"] == "pelikone"
    assert tyo["tulos"]["vastaus"] == "ok hei"
    assert herätetyt == []
    assert [i["tila"] for i in ilmoitukset] == ["valmis"]


def test_sammunut_kone_heratetaan_ja_jono_puretaan(tmp_path):
    pelikone = _node("pelikone", "poissa", ["comfy.aja"])
    rek = ValeRekisteri(_node("paakone", "elossa", ["llm.chat"]), pelikone)
    kello = Kello()
    j, herätetyt, ilmoitukset = _jono(tmp_path, rek, kello=kello)
    tyo = j.lisaa("comfy.aja", {"workflow": {"1": {}}})
    assert tyo["tila"] == "odottaa_heratysta"
    assert tyo["kohde"] == "pelikone"
    assert herätetyt == ["pelikone"]
    # Toinen kierros ei herätä uudelleen eikä aja vielä.
    kello.t += 30
    j.kasittele()
    assert herätetyt == ["pelikone"] and rek.kutsut == []
    # Kone herää: seuraava kierros ajaa työn.
    pelikone.tila = "elossa"
    j.kasittele()
    valmis = j.hae(tyo["job_id"])
    assert valmis["tila"] == "valmis" and valmis["node"] == "pelikone"
    assert rek.kutsut == [("comfy.aja", {"workflow": {"1": {}}}, "pelikone")]
    assert ilmoitukset[-1]["job_id"] == tyo["job_id"]


def test_tuntematon_manifest_heratetaan(tmp_path):
    # Kone oli sammuksissa koko koordinaattorin ajon: kyvyt eivät ole tiedossa.
    rek = ValeRekisteri(_node("pelikone", "poissa", None))
    j, herätetyt, _ = _jono(tmp_path, rek)
    assert j.lisaa("comfy.aja")["tila"] == "odottaa_heratysta"
    assert herätetyt == ["pelikone"]


def test_heratyksen_aikaraja(tmp_path):
    rek = ValeRekisteri(_node("pelikone", "poissa", ["comfy.aja"]))
    kello = Kello()
    j, _, ilmoitukset = _jono(tmp_path, rek, kello=kello)
    tyo = j.lisaa("comfy.aja", odota_s=120)
    kello.t += 121
    j.kasittele()
    paatetty = j.hae(tyo["job_id"])
    assert paatetty["tila"] == "epaonnistui"
    assert "ei herännyt" in paatetty["virhe"]
    assert ilmoitukset[-1]["tila"] == "epaonnistui"


def test_herätys_epäonnistuu(tmp_path):
    rek = ValeRekisteri(_node("pelikone", "poissa", ["comfy.aja"]))
    j, _, _ = _jono(tmp_path, rek, heraata=lambda nid: False)
    tyo = j.lisaa("comfy.aja")
    assert tyo["tila"] == "epaonnistui" and "herätys" in tyo["virhe"]


def test_kykya_ei_ole_kenellakaan(tmp_path):
    rek = ValeRekisteri(_node("paakone", "elossa", ["llm.chat"]))
    j, herätetyt, _ = _jono(tmp_path, rek)
    tyo = j.lisaa("video.generoi")
    assert tyo["tila"] == "epaonnistui" and herätetyt == []


def test_peru_odottava(tmp_path):
    rek = ValeRekisteri(_node("pelikone", "poissa", ["comfy.aja"]))
    j, _, _ = _jono(tmp_path, rek)
    tyo = j.lisaa("comfy.aja")
    assert j.peru(tyo["job_id"]) is True
    assert j.hae(tyo["job_id"])["tila"] == "peruttu"
    assert j.peru(tyo["job_id"]) is False
    rek._nodet["pelikone"].tila = "elossa"
    j.kasittele()
    assert rek.kutsut == []  # peruttua ei ajeta


def test_kohde_node(tmp_path):
    rek = ValeRekisteri(_node("paakone", "elossa", ["llm.chat"]),
                        _node("pelikone", "poissa", ["llm.chat"]))
    j, herätetyt, _ = _jono(tmp_path, rek)
    tyo = j.lisaa("llm.chat", {"kehote": "iso malli"}, kohde="pelikone")
    assert tyo["tila"] == "odottaa_heratysta" and herätetyt == ["pelikone"]


def test_yhteysvirhe_yritetaan_uudelleen(tmp_path):
    rek = ValeRekisteri(_node("pelikone", "elossa", ["llm.chat"]))
    rek.virhe = NodeVirhe("YHTEYS", "katkesi", "pelikone")
    j, _, _ = _jono(tmp_path, rek)
    tyo = j.lisaa("llm.chat")
    assert j.hae(tyo["job_id"])["tila"] == "jonossa"
    rek.virhe = None
    j.kasittele()
    tyo = j.hae(tyo["job_id"])
    assert tyo["tila"] == "valmis" and tyo["yritykset"] == 2


def test_kaatunut_tyo_palaa_jonoon_kaynnistyksessa(tmp_path):
    rek = ValeRekisteri(_node("pelikone", "poissa", ["llm.chat"]))
    j, _, _ = _jono(tmp_path, rek)
    a = j.lisaa("llm.chat")["job_id"]
    b = j.lisaa("llm.chat")["job_id"]
    j._paivita(a, tila="kaynnissa", yritykset=1)
    j._paivita(b, tila="kaynnissa", yritykset=MAX_YRITYKSET)
    j2, _, _ = _jono(tmp_path, rek)
    assert j2.hae(a)["tila"] == "jonossa"
    assert j2.hae(b)["tila"] == "epaonnistui"
    assert [t["job_id"] for t in j2.listaa(vain_avoimet=True)] == [a]


def test_meta_jaa_aivoille_eika_mene_nodelle(tmp_path):
    rek = ValeRekisteri(_node("pelikone", "elossa", ["comfy.aja"]))
    j, _, _ = _jono(tmp_path, rek)
    tyo = j.lisaa("comfy.aja", {"workflow": {}}, meta={"place": "työpöytä"})
    assert tyo["meta"] == {"place": "työpöytä"}
    assert rek.kutsut == [("comfy.aja", {"workflow": {}}, "pelikone")]
