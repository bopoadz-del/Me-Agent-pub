"""Phase 2d: an agent's session token is good only while it is that agent's
current session, and only the current session can release the lease.

Session JWTs were checked for signature and expiry only. Rotating an agent's
secret (hive-admin agent add) left every token already issued working for
up to 24 h -- full memory read on every heartbeat, delta writes -- and any
socket opened with any token of the agent released the agent's lease when
it closed, letting a second agent prove in. Each login now mints a session
id (jti) recorded on the agent's row; re-enrolment clears it and a new login
replaces it. The sync socket checks it on every frame, and closing a socket
releases the lease only when it was the current session's.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hive import admin_cli
from hive.main import create_app

KEY = "k" * 40


@pytest.fixture()
def hive(tmp_path):
    db = str(tmp_path / "hive.db")
    secrets = {a: admin_cli.enroll_agent(a, db_path=db) for a in ("a", "b")}
    with TestClient(create_app(db_path=db, jwt_key=KEY)) as client:
        yield client, db, secrets


def _login(client, secrets, agent_id):
    nonce = client.get("/auth/challenge", params={"agent_id": agent_id}).json()["nonce"]
    proof = hmac.new(secrets[agent_id].encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return client.post("/auth/prove", json={"agent_id": agent_id, "nonce": nonce, "proof": proof})


def _heartbeat_answered(client, token: str, agent_id: str) -> bool:
    """Open /sync with ``token``, send one heartbeat; True when the hive answers."""
    with client.websocket_connect("/sync") as ws:
        ws.send_text(json.dumps({"auth": token}))
        ws.send_text(json.dumps({"msg_type": "heartbeat", "session_id": agent_id}))
        try:
            ws.receive_text()
            return True
        except WebSocketDisconnect as exc:
            assert exc.code == 4401
            return False


def test_the_current_session_is_answered(hive):
    client, _db, secrets = hive
    token = _login(client, secrets, "a").json()["session_token"]
    assert _heartbeat_answered(client, token, "a")


def test_rotating_an_agents_secret_ends_its_sessions(hive):
    client, db, secrets = hive
    token = _login(client, secrets, "a").json()["session_token"]
    admin_cli.enroll_agent("a", db_path=db)  # rotate
    assert not _heartbeat_answered(client, token, "a")


def test_a_new_login_ends_the_previous_session(hive):
    client, _db, secrets = hive
    old = _login(client, secrets, "a").json()["session_token"]
    _login(client, secrets, "a")
    assert not _heartbeat_answered(client, old, "a")


def test_a_stale_socket_cannot_release_the_current_lease(hive):
    client, _db, secrets = hive
    old = _login(client, secrets, "a").json()["session_token"]
    assert _login(client, secrets, "a").status_code == 200  # the current session
    _heartbeat_answered(client, old, "a")  # opened and closed with the old token
    assert _login(client, secrets, "b").status_code == 409, "a's current lease must still hold"


def test_a_boot_time_secret_change_ends_the_agents_sessions(hive, tmp_path, monkeypatch):
    """The hive's boot-time enrolment is the other way a secret changes; the
    rule is the same there."""
    import importlib.util
    from pathlib import Path

    client, _db, secrets = hive
    token = _login(client, secrets, "a").json()["session_token"]
    script = Path(__file__).resolve().parents[2] / "scripts" / "enroll_bootstrap_agent.py"
    spec = importlib.util.spec_from_file_location("enroll_bootstrap_agent", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOOTSTRAP_AGENT_ID", "a")
    monkeypatch.setenv("BOOTSTRAP_AGENT_SECRET", "n" * 40)
    monkeypatch.setenv("HIVE_JWT_KEY", KEY)
    assert mod.main() == 0
    assert not _heartbeat_answered(client, token, "a")


def test_a_boot_with_the_same_secret_keeps_the_session(hive, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    client, _db, secrets = hive
    token = _login(client, secrets, "a").json()["session_token"]
    script = Path(__file__).resolve().parents[2] / "scripts" / "enroll_bootstrap_agent.py"
    spec = importlib.util.spec_from_file_location("enroll_bootstrap_agent", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOOTSTRAP_AGENT_ID", "a")
    monkeypatch.setenv("BOOTSTRAP_AGENT_SECRET", secrets["a"])
    monkeypatch.setenv("HIVE_JWT_KEY", KEY)
    assert mod.main() == 0
    assert _heartbeat_answered(client, token, "a")
