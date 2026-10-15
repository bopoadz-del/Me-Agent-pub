"""LLM client ABC, OllamaClient, and declared test seam MockLLMClient."""
from __future__ import annotations

import json
import re
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

import httpx


#: Every generate call samples with this seed at temperature 0, so one prompt
#: gets one answer.
GENERATION_SEED = 42

#: config/model.env -- the one place the model is named (repo root, or /app
#: inside an image that copies config/).
MODEL_CONFIG = Path(__file__).resolve().parents[2] / "config" / "model.env"


def configured_model(config_path: Optional[Path] = None) -> str:
    """The model to call: OLLAMA_MODEL (compose sets it from config/model.env),
    else config/model.env itself. No default: an unset model fails closed."""
    import os

    name = os.environ.get("OLLAMA_MODEL", "").strip()
    if name:
        return name
    path = config_path or MODEL_CONFIG
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("OLLAMA_MODEL="):
                name = line.split("=", 1)[1].strip()
                if name:
                    return name
    except OSError:
        pass
    raise RuntimeError("OLLAMA_MODEL is not configured (set it in config/model.env)")


class LLMClient(ABC):
    @abstractmethod
    def generate(self, prompt: str, system: str, format: Any = "json") -> dict:
        ...


class OllamaClient(LLMClient):
    """Native Ollama `/api/generate` client for the local Ollama server.

    Local only (owner ruling 4): no hosted model, no API key, no Authorization
    header. OLLAMA_URL defaults to the local server."""

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None) -> None:
        import os

        self.base_url = (base_url or os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.model = model or configured_model()

    def generate(self, prompt: str, system: str, format: Any = "json") -> dict:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "format": format,
            # Grounded extraction is deterministic: Ollama's default sampling
            # (temperature 0.8, random seed) made the same commit pass the
            # live E2E on one run and fail it on the next.
            "options": {"temperature": 0, "seed": GENERATION_SEED},
        }
        headers = {"Content-Type": "application/json"}
        with httpx.Client(timeout=120.0) as client:
            response = client.post(f"{self.base_url}/api/generate", json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
        raw = body.get("response", "{}")
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)


class MockLLMClient(LLMClient):
    """Declared test seam — only valid when TEST_MODE=true and LLM_CLIENT=mock."""

    def __init__(
        self,
        handler: Optional[Callable[..., dict]] = None,
        responses: Optional[dict[str, Callable[..., dict]]] = None,
    ) -> None:
        self._handler = handler
        self._responses = responses or {}
        self._runner: Optional[Callable[..., dict]] = None

    def configure(self, handler: Callable[..., dict]) -> None:
        self._handler = handler

    def set_handler(self, handler: Callable[..., dict]) -> None:
        self._handler = handler

    def set_runner(self, runner: Callable[..., dict]) -> None:
        self._runner = runner

    def generate(self, prompt: str, system: str, format: Any = "json") -> dict:
        if self._runner is not None:
            return self._runner(prompt, system, format)
        if self._handler is not None:
            return self._handler(prompt, system, format)
        for key, fn in self._responses.items():
            if key in prompt or key in system:
                return fn(prompt, system, format)
        nums = [int(x) for x in re.findall(r"\b(\d+)\b", prompt + " " + system)]
        a, b = 5, 3
        if "a=" in prompt or "a=" in system:
            ma = re.search(r"a\s*=\s*(\d+)", prompt + " " + system)
            mb = re.search(r"b\s*=\s*(\d+)", prompt + " " + system)
            if ma:
                a = int(ma.group(1))
            if mb:
                b = int(mb.group(1))
        elif len(nums) >= 2:
            a, b = nums[0], nums[1]
        if "berth" in prompt.lower() or "berth" in system.lower():
            dwell = nums[0] if nums else 24
            rate = nums[1] if len(nums) > 1 else 10
            return {
                "berth_fee_usd": float(dwell * rate),
                "citations": [{"source": "evidence", "excerpt": f"{dwell}", "confidence": 1.0}],
            }
        return {
            "sum": a + b,
            "citations": [
                {"source": "evidence", "excerpt": f"a={a}, b={b}", "confidence": 1.0}
            ],
        }


def build_llm_client() -> LLMClient:
    import os

    mode = os.environ.get("LLM_CLIENT", "ollama").lower()
    if mode == "mock":
        return MockLLMClient()
    return OllamaClient()
