"""The coordinator: several PCs as one GPU pool.

`Pool.start()` brings up, in-process:
  1. the coordinator's own node (jarvis_node.NodePalvelin) in a thread, so the
     router treats every machine, including this one, the same way;
  2. the router (jarvis_core.reititin.NodeRekisteri), which polls every node's
     heartbeat: alive or not, free VRAM, loaded Ollama models, active game;
  3. the SQLite job queue (jarvis_core.jono.Jono): a job for a sleeping machine
     waits in the queue, the machine is woken up and the job runs once it is back;
  4. an idle watcher: a machine the pool woke up is put back to sleep (or the
     user is asked) once it has been idle for IDLE_SEC after its jobs.

Token: environment variable JARVIS_NODE_TOKEN or <data_dir>/node_token.txt
(created on first start; never commit it).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis_core import protokolla, reititin
from jarvis_core.jono import Jono

from . import devices

logger = logging.getLogger(__name__)

IDLE_SEC = 20 * 60
USER_PRESENT_SEC = 10 * 60  # mouse/keyboard used this recently = someone is at the machine
WAKE_WAIT_SEC = 300.0

Notify = Callable[[str], Any]


def load_or_create_token(path: str | Path) -> str:
    """Shared HMAC token; created once and stored in `path`."""
    value = protokolla.lue_token(path)
    if value:
        return value
    value = protokolla.luo_token()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(value, encoding="utf-8")
    return value


STATE_TEXT = {"jonossa": "queued", "odottaa_heratysta": "waiting for wake-up",
              "kaynnissa": "running", "valmis": "done", "epaonnistui": "failed",
              "peruttu": "cancelled"}


def job_text(job: dict) -> str:
    name = job.get("kuvaus") or job.get("kyky")
    out = f"[{job['job_id']}] {name}: {STATE_TEXT.get(job['tila'], job['tila'])}"
    if job.get("node") or job.get("kohde"):
        out += f" ({job.get('node') or job.get('kohde')})"
    if job.get("virhe") and job["tila"] == "epaonnistui":
        out += f" — {job['virhe']}"
    return out


def finished_text(job: dict) -> str:
    """Notification text for a finished queued job."""
    name = job.get("kuvaus") or job.get("kyky")
    if job["tila"] != "valmis":
        return f"Queued job '{name}' failed: {job.get('virhe') or job['tila']}"
    answer = str((job.get("tulos") or {}).get("vastaus") or "").strip()
    title = f"Queued job '{name}' done ({job.get('node') or '?'})"
    return f"{title}:\n{answer}" if answer else title + "."


class Pool:
    """Own node + router + job queue + wake-up / keep-awake / idle watcher.

    `registry` is a callable returning the devices dict (re-read on demand, so
    edits to devices.json apply without a restart) or a plain dict.
    `notify(text)` receives human-readable messages (job finished, idle machine).
    `on_job_done(job)` may return a custom message for a finished job.
    """

    def __init__(self, registry: Callable[[], devices.Registry] | devices.Registry,
                 data_dir: str | Path, notify: Notify | None = None,
                 on_job_done: Callable[[dict], str | None] | None = None,
                 token: str | None = None) -> None:
        self._registry_fn = registry if callable(registry) else (lambda: registry)
        self.data_dir = Path(data_dir)
        self.notify = notify
        self.on_job_done = on_job_done
        self._token = token
        self._lock = threading.Lock()
        self.own_node: Any = None
        self.router: reititin.NodeRekisteri | None = None
        self.queue: Jono | None = None
        # Machines the pool woke up for queued jobs: node_id -> wake time.
        self.woken: dict[str, float] = {}
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    # ---------- setup ----------

    def registry(self) -> devices.Registry:
        return self._registry_fn()

    def token(self) -> str:
        if not self._token:
            self._token = load_or_create_token(self.data_dir / "node_token.txt")
        return self._token

    def own_id(self) -> str:
        return next((k for k, d in self.registry().items() if d.get("paikallinen")), "aivot")

    def start(self, own_node: bool = True, poll_sec: float = 10.0, queue_sec: float = 5.0,
              idle_watch: bool = True) -> reititin.NodeRekisteri:
        """Start everything. Safe to call more than once."""
        with self._lock:
            if self.router is not None:
                return self.router
            token = self.token()
            registry = self.registry()
            own = self.own_id()
            if own_node:
                self._start_own_node(own, registry.get(own, {}), token)
            self.router = reititin.rekisteri_laitteista(registry, token, lahettaja=own)
            reititin.aseta_oletus(self.router)
            self.router.kaynnista(poll_sec)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.queue = Jono(self.data_dir / "jono.db", self.router, heraata=self.wake,
                              ilmoita=self._job_finished)
            self.queue.kaynnista(queue_sec)
        if idle_watch:
            self._stop.clear()
            self._watcher = threading.Thread(target=self._idle_loop, daemon=True,
                                             name="pool-idle-watch")
            self._watcher.start()
        return self.router

    def _start_own_node(self, own: str, device: dict, token: str) -> None:
        try:
            from jarvis_core.kayttoonotto import oletus_config_aivoille
            from jarvis_node.node import NodePalvelin

            listen = ["127.0.0.1"] + ([device["tailscale"]] if device.get("tailscale") else [])
            config = oletus_config_aivoille(listen)
            config["node_id"] = own
            port = device.get("node_portti")
            config["portti"] = int(protokolla.NODE_PORTTI if port is None else port)
            self.own_node = NodePalvelin(config, token).start()
            logger.info("Own node listening on port %s", self.own_node.portti)
        except Exception:
            logger.exception("Own node did not start (port in use?)")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self.queue is not None:
                self.queue.pysayta()
                self.queue = None
            if self.router is not None:
                self.router.pysayta()
                reititin.aseta_oletus(None)
                self.router = None
            if self.own_node is not None:
                try:
                    self.own_node.stop()
                except Exception:
                    logger.debug("Own node stop failed", exc_info=True)
                self.own_node = None

    # ---------- wake-up and keep-awake ----------

    def wake(self, node_id: str) -> bool:
        """Wake a machine: Wake-on-LAN from here, and through every other live node.

        Relaying through another node helps when the coordinator is on a
        different network (e.g. a VPS) or broadcast does not get through.
        """
        device = self.registry().get(node_id) or {}
        if not device.get("mac"):
            logger.warning("Cannot wake %s: no MAC address in the registry", node_id)
            return False
        sent = False
        try:
            sent = devices.wake(device)
        except OSError:
            logger.debug("Local Wake-on-LAN failed", exc_info=True)
        r = self.router
        if r is not None:
            own = self.own_id()  # the own node is this machine, which already sent it
            for n in r.nodet():
                if n.node_id in (node_id, own) or n.tila != "elossa" or not n.tukee("laite.herata"):
                    continue
                try:
                    r.kutsu("laite.herata", {"mac": device["mac"], "lan": device.get("lan") or ""},
                            aikaraja_s=15, node=n.node_id)
                    sent = True
                except reititin.NodeVirhe as exc:
                    logger.debug("Wake-up via %s failed: %s", n.node_id, exc)
        logger.info("Wake-up %s: %s", node_id, "sent" if sent else "failed")
        if sent:
            self.woken.setdefault(node_id, time.time())
        return sent

    def keep_awake(self, node_id: str, minutes: float = 15.0, wait_sec: float = 60.0,
                   sleep: Callable[[float], None] = time.sleep) -> bool:
        """Ask the node to keep its machine awake (SetThreadExecutionState on the node).

        Waits up to `wait_sec` for a just-woken node to answer. Never raises.
        """
        r = self.router
        if r is None:
            return False
        deadline = time.monotonic() + wait_sec
        while True:
            try:
                r.kutsu("jarjestelma.pida_hereilla", {"minuutit": minutes}, aikaraja_s=15,
                        node=node_id)
                return True
            except reititin.NodeVirhe as exc:
                if exc.koodi == "EI_KYKYA" or time.monotonic() >= deadline:
                    logger.info("Keep-awake failed (%s): %s", node_id, exc)
                    return False
            sleep(5.0)
            r.paivita()

    def wake_and_wait(self, device_key: str) -> str:
        return devices.wake_and_wait(device_key, self.registry(), send=self.wake,
                                     keep_awake=self.keep_awake)

    def power_off(self, device_key: str, mode: str = "sleep") -> str:
        return devices.power_off(device_key, mode, self.registry(), busy=self.busy)

    # ---------- idle watcher ----------

    def busy(self, node_id: str) -> str | None:
        """Why the machine should not be powered off right now, or None."""
        if self.queue is not None:
            open_jobs = [t for t in self.queue.listaa(vain_avoimet=True)
                         if node_id in (t.get("kohde"), t.get("node"))]
            if open_jobs:
                return f"{len(open_jobs)} jobs queued or running"
        r = self.router
        node = next((n for n in r.nodet() if n.node_id == node_id), None) if r else None
        beat = (node.syke if node and node.tila == "elossa" else None) or {}
        if beat.get("peli_aktiivinen"):
            return f"a game is running ({beat['peli_aktiivinen']})"
        if int(beat.get("tyot_kaynnissa") or 0) or int(beat.get("tyot_jonossa") or 0):
            return "the node has unfinished jobs"
        if int(beat.get("hereilla_s") or 0) > 0:
            # Direct (non-queued) jobs also request keep-awake on the node:
            # a sign of recent work.
            return "kept awake because of recent jobs"
        idle = beat.get("kayttaja_joutilas_s")
        if idle is not None and float(idle) < USER_PRESENT_SEC:
            return f"someone is using it (input {int(float(idle) // 60)} min ago)"
        return None

    def _last_job(self, node_id: str) -> float:
        if self.queue is None:
            return 0.0
        times = [float(t.get("paivitetty") or 0) for t in self.queue.listaa(raja=200)
                 if node_id in (t.get("kohde"), t.get("node"))]
        return max(times, default=0.0)

    def check_idle(self, now: float | None = None,
                   power_off: Callable[..., str] | None = None) -> None:
        """A machine the pool woke up has been idle IDLE_SEC: sleep it or ask the user.

        devices.json: "automaattisammutus": true puts it to sleep automatically
        ("automaattisammutus_tapa": "sammuta" shuts it down instead); otherwise
        the user is asked once per wake-up.
        """
        now = time.time() if now is None else now
        power_off = power_off or devices.power_off
        for node_id, woken_at in list(self.woken.items()):
            if self.busy(node_id):
                continue
            quiet_since = max(woken_at, self._last_job(node_id))
            if now - quiet_since < IDLE_SEC:
                continue
            self.woken.pop(node_id, None)  # once per wake-up
            minutes = int((now - quiet_since) // 60)
            device = self.registry().get(node_id) or {}
            if device.get("automaattisammutus"):
                # Sleep by default: wakes in ~16 s over WoL and programs stay open.
                mode = "shutdown" if device.get("automaattisammutus_tapa") == "sammuta" else "sleep"
                self._say(f"{node_id} has been idle for {minutes} min after its jobs. "
                          + power_off(node_id, mode, self.registry(), busy=self.busy))
            else:
                self._say(f"I woke {node_id} for queued jobs and it has been idle for "
                          f"{minutes} min. Shall I put it to sleep?")

    def _idle_loop(self) -> None:
        while not self._stop.wait(60.0):
            try:
                self.check_idle()
            except Exception:
                logger.exception("Idle watcher crashed")

    # ---------- queue ----------

    def enqueue(self, kyky: str, args: dict, description: str = "", requester: str = "",
                target: str | None = None, timeout_sec: float = 600,
                meta: dict | None = None) -> dict:
        """Run a job now if a capable machine is up; otherwise wake one and queue it."""
        if self.queue is None:
            raise RuntimeError("The job queue is not running")
        wait = WAKE_WAIT_SEC
        if target:
            for app in (self.registry().get(target) or {}).get("sovellukset", {}).values():
                wait = max(wait, float(app.get("odota_s") or 0))
        return self.queue.lisaa(kyky, args, pyytaja=requester, kuvaus=description, kohde=target,
                                odota_s=wait, aikaraja_s=timeout_sec, meta=meta)

    def _job_finished(self, job: dict) -> None:
        text = ""
        if self.on_job_done is not None:
            try:
                text = self.on_job_done(job) or ""
            except Exception as exc:  # noqa: BLE001
                logger.exception("on_job_done failed for %s", job.get("job_id"))
                text = f"Queued job finished, but post-processing failed: {exc}"
        self._say(text or finished_text(job))

    def _say(self, text: str) -> None:
        logger.info("%s", text[:200])
        if self.notify is not None:
            try:
                self.notify(text)
            except Exception:
                logger.debug("notify failed", exc_info=True)

    # ---------- status ----------

    def queue_text(self) -> str:
        if self.queue is None:
            return "The job queue is not running."
        return "\n".join(job_text(t) for t in self.queue.listaa(raja=10)) or "The queue is empty."

    def status_text(self) -> str:
        r = self.router
        if r is None:
            return "The pool is not running."
        rows = []
        for n in r.tila():
            parts = [f"{n.get('nimi') or n['node_id']}: {n['tila']}"]
            if n.get("gpu"):
                parts.append(f"{n['gpu']}, VRAM free {_gb(n.get('vram_vapaa_mb'))}"
                             f"/{_gb(n.get('vram_mb'))}")
            if n.get("cpu_pct") is not None:
                parts.append(f"CPU {n['cpu_pct']:.0f} %")
            loaded = n.get("ollama_ladattu") or []
            if loaded:
                parts.append("Ollama loaded: " + ", ".join(
                    f"{m.get('nimi')} ({_gb(m.get('vram_mb'))})" for m in loaded))
            jobs = n.get("tyot") or {}
            if jobs.get("kaynnissa") or jobs.get("jonossa"):
                parts.append(f"jobs {jobs.get('kaynnissa', 0)} running / "
                             f"{jobs.get('jonossa', 0)} queued")
            if n.get("peli_aktiivinen"):
                parts.append(f"game running: {n['peli_aktiivinen']}")
            if n.get("virhe") and n["tila"] != "elossa":
                parts.append(f"error: {n['virhe']}")
            rows.append(" · ".join(parts))
        return "\n".join(rows) or "No nodes in the registry."


def _gb(mb: Any) -> str:
    try:
        return f"{float(mb) / 1024:.1f} GB"
    except (TypeError, ValueError):
        return "?"
