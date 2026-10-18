"""Metering slice A end to end (issue #22), no model server (LLM_CLIENT=replay).

A borrowed mission runs through the real worker, orchestrator and block
subprocesses on the compiled port_ops kit. When it ends, the grant's usage
shows one completed row with its steps and model calls counted, and model
tokens left empty because the replay adapter reports none. The same mission
without evidence is refused on the borrower's input: recorded, and counted.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
REPLIES = ROOT / "tests" / "fixtures" / "replay" / "port_ops_mission.json"
TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TARIFF = "Container type: dry. Dwell hours: 24. Base rate: 10 USD/hr.\n"
MISSION = "Calculate berth fee for a dry container using attached tariff evidence"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    from agent import bootstrapper
    from agent.memory import LocalStore
    from common.models.schemas import BlockDef
    from domain_kits.compiler.engine import compile_sheet

    data = tmp_path / "data"
    data.mkdir()
    for key, value in {
        "TEST_MODE": "true", "LLM_CLIENT": "replay", "LLM_REPLAY_FILE": str(REPLIES),
        "DATA_DIR": str(data), "AGENT_ID": "agent-metering", "AGENT_API_TOKEN": TOKEN, "AIRGAP": "true",
    }.items():
        monkeypatch.setenv(key, value)
    compiled = compile_sheet(ROOT / "domain-kits" / "sheets" / "port_ops.yaml",
                             out_dir=tmp_path / "blocks", tests_out_dir=tmp_path / "kit-tests")
    store = LocalStore(str(data / "agent.db"))
    store.apply_blocks([BlockDef.model_validate(json.loads(Path(p).read_text(encoding="utf-8")))
                        for p in compiled["blocks"]])
    runtime = bootstrapper._build_runtime(store)
    runtime["comms"].start()
    try:
        with TestClient(bootstrapper._create_api(runtime)) as client:
            yield client
    finally:
        runtime["comms"].stop()


def _usage_row(client, grant_id, mission_id, timeout_s=180.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        items = client.get(f"/lend/{grant_id}/usage", headers=AUTH).json()["items"]
        rows = [r for r in items if r["mission_id"] == mission_id]
        if rows:
            return rows[0]
        time.sleep(0.5)
    raise AssertionError(f"no usage row for {mission_id} within {timeout_s}s")


def test_a_borrowed_mission_leaves_one_usage_row(agent):
    client = agent
    lent = client.post("/lend", headers=AUTH, json={
        "borrower": "visiting team", "scopes": ["mission:submit", "mission:read", "evidence:upload"],
        "domains": ["port_terminal"], "ttl_seconds": 3600, "max_missions": 3}).json()
    borrower = {"Authorization": "Bearer " + client.post(
        "/lend/redeem", json={"code": lent["redeem_code"]}).json()["token"]}
    path = client.post("/evidence/tariff.txt", content=TARIFF.encode("utf-8"), headers=borrower).json()["path"]
    mid = client.post("/mission", headers=borrower, json={
        "natural_language": MISSION, "domain_hint": "port_terminal",
        "attached_evidence": [{"type": "txt", "path": path}]}).json()["mission_id"]
    row = _usage_row(client, lent["grant_id"], mid)
    assert row["outcome"] == "completed" and row["counted"] is True
    assert row["steps"] >= 1 and row["model_calls"] >= 1 and row["model_tokens"] is None
    assert row["started_at"] <= row["ended_at"]
    refused = client.post("/mission", headers=borrower, json={
        "natural_language": MISSION, "domain_hint": "port_terminal", "attached_evidence": []}).json()["mission_id"]
    row = _usage_row(client, lent["grant_id"], refused)
    assert row["outcome"] == "refused_input" and row["counted"] is True
    assert client.get(f"/lend/{lent['grant_id']}/usage", headers=borrower).status_code == 200
