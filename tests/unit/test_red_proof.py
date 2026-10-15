"""The red-proof selector and verdict are general: test files come from the
diff's paths, outcomes from the junit report; nothing names a PR or a test."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("red_proof", ROOT / "scripts" / "red_proof.py")
red_proof = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(red_proof)


def test_only_test_modules_under_tests_are_selected():
    paths = [
        "agent/comms.py",
        "tests/unit/test_a.py",
        "tests/integration/test_b.py",
        "tests/conftest.py",
        "tests/fixtures/data.json",
        "scripts/test_helper.py",
        "tests/unit/helpers.py",
        "tests/unit/test_a.py",
    ]
    assert red_proof.changed_test_files(paths) == ["tests/integration/test_b.py", "tests/unit/test_a.py"]


def test_a_change_with_no_test_file_selects_nothing():
    assert red_proof.changed_test_files(["agent/comms.py", "README.md"]) == []


def test_outcomes_read_every_result_kind(tmp_path):
    xml = tmp_path / "r.xml"
    xml.write_text(
        """<testsuites><testsuite>
        <testcase classname="tests.unit.test_x" name="test_pass"/>
        <testcase classname="tests.unit.test_x" name="test_fail"><failure message="x"/></testcase>
        <testcase classname="tests.unit.test_x" name="test_err"><error message="x"/></testcase>
        <testcase classname="tests.unit.test_x" name="test_skip"><skipped message="x"/></testcase>
        </testsuite></testsuites>""",
        encoding="utf-8",
    )
    assert red_proof.outcomes(str(xml)) == {
        "tests.unit.test_x::test_pass": "passed",
        "tests.unit.test_x::test_fail": "failed",
        "tests.unit.test_x::test_err": "error",
        "tests.unit.test_x::test_skip": "skipped",
    }


def test_a_file_that_cannot_import_on_base_does_not_hide_the_others(tmp_path):
    cmd = red_proof.pytest_command(["tests/unit/test_a.py", "tests/unit/test_b.py"], tmp_path / "r.xml")
    assert "--continue-on-collection-errors" in cmd
    assert "tests/unit/test_a.py" in cmd and "tests/unit/test_b.py" in cmd


def _repo(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_shared.py").write_text("def test_one():\n    assert True\n")
    git("add", "-A")
    git("commit", "-q", "-m", "fork point")
    git("checkout", "-q", "-b", "feature")
    (tmp_path / "tests" / "unit" / "test_mine.py").write_text("def test_mine():\n    assert True\n")
    git("add", "-A")
    git("commit", "-q", "-m", "the PR's own test")
    git("checkout", "-q", "main")
    (tmp_path / "tests" / "unit" / "test_shared.py").write_text("def test_one():\n    assert 1\n")
    git("commit", "-q", "-am", "the target branch moves on")
    return git("rev-parse", "main"), git("rev-parse", "feature")


def test_only_the_prs_own_tests_are_proved_when_the_target_has_moved_on(tmp_path):
    """The proof compares the head with its merge-base, as a PR's diff does: a
    test the target branch changed after the fork is not the PR's."""
    base_tip, head = _repo(tmp_path)
    assert red_proof.files_under_proof(base_tip, head, cwd=tmp_path) == ["tests/unit/test_mine.py"]


def test_the_proof_runs_on_the_fork_point(tmp_path):
    import subprocess

    base_tip, head = _repo(tmp_path)
    fork = subprocess.run(
        ["git", "rev-parse", "main~1"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert red_proof.proof_base(base_tip, head, cwd=tmp_path) == fork
