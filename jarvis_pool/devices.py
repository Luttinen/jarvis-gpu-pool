"""Device registry and power control: Wake-on-LAN, SSH commands, sleep/shutdown.

The registry is a JSON file (see examples/devices.example.json) with one entry
per machine: addresses (Tailscale first, LAN as fallback), OS, MAC address for
Wake-on-LAN, SSH user and the node port.

Windows remote-control pitfall: a process started from an OpenSSH session dies
when the connection closes (the session's job object). The sleep timer is
therefore created with WMI (Win32_Process.Create), whose parent is WmiPrvSE,
so it survives the SSH session. All remote PowerShell runs through
-EncodedCommand, so there are no quoting problems.

Only stdlib. Network, SSH and sleeping are injectable for tests.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SSH_CONNECT_SEC = 8
WAKE_WAIT_SEC = 150.0
POWER_OFF_DELAY_SEC = 60

Registry = dict[str, dict[str, Any]]

# Remembered working SSH target per device (first address that answered).
_ssh_memory: dict[str, str] = {}


# --------------------------------------------------------------------------
# Registry


def load_registry(path: str | Path) -> Registry:
    """Read devices.json; accepts {"laitteet": {...}} or a bare {key: device} dict."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Device registry %s is missing or invalid", path)
        return {}
    devices = data.get("laitteet", data.get("devices", data))
    return {str(k).lower(): v for k, v in devices.items() if isinstance(v, dict)}


def resolve(name: str, registry: Registry) -> str | None:
    """Device key from its key, display name ("nimi") or one of its aliases ("aliakset")."""
    wanted = " ".join(str(name or "").lower().split())
    if not wanted:
        return None
    if wanted in registry:
        return wanted
    for key, device in registry.items():
        names = [str(device.get("nimi", "")).lower(),
                 *[str(a).lower() for a in device.get("aliakset", [])]]
        if wanted in names:
            return key
    return None


def is_windows(device: dict[str, Any]) -> bool:
    return str(device.get("os") or "windows").lower().startswith("win")


# --------------------------------------------------------------------------
# Network


