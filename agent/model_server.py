"""Share the Ollama server; never take it over.

Before a mission runs, the agent asks the server what it has loaded. If
another model is loaded -- someone else's work -- the mission waits in the
queue and reports WAITING. The agent never unloads or evicts a model and never
starts a second Ollama: the only request it sends to decide is GET /api/ps.

Ollama exposes loaded models, not in-flight requests. A job on another model
shows as that model loaded; a job on our own model is queued by the server
itself and evicts nothing.
"""
from __future__ import annotations

from typing import Optional

import httpx

WAITING = "waiting for the model server"


def _same_model(a: str, b: str) -> bool:
    def norm(name: str) -> str:
        return name if ":" in name else f"{name}:latest"

    return norm(a.strip()) == norm(b.strip())


class ModelServerGate:
    def __init__(self, base_url: str, model: str, client: Optional[httpx.Client] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = client

    def busy(self) -> Optional[str]:
        """None when the mission may run now; otherwise why it waits."""
        try:
            if self._client is not None:
                response = self._client.get(f"{self.base_url}/api/ps", timeout=10.0)
            else:
                response = httpx.get(f"{self.base_url}/api/ps", timeout=10.0)
            response.raise_for_status()
            loaded = response.json().get("models") or []
        except (httpx.HTTPError, ValueError) as exc:
            return f"{WAITING} (unreachable: {type(exc).__name__})"
        others = sorted(
            {
                str(m.get("name") or m.get("model") or "")
                for m in loaded
                if isinstance(m, dict) and not _same_model(str(m.get("name") or m.get("model") or ""), self.model)
            }
            - {""}
        )
        if others:
            return f"{WAITING} ({', '.join(others)} loaded)"
        return None
