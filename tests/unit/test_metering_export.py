"""Metering slice B (issue #24): a per-grant usage summary and an export.

GET /lend/{grant_id}/usage/summary counts the grant's missions by outcome and
sums the billable units over the missions that count. The units are data
(METERING_UNITS): each names the usage column it sums, or "*" for a count of
missions. GET /lend/{grant_id}/usage/export gives the same window as JSON
({summary, items}) or CSV (one line per usage row, as an attachment). Both take
an optional since/until on the mission's end time. No payment provider is
wired: the export is the contract. An export larger than max_usage_export_rows
is refused; a borrower reads only its own grant.
"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

TOKEN = "t" * 40
OWNER = {"Authorization": f"Bearer {TOKEN}"}


class Queue:
    def queue_depth(self) -> int:
        return 0

    def enqueue(self, profile) -> bool:
        return True

    def is_queued(self, mission_id: str) -> bool:
        return False

    def waiting_reason(self, mission_id: str):
        return None


@pytest.fixture
def api(tmp_path, monkeypatch):
    from agent import bootstrapper
    from agent.memory import LocalStore

    monkeypatch.setenv("AGENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    store = LocalStore(str(tmp_path / "data" / "agent.db"))
    with TestClient(bootstrapper._create_api({"comms": Queue(), "store": store, "registry": None})) as client:
        client.conn = store.conn
        yield client


def _borrow(client, scopes=("mission:submit", "mission:read")):
    lent = client.post("/lend", headers=OWNER, json={
        "borrower": "visiting team", "scopes": list(scopes), "domains": ["port_terminal"],
        "ttl_seconds": 3600, "max_missions": 50}).json()
    token = client.post("/lend/redeem", json={"code": lent["redeem_code"]}).json()["token"]
    return lent["grant_id"], {"Authorization": f"Bearer {token}"}


def _use(conn, gid, mid, status="success", ended="2026-10-18T20:00:05+00:00", usage=None, cause=None):
    from agent import lending, metering
    from common.models.schemas import ExecutionResult, StepOutput

    lending.claim_mission(conn, gid, mid)
    output = {"reason": "x", "cause": cause} if cause else {}
    step = StepOutput(step_id="s", block_id="b", output=output, execution_time_ms=1,
                      validation_status="passed" if status == "success" else "failed", usage=usage or {})
    result = ExecutionResult(mission_id=mid, status=status, outputs=[step], citations=[], confidence=0.0)
    metering.record(conn, grant_id=gid, mission_id=mid, started_at="2026-10-18T20:00:00+00:00",
                    ended_at=ended, result=result)


def test_the_summary_counts_outcomes_and_bills_only_the_missions_that_count(api):
    gid, _ = _borrow(api)
    _use(api.conn, gid, "m1", usage={"tool_calls": 2, "model_calls": 1, "model_tokens": 10})
    _use(api.conn, gid, "m2", status="failure", cause="input", usage={"tool_calls": 1, "model_calls": 1})
    _use(api.conn, gid, "m3", status="failure", usage={"tool_calls": 9, "model_calls": 9, "model_tokens": 99})
    body = api.get(f"/lend/{gid}/usage/summary", headers=OWNER).json()
    assert body["outcomes"] == {"completed": 1, "refused_input": 1, "failed_by_agent": 1}
    units = body["units"]
    assert units["missions"] == 2 and units["steps"] == 2
    assert units["tool_calls"] == 3 and units["model_calls"] == 2 and units["model_tokens"] == 10


def test_model_tokens_stay_empty_when_no_adapter_reported_any(api):
    gid, _ = _borrow(api)
    _use(api.conn, gid, "m1", usage={"model_calls": 1})
    assert api.get(f"/lend/{gid}/usage/summary", headers=OWNER).json()["units"]["model_tokens"] is None


def test_the_billable_units_are_a_data_table(api, monkeypatch):
    from common.models import schemas

    monkeypatch.setitem(schemas.METERING_UNITS, "model_calls_all", {"column": "model_calls", "counted_only": False})
    gid, _ = _borrow(api)
    _use(api.conn, gid, "m1", usage={"model_calls": 2})
    _use(api.conn, gid, "m2", status="failure", usage={"model_calls": 5})
    units = api.get(f"/lend/{gid}/usage/summary", headers=OWNER).json()["units"]
    assert units["model_calls"] == 2 and units["model_calls_all"] == 7


def test_the_window_filters_on_when_each_mission_ended(api):
    gid, _ = _borrow(api)
    _use(api.conn, gid, "early", ended="2026-10-01T00:00:00+00:00")
    _use(api.conn, gid, "late", ended="2026-10-20T00:00:00+00:00")
    q = "since=2026-10-10T00:00:00%2B00:00&until=2026-10-31T00:00:00%2B00:00"
    assert api.get(f"/lend/{gid}/usage/summary?{q}", headers=OWNER).json()["units"]["missions"] == 1
    items = api.get(f"/lend/{gid}/usage/export?format=json&{q}", headers=OWNER).json()["items"]
    assert [i["mission_id"] for i in items] == ["late"]
    assert api.get(f"/lend/{gid}/usage/summary?since=yesterday", headers=OWNER).status_code == 422


def test_the_json_export_carries_the_summary_and_every_row(api):
    gid, _ = _borrow(api)
    _use(api.conn, gid, "m1")
    _use(api.conn, gid, "m2")
    body = api.get(f"/lend/{gid}/usage/export?format=json", headers=OWNER).json()
    assert body["summary"]["units"]["missions"] == 2
    assert [i["mission_id"] for i in body["items"]] == ["m1", "m2"]


def test_the_csv_export_is_one_line_per_row_as_an_attachment(api):
    from agent import metering

    gid, _ = _borrow(api)
    _use(api.conn, gid, "m1", usage={"tool_calls": 2})
    r = api.get(f"/lend/{gid}/usage/export?format=csv", headers=OWNER)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert list(rows[0]) == list(metering.EXPORT_COLUMNS)
    assert len(rows) == 1 and rows[0]["mission_id"] == "m1" and rows[0]["tool_calls"] == "2"


def test_a_csv_cell_that_would_run_as_a_formula_is_escaped():
    from agent import metering

    text = metering.to_csv([{c: "" for c in metering.EXPORT_COLUMNS} | {"mission_id": "=HYPERLINK(1)",
                                                                       "grant_id": "+1", "outcome": "@x"}])
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["mission_id"] == "'=HYPERLINK(1)" and row["grant_id"] == "'+1" and row["outcome"] == "'@x"


def test_an_export_over_the_bound_is_refused(api, monkeypatch):
    from common.logging import OPERATIONAL_CONSTRAINTS

    monkeypatch.setitem(OPERATIONAL_CONSTRAINTS, "max_usage_export_rows", 1)
    gid, _ = _borrow(api)
    _use(api.conn, gid, "m1")
    assert api.get(f"/lend/{gid}/usage/export?format=csv", headers=OWNER).status_code == 200
    _use(api.conn, gid, "m2")
    assert api.get(f"/lend/{gid}/usage/export?format=csv", headers=OWNER).status_code == 422


def test_an_unknown_format_is_refused(api):
    gid, _ = _borrow(api)
    assert api.get(f"/lend/{gid}/usage/export?format=xlsx", headers=OWNER).status_code == 422


@pytest.mark.parametrize("route", ["usage/summary", "usage/export?format=json"])
def test_a_borrower_reads_only_its_own_grant(api, route):
    gid, auth = _borrow(api)
    other, _ = _borrow(api)
    _gid, submit_only = _borrow(api, scopes=("mission:submit",))
    assert api.get(f"/lend/{gid}/{route}", headers=auth).status_code == 200
    assert api.get(f"/lend/{other}/{route}", headers=auth).status_code == 404
    assert api.get(f"/lend/{_gid}/{route}", headers=submit_only).status_code == 403
    assert api.get(f"/lend/nope/{route}", headers=OWNER).status_code == 404
    assert api.get(f"/lend/{gid}/{route}").status_code == 401
