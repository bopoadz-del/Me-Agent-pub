"""One command for a reader who has Docker and nothing else.

`make run` brings the stack up with its own Ollama server (compose profile
bundled-ollama), which pulls the model named in config/model.env: the same
path CI's live job takes. `make up` keeps using a host Ollama. `make down`
stops either stack, bundled server included, and keeps every volume.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _dry_run(target: str) -> str:
    if shutil.which("make") is None:
        pytest.skip("make is not installed here")
    return subprocess.run(
        ["make", "-n", target], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def test_make_run_starts_the_stack_on_its_own_ollama():
    out = _dry_run("run")
    assert "COMPOSE_PROFILES=bundled-ollama" in out
    assert "OLLAMA_URL=http://ollama:11434" in out
    assert "scripts/local_up.sh" in out


def test_make_up_still_uses_the_host_ollama():
    assert "bundled-ollama" not in _dry_run("up")


def test_make_down_stops_the_bundled_server_too_and_keeps_volumes():
    script = (ROOT / "scripts" / "local_down.sh").read_text(encoding="utf-8")
    down = [ln for ln in script.splitlines() if ln.strip().startswith("docker compose")]
    assert down and all("--profile bundled-ollama" in ln and "down" in ln for ln in down)
    assert all(" -v" not in ln and "--volumes" not in ln for ln in down)
