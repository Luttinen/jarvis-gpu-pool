"""Työjono: GPU-työ odottaa, kun kone on sammuksissa, ja kone herätetään.

Reititin osaa ajaa työn vain elossa olevalla nodella. Jono hoitaa loput:
työ tallennetaan SQLiteen (`jono.db`), kohdekone herätetään ja työ ajetaan,
kun kone tulee verkkoon. Kutsuja saa vastauksen heti ("herätän koneen") ja
tuloksen myöhemmin `ilmoita`-callbackilla.

Tilat: jonossa → (odottaa_heratysta →) kaynnissa → valmis | epaonnistui | peruttu.
Kaatumisen jälkeen kesken jääneet `kaynnissa`-työt palaavat jonoon (max
MAX_YRITYKSET). Vain stdlib.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .reititin import NodeRekisteri, NodeVirhe

TILAT = ("jonossa", "odottaa_heratysta", "kaynnissa", "valmis", "epaonnistui", "peruttu")
AVOIMET = ("jonossa", "odottaa_heratysta", "kaynnissa")
MAX_YRITYKSET = 3
OLETUS_ODOTA_S = 300.0  # herätyksen aikaraja (devices.json: sovelluksen odota_s)

_SKEEMA = """
CREATE TABLE IF NOT EXISTS tyot (
    job_id TEXT PRIMARY KEY,
    kyky TEXT NOT NULL,
    args TEXT NOT NULL,
    pyytaja TEXT NOT NULL DEFAULT '',
    kuvaus TEXT NOT NULL DEFAULT '',
    tila TEXT NOT NULL,
    kohde TEXT,
    node TEXT,
    luotu REAL NOT NULL,
    paivitetty REAL NOT NULL,
    heratetty REAL,
    yritykset INTEGER NOT NULL DEFAULT 0,
    odota_s REAL NOT NULL,
    aikaraja_s REAL NOT NULL,
    tulos TEXT,
    virhe TEXT NOT NULL DEFAULT '',
    meta TEXT
)
"""


class Jono:
    """SQLite-jono reitittimen päällä.

    heraata(node_id) -> bool lähettää herätyksen (WoL); ilmoita(job) kutsutaan,
    kun työ päättyy (valmis/epaonnistui). Molemmat injektoitavia testejä varten,
    samoin kello ja ajo (`aja_saikeessa=False` ajaa työn kutsujan säikeessä).
    """

    def __init__(self, polku: str | Path, rekisteri: NodeRekisteri,
                 heraata: Callable[[str], bool] | None = None,
                 ilmoita: Callable[[dict], Any] | None = None,
                 kello: Callable[[], float] = time.time,
                 aja_saikeessa: bool = True) -> None:
        self.polku = str(polku)
        self.rekisteri = rekisteri
        self.heraata = heraata or (lambda _nid: False)
        self.ilmoita = ilmoita
        self.kello = kello
        self.aja_saikeessa = aja_saikeessa
        self._lukko = threading.RLock()
        self._seis = threading.Event()
        self._saie: threading.Thread | None = None
        self._ajossa: set[str] = set()
        with self._yhteys() as db:
            db.execute(_SKEEMA)
            sarakkeet = {r[1] for r in db.execute("PRAGMA table_info(tyot)")}
            if "meta" not in sarakkeet:
                db.execute("ALTER TABLE tyot ADD COLUMN meta TEXT")
            # Edellinen ajo kaatui kesken työn: takaisin jonoon tai luovutus.
            nyt = self.kello()
            db.execute("UPDATE tyot SET tila='jonossa', paivitetty=? "
                       "WHERE tila='kaynnissa' AND yritykset < ?", (nyt, MAX_YRITYKSET))
            db.execute("UPDATE tyot SET tila='epaonnistui', paivitetty=?, "
                       "virhe='keskeytyi liian monta kertaa' WHERE tila='kaynnissa'", (nyt,))

    # ---------- tallennus ----------

    def _yhteys(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.polku, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _rivi(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["args"] = json.loads(d["args"] or "{}")
        d["tulos"] = json.loads(d["tulos"]) if d.get("tulos") else None
        d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
        return d

    def _paivita(self, job_id: str, **kentat: Any) -> None:
        kentat["paivitetty"] = self.kello()
        if "tulos" in kentat and kentat["tulos"] is not None:
            kentat["tulos"] = json.dumps(kentat["tulos"], ensure_ascii=False, default=str)
        sarakkeet = ", ".join(f"{k}=?" for k in kentat)
        with self._lukko, self._yhteys() as db:
            db.execute(f"UPDATE tyot SET {sarakkeet} WHERE job_id=?",  # noqa: S608 - avaimet koodista
                       (*kentat.values(), job_id))

    def hae(self, job_id: str) -> dict | None:
        with self._yhteys() as db:
            r = db.execute("SELECT * FROM tyot WHERE job_id=?", (job_id,)).fetchone()
        return self._rivi(r) if r else None

    def listaa(self, vain_avoimet: bool = False, raja: int = 50) -> list[dict]:
        ehto = f"WHERE tila IN ({','.join('?' * len(AVOIMET))})" if vain_avoimet else ""
        with self._yhteys() as db:
            rivit = db.execute(f"SELECT * FROM tyot {ehto} ORDER BY luotu DESC LIMIT ?",  # noqa: S608
                               (*(AVOIMET if vain_avoimet else ()), raja)).fetchall()
        return [self._rivi(r) for r in rivit]

    # ---------- julkinen rajapinta ----------

    def lisaa(self, kyky: str, args: dict | None = None, pyytaja: str = "",
              kuvaus: str = "", kohde: str | None = None,
              odota_s: float = OLETUS_ODOTA_S, aikaraja_s: float = 300.0,
              meta: dict | None = None) -> dict:
        """Lisää työ jonoon ja yritä sitä heti. Palauttaa työn tilan lisäyksen jälkeen.

        `meta` jää aivoille (esim. minne valmis kuva tallennetaan); nodelle menee vain args.
        """
        nyt = self.kello()
        job_id = uuid.uuid4().hex[:12]
        with self._lukko, self._yhteys() as db:
            db.execute("INSERT INTO tyot (job_id, kyky, args, pyytaja, kuvaus, tila, kohde, "
                       "luotu, paivitetty, odota_s, aikaraja_s, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, kyky, json.dumps(args or {}, ensure_ascii=False), pyytaja,
                        kuvaus, "jonossa", kohde, nyt, nyt, float(odota_s), float(aikaraja_s),
                        json.dumps(meta or {}, ensure_ascii=False)))
        self.kasittele()
        return self.hae(job_id) or {}

    def peru(self, job_id: str) -> bool:
        """Peru odottava työ. Käynnissä olevaa ei keskeytetä (node ajaa sen loppuun)."""
        with self._lukko:
            tyo = self.hae(job_id)
            if not tyo or tyo["tila"] not in ("jonossa", "odottaa_heratysta"):
                return False
            self._paivita(job_id, tila="peruttu")
        return True

    # ---------- käsittely ----------

    def _herattavat(self, tyo: dict) -> list[str]:
        """Nodet, joiden herääminen auttaisi: kohde, kyvyn osaavat tai tuntemattomat."""
        if tyo.get("kohde"):
            return [tyo["kohde"]]
        nodet = [n for n in self.rekisteri.nodet() if n.tila != "elossa"]
        osaavat = [n.node_id for n in nodet if n.manifest is not None and n.tukee(tyo["kyky"])]
        # Manifest puuttuu, jos kone oli sammuksissa koko koordinaattorin ajon:
        # kyvyt selviävät vasta herättyä.
        tuntemattomat = [n.node_id for n in nodet if n.manifest is None]
        return osaavat or tuntemattomat

    def _valitse(self, tyo: dict) -> str | None:
        if tyo.get("kohde"):
            n = next((n for n in self.rekisteri.nodet() if n.node_id == tyo["kohde"]), None)
            if n is None or n.tila != "elossa":
                return None
            return n.node_id if n.manifest is None or n.tukee(tyo["kyky"]) else None
        n = self.rekisteri.valitse(tyo["kyky"])
        return n.node_id if n else None

    def kasittele(self) -> None:
        """Yksi kierros: aja ajettavat, herätä tarvittavat, pudota vanhentuneet."""
        with self._yhteys() as db:
            rivit = db.execute("SELECT * FROM tyot WHERE tila IN ('jonossa','odottaa_heratysta') "
                               "ORDER BY luotu").fetchall()
        for tyo in (self._rivi(r) for r in rivit):
            with self._lukko:
                tuore = self.hae(tyo["job_id"])
                if not tuore or tuore["tila"] not in ("jonossa", "odottaa_heratysta"):
                    continue
                if tyo["job_id"] in self._ajossa:
                    continue
                nid = self._valitse(tuore)
                if nid is not None:
                    self._ajossa.add(tyo["job_id"])
                    self._paivita(tyo["job_id"], tila="kaynnissa", node=nid,
                                  yritykset=tuore["yritykset"] + 1)
                    self._kaynnista(tuore, nid)
                    continue
                self._odota_tai_heraata(tuore)

    def _odota_tai_heraata(self, tyo: dict) -> None:
        nyt = self.kello()
        if tyo["tila"] == "odottaa_heratysta":
            if nyt - float(tyo["heratetty"] or nyt) > float(tyo["odota_s"]):
                self._paata(tyo["job_id"], "epaonnistui",
                            virhe=f"kone ei herännyt {tyo['odota_s']:.0f} s:ssa")
            return
        kohteet = self._herattavat(tyo)
        if not kohteet:
            self._paata(tyo["job_id"], "epaonnistui",
                        virhe=f"millään koneella ei ole kykyä {tyo['kyky']}")
            return
        herätetyt = []
        for nid in kohteet:
            try:
                if self.heraata(nid):
                    herätetyt.append(nid)
            except Exception:  # noqa: BLE001 - herätys ei kaada jonoa
                pass
        if not herätetyt:
            self._paata(tyo["job_id"], "epaonnistui",
                        virhe=f"herätys ei onnistunut ({', '.join(kohteet)})")
            return
        self._paivita(tyo["job_id"], tila="odottaa_heratysta", heratetty=nyt,
                      kohde=tyo.get("kohde") or (herätetyt[0] if len(herätetyt) == 1 else None))

    def _kaynnista(self, tyo: dict, nid: str) -> None:
        if self.aja_saikeessa:
            threading.Thread(target=self._aja, args=(tyo, nid), daemon=True,
                             name=f"jono-{tyo['job_id']}").start()
        else:
            self._aja(tyo, nid)

    def _aja(self, tyo: dict, nid: str) -> None:
        try:
            tulos = self.rekisteri.kutsu(tyo["kyky"], tyo["args"], aikaraja_s=tyo["aikaraja_s"],
                                         node=nid)
        except NodeVirhe as e:
            if e.koodi in ("YHTEYS",) and tyo["yritykset"] + 1 < MAX_YRITYKSET:
                # Kone putosi kesken: uusi yritys seuraavalla kierroksella.
                self._paivita(tyo["job_id"], tila="jonossa", virhe=str(e))
            else:
                self._paata(tyo["job_id"], "epaonnistui", virhe=str(e))
        except Exception as e:  # noqa: BLE001
            self._paata(tyo["job_id"], "epaonnistui", virhe=f"{type(e).__name__}: {e}")
        else:
            self._paata(tyo["job_id"], "valmis", tulos=tulos)
        finally:
            with self._lukko:
                self._ajossa.discard(tyo["job_id"])

    def _paata(self, job_id: str, tila: str, **kentat: Any) -> None:
        self._paivita(job_id, tila=tila, **kentat)
        if self.ilmoita is not None:
            tyo = self.hae(job_id)
            if tyo is not None:
                try:
                    self.ilmoita(tyo)
                except Exception:  # noqa: BLE001 - kutsujan callback ei kaada jonoa
                    pass

    # ---------- taustasilmukka ----------

    def kaynnista(self, vali_s: float = 5.0) -> None:
        if self._saie and self._saie.is_alive():
            return
        self._seis.clear()

        def silmukka() -> None:
            while not self._seis.is_set():
                try:
                    self.kasittele()
                except Exception:  # noqa: BLE001 - taustasäie ei saa kuolla
                    pass
                self._seis.wait(vali_s)

        self._saie = threading.Thread(target=silmukka, name="jarvis-jono", daemon=True)
        self._saie.start()

    def pysayta(self) -> None:
        self._seis.set()
        if self._saie:
            self._saie.join(timeout=5)
        self._saie = None
