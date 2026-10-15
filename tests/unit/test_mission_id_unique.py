"""Phase 2d: a mission sent to the agent must carry an id the agent has not used.

POST /mission mints a fresh id, but a hive command names its own. A command
reusing the id of a stored or queued mission was accepted, and its run
replaced that mission's state and result (INSERT OR REPLACE / ON CONFLICT
UPDATE) and shared its evidence directory. A mission id is now accepted only
while no stored or queued mission has it; resuming a mission uses its own
action and is not affected.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from agent.comms import CommsLink
from agent.memory import LocalStore
from common.models.schemas import Citation, ExecutionResult, StepOutput


class _Orchestrator:
    def __init__(self, store: Any) -> None:
        self.store = store


def _result(mission_id: str) -> ExecutionResult:
    return ExecutionResult(
        mission_id=mission_id,
        status="success",
        outputs=[
            StepOutput(step_id="s1", block_id="b", output={"v": 1}, validation_status="passed", execution_time_ms=1)
        ],
        citations=[Citation(source="t.txt", excerpt="v: 1", confidence=1.0)],
        confidence=1.0,
    )


@pytest.fixture()
def link(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store = LocalStore(str(tmp_path / "agent.db"))
    sent: list[dict] = []
    return CommsLink(_Orchestrator(store), publish_fn=sent.append), store, sent


def _run(link_obj: CommsLink, mission_id: str) -> None:
    link_obj.handle_command(
        {"action": "run_mission", "profile": {"mission_id": mission_id, "natural_language": "x"}}
    )


def test_a_fresh_id_is_queued(link):
    comms, _store, _sent = link
    _run(comms, "m-new")
    assert comms.is_queued("m-new")


def test_an_id_already_queued_is_refused(link):
    comms, _store, sent = link
    _run(comms, "m-q")
    _run(comms, "m-q")
    assert comms.queue_depth() == 1
    assert sent and "already" in json.dumps(sent[-1])


def test_an_id_already_stored_is_refused_and_its_result_kept(link):
    comms, store, sent = link
    store.save_mission_result(_result("m-done"))
    before = store.get_mission_result("m-done")
    _run(comms, "m-done")
    assert comms.queue_depth() == 0
    assert store.get_mission_result("m-done") == before
    assert sent and "already" in json.dumps(sent[-1])