def addresses(device: dict[str, Any]) -> list[str]:
    """Addresses in order of preference: Tailscale works from anywhere."""
    out = []
    for key in ("tailscale", "lan", "julkinen"):
        value = str(device.get(key) or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ping(host: str) -> bool:
    flag = "-n" if os.name == "nt" else "-c"
    try:
        done = subprocess.run(["ping", flag, "1", "-w", "800" if os.name == "nt" else "1", host],
                              capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and "TTL=" in done.stdout.upper()


def online(device: dict[str, Any]) -> str | None:
    """First address that answers (SSH port, then ping), or None."""
    if device.get("paikallinen"):
        return "127.0.0.1"
    port = int(device.get("ssh_portti") or 22)
    for host in addresses(device):
        if port_open(host, port):
            return host
    for host in addresses(device):
        if _ping(host):
            return host
    return None


def magic_packet(mac: str) -> bytes:
    clean = str(mac or "").replace(":", "").replace("-", "").strip()
    if len(clean) != 12:
        raise ValueError(f"invalid MAC address '{mac}'")
    return bytes.fromhex("FF" * 6 + clean * 16)


def wake(device: dict[str, Any]) -> bool:
    """Wake-on-LAN magic packet to the LAN broadcast. Works when this machine is on the same LAN."""
    try:
        packet = magic_packet(str(device.get("mac") or ""))
    except ValueError:
        return False
    lan = str(device.get("lan") or "")
    targets = ["255.255.255.255"]
    if lan.count(".") == 3:
        targets.append(lan.rsplit(".", 1)[0] + ".255")
    sent = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            for port in (9, 7):
                try:
                    sock.sendto(packet, (target, port))
                    sent = True
                except OSError:
                    pass
    return sent


# --------------------------------------------------------------------------
# SSH


def ssh_targets(device: dict[str, Any]) -> list[str]:
    user = str(device.get("ssh_kayttaja") or "").strip()
    out = []
    alias = str(device.get("ssh_alias") or "").strip()
    if alias:
        out.append(alias)
    for host in addresses(device):
        out.append(f"{user}@{host}" if user else host)
    return out


def ps_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _ssh_run(target: str, remote_command: str, timeout: float, stdin: bytes | None = None,
             port: int = 22) -> tuple[int, str]:
    if shutil.which("ssh") is None:
        return 127, "ssh is not installed on this machine"
    args = ["ssh", "-p", str(port), "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_SEC}",
            "-o", "StrictHostKeyChecking=accept-new", target, remote_command]
    try:
        done = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return 124, f"timed out ({timeout:.0f} s)"
    except OSError as exc:
        return 127, f"ssh did not start: {exc}"
    out = (done.stdout or b"").decode("utf-8", errors="replace")
    err = (done.stderr or b"").decode("utf-8", errors="replace")
    # PowerShell's CLIXML progress noise on stderr is not an error
    if "#< CLIXML" in err:
        err = ""
    return done.returncode, (out + ("\n" + err if err.strip() else "")).strip()


def _local(script: str, timeout: float) -> tuple[int, str]:
    cmd = (["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", ps_encoded(script)]
           if os.name == "nt" else ["bash", "-c", script])
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return 124, f"timed out ({timeout:.0f} s)"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()[:4000]


def run_remote(device: dict[str, Any], script: str, timeout: float = 60.0) -> tuple[int, str]:
    """Run a script on the device: PowerShell on Windows, bash elsewhere.

    Tries the addresses in order and remembers the one that worked. Exit code
    255 with a connection error means "try the next address".
    """
    if device.get("paikallinen"):
        return _local(script, timeout)
    port = int(device.get("ssh_portti") or 22)
    key = str(device.get("_key") or "")
    targets = ssh_targets(device)
    remembered = _ssh_memory.get(key)
    if remembered in targets:
        targets.remove(remembered)
        targets.insert(0, remembered)
    last = (255, "no SSH address")
    for target in targets:
        if is_windows(device):
            code, out = _ssh_run(target, "powershell -NoProfile -NonInteractive -EncodedCommand "
                                         f"{ps_encoded(script)}", timeout, port=port)
        else:
            code, out = _ssh_run(target, "bash -s", timeout,
                                 stdin=script.replace("\r\n", "\n").encode("utf-8"), port=port)
        if code == 255 and ("Connection" in out or "timed out" in out or "resolve" in out
                            or "Permission denied" in out or "No route" in out or not out):
            last = (code, f"{target}: {out[:200]}")
            continue
        if key:
            _ssh_memory[key] = target
        return code, out[:4000]
    return last


def copy_file(device: dict[str, Any], local: str, remote: str, upload: bool,
              timeout: float = 600.0) -> tuple[int, str]:
    """scp in either direction. Remote Windows paths look like C:/path/file.zip."""
    if shutil.which("scp") is None:
        return 127, "scp is not installed"
    port = int(device.get("ssh_portti") or 22)
    remote = remote.replace("\\", "/")
    last = (255, "no address")
    for target in ssh_targets(device):
        pair = [local, f"{target}:{remote}"] if upload else [f"{target}:{remote}", local]
        try:
            done = subprocess.run(["scp", "-P", str(port), "-o", "BatchMode=yes",
                                   "-o", f"ConnectTimeout={SSH_CONNECT_SEC}", *pair],
                                  capture_output=True, text=True, timeout=timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        except OSError as exc:
            return 127, str(exc)
        if done.returncode == 0:
            return 0, "ok"
        last = (done.returncode, (done.stderr or "").strip()[:300])
    return last


# --------------------------------------------------------------------------
# Sleep / shutdown
#
# Windows: shutdown shows a warning on screen and can be cancelled (shutdown /a).
# Sleep uses SetSuspendState (not hibernate), so Wake-on-LAN wakes it reliably.
#
# The sleep timer must not be a child of the SSH session: Windows OpenSSH kills
# the session's processes when the connection closes, and a Start-Job timer
# died immediately. Win32_Process.Create starts it outside the session. The
# timer's command line starts with a marker, so "cancel" can find and stop it.

_SLEEP_MARKER = "# JarvisPoolSleep"
# Same EncodedCommand prefix for every delay (the marker line comes first).
_SLEEP_ID = ps_encoded(_SLEEP_MARKER + "\n")[:24]

MODES = {"shutdown": "shutdown", "sammuta": "shutdown",
         "sleep": "sleep", "nukku": "sleep",
         "cancel": "cancel", "peru": "cancel"}


def _sleep_command(delay: int) -> str:
    inner = (f"{_SLEEP_MARKER}\nStart-Sleep -Seconds {int(delay)}\n"
             "Add-Type -AssemblyName System.Windows.Forms\n"
             "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)\n")
    return f"powershell -NoProfile -WindowStyle Hidden -EncodedCommand {ps_encoded(inner)}"


def power_script(mode: str, delay: int) -> str:
    if mode == "shutdown":
        return (f'shutdown /s /t {delay} /c "Shutting down in {delay} s. Cancel: shutdown /a"\n'
                'if ($LASTEXITCODE -eq 0) { "SHUTDOWN_SCHEDULED" }')
    if mode == "sleep":
        return ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                f"-Arguments @{{CommandLine = '{_sleep_command(delay)}'}}\n"
                f"msg * /TIME:{delay} \"This PC will go to sleep in {delay} s.\" 2>$null\n"
                "if ($r.ReturnValue -eq 0) { 'SLEEP_SCHEDULED' } else { \"ERROR $($r.ReturnValue)\" }")
    return ("shutdown /a 2>$null\n"
            "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{_SLEEP_ID}*' }} | "
            "Invoke-CimMethod -MethodName Terminate | Out-Null\n\"CANCELLED\"")


def power_off(device_key: str, mode: str, registry: Registry,
              busy: Callable[[str], str | None] | None = None,
              delay: int = POWER_OFF_DELAY_SEC,
              runner: Callable[..., tuple[int, str]] | None = None,
              is_online: Callable[[dict[str, Any]], str | None] | None = None) -> str:
    """Put another Windows machine to sleep, shut it down, or cancel. Never the local machine.

    `busy(key)` returns a reason why the machine is in use (game, jobs, user),
    or None; when it returns a reason nothing is done.
    """
    runner = runner or run_remote
    is_online = is_online or online
    key = resolve(device_key, registry)
    if not key:
        return f"Unknown device '{device_key}'."
    device = dict(registry[key], _key=key)
    if device.get("paikallinen"):
        return "Refusing to power off the local machine."
    if not is_windows(device):
        return f"{key} is not a Windows machine."
    mode = MODES.get(mode, "shutdown")
    if is_online(device) is None:
        return f"{key} is already off." if mode != "cancel" else f"{key} does not answer."
    if mode != "cancel" and busy is not None:
        reason = busy(key)
        if reason:
            return f"Not powering off {key}: {reason}."
    code, out = runner(device, power_script(mode, int(delay)), timeout=30.0)
    if mode == "cancel":
        return f"Power-off cancelled on {key}." if "CANCELLED" in out else f"Cancel failed: {out[:200]}"
    if code != 0 or "SCHEDULED" not in out:
        return f"{key} refused to power off: {out[:300] or code}"
    logger.info("Device %s: %s in %d s", key, mode, delay)
    verb = "shuts down" if mode == "shutdown" else "goes to sleep"
    return f"{key} {verb} in {delay} s (a warning is shown on its screen)."


# --------------------------------------------------------------------------
# Wake and wait


def wake_and_wait(device_key: str, registry: Registry,
                  send: Callable[[str], bool] | None = None,
                  keep_awake: Callable[[str], bool] | None = None,
                  sleep: Callable[[float], None] = time.sleep,
                  is_online: Callable[[dict[str, Any]], str | None] | None = None,
                  wait_sec: float = WAKE_WAIT_SEC,
                  clock: Callable[[], float] = time.monotonic) -> str:
    """Wake a machine and wait until it answers.

    `send(key)` sends the wake-up (default: Wake-on-LAN from this machine).
    `keep_awake(key)` asks its node to keep it awake: a network-woken Windows
    PC otherwise falls back asleep after ~2 minutes without user input.
    """
    is_online = is_online or online
    key = resolve(device_key, registry)
    if not key:
        return f"Unknown device '{device_key}'."
    device = dict(registry[key], _key=key)
    if device.get("paikallinen"):
        return "This machine is already awake."
    if is_online(device) is not None:
        return f"{key} is already on."
    if send is None:
        def send(_k: str) -> bool:
            return wake(device)
    if not send(key):
        return f"Could not send a wake-up to {key} (MAC missing or network down)."
    started = clock()
    while clock() - started < wait_sec:
        sleep(5.0)
        if is_online(device) is not None:
            took = clock() - started
            kept = bool(keep_awake and keep_awake(key))
            return (f"{key} woke up in {took:.0f} s"
                    + (" and stays awake for 15 min." if kept else
                       ". Note: keep-awake failed; Windows may fall asleep in ~2 min."))
    return (f"Wake-up sent, but {key} did not answer in {wait_sec:.0f} s. "
            "Check the Wake-on-LAN settings (BIOS and network adapter).")
