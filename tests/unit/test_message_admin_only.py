"""Phase 2d: only an admin may queue a command for an agent, and it is bounded.

POST /message stores a payload that the target agent receives on its next
heartbeat as a command (comms.handle_command runs it as a mission). It took
any valid session token, so one agent -- or anyone holding one agent's token
-- could run a mission of its choosing on any other agent, with a payload of
any size. Commands now take the admin role, the same rule /admin/blocks
applies, and a payload larger than MESSAGE_PAYLOAD_MAX_BYTES is refused.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac

import httpx

from common.models.schemas import AuthProof
from hive import admin_cli
from hive import auth as hive_auth
from hive import main as hive_main
from hive.main import create_app

KEY = "k" * 40


def _post(app, token: str, payload: dict) -> int:
    async def run() -> int:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hive") as c:
            r = await c.post(
                "/message",
                json={"target_agent_id": "agent-2", "payload": payload},
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code

    return asyncio.run(run())


def test_an_agent_session_cannot_command_another_agent(tmp_path):
    db = str(tmp_path / "hive.db")
    secret = admin_cli.enroll_agent("agent-1", db_path=db)
    app = create_app(db_path=db, jwt_key=KEY)
    nonce = hive_auth.issue_challenge(app.state.db, "agent-1").nonce
    proof = hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    grant, status, _ = hive_auth.verify_and_grant(
        app.state.db, AuthProof(agent_id="agent-1", nonce=nonce, proof=proof), KEY
    )
    assert status == 200, "a live agent session"
    assert _post(app, grant.session_token, {"action": "run_mission"}) == 403


def test_an_admin_can_command_an_agent(tmp_path):
    app = create_app(db_path=str(tmp_path / "hive.db"), jwt_key=KEY)
    assert _post(app, hive_auth.mint_admin_token(KEY), {"action": "run_mission"}) == 200


def test_an_oversized_command_is_refused(tmp_path):
    app = create_app(db_path=str(tmp_path / "hive.db"), jwt_key=KEY)
    big = {"blob": "x" * (hive_main.MESSAGE_PAYLOAD_MAX_BYTES + 1)}
    assert _post(app, hive_auth.mint_admin_token(KEY), big) == 413
