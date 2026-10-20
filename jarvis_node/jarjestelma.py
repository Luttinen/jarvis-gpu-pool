"""Koneen mittarit noden sykkeeseen: CPU, RAM, GPU (nvidia-smi) ja aktiivinen peli.

Vain stdlib. Windowsissa ctypes (GetSystemTimes, GlobalMemoryStatusEx) ja
tasklist, Linuxissa /proc. Hitaat kyselyt (nvidia-smi, tasklist) välimuistissa.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import subprocess
import threading
import time
from typing import Any

log = logging.getLogger("jarvis_node.jarjestelma")

OLETUS_PELIT = ["GTA5.exe", "FiveM.exe", "cs2.exe", "PathOfExile.exe",
                "PathOfExileSteam.exe", "TslGame.exe"]
CREATE_NO_WINDOW = 0x08000000


def _aja(argv: list[str], timeout: float) -> str:
    """Aja komento ilman konsoli-ikkunaa, palauta stdout ('' virheellä)."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=timeout,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("komento %s epäonnistui: %s", argv[0], exc)
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout or ""


# --------------------------------------------------------------------------
# CPU


def _cpu_ajat() -> tuple[float, float] | None:
    """(idle, total) kumulatiivisina; None jos ei saatavilla."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None

        def arvo(ft: Any) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        # kernel-aika sisältää idlen
        return float(arvo(idle)), float(arvo(kernel) + arvo(user))
    try:
        with open("/proc/stat", encoding="ascii") as f:
            osat = f.readline().split()[1:]
    except OSError:
        return None
    luvut = [float(x) for x in osat]
    idle = luvut[3] + (luvut[4] if len(luvut) > 4 else 0.0)
    return idle, sum(luvut[:8])


def _muisti_mb() -> tuple[int, int]:
    """(yhteensä, vapaa) megatavuina."""
    if os.name == "nt":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        tila = MEMORYSTATUSEX()
        tila.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(tila)):  # type: ignore[attr-defined]
            return int(tila.ullTotalPhys // 1048576), int(tila.ullAvailPhys // 1048576)
        return 0, 0
    arvot: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for rivi in f:
                nimi, _, loput = rivi.partition(":")
                osat = loput.split()
                if osat:
                    arvot[nimi] = int(osat[0])
    except OSError:
        return 0, 0
    total = arvot.get("MemTotal", 0)
    vapaa = arvot.get("MemAvailable", arvot.get("MemFree", 0))
    return total // 1024, vapaa // 1024


def _parse_nvidia(teksti: str) -> dict[str, Any] | None:
    for rivi in teksti.splitlines():
        osat = [o.strip() for o in rivi.split(",")]
        if len(osat) < 4:
            continue

        def luku(x: str) -> float | None:
            try:
                return float(x)
            except ValueError:
                return None

        pct, total, vapaa = luku(osat[1]), luku(osat[2]), luku(osat[3])
        return {"nimi": osat[0], "pct": pct,
                "vram_mb": int(total or 0), "vram_vapaa_mb": int(vapaa or 0)}
    return None


def _parse_tasklist(teksti: str) -> set[str]:
    nimet: set[str] = set()
    for rivi in csv.reader(io.StringIO(teksti)):
        if rivi:
            nimet.add(rivi[0].strip().lower())
    return nimet


class Jarjestelma:
    """Mittarit. Testit korvaavat tämän valeoliolla, jolla samat metodit."""

    def __init__(self) -> None:
        self._lukko = threading.Lock()
        self._cpu_edellinen: tuple[float, float] | None = None
        self._gpu_valimuisti: tuple[float, dict[str, Any] | None] | None = None
        self._peli_valimuisti: tuple[float, str | None] | None = None

    def cpu_ytimet(self) -> int:
        return os.cpu_count() or 1

    def cpu_pct(self) -> float:
        with self._lukko:
            nyt = _cpu_ajat()
            if nyt is None:
                return 0.0
            edellinen = self._cpu_edellinen
            if edellinen is None:
                time.sleep(0.2)
                edellinen, nyt = nyt, _cpu_ajat() or nyt
            self._cpu_edellinen = nyt
        d_idle = nyt[0] - edellinen[0]
        d_total = nyt[1] - edellinen[1]
        if d_total <= 0:
            return 0.0
        return round(max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total))), 1)

    def muisti(self) -> tuple[int, int]:
        return _muisti_mb()

    def gpu(self) -> dict[str, Any] | None:
        """{"nimi","pct","vram_mb","vram_vapaa_mb"} tai None. Välimuisti 3 s."""
        nyt = time.monotonic()
        with self._lukko:
            if self._gpu_valimuisti and nyt - self._gpu_valimuisti[0] < 3.0:
                return self._gpu_valimuisti[1]
        teksti = _aja(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.total,memory.free",
                       "--format=csv,noheader,nounits"], timeout=5.0)
        tulos = _parse_nvidia(teksti) if teksti else None
        with self._lukko:
            self._gpu_valimuisti = (nyt, tulos)
        return tulos

    def kayttaja_joutilas_s(self) -> float | None:
        """Sekunteja viimeisestä hiiren/näppäimistön käytöstä (Windows), muuten None.

        Toimii, kun node ajetaan käyttäjän istunnossa (ajastettu tehtävä kirjautuessa).
        """
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        nyt = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return round(((nyt - info.dwTime) & 0xFFFFFFFF) / 1000.0, 1)

    def peli_aktiivinen(self, pelit: list[str] | None = None) -> str | None:
        """Käynnissä olevan tunnetun pelin prosessinimi (Windows). Välimuisti 15 s."""
        if os.name != "nt":
            return None
        nyt = time.monotonic()
        with self._lukko:
            if self._peli_valimuisti and nyt - self._peli_valimuisti[0] < 15.0:
                return self._peli_valimuisti[1]
        prosessit = _parse_tasklist(_aja(["tasklist", "/FO", "CSV", "/NH"], timeout=10.0))
        loydetty = None
        for peli in pelit if pelit is not None else OLETUS_PELIT:
            if peli.lower() in prosessit:
                loydetty = peli
                break
        with self._lukko:
            self._peli_valimuisti = (nyt, loydetty)
        return loydetty
