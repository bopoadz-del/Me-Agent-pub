"""Phase 2d: an unauthenticated request spends only its own sender's budget.

GET /auth/challenge needs no credentials, so charging it to the agent id it
names let anyone spend that agent's budget and lock the real agent out of
login (and, on re-login after expiry, out of the hive). The challenge is now
charged to the client address only; the proof is an HMAC over a 32+ character
secret, so a per-agent challenge budget bought no brute-force protection.
The agent, for its part, treats a refused or malformed challenge as a refused
login (RuntimeError), never a crash.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from agent import bootstrapper
from hive import auth as hive_auth
from hive.main import create_app

KEY = "k" * 40


def _challenges(app, client_host: str, agent_id: str, n: int) -> list[int]:
    async def run() -> list[int]:
        transport = httpx.ASGITransport(app=app, client=(client_host, 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://hive") as c:
            return [
                (await c.get("/auth/challenge", params={"agent_id": agent_id})).status_code
                for _ in range(n)
            ]

    return asyncio.run(run())


def test_one_client_cannot_spend_another_clients_challenges_for_the_same_agent(tmp_path):
    app = create_app(db_path=str(tmp_path / "hive.db"), jwt_key=KEY)
    attacker = _challenges(app, "10.0.0.66", "agent-1", hive_auth.CHALLENGE_RATE_LIMIT + 1)
    assert attacker[-1] == 429, "the attacker's own address is still limited"
    assert _challenges(app, "10.0.0.5", "agent-1", 1) == [200], (
        "the real agent's challenge must not be refused because someone else asked for it"
    )


def test_rotating_agent_ids_from_one_address_is_still_limited(tmp_path):
    app = create_app(db_path=str(tmp_path / "hive.db"), jwt_key=KEY)
    codes = [
        _challenges(app, "10.0.0.66", f"agent-{i}", 1)[0]
        for i in range(hive_auth.CHALLENGE_RATE_LIMIT + 1)
    ]
    assert codes[-1] == 429


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


@pytest.mark.parametrize("status,body", [(429, {"detail": "rate limit exceeded"}), (200, {"no": "nonce"})])
def test_a_refused_or_malformed_challenge_is_a_refused_login_not_a_crash(monkeypatch, status, body):
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp(status, body)

        def post(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("no proof is sent without a nonce")

    monkeypatch.setenv("HIVE_URL", "http://hive")
    monkeypatch.setenv("AGENT_ID", "agent-1")
    monkeypatch.setenv("AGENT_SECRET", "s" * 40)
    monkeypatch.setattr(bootstrapper.httpx, "Client", _Client)
    with pytest.raises(RuntimeError):
        bootstrapper._hive_login()
