"""Koneiden välinen protokolla: allekirjoitetut HTTP-pyynnöt.

Aivot (koordinaattori) pyytävät töitä nodeilta (muut koneet ja koordinaattorin
oma node) HTTP:llä. Jokainen pyyntö allekirjoitetaan jaetulla tokenilla
(HMAC-SHA256) — token ei kulje verkossa, ja aikaleima + kertakäyttöinen nonce
estävät pyynnön toiston. Lisäksi node hyväksyy vain loopbackin, Tailscalen ja
yksityisten verkkojen osoitteet.

Vain stdlib: sama tiedosto ajetaan myös esim. ComfyUI:n upotetulla Pythonilla.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

VERSIO = "0.3.0"
NODE_PORTTI = 8791

OTSAKE_LAHETTAJA = "X-Jarvis-Lahettaja"
OTSAKE_TS = "X-Jarvis-Ts"
OTSAKE_NONCE = "X-Jarvis-Nonce"
OTSAKE_ALLEKIRJOITUS = "X-Jarvis-Allekirjoitus"

SALLITUT_VERKOT = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48",
    "192.168.0.0/16", "10.0.0.0/8",
))


def luo_token() -> str:
    return secrets.token_hex(32)


def lue_token(polku: str | Path | None = None) -> str:
    """Token ympäristömuuttujasta JARVIS_NODE_TOKEN, muuten tiedostosta."""
    env = os.environ.get("JARVIS_NODE_TOKEN", "").strip()
    if env:
        return env
    if polku:
        try:
            return Path(polku).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def _viesti(method: str, path: str, ts: int, nonce: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body or b"").hexdigest()
    return f"{method.upper()}\n{path}\n{ts}\n{nonce}\n{digest}".encode()


def _hmac(token: str, viesti: bytes) -> str:
    return hmac.new(token.encode("utf-8"), viesti, hashlib.sha256).hexdigest()


def allekirjoita(token: str, method: str, path: str, body: bytes, lahettaja: str,
                 ts: int | None = None, nonce: str | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else int(ts)
    nonce = nonce or secrets.token_hex(16)
    return {
        OTSAKE_LAHETTAJA: lahettaja,
        OTSAKE_TS: str(ts),
        OTSAKE_NONCE: nonce,
        OTSAKE_ALLEKIRJOITUS: _hmac(token, _viesti(method, path, ts, nonce, body)),
    }


def _hae(headers: Mapping[str, str], nimi: str) -> str:
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = getter(nimi)
        if value is not None:
            return str(value)
    lower = nimi.lower()
    for key, value in headers.items():
        if str(key).lower() == lower:
            return str(value)
    return ""


class Tarkistaja:
    """Tarkistaa allekirjoituksen, aikaikkunan ja noncen kertakäyttöisyyden."""

    def __init__(self, token: str, ikkuna_s: int = 60,
                 kello: Callable[[], float] = time.time) -> None:
        self.token = token or ""
        self.ikkuna_s = ikkuna_s
        self.kello = kello
        self._nonces: dict[str, float] = {}
        self._lukko = threading.Lock()

    def tarkista(self, method: str, path: str, headers: Mapping[str, str],
                 body: bytes) -> tuple[bool, str]:
        if not self.token:
            return False, "nodella ei ole tokenia"
        ts_text = _hae(headers, OTSAKE_TS)
        nonce = _hae(headers, OTSAKE_NONCE)
        sig = _hae(headers, OTSAKE_ALLEKIRJOITUS)
        if not ts_text or not nonce or not sig:
            return False, "allekirjoitus puuttuu"
        try:
            ts = int(ts_text)
        except ValueError:
            return False, "virheellinen aikaleima"
        now = self.kello()
        if abs(now - ts) > self.ikkuna_s:
            return False, "aikaleima ikkunan ulkopuolella"
        odotettu = _hmac(self.token, _viesti(method, path, ts, nonce, body))
        if not hmac.compare_digest(odotettu, sig):
            return False, "väärä allekirjoitus"
        with self._lukko:
            raja = now - 2 * self.ikkuna_s
            for vanha in [n for n, t in self._nonces.items() if t < raja]:
                del self._nonces[vanha]
            if nonce in self._nonces:
                return False, "nonce on jo käytetty"
            self._nonces[nonce] = now
        return True, ""


def sallittu_osoite(ip: str) -> bool:
    try:
        osoite = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    mapped = getattr(osoite, "ipv4_mapped", None)
    if mapped is not None:
        osoite = mapped
    return any(osoite in verkko for verkko in SALLITUT_VERKOT if verkko.version == osoite.version)
