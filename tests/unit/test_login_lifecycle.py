"""Phase 2: the single-agent lock is a lease that ends; the agent logs in
again when its session expires; success is marked only once the hive replies."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from agent.memory import LocalStore, SyncEngine
from common.models.schemas import AuthProof
from common.storage.db import open_db
from hive import admin_cli
from hive import auth as hive_auth

KEY = "k" * 40


def _login(conn, agent_id, secret):
    nonce = hive_auth.issue_challenge(conn, agent_id).nonce
    proof = hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return hive_auth.verify_and_grant(conn, AuthProof(agent_id=agent_id, nonce=nonce, proof=proof), KEY)


@pytest.fixture()
def hive_db(tmp_path):
    db = str(tmp_path / "hive.db")
    secrets = {a: admin_cli.enroll_agent(a, db_path=db) for a in ("a", "b")}
    return open_db(db), secrets


def test_a_second_agent_is_locked_out_while_the_first_holds_the_lease(hive_db):
    conn, secrets = hive_db
    assert _login(conn, "a", secrets["a"])[1] == 200
    assert _login(conn, "b", secrets["b"])[1] == 409


def test_the_lock_clears_when_the_holder_disconnects(hive_db):
    conn, secrets = hive_db
    grant = _login(conn, "a", secrets["a"])[0]
    session_id = jwt.decode(grant.session_token, KEY, algorithms=[hive_auth.JWT_ALGORITHM])["jti"]
    hive_auth.release_agent(conn, "a", session_id)
    assert _login(conn, "b", secrets["b"])[1] == 200


def test_an_expired_lease_does_not_lock_anyone_out(hive_db):
    conn, secrets = hive_db
    _login(conn, "a", secrets["a"])
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    conn.execute("UPDATE agents SET active_until = ? WHERE agent_id = 'a'", (past,))
    conn.commit()
    assert _login(conn, "b", secrets["b"])[1] == 200


# -- the agent side ------------------------------------------------------------------


def _token(exp_in: float) -> str:
    return jwt.encode({"sub": "a", "exp": int(time.time() + exp_in)}, KEY, algorithm="HS256")


def _engine(tmp_path, token, reauth=None):
    store = LocalStore(str(tmp_path / "agent.db"))
    return SyncEngine(store, "a", "http://hive", token, reauth=reauth)


class _SilentSocket:
    """The hive takes the auth frame and never answers."""

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        raise ConnectionError("closed before any reply")


def test_success_is_not_marked_before_the_hive_replies(tmp_path):
    engine = _engine(tmp_path, _token(3600))
    engine._last_success = 0.0
    with pytest.raises(ConnectionError):
        asyncio.run(engine._session(_SilentSocket()))
    assert engine._last_success == 0.0


class _Closed(Exception):
    def __init__(self, code):
        super().__init__(f"closed {code}")
        self.rcvd = type("Close", (), {"code": code})()


def test_an_expired_session_logs_in_again(tmp_path):
    engine = _engine(tmp_path, _token(-10), reauth=lambda: "fresh-token")
    engine._renew_login(ConnectionError("gone"))
    assert engine.session_token == "fresh-token"


def test_a_refused_session_logs_in_again_even_before_expiry(tmp_path):
    engine = _engine(tmp_path, _token(3600), reauth=lambda: "fresh-token")
    engine._renew_login(_Closed(4401))
    assert engine.session_token == "fresh-token"


def test_a_live_session_is_kept_on_a_network_error(tmp_path):
    token = _token(3600)
    engine = _engine(tmp_path, token, reauth=lambda: pytest.fail("must not log in again"))
    engine._renew_login(ConnectionError("blip"))
    assert engine.session_token == token


def test_a_failed_login_keeps_the_old_token_and_retries_later(tmp_path):
    token = _token(-10)

    def refuse():
        raise RuntimeError("hive down")

    engine = _engine(tmp_path, token, reauth=refuse)
    engine._renew_login(ConnectionError("gone"))
    assert engine.session_token == token


def test_closing_the_sync_socket_releases_the_lock(tmp_path):
    from fastapi.testclient import TestClient

    from hive.main import create_app

    db = str(tmp_path / "hive.db")
    secrets = {a: admin_cli.enroll_agent(a, db_path=db) for a in ("a", "b")}

    def login(client, agent_id):
        nonce = client.get("/auth/challenge", params={"agent_id": agent_id}).json()["nonce"]
        proof = hmac.new(secrets[agent_id].encode(), nonce.encode(), hashlib.sha256).hexdigest()
        return client.post("/auth/prove", json={"agent_id": agent_id, "nonce": nonce, "proof": proof})

    with TestClient(create_app(db_path=db, jwt_key=KEY)) as client:
        token = login(client, "a").json()["session_token"]
        assert login(client, "b").status_code == 409
        with client.websocket_connect("/sync") as ws:
            ws.send_text(json.dumps({"auth": token}))
        assert login(client, "b").status_code == 200
