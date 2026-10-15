"""Local-only deployment on a local Ollama (owner rulings 4 and 6).

- No hosted model: no API key is read or sent, and OLLAMA_URL defaults to the
  local server.
- No Render: no render.yaml, and no Render-only code path.
- One model: its name comes from config/model.env and from nowhere else in
  code or deployment config, so the pulled model and the called model cannot
  drift.
- The local stack is hive + agent. It never starts its own Ollama; CI enables
  the bundled server through a compose profile.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MODEL_CONFIG = ROOT / "config" / "model.env"


def _tracked(*patterns: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", *patterns], cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / p for p in out.stdout.split()]


def _configured_model() -> str:
    for line in MODEL_CONFIG.read_text(encoding="utf-8").splitlines():
        if line.startswith("OLLAMA_MODEL="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("config/model.env has no OLLAMA_MODEL")


# -- no hosted model ------------------------------------------------------------------


def test_no_tracked_file_reads_an_ollama_api_key():
    hits = [
        str(p.relative_to(ROOT))
        for p in _tracked("*.py", "*.yml", "*.yaml", "*.sh", "*.env", "*Dockerfile*", "Makefile")
        if "OLLAMA_API_KEY" in p.read_text(encoding="utf-8", errors="ignore") and p.name != Path(__file__).name
    ]
    assert hits == []


def test_the_client_never_sends_an_authorization_header(monkeypatch):
    from common.models.llm_client import OllamaClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower() for k in request.headers}
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"response": json.dumps({"ok": True})})

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    OllamaClient(model="m").generate(prompt="p", system="s")
    assert "authorization" not in seen["headers"]
    assert seen["url"].startswith("http://127.0.0.1:11434/")


# -- no Render ------------------------------------------------------------------------


def test_there_is_no_render_deployment():
    assert not (ROOT / "render.yaml").exists()
    hits = [
        str(p.relative_to(ROOT))
        for p in _tracked("*.py", "*.yml", "*.yaml", "*.sh", "Makefile", "*Dockerfile*")
        if "RENDER" in p.read_text(encoding="utf-8", errors="ignore") and p.name != Path(__file__).name
    ]
    assert hits == []


# -- one model, one config value -------------------------------------------------------


def test_the_model_name_lives_only_in_its_config_file():
    model = _configured_model()
    assert model
    hits = [
        str(p.relative_to(ROOT))
        for p in _tracked("*.py", "*.yml", "*.yaml", "*.sh", "Makefile", "*Dockerfile*", "*.json", "*.txt")
        # The bake-off's candidate list names models it may try, not the one in use.
        if p not in (MODEL_CONFIG, ROOT / "config" / "model-candidates.txt")
        and model in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits == []


def test_code_has_no_default_model(monkeypatch, tmp_path):
    from common.models import llm_client

    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="OLLAMA_MODEL"):
        llm_client.configured_model(tmp_path / "missing.env")
    assert llm_client.configured_model() == _configured_model()


# -- the local stack ---------------------------------------------------------------------


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_the_local_stack_is_hive_and_agent_only():
    services = _compose()["services"]
    default = {name for name, svc in services.items() if not svc.get("profiles")}
    assert default == {"hive-server", "agent-instance"}
    assert services["ollama"]["profiles"] == ["bundled-ollama"]


def test_the_agent_defaults_to_the_local_ollama_server_and_reads_the_model_config():
    agent = _compose()["services"]["agent-instance"]
    assert "host.docker.internal:11434" in str(agent["environment"]["OLLAMA_URL"])
    assert "config/model.env" in agent.get("env_file", [])
    assert "OLLAMA_MODEL" not in agent["environment"]
    assert "host.docker.internal:host-gateway" in agent.get("extra_hosts", [])


def test_state_lives_on_named_local_volumes():
    compose = _compose()
    assert {"hive-data", "agent-data"} <= set(compose["volumes"])
