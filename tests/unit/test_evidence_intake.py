"""Ruling 1: evidence is copied into the whitelist on intake; the whitelist is
never widened, and nothing outside it is ever read.

The reader trusts DATA_DIR/evidence only. A mission may attach a file from a
configured intake directory (EVIDENCE_INTAKE_DIRS); on intake the file is
copied to DATA_DIR/evidence/<mission id>/ and the mission reads the copy. A
path outside both is refused at intake. The spawner no longer falls back to a
raw read when the whitelist refuses a path.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from agent.swarm import spawner, tools


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    intake = tmp_path / "intake"
    outside = tmp_path / "outside"
    for d in (data, intake, outside):
        d.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("EVIDENCE_INTAKE_DIRS", str(intake))
    return data, intake, outside


def test_the_whitelist_is_only_the_data_dir_evidence(dirs):
    data, _intake, _outside = dirs
    assert tools._allowed_evidence_roots() == [(data / "evidence").resolve()]


def test_a_file_from_an_intake_dir_is_copied_into_the_whitelist(dirs):
    data, intake, _outside = dirs
    (intake / "tariff.txt").write_text("Base rate: 10 USD/hr.\n", encoding="utf-8")
    items = tools.intake_evidence([{"type": "txt", "path": str(intake / "tariff.txt")}], "m-1")
    copied = Path(items[0]["path"])
    assert copied == (data / "evidence" / "m-1" / "tariff.txt").resolve()
    assert tools.file_reader(str(copied)) == "Base rate: 10 USD/hr.\n"


def test_a_file_already_in_the_whitelist_is_kept_where_it_is(dirs):
    data, _intake, _outside = dirs
    target = data / "evidence" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    items = tools.intake_evidence([{"type": "txt", "path": str(target)}], "m-2")
    assert Path(items[0]["path"]) == target.resolve()


def test_a_path_outside_the_whitelist_and_the_intake_dirs_is_refused(dirs):
    _data, _intake, outside = dirs
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(PermissionError):
        tools.intake_evidence([{"type": "txt", "path": str(outside / "secret.txt")}], "m-3")


def test_a_symlink_in_an_intake_dir_cannot_smuggle_an_outside_file(dirs):
    _data, intake, outside = dirs
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    try:
        (intake / "link.txt").symlink_to(outside / "secret.txt")
    except OSError as exc:  # e.g. Windows without the symlink privilege
        pytest.skip(f"this OS cannot create symlinks here ({exc.__class__.__name__}: {exc.strerror}); runs on Linux CI")
    with pytest.raises(PermissionError):
        tools.intake_evidence([{"type": "txt", "path": str(intake / "link.txt")}], "m-4")


def test_the_reader_refuses_anything_outside_the_whitelist(dirs):
    _data, intake, _outside = dirs
    (intake / "t.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError):
        tools.file_reader(str(intake / "t.txt"))


def test_the_spawner_never_falls_back_to_a_raw_read():
    src = inspect.getsource(spawner._child_worker)
    assert "read_text" not in src, "a refused path must stay refused"


def test_a_hive_command_with_outside_evidence_is_refused_and_reported(dirs):
    """The hive path admits evidence by the same rule as the API."""
    from agent.comms import CommsLink

    _data, _intake, outside = dirs
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    sent: list[dict] = []
    link = CommsLink(orchestrator=None)
    link.enqueue = lambda profile: sent.append({"queued": profile}) or True
    link._emit_block_update = lambda result: sent.append({"result": result})
    link.handle_command(
        {
            "action": "run_mission",
            "profile": {
                "natural_language": "x",
                "attached_evidence": [{"type": "txt", "path": str(outside / "secret.txt")}],
            },
        }
    )
    assert len(sent) == 1 and "result" in sent[0]
    assert sent[0]["result"].status == "failure"
    assert "intake" in sent[0]["result"].outputs[0].output["reason"]
