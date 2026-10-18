"""One real mission end to end with no model server (LLM_CLIENT=replay).

The agent's own runtime -- HTTP API, queue worker, orchestrator, block
subprocesses, grounding -- runs on the compiled port_ops kit. Only the model's
replies come from tests/fixtures/replay/port_ops_mission.json. The test checks
the mission contract: a grounded answer cites verbatim evidence lines and
carries the value the kit's formula gives, and the same mission without
evidence is refused. Outside CI the Ollama client stays the default.
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
        "DATA_DIR": str(data), "AGENT_ID": "agent-smoke", "AGENT_API_TOKEN": TOKEN, "AIRGAP": "true",
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
            yield client, data
    finally:
        runtime["comms"].stop()


def _run(client, body: dict, timeout_s: float = 180.0, auth: dict = AUTH) -> dict:
    mission_id = client.post("/mission", json=body, headers=auth).json()["mission_id"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(f"/mission/{mission_id}", headers=auth)
        if r.status_code == 200 and r.json().get("result") is not None:
            return r.json()["result"]
        time.sleep(0.5)
    raise AssertionError(f"mission {mission_id} gave no result within {timeout_s}s")


def _passed_values(result: dict, field: str) -> list:
    out = []

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if key == field:
                    out.append(val)
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk([o["output"] for o in result["outputs"] if o["validation_status"] == "passed"])
    return out


def test_a_mission_with_evidence_is_answered_from_cited_lines(agent):
    client, data = agent
    evidence = data / "evidence" / "smoke" / "tariff.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(TARIFF, encoding="utf-8")
    result = _run(client, {
        "natural_language": MISSION, "domain_hint": "port_terminal",
        "attached_evidence": [{"type": "txt", "path": str(evidence)}],
    })
    assert result["status"] == "success", result
    assert result["confidence"] >= 0.9
    assert result["citations"] and all(c["excerpt"] in TARIFF for c in result["citations"])
    assert 240 in _passed_values(result, "berth_fee_usd")


def test_the_same_mission_without_evidence_is_refused(agent):
    client, _data = agent
    result = _run(client, {"natural_language": MISSION, "domain_hint": "port_terminal", "attached_evidence": []})
    assert result["status"] == "failure" and result["confidence"] == 0.0 and result["citations"] == []
    assert "evidence" in " ".join(str(o["output"].get("reason", "")) for o in result["outputs"]).lower()


def test_a_borrowed_mission_runs_end_to_end_on_the_borrowers_own_evidence(agent):
    """Lending slice 1 (issue #12): intake -> decision -> evidence record. The
    owner lends, the borrower redeems, uploads its evidence and runs the
    mission; the result is grounded on the borrower's own lines. Revoking the
    grant ends the borrower's access."""
    client, data = agent
    lent = client.post("/lend", headers=AUTH, json={
        "borrower": "visiting team",
        "scopes": ["mission:submit", "mission:read", "evidence:upload", "evidence:read"],
        "domains": ["port_terminal"], "ttl_seconds": 3600, "max_missions": 2}).json()
    borrower = {"Authorization": "Bearer " + client.post(
        "/lend/redeem", json={"code": lent["redeem_code"]}).json()["token"]}
    upload = client.post("/evidence/tariff.txt", content=TARIFF.encode("utf-8"), headers=borrower)
    assert upload.status_code == 200, upload.text
    path = Path(upload.json()["path"]).resolve()
    assert path.is_relative_to((data / "evidence" / f"grant-{lent['grant_id']}").resolve())
    result = _run(client, {
        "natural_language": MISSION, "domain_hint": "port_terminal",
        "attached_evidence": [{"type": "txt", "path": str(path)}],
    }, auth=borrower)
    assert result["status"] == "success", result
    assert result["citations"] and all(c["excerpt"] in TARIFF for c in result["citations"])
    assert 240 in _passed_values(result, "berth_fee_usd")
    assert client.post(f"/lend/{lent['grant_id']}/revoke", headers=AUTH).status_code == 200
    assert client.get("/evidence", headers=borrower).status_code == 401
