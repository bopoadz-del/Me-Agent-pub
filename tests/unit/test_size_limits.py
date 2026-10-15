"""Phase 2d: every input the agent and hive accept has a size bound.

A mission's text and evidence list, an intake file, and a memory value an
agent syncs to the hive were all unbounded: one request could fill memory or
disk, and a hive-sent mission skipped the disk-limit check POST /mission
applies. The bounds are operational constraints (common.logging,
spec Section 10), each enforced where every path passes:

- max_mission_text_chars, max_evidence_items: MissionProfile (POST /mission,
  hive commands and resumed missions all build one); POST /mission answers 422.
- max_evidence_file_mb: evidence intake, before anything is copied.
- max_memory_value_kb: the hive's memory_delta handler; a larger value is
  not stored and the hive answers with the reason.
- max_disk_gb: also checked for a hive-sent mission, before intake.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent import comms as comms_mod
from agent.swarm import tools
from common.logging import OPERATIONAL_CONSTRAINTS
from common.models.schemas import MissionProfile
from common.storage.db import open_db
from hive import sync as hive_sync


def test_the_bounds_are_operational_constraints():
    for name in ("max_mission_text_chars", "max_evidence_items", "max_evidence_file_mb", "max_memory_value_kb"):
        assert int(OPERATIONAL_CONSTRAINTS[name]) > 0, name


def test_a_mission_text_over_the_bound_is_refused():
    limit = int(OPERATIONAL_CONSTRAINTS["max_mission_text_chars"])
    MissionProfile(natural_language="x" * limit)
    with pytest.raises(ValidationError):
        MissionProfile(natural_language="x" * (limit + 1))


def test_an_evidence_list_over_the_bound_is_refused():
    limit = int(OPERATIONAL_CONSTRAINTS["max_evidence_items"])
    item = {"type": "txt", "path": "/data/evidence/m/x.txt"}
    MissionProfile(natural_language="x", attached_evidence=[item] * limit)
    with pytest.raises(ValidationError):
        MissionProfile(natural_language="x", attached_evidence=[item] * (limit + 1))


def test_the_mission_api_body_answers_422_not_500():
    from agent.bootstrapper import MissionCreateBody

    limit = int(OPERATIONAL_CONSTRAINTS["max_mission_text_chars"])
    with pytest.raises(ValidationError):
        MissionCreateBody(natural_language="x" * (limit + 1))


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "evidence").mkdir(parents=True)
    intake = tmp_path / "intake"
    intake.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("EVIDENCE_INTAKE_DIRS", str(intake))
    return data, intake


def test_an_intake_file_over_the_bound_is_refused_before_any_copy(dirs):
    data, intake = dirs
    big = intake / "big.txt"
    with big.open("wb") as fh:  # sparse: the size is real, the disk use is not
        fh.seek(int(OPERATIONAL_CONSTRAINTS["max_evidence_file_mb"]) * 1024 * 1024)
        fh.write(b"x")
    with pytest.raises(PermissionError):
        tools.intake_evidence([{"type": "txt", "path": str(big)}], "m1")
    assert not (data / "evidence" / "m1" / "big.txt").exists()


def test_an_intake_file_within_the_bound_is_copied(dirs):
    data, intake = dirs
    (intake / "t.txt").write_text("Base rate: 10 USD/hr.", encoding="utf-8")
    out = tools.intake_evidence([{"type": "txt", "path": str(intake / "t.txt")}], "m1")
    assert (data / "evidence" / "m1" / "t.txt").exists() and out[0]["path"].endswith("t.txt")


def test_a_memory_value_over_the_bound_is_not_stored(tmp_path):
    conn = open_db(str(tmp_path / "hive.db"))
    limit = int(OPERATIONAL_CONSTRAINTS["max_memory_value_kb"]) * 1024
    big = {"blob": "x" * limit}
    out = hive_sync.handle_agent_delta(conn, "agent-1", {"key": "k", "value": big}, {"agent-1": 1})
    assert conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 0
    assert out and "too large" in json.dumps(out[0].payload)


def test_a_memory_value_within_the_bound_is_stored(tmp_path):
    conn = open_db(str(tmp_path / "hive.db"))
    hive_sync.handle_agent_delta(conn, "agent-1", {"key": "k", "value": {"v": 1}}, {"agent-1": 1})
    assert conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 1


def test_a_hive_sent_mission_is_refused_when_the_disk_limit_is_reached(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(comms_mod, "disk_over_limit", lambda path: True, raising=False)
    sent: list[dict] = []
    link = comms_mod.CommsLink(orchestrator=None, publish_fn=sent.append)
    link.handle_command({"action": "run_mission", "profile": {"natural_language": "x"}})
    assert link.queue_depth() == 0
    assert sent and "disk" in json.dumps(sent[-1])
