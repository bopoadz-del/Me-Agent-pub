"""Metering slice A (issue #22): an append-only usage record per borrowed mission.

Every borrowed mission writes one row when it ends: grant, mission, start and
end, steps, tool calls, model calls, model tokens (only when the adapter
reports them), an outcome code, and whether it counted. The outcome comes from
a structured cause on the failed step, never from its text:
  completed         the mission succeeded
  refused_input     the borrower's own input was refused (evidence, cost cap)
  refused_by_agent  the agent could not plan it or turned it away
  failed_by_agent   a step failed or the worker raised
Which outcomes count against the quota is data (USAGE_OUTCOMES); an outcome
that does not count gives its slot back, once. Rows are never updated or
deleted. GET /lend/{grant_id}/usage shows the owner any grant and a borrower
only its own.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 40
OWNER = {"Authorization": f"Bearer {TOKEN}"}
MISSION = "Calculate berth fee for a dry container using attached tariff evidence"


def _registry():
    import tempfile

    from agent.swarm.registry import BlockRegistry
    from common.models.schemas import BlockDef
    from domain_kits.compiler.engine import compile_sheet

    out = Path(tempfile.mkdtemp())
    compiled = compile_sheet(ROOT / "domain-kits" / "sheets" / "port_ops.yaml",
                             out_dir=out / "blocks", tests_out_dir=out / "kit-tests")
    registry = BlockRegistry()
    for path in compiled["blocks"]:
        registry.register(BlockDef.model_validate(json.loads(Path(path).read_text(encoding="utf-8"))))
    return registry


def _refusal(reason: str):
    from agent.orchestrator.engine import HeadlessOrchestrator
    from common.models.schemas import ExecutionPlan, PlanStep

    orch = HeadlessOrchestrator(_registry(), None, None, None)
    step = PlanStep(block_id="__refusal__", prompt_scope=reason, tool_set=[], timeout_seconds=1)
    return orch.execute(ExecutionPlan(mission_id="m", steps=[step]))


def _result(status, outputs):
    from common.models.schemas import ExecutionResult, StepOutput

    return ExecutionResult(mission_id="m", status=status, citations=[], confidence=1.0 if status == "success" else 0.0,
                           outputs=[StepOutput(**o) for o in outputs])


def _step(validation="passed", usage=None, output=None):
    return {"step_id": "s", "block_id": "b", "output": output or {}, "validation_status": validation,
            "execution_time_ms": 1, "usage": usage or {}}


# --- outcome codes ----------------------------------------------------------

def test_a_refusal_of_the_borrowers_own_input_is_refused_input():
    from agent import metering

    assert metering.outcome(_refusal("cost")) == "refused_input"
    assert metering.outcome(_refusal("refuse: a tariff document")) == "refused_input"


def test_a_plan_the_agent_cannot_make_is_refused_by_agent():
    from agent import metering

    assert metering.outcome(_refusal("no matching blocks")) == "refused_by_agent"
    assert metering.outcome(_refusal("missing dependency block: x")) == "refused_by_agent"


def test_a_step_that_needs_evidence_the_mission_lacks_is_refused_input():
    from agent import metering
    from agent.orchestrator.engine import HeadlessOrchestrator
    from common.models.schemas import ExecutionPlan, PlanStep

    registry = _registry()
    block = next(b for b in registry.all_blocks() if b.evidence_standard)
    orch = HeadlessOrchestrator(registry, None, None, None)
    plan = ExecutionPlan(mission_id="m", steps=[PlanStep(block_id=block.block_id, prompt_scope=MISSION,
                                                         tool_set=[], timeout_seconds=1)])
    assert metering.outcome(orch.execute(plan)) == "refused_input"


def test_a_failed_step_or_a_success_is_classified_by_status_not_text():
    from agent import metering

    # A model failure whose reason happens to say "evidence required" is still the agent's.
    failed = _result("failure", [_step("failed", output={"reason": "evidence required: held by no line"})])
    assert metering.outcome(failed) == "failed_by_agent"
    assert metering.outcome(_result("success", [_step()])) == "completed"


def test_every_outcome_has_a_row_in_the_data_table():
    from common.models.schemas import USAGE_OUTCOMES

    assert set(USAGE_OUTCOMES) == {"completed", "refused_input", "refused_by_agent", "failed_by_agent"}
    assert {k for k, v in USAGE_OUTCOMES.items() if v["counts"]} == {"completed", "refused_input"}


# --- the record -------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    from agent import lending

    c = sqlite3.connect(str(tmp_path / "agent.db"))
    lending.ensure_schema(c)
    return c


def _grant(conn, max_missions=1):
    from agent import lending

    return lending.create_grant(conn, borrower="visiting team", scopes=["mission:submit", "mission:read"],
                                domains=["port_terminal"], ttl_seconds=3600, max_missions=max_missions)["grant_id"]


def _used(conn, gid):
    return conn.execute("SELECT missions_used FROM grants WHERE grant_id = ?", (gid,)).fetchone()[0]


def _record(conn, gid, mid, result):
    from agent import metering

    return metering.record(conn, grant_id=gid, mission_id=mid, started_at="2026-10-18T20:00:00+00:00",
                           ended_at="2026-10-18T20:00:05+00:00", result=result)


def test_an_outcome_that_does_not_count_gives_its_slot_back_once(conn):
    from agent import lending

    gid = _grant(conn)
    assert lending.claim_mission(conn, gid, "m1")
    assert _record(conn, gid, "m1", _result("failure", [_step("failed")])) is True
    assert _used(conn, gid) == 0
    assert _record(conn, gid, "m1", _result("failure", [_step("failed")])) is False  # one row per mission
    assert _used(conn, gid) == 0
    assert lending.claim_mission(conn, gid, "m2")


def test_an_outcome_that_counts_keeps_its_slot(conn):
    from agent import lending

    gid = _grant(conn)
    assert lending.claim_mission(conn, gid, "m1")
    _record(conn, gid, "m1", _result("success", [_step()]))
    assert _used(conn, gid) == 1


def test_the_record_carries_the_counts_and_tokens_only_when_reported(conn):
    from agent import lending, metering

    gid = _grant(conn, max_missions=5)
    lending.claim_mission(conn, gid, "m1")
    _record(conn, gid, "m1", _result("success", [
        _step(usage={"tool_calls": 2, "model_calls": 1, "model_tokens": 30}),
        _step(usage={"tool_calls": 1, "model_calls": 2, "model_tokens": 12}),
    ]))
    lending.claim_mission(conn, gid, "m2")
    _record(conn, gid, "m2", _result("success", [_step(usage={"tool_calls": 0, "model_calls": 1})]))
    rows = {r["mission_id"]: r for r in metering.usage(conn, gid)}
    assert rows["m1"]["steps"] == 2 and rows["m1"]["tool_calls"] == 3 and rows["m1"]["model_calls"] == 3
    assert rows["m1"]["model_tokens"] == 42
    assert rows["m2"]["model_tokens"] is None
    assert rows["m1"]["outcome"] == "completed" and rows["m1"]["counted"] is True
    assert rows["m1"]["started_at"] < rows["m1"]["ended_at"]


def test_usage_rows_are_never_updated_or_deleted(conn):
    from agent import lending, metering

    gid = _grant(conn)
    lending.claim_mission(conn, gid, "m1")
    _record(conn, gid, "m1", _result("success", [_step()]))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(f"UPDATE {metering.USAGE_TABLE} SET outcome = 'failed_by_agent'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(f"DELETE FROM {metering.USAGE_TABLE}")


def test_a_released_slot_is_not_given_back_twice(conn):
    """The queue refused the mission and already gave the slot back (#17)."""
    from agent import lending

    gid = _grant(conn)
    lending.claim_mission(conn, gid, "m1")
    lending.release_mission(conn, gid, "m1")
    _record(conn, gid, "m1", _result("failure", []))
    assert _used(conn, gid) == 0



def test_a_slot_already_given_back_is_not_given_back_again(conn):
    """Another mission still holds a slot, so only the hold check can stop a
    second refund for a mission whose slot the queue already returned."""
    from agent import lending

    gid = _grant(conn, max_missions=2)
    assert lending.claim_mission(conn, gid, "m1") and lending.claim_mission(conn, gid, "m2")
    lending.release_mission(conn, gid, "m2")
    assert _used(conn, gid) == 1
    _record(conn, gid, "m2", _result("failure", [_step("failed")]))
    assert _used(conn, gid) == 1

# --- the worker -------------------------------------------------------------

class _Orchestrator:
    def __init__(self, store, result=None, raises=False):
        self.store, self._result, self._raises = store, result, raises

    def resolve(self, profile):
        if self._raises:
            raise RuntimeError("planner crashed")
        return profile

    def execute(self, plan):
        return self._result


def _profile(mid):
    from common.models.schemas import MissionProfile

    return MissionProfile(mission_id=mid, natural_language=MISSION, domain_hint="port_terminal")


def test_the_worker_records_a_borrowed_missions_usage(tmp_path):
    from agent import lending, metering
    from agent.comms import CommsLink
    from agent.memory import LocalStore

    store = LocalStore(str(tmp_path / "agent.db"))
    gid = _grant(store.conn, max_missions=2)
    lending.claim_mission(store.conn, gid, "m1")
    CommsLink(_Orchestrator(store, _result("success", [_step()])))._run_one(_profile("m1"))
    lending.claim_mission(store.conn, gid, "m2")
    CommsLink(_Orchestrator(store, raises=True))._run_one(_profile("m2"))
    rows = {r["mission_id"]: r for r in metering.usage(store.conn, gid)}
    assert rows["m1"]["outcome"] == "completed"
    assert rows["m2"]["outcome"] == "failed_by_agent" and rows["m2"]["counted"] is False
    assert store.get_mission_result("m2")["status"] == "failure"
    assert _used(store.conn, gid) == 1


def test_the_owners_own_missions_write_no_usage_row(tmp_path):
    from agent import metering
    from agent.comms import CommsLink
    from agent.memory import LocalStore

    store = LocalStore(str(tmp_path / "agent.db"))
    CommsLink(_Orchestrator(store, _result("success", [_step()])))._run_one(_profile("own"))
    metering.ensure_schema(store.conn)
    assert store.conn.execute(f"SELECT COUNT(*) FROM {metering.USAGE_TABLE}").fetchone()[0] == 0


# --- the model adapter ------------------------------------------------------

def test_the_ollama_adapter_reports_its_token_counts(monkeypatch):
    from common.models import llm_client

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"response": "{}", "prompt_eval_count": 20, "eval_count": 7}

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return Response()

    monkeypatch.setattr(llm_client.httpx, "Client", Client)
    client = llm_client.OllamaClient(base_url="http://model", model="m")
    client.generate("p", "s")
    assert client.last_usage == {"model_tokens": 27}
    assert getattr(llm_client.MockLLMClient(), "last_usage", None) is None


