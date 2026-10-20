"""devices.py: registry, Wake-on-LAN, power-off and wake-and-wait (no network, no SSH)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis_pool import devices

REPO = Path(__file__).resolve().parent.parent

REG = {
    "desktop": {"nimi": "Desktop", "paikallinen": True, "os": "windows"},
    "gaming-pc": {"nimi": "Gaming PC", "os": "windows", "lan": "192.168.1.20",
                  "mac": "00-11-22-33-44-55", "aliakset": ["second pc"]},
    "server": {"nimi": "Server", "os": "linux"},
}


def test_load_registry(tmp_path):
    reg = devices.load_registry(REPO / "examples" / "devices.example.json")
    assert set(reg) == {"desktop", "gaming-pc"}
    assert reg["gaming-pc"]["node_portti"] == 8791
    (tmp_path / "d.json").write_text(json.dumps({"PC": {"os": "linux"}}), encoding="utf-8")
    assert devices.load_registry(tmp_path / "d.json") == {"pc": {"os": "linux"}}
    assert devices.load_registry(tmp_path / "missing.json") == {}


def test_resolve():
    assert devices.resolve("gaming-pc", REG) == "gaming-pc"
    assert devices.resolve("Gaming  PC", REG) == "gaming-pc"
    assert devices.resolve("second pc", REG) == "gaming-pc"
    assert devices.resolve("nope", REG) is None
    assert devices.resolve("", REG) is None


def test_addresses_order():
    d = {"lan": "192.168.1.20", "tailscale": "100.64.0.20", "julkinen": "192.168.1.20"}
    assert devices.addresses(d) == ["100.64.0.20", "192.168.1.20"]


def test_wake_sends_magic_packet(monkeypatch):
    sent = []

    class FakeSocket:
        def __init__(self, *_a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def setsockopt(self, *_a):
            pass

        def sendto(self, data, addr):
            sent.append((data, addr))

    monkeypatch.setattr(devices.socket, "socket", FakeSocket)
    assert devices.wake(REG["gaming-pc"]) is True
    packet = sent[0][0]
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6
    assert packet[6:12] == bytes.fromhex("001122334455")
    assert {a for _, a in sent} == {("255.255.255.255", 9), ("255.255.255.255", 7),
                                    ("192.168.1.255", 9), ("192.168.1.255", 7)}
    assert devices.wake({"mac": "bad"}) is False
    with pytest.raises(ValueError):
        devices.magic_packet("12:34")


def test_ps_encoded_roundtrip():
    import base64

    assert base64.b64decode(devices.ps_encoded("Write-Output 'ä'")).decode("utf-16-le") == \
        "Write-Output 'ä'"


def test_power_off():
    ran: list[str] = []

    def runner(d, s, timeout=60.0):
        ran.append(s)
        return 0, "SHUTDOWN_SCHEDULED"

    up = lambda d: "192.168.1.20"  # noqa: E731
    assert "local machine" in devices.power_off("desktop", "shutdown", REG, runner=runner,
                                                is_online=up)
    assert "not a Windows" in devices.power_off("server", "shutdown", REG, runner=runner,
                                                is_online=up)
    assert "gaming" in devices.power_off("gaming-pc", "shutdown", REG, runner=runner,
                                         is_online=up, busy=lambda k: "gaming")
    assert ran == []
    answer = devices.power_off("second pc", "sammuta", REG, runner=runner, is_online=up,
                               busy=lambda k: None, delay=60)
    assert answer.startswith("gaming-pc shuts down in 60 s") and "shutdown /s /t 60" in ran[-1]

    answer = devices.power_off("gaming-pc", "sleep", REG, is_online=up,
                               runner=lambda d, s, timeout=60.0: ran.append(s) or (0, "SLEEP_SCHEDULED"))
    assert "goes to sleep" in answer
    assert "Win32_Process" in ran[-1] and "SetSuspendState" not in ran[-1]  # inside EncodedCommand

    assert "cancelled" in devices.power_off("gaming-pc", "cancel", REG, is_online=up,
                                            runner=lambda d, s, timeout=60.0: (0, "CANCELLED"))
    assert "already off" in devices.power_off("gaming-pc", "sleep", REG, runner=runner,
                                              is_online=lambda d: None)


def test_sleep_timer_can_be_found_by_cancel():
    # The cancel script searches for the EncodedCommand prefix of the marker line.
    sleep_cmd = devices._sleep_command(60)
    assert devices._SLEEP_ID in sleep_cmd
    assert devices._SLEEP_ID in devices.power_script("cancel", 60)


def test_wake_and_wait():
    state = {"on": False, "woken": [], "kept": []}

    def is_online(d):
        return "192.168.1.20" if state["on"] or d.get("paikallinen") else None

    def send(key):
        state["woken"].append(key)
        return True

    def sleep(_s):
        state["on"] = True  # the machine wakes during the first wait

    def keep(key):
        state["kept"].append(key)
        return True

    assert devices.wake_and_wait("desktop", REG, is_online=is_online) == \
        "This machine is already awake."
    answer = devices.wake_and_wait("second pc", REG, send=send, keep_awake=keep, sleep=sleep,
                                   is_online=is_online)
    assert answer.startswith("gaming-pc woke up") and "stays awake for 15 min" in answer
    assert state["woken"] == ["gaming-pc"] and state["kept"] == ["gaming-pc"]
    assert devices.wake_and_wait("gaming-pc", REG, send=send, is_online=is_online) == \
        "gaming-pc is already on."
    state["on"] = False
    assert "Could not send" in devices.wake_and_wait("gaming-pc", REG, send=lambda k: False,
                                                     is_online=is_online)


def test_wake_and_wait_gives_up():
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s

    answer = devices.wake_and_wait("gaming-pc", REG, send=lambda k: True, sleep=sleep,
                                   is_online=lambda d: None, wait_sec=20,
                                   clock=lambda: t["now"])
    assert "did not answer in 20 s" in answer
