"""The agent shares the Ollama server; it never takes it over (owner task step 9).

Before a mission runs, the agent asks the server what it has loaded
(GET /api/ps). If another model is loaded -- someone else's work -- the
mission waits in the queue and reports "waiting for the model server". The
agent never unloads or evicts a model and never starts a second Ollama: the
only request it sends to decide is GET /api/ps.

Ollama exposes loaded models, not in-flight requests. A job on another model
shows as that model loaded; a job on our own model is queued by the server
itself and evicts nothing.
"""
from __future__ import annotations

import threading
import time

import httpx

from agent.model_server import WAITING, ModelServerGate
from common.models.schemas import ExecutionResult, MissionProfile

MODEL = "ours:3b"


def _server(loaded: list[str], seen: list[tuple[str, str]], fail: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if fail:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"models": [{"name": n, "model": n} for n in loaded]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_another_loaded_model_means_wait():
    seen: list = []
    gate = ModelServerGate("http://ollama:11434", MODEL, client=_server(["their-slm:7b"], seen))
    reason = gate.busy()
    assert reason is not None and reason.startswith(WAITING)


def test_our_model_loaded_or_nothing_loaded_means_go():
    for loaded in ([MODEL], []):
        gate = ModelServerGate("http://ollama:11434", MODEL, client=_server(loaded, []))
        assert gate.busy() is None


def test_a_latest_tag_is_the_same_model():
    gate = ModelServerGate("http://ollama:11434", "ours", client=_server(["ours:latest"], []))
    assert gate.busy() is None


def test_an_unreachable_server_means_wait_not_start_one():
    gate = ModelServerGate("http://ollama:11434", MODEL, client=_server([], [], fail=True))
    reason = gate.busy()
    assert reason is not None and reason.startswith(WAITING)


def test_deciding_sends_nothing_but_a_read_of_what_is_loaded():
    seen: list = []
    gate = ModelServerGate("http://ollama:11434", MODEL, client=_server(["their-slm:7b"], seen))
    for _ in range(3):
        gate.busy()
    assert set(seen) == {("GET", "/api/ps")}


class _Orchestrator:
    def __init__(self) -> None:
        self.ran: list[str] = []
        self.store = None

    def resolve(self, profile):
        return profile

    def execute(self, plan):
        self.ran.append(plan.mission_id)
        return ExecutionResult(mission_id=plan.mission_id, status="success", outputs=[], citations=[], confidence=1.0)


class _FlippingGate:
    """Busy until released, like a server whose other model is later unloaded by its owner."""

    def __init__(self) -> None:
        self.free = threading.Event()
        self.checks = 0

    def busy(self):
        self.checks += 1
        return None if self.free.is_set() else f"{WAITING} (their-slm:7b is loaded)"


def test_a_mission_waits_in_the_queue_and_reports_it_until_the_server_is_free():
    from agent.comms import CommsLink

    orch = _Orchestrator()
    gate = _FlippingGate()
    comms = CommsLink(orch, model_gate=gate, poll_seconds=0.05)
    comms.start()
    try:
        profile = MissionProfile(natural_language="x")
        assert comms.enqueue(profile)
        deadline = time.time() + 2
        while comms.waiting_reason(profile.mission_id) is None and time.time() < deadline:
            time.sleep(0.01)
        assert comms.waiting_reason(profile.mission_id).startswith(WAITING)
        assert orch.ran == []
        gate.free.set()
        deadline = time.time() + 2
        while not orch.ran and time.time() < deadline:
            time.sleep(0.01)
        assert orch.ran == [profile.mission_id]
        assert comms.waiting_reason(profile.mission_id) is None
    finally:
        comms.stop()
