"""Testit: allekirjoitus, tarkistus, osoitesuodatus ja tokenin luku."""

from __future__ import annotations

import pytest

from jarvis_core import protokolla as p

TOKEN = "a" * 64


class Kello:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _otsakkeet(**kw):
    kello = kw.pop("kello", None)
    ts = int(kello()) if kello else None
    return p.allekirjoita(kw.pop("token", TOKEN), kw.pop("method", "POST"),
                          kw.pop("path", "/call?x=1"), kw.pop("body", b'{"a":1}'),
                          "paakone", ts=kw.pop("ts", ts), nonce=kw.pop("nonce", None))


def test_luo_token_pituus_ja_satunnaisuus():
    a, b = p.luo_token(), p.luo_token()
    assert len(a) == 64 and a != b
    int(a, 16)


def test_allekirjoitus_palauttaa_neljä_otsaketta():
    h = _otsakkeet()
    assert set(h) == {p.OTSAKE_LAHETTAJA, p.OTSAKE_TS, p.OTSAKE_NONCE, p.OTSAKE_ALLEKIRJOITUS}
    assert h[p.OTSAKE_LAHETTAJA] == "paakone"


def test_round_trip():
    kello = Kello()
    t = p.Tarkistaja(TOKEN, kello=kello)
    h = _otsakkeet(kello=kello)
    assert t.tarkista("POST", "/call?x=1", h, b'{"a":1}') == (True, "")


def test_round_trip_method_pienellä():
    kello = Kello()
    t = p.Tarkistaja(TOKEN, kello=kello)
    h = _otsakkeet(kello=kello, method="get", body=b"")
    assert t.tarkista("GET", "/call?x=1", h, b"")[0]


@pytest.mark.parametrize("muutos", [
    {"token": "b" * 64},
    {"path": "/call?x=2"},
    {"body": b'{"a":2}'},
    {"method": "GET"},
])
def test_väärä_allekirjoitus(muutos):
    kello = Kello()
    t = p.Tarkistaja(TOKEN, kello=kello)
    h = _otsakkeet(kello=kello, **muutos)
    ok, syy = t.tarkista("POST", "/call?x=1", h, b'{"a":1}')
    assert not ok and "allekirjoitus" in syy


def test_peukaloitu_allekirjoitus():
    kello = Kello()
    t = p.Tarkistaja(TOKEN, kello=kello)
    h = _otsakkeet(kello=kello)
    h[p.OTSAKE_ALLEKIRJOITUS] = "0" * 64
    assert t.tarkista("POST", "/call?x=1", h, b'{"a":1}') == (False, "väärä allekirjoitus")


@pytest.mark.parametrize("siirto", [-61, 61, -3600])
def test_vanha_tai_tulevaisuuden_ts(siirto):
    kello = Kello()
    t = p.Tarkistaja(TOKEN, ikkuna_s=60, kello=kello)
    h = _otsakkeet(ts=int(kello()) + siirto)
    ok, syy = t.tarkista("POST", "/call?x=1", h, b'{"a":1}')
    assert not ok and "ikkunan" in syy


def test_ts_ikkunan_reunalla_kelpaa():
    kello = Kello()
    t = p.Tarkistaja(TOKEN, ikkuna_s=60, kello=kello)
    h = _otsakkeet(ts=int(kello()) - 60)
    assert t.tarkista("POST", "/call?x=1", h, b'{"a":1}')[0]


def test_virheellinen_ts():
    t = p.Tarkistaja(TOKEN, kello=Kello())
    h = _otsakkeet(kello=Kello())
    h[p.OTSAKE_TS] = "eiluku"
    assert t.tarkista("POST", "/call?x=1", h, b'{"a":1}') == (False, "virheellinen aikaleima")


def test_uusittu_nonce_hylätään():
    kello = Kello()
    t = p.Tarkistaja(TOKEN, kello=kello)
    h = _otsakkeet(kello=kello, nonce="n1")
    assert t.tarkista("POST", "/call?x=1", h, b'{"a":1}')[0]
    ok, syy = t.tarkista("POST", "/call?x=1", h, b'{"a":1}')
    assert not ok and "nonce" in syy
    # eri nonce samalla hetkellä kelpaa
    assert t.tarkista("POST", "/call?x=1", _otsakkeet(kello=kello, nonce="n2"), b'{"a":1}')[0]


