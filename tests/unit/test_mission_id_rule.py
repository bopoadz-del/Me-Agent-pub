"""Phase 2d: a mission id is a plain name, and intake never writes outside it.

The mission id names a directory under DATA_DIR/evidence/. It was never
validated, and Path("..").name == "..", so a hive-sent profile with
mission_id ".." made evidence intake copy a file into DATA_DIR itself -- an
intake file named agent.db replaced the agent's own database. One id rule
now holds at the schema, so every entry point (POST /mission, a hive
command, a resumed mission) inherits it, and intake checks that the copy
lands strictly inside the mission's evidence directory.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.swarm import tools
from common.models.schemas import MissionProfile

BAD_IDS = ["..", ".", "", "a/b", "a\\b", "../x", "x" * 65, "-lead", "sp ace", "nul\x00"]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_the_schema_refuses_an_id_that_is_not_a_plain_name(bad):
    with pytest.raises(ValidationError):
        MissionProfile(mission_id=bad, natural_language="x")


@pytest.mark.parametrize("good", ["m1", "m-4", "mission_2", "3f2b9c1e-0d4a-4c1b-9a7e-2b6f1d9e8c70"])
def test_the_schema_keeps_plain_names_and_uuids(good):
    assert MissionProfile(mission_id=good, natural_language="x").mission_id == good


def test_the_generated_default_id_obeys_the_rule():
    MissionProfile.model_validate(MissionProfile(natural_language="x").model_dump())


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "evidence").mkdir(parents=True)
    intake = tmp_path / "intake"
    intake.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("EVIDENCE_INTAKE_DIRS", str(intake))
    return data, intake


@pytest.mark.parametrize("bad", ["..", ".", ""])
def test_intake_never_writes_outside_the_missions_evidence_dir(dirs, bad):
    data, intake = dirs
    (intake / "agent.db").write_text("attacker", encoding="utf-8")
    with pytest.raises((PermissionError, ValueError)):
        tools.intake_evidence([{"type": "txt", "path": str(intake / "agent.db")}], bad)
    assert not (data / "agent.db").exists()
