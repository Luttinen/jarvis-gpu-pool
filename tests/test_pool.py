"""pool.py: own node + router for real, queue/idle watcher with fake routers."""

from __future__ import annotations

import pytest

from jarvis_core import reititin
from jarvis_core.jono import Jono
from jarvis_core.reititin import Node
from jarvis_pool import pool as pool_mod
from jarvis_pool.pool import Pool, finished_text, job_text


class FakeRouter:
    def __init__(self, *nodes: Node) -> None:
        self._nodes = list(nodes)
        self.calls: list[tuple] = []

    def nodet(self):
        return self._nodes

    def valitse(self, kyky, *_a, **_k):
        return next((n for n in self._nodes if n.tila == "elossa" and n.tukee(kyky)), None)

    def kutsu(self, kyky, args, aikaraja_s=300, node=None, **_k):
        self.calls.append((kyky, node, args))
        return {"vastaus": "ok", "_node": node}

    def paivita(self):
        pass


def test_own_node_and_router(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_NODE_TOKEN", raising=False)
    registry = {"test": {"nimi": "Test", "paikallinen": True, "os": "linux", "node_portti": 0}}
    p = Pool(registry, tmp_path)
    # port 0 = any free port; tell a second router the real port afterwards
    r = p.start(idle_watch=False)
    try:
        registry["test"]["node_portti"] = p.own_node.portti
        r2 = reititin.rekisteri_laitteista(registry, p.token(), lahettaja="test")
        r2.paivita()
        row = r2.tila()[0]
        assert row["tila"] == "elossa"
        assert {"jarjestelma.tila", "jarjestelma.pida_hereilla", "laite.herata"} <= set(row["kyvyt"])
        result = r2.kutsu("jarjestelma.tila", {}, aikaraja_s=20)
        assert "cpu_pct" in result and "hereilla_s" in result
        assert (tmp_path / "node_token.txt").read_text().strip() == p.token()
        assert p.start() is r and p.queue is not None
        # The pool's own router was built while node_portti was still 0 (= skipped).
        assert p.status_text() == "No nodes in the registry."
    finally:
        p.stop()
    assert p.router is None and "not running" in p.status_text()


def test_texts():
    done = {"job_id": "a1", "kyky": "llm.chat", "kuvaus": "question", "tila": "valmis",
            "node": "gaming-pc", "tulos": {"vastaus": "42"}}
    assert finished_text(done).endswith(":\n42") and "gaming-pc" in finished_text(done)
    failed = dict(done, tila="epaonnistui", virhe="machine did not wake in 300 s")
    assert "did not wake" in finished_text(failed)
    assert "waiting for wake-up" in job_text(dict(done, tila="odottaa_heratysta"))


def test_wake_without_mac(tmp_path):
    assert Pool({"x": {"nimi": "X"}}, tmp_path, token="t").wake("x") is False


def test_wake_relays_through_other_nodes(tmp_path, monkeypatch):
    registry = {"desktop": {"paikallinen": True},
                "gaming-pc": {"mac": "00-11-22-33-44-55", "lan": "192.168.1.20"},
                "laptop": {}}
    helper = Node(node_id="laptop", url="http://x", tila="elossa",
                  manifest={"kyvyt": [{"nimi": "laite.herata"}]})
    own = Node(node_id="desktop", url="http://y", tila="elossa",
               manifest={"kyvyt": [{"nimi": "laite.herata"}]})
    p = Pool(registry, tmp_path, token="t")
    p.router = FakeRouter(helper, own)
    monkeypatch.setattr(pool_mod.devices, "wake", lambda d: False)
    assert p.wake("gaming-pc") is True
    assert p.router.calls == [("laite.herata", "laptop",
                               {"mac": "00-11-22-33-44-55", "lan": "192.168.1.20"})]
    assert "gaming-pc" in p.woken


def test_keep_awake_retries_until_node_answers(tmp_path):
    p = Pool({}, tmp_path, token="t")

    class Router(FakeRouter):
        fails = 2

        def kutsu(self, kyky, args, aikaraja_s=300, node=None, **_k):
            if self.fails:
                self.fails -= 1
                raise reititin.NodeVirhe("YHTEYS", "not up yet", node)
            return super().kutsu(kyky, args, aikaraja_s, node)

    p.router = Router()
    slept: list[float] = []
    assert p.keep_awake("gaming-pc", minutes=15, sleep=slept.append) is True
    assert p.router.calls == [("jarjestelma.pida_hereilla", "gaming-pc", {"minuutit": 15})]
    assert slept == [5.0, 5.0]


def test_queue_wakes_and_runs_when_machine_is_up(tmp_path):
    gpu = Node(node_id="gaming-pc", url="http://x", tila="poissa",
               manifest={"kyvyt": [{"nimi": "comfy.aja"}]})
    router = FakeRouter(gpu)
    messages: list[str] = []
    p = Pool({"gaming-pc": {"sovellukset": {"comfyui": {"odota_s": 400}}}}, tmp_path,
             notify=messages.append, token="t")
    woken: list[str] = []
    p.router = router
    p.queue = Jono(tmp_path / "jono.db", router, heraata=lambda n: woken.append(n) or True,
                   ilmoita=p._job_finished, aja_saikeessa=False)
    job = p.enqueue("comfy.aja", {"workflow": {"1": {}}}, description="picture",
                    target="gaming-pc")
    assert job["tila"] == "odottaa_heratysta" and job["odota_s"] == 400 and woken == ["gaming-pc"]
    assert "gaming-pc" in p.queue_text()
    gpu.tila = "elossa"
    p.queue.kasittele()
    assert router.calls == [("comfy.aja", "gaming-pc", {"workflow": {"1": {}}})]
    assert len(messages) == 1 and "done (gaming-pc)" in messages[0]


def test_on_job_done_overrides_message(tmp_path):
    messages: list[str] = []
    p = Pool({}, tmp_path, notify=messages.append, on_job_done=lambda job: f"custom {job['kyky']}",
             token="t")
    p._job_finished({"job_id": "1", "kyky": "llm.chat", "tila": "valmis"})
    assert messages == ["custom llm.chat"]


@pytest.fixture
def idle_pool(tmp_path):
    gpu = Node(node_id="gaming-pc", url="http://x", tila="elossa",
               syke={"peli_aktiivinen": None, "tyot_kaynnissa": 0, "kayttaja_joutilas_s": 3600})
    registry = {"gaming-pc": {}}
    messages: list[str] = []
    p = Pool(lambda: registry, tmp_path, notify=messages.append, token="t")
    p.router = FakeRouter(gpu)
    p.queue = Jono(tmp_path / "jono.db", p.router, aja_saikeessa=False)
    p.woken = {"gaming-pc": 1000.0}
    return p, gpu, registry, messages


def test_idle_machine_is_suggested_for_sleep(idle_pool):
    p, gpu, _registry, messages = idle_pool
    p.check_idle(now=1000.0 + pool_mod.IDLE_SEC - 1)
    assert messages == []
    gpu.syke["peli_aktiivinen"] = "cs2.exe"
    assert "game" in p.busy("gaming-pc")
    p.check_idle(now=1000.0 + pool_mod.IDLE_SEC + 1)
    assert messages == []  # someone is playing: no suggestion
    gpu.syke["peli_aktiivinen"] = None
    gpu.syke["kayttaja_joutilas_s"] = 30
    assert "using it" in p.busy("gaming-pc")
    gpu.syke["kayttaja_joutilas_s"] = 3600
    p.check_idle(now=1000.0 + pool_mod.IDLE_SEC + 1)
    assert len(messages) == 1 and "Shall I put it to sleep" in messages[0]
    p.check_idle(now=1000.0 + 2 * pool_mod.IDLE_SEC)
    assert len(messages) == 1  # only once per wake-up


def test_automatic_sleep(idle_pool):
    p, gpu, registry, messages = idle_pool
    registry["gaming-pc"]["automaattisammutus"] = True
    gpu.syke["hereilla_s"] = 300
    calls: list[tuple] = []

    def power_off(key, mode, reg, busy=None):
        calls.append((key, mode))
        return "ok"

    later = 1000.0 + pool_mod.IDLE_SEC + 1
    assert "awake" in p.busy("gaming-pc")  # a recent direct job keeps it awake
    p.check_idle(now=later, power_off=power_off)
    assert calls == []
    gpu.syke["hereilla_s"] = 0
    p.check_idle(now=later, power_off=power_off)
    assert calls == [("gaming-pc", "sleep")] and "idle for" in messages[0]