def test_nonce_välimuisti_siivotaan():
    kello = Kello()
    t = p.Tarkistaja(TOKEN, ikkuna_s=60, kello=kello)
    assert t.tarkista("POST", "/call?x=1", _otsakkeet(kello=kello, nonce="vanha"), b'{"a":1}')[0]
    kello.t += 500
    assert t.tarkista("POST", "/call?x=1", _otsakkeet(kello=kello, nonce="uusi"), b'{"a":1}')[0]
    assert "vanha" not in t._nonces and "uusi" in t._nonces


@pytest.mark.parametrize("puuttuu", [p.OTSAKE_TS, p.OTSAKE_NONCE, p.OTSAKE_ALLEKIRJOITUS])
def test_puuttuva_otsake(puuttuu):
    kello = Kello()
    t = p.Tarkistaja(TOKEN, kello=kello)
    h = _otsakkeet(kello=kello)
    del h[puuttuu]
    assert t.tarkista("POST", "/call?x=1", h, b'{"a":1}') == (False, "allekirjoitus puuttuu")


def test_otsakkeiden_kirjainkoko():
    kello = Kello()
    t = p.Tarkistaja(TOKEN, kello=kello)
    h = {k.lower(): v for k, v in _otsakkeet(kello=kello).items()}
    assert t.tarkista("POST", "/call?x=1", h, b'{"a":1}')[0]
    h2 = {k.upper(): v for k, v in _otsakkeet(kello=kello).items()}
    assert t.tarkista("POST", "/call?x=1", h2, b'{"a":1}')[0]


def test_otsakkeet_email_message_oliona():
    from email.message import Message
    kello = Kello()
    t = p.Tarkistaja(TOKEN, kello=kello)
    m = Message()
    for k, v in _otsakkeet(kello=kello).items():
        m[k.lower()] = v
    assert t.tarkista("POST", "/call?x=1", m, b'{"a":1}')[0]


def test_tyhjä_token_tarkistajalla():
    kello = Kello()
    t = p.Tarkistaja("", kello=kello)
    h = _otsakkeet(kello=kello, token="")
    assert t.tarkista("POST", "/call?x=1", h, b'{"a":1}') == (False, "nodella ei ole tokenia")
    assert p.Tarkistaja(None, kello=kello).tarkista("GET", "/", h, b"")[0] is False  # type: ignore[arg-type]


@pytest.mark.parametrize("ip,odotus", [
    ("127.0.0.1", True),
    ("127.5.5.5", True),
    ("::1", True),
    ("100.64.0.20", True),
    ("100.64.0.1", True),
    ("100.127.255.255", True),
    ("100.128.0.1", False),
    ("100.63.255.255", False),
    ("fd7a:115c:a1e0::1234", True),
    ("fd7a:115c:a1e1::1", False),
    ("192.168.1.20", True),
    ("10.1.2.3", True),
    ("172.16.0.1", False),
    ("8.8.8.8", False),
    ("::ffff:192.168.1.5", True),
    ("::ffff:8.8.8.8", False),
    ("::ffff:127.0.0.1", True),
    ("fe80::1%eth0", False),
    ("2001:4860:4860::8888", False),
    ("", False),
    ("ei-osoite", False),
])
def test_sallittu_osoite(ip, odotus):
    assert p.sallittu_osoite(ip) is odotus


def test_lue_token_env_voittaa(tmp_path, monkeypatch):
    f = tmp_path / "token.txt"
    f.write_text("tiedostosta\n", encoding="utf-8")
    monkeypatch.setenv("JARVIS_NODE_TOKEN", "  ymparistosta ")
    assert p.lue_token(f) == "ymparistosta"


def test_lue_token_tiedostosta(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_NODE_TOKEN", raising=False)
    f = tmp_path / "token.txt"
    f.write_text("  tiedostosta\r\n", encoding="utf-8")
    assert p.lue_token(f) == "tiedostosta"
    assert p.lue_token(str(f)) == "tiedostosta"


def test_lue_token_puuttuu(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_NODE_TOKEN", "   ")
    assert p.lue_token(tmp_path / "eiole.txt") == ""
    assert p.lue_token() == ""
    assert p.lue_token(None) == ""
