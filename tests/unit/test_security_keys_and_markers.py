"""Phase 2: harness markers are test-only, the hive never serves without its
key, and keys are long enough and never reused."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.swarm import spawner
from common.models.schemas import BlockDef, PlanStep

REPO_ROOT = Path(__file__).resolve().parents[2]


def _block(prompt: str) -> BlockDef:
    return BlockDef(
        block_id="b",
        name="b",
        domain="d",
        version_clock=1,
        system_prompt_template=prompt,
        input_schema={},
        output_json_schema={"type": "object"},
        tools=[],
        evidence_standard="",
    )


def _step() -> PlanStep:
    return PlanStep(step_id="s", block_id="b", prompt_scope="x", tool_set=[])


@pytest.mark.parametrize("marker", ["TEST_HARNESS_ENVDUMP", "TEST_HARNESS_SLEEPER", "TEST_HARNESS_SEARCHER"])
def test_harness_markers_do_nothing_outside_test_mode(marker, monkeypatch):
    monkeypatch.setenv("TEST_MODE", "false")
    assert spawner._execute_harness_block(_block(f"{marker}: go"), _step()) == {}


def test_harness_markers_work_in_test_mode(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    out = spawner._execute_harness_block(_block("TEST_HARNESS_ENVDUMP"), _step())
    assert "TEST_MODE" in out["env"]


def test_hive_module_builds_no_app_at_import(monkeypatch):
    """No unconfigured fallback app: the server is built by create_app()."""
    monkeypatch.delenv("HIVE_JWT_KEY", raising=False)
    import hive.main

    module = importlib.reload(hive.main)
    assert not hasattr(module, "app")
    with pytest.raises(RuntimeError, match="HIVE_JWT_KEY"):
        module.create_app()


def test_hive_entrypoint_serves_the_factory():
    text = (REPO_ROOT / "scripts" / "hive_docker_entrypoint.sh").read_text(encoding="utf-8")
    assert "--factory hive.main:create_app" in text


def _validate(env: dict[str, str]) -> subprocess.CompletedProcess:
    code = "from agent.bootstrapper import _validate_env; _validate_env()"
    full = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "HIVE_"))}
    full.update({"PYTHONPATH": str(REPO_ROOT), **env})
    return subprocess.run([sys.executable, "-c", code], env=full, capture_output=True, text=True)


def test_a_short_api_token_is_refused():
    r = _validate({"AGENT_ID": "a", "AGENT_API_TOKEN": "x" * 31, "AIRGAP": "true"})
    assert r.returncode != 0 and "AGENT_API_TOKEN must be at least 32" in r.stderr


def test_a_short_agent_secret_is_refused():
    r = _validate({
        "AGENT_ID": "a", "AGENT_API_TOKEN": "t" * 32, "AGENT_SECRET": "s" * 31,
        "HIVE_URL": "http://hive",
    })
    assert r.returncode != 0 and "AGENT_SECRET must be at least 32" in r.stderr


def test_the_api_token_and_agent_secret_must_differ():
    r = _validate({
        "AGENT_ID": "a", "AGENT_API_TOKEN": "k" * 40, "AGENT_SECRET": "k" * 40,
        "HIVE_URL": "http://hive",
    })
    assert r.returncode != 0 and "must be different keys" in r.stderr


def test_valid_distinct_keys_pass():
    r = _validate({
        "AGENT_ID": "a", "AGENT_API_TOKEN": "t" * 32, "AGENT_SECRET": "s" * 32,
        "HIVE_URL": "http://hive",
    })
    assert r.returncode == 0, r.stderr


def test_bootstrap_secret_may_not_reuse_the_jwt_key(tmp_path):
    key = "j" * 40
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "DATA_DIR": str(tmp_path),
           "HIVE_JWT_KEY": key, "BOOTSTRAP_AGENT_SECRET": key, "BOOTSTRAP_AGENT_ID": "a"}
    r = subprocess.run([sys.executable, "scripts/enroll_bootstrap_agent.py"], cwd=REPO_ROOT,
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "must be different keys" in (r.stdout + r.stderr)