# --- the route --------------------------------------------------------------

class Queue:
    def __init__(self) -> None:
        self.profiles: dict[str, object] = {}
        self.refuse = False

    def queue_depth(self) -> int:
        return len(self.profiles)

    def enqueue(self, profile) -> bool:
        if self.refuse:
            return False
        self.profiles[profile.mission_id] = profile
        return True

    def is_queued(self, mission_id: str) -> bool:
        return mission_id in self.profiles

    def waiting_reason(self, mission_id: str):
        return None


@pytest.fixture
def api(tmp_path, monkeypatch):
    from agent import bootstrapper
    from agent.memory import LocalStore

    monkeypatch.setenv("AGENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    store = LocalStore(str(tmp_path / "data" / "agent.db"))
    runtime = {"comms": Queue(), "store": store, "registry": _registry()}
    with TestClient(bootstrapper._create_api(runtime)) as client:
        client.store, client.queue = store, runtime["comms"]
        yield client


def _borrow(client, scopes=("mission:submit", "mission:read"), max_missions=3):
    lent = client.post("/lend", headers=OWNER, json={
        "borrower": "visiting team", "scopes": list(scopes), "domains": ["port_terminal"],
        "ttl_seconds": 3600, "max_missions": max_missions}).json()
    token = client.post("/lend/redeem", json={"code": lent["redeem_code"]}).json()["token"]
    return lent["grant_id"], {"Authorization": f"Bearer {token}"}


def test_the_owner_reads_any_grants_usage_and_a_borrower_only_its_own(api):
    gid, auth = _borrow(api)
    other, _ = _borrow(api)
    from agent import lending

    lending.claim_mission(api.store.conn, gid, "m1")
    _record(api.store.conn, gid, "m1", _result("success", [_step()]))
    owner_view = api.get(f"/lend/{gid}/usage", headers=OWNER)
    assert owner_view.status_code == 200 and [r["mission_id"] for r in owner_view.json()["items"]] == ["m1"]
    assert api.get(f"/lend/{gid}/usage", headers=auth).json()["items"] == owner_view.json()["items"]
    assert api.get(f"/lend/{other}/usage", headers=auth).status_code == 404
    assert api.get(f"/lend/{gid}/usage").status_code == 401


def test_reading_usage_takes_the_read_scope(api):
    gid, submit_only = _borrow(api, scopes=("mission:submit",))
    assert api.get(f"/lend/{gid}/usage", headers=submit_only).status_code == 403


def test_the_usage_page_size_is_bounded_by_data(api):
    from common.logging import OPERATIONAL_CONSTRAINTS

    gid, _ = _borrow(api)
    cap = int(OPERATIONAL_CONSTRAINTS["max_usage_list_page"])
    assert api.get(f"/lend/{gid}/usage?limit={cap}", headers=OWNER).status_code == 200
    assert api.get(f"/lend/{gid}/usage?limit={cap + 1}", headers=OWNER).status_code == 422


def test_a_mission_the_queue_turns_away_is_recorded_and_not_counted(api):
    from agent import metering

    gid, auth = _borrow(api, max_missions=1)
    api.queue.refuse = True
    r = api.post("/mission", headers=auth, json={"natural_language": MISSION, "domain_hint": "port_terminal"})
    assert r.status_code == 429
    rows = metering.usage(api.store.conn, gid)
    assert [(row["outcome"], row["counted"], row["steps"]) for row in rows] == [("refused_by_agent", False, 0)]
    assert _used(api.store.conn, gid) == 0
