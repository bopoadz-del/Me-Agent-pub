"""Lending slice 2, part A: grants held by the hive (issue #19).

An admin lends an enrolled agent: POST /lend names the agent, the borrower,
scopes, domains, a window and a quota, and returns a one-time redeem code
(only its hash is stored). POST /lend/redeem turns the code into a borrower
token that names its agent and is signed with a key derived from THAT agent's
enrolment secret: the agent can verify it, no other agent can, and the agent
never holds the hive's key. Revoking advances the grant's epoch. Field rules
and the token format are shared with the agent (common/lending_tokens.py).
"""
from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from common.models.schemas import AuthProof
from hive import admin_cli
from hive import auth as hive_auth

KEY = "k" * 40
GRANT = {"agent_id": "agent-1", "borrower": "visiting team", "scopes": ["mission:submit", "mission:read"],
         "domains": ["port_terminal"], "ttl_seconds": 3600, "max_missions": 3}


@pytest.fixture
def hive(tmp_path):
    from hive.main import create_app

    db = str(tmp_path / "hive.db")
    secrets = {a: admin_cli.enroll_agent(a, db_path=db) for a in ("agent-1", "agent-2")}
    app = create_app(db_path=db, jwt_key=KEY)
    admin = {"Authorization": f"Bearer {hive_auth.mint_admin_token(KEY, epoch=hive_auth.admin_epoch(app.state.db))}"}
    with TestClient(app) as client:
        yield client, app, admin, secrets


def _agent_session(app, secrets, agent_id="agent-1") -> dict[str, str]:
    nonce = hive_auth.issue_challenge(app.state.db, agent_id).nonce
    proof = hmac.new(secrets[agent_id].encode(), nonce.encode(), hashlib.sha256).hexdigest()
    grant, status, _ = hive_auth.verify_and_grant(
        app.state.db, AuthProof(agent_id=agent_id, nonce=nonce, proof=proof), KEY
    )
    assert status == 200
    return {"Authorization": f"Bearer {grant.session_token}"}


def test_an_admin_lends_an_enrolled_agent_and_only_the_code_hash_is_stored(hive):
    client, app, admin, _secrets = hive
    r = client.post("/lend", json=GRANT, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["grant_id"] and body["redeem_code"] and body["agent_id"] == "agent-1"
    assert body["redeem_code"] not in "\n".join(str(row) for row in app.state.db.iterdump())


def test_lending_takes_the_admin_role(hive):
    client, app, _admin, secrets = hive
    assert client.post("/lend", json=GRANT).status_code == 401
    assert client.post("/lend", json=GRANT, headers=_agent_session(app, secrets)).status_code == 403


def test_an_agent_that_is_not_enrolled_cannot_be_lent(hive):
    client, _app, admin, _secrets = hive
    assert client.post("/lend", json={**GRANT, "agent_id": "nobody"}, headers=admin).status_code == 422


@pytest.mark.parametrize("over", [{"scopes": ["admin"]}, {"domains": []}, {"ttl_seconds": 0}, {"max_missions": 0}])
def test_the_shared_field_rules_apply(hive, over):
    client, _app, admin, _secrets = hive
    assert client.post("/lend", json={**GRANT, **over}, headers=admin).status_code == 422


def test_the_token_names_its_agent_and_only_that_agents_secret_verifies_it(hive):
    from common import lending_tokens

    client, _app, admin, secrets = hive
    code = client.post("/lend", json=GRANT, headers=admin).json()["redeem_code"]
    r = client.post("/lend/redeem", json={"code": code})
    assert r.status_code == 200
    claims = lending_tokens.decode(r.json()["token"], secrets["agent-1"])
    assert claims["agent"] == "agent-1" and claims["role"] == "borrower"
    with pytest.raises(lending_tokens.TokenRefused):
        lending_tokens.decode(r.json()["token"], secrets["agent-2"])
    with pytest.raises(lending_tokens.TokenRefused):
        lending_tokens.decode(r.json()["token"], KEY)


def test_a_code_redeems_once(hive):
    client, _app, admin, _secrets = hive
    code = client.post("/lend", json=GRANT, headers=admin).json()["redeem_code"]
    assert client.post("/lend/redeem", json={"code": code}).status_code == 200
    assert client.post("/lend/redeem", json={"code": code}).status_code == 403


def test_revoking_is_admin_only_and_advances_the_epoch(hive):
    client, app, admin, secrets = hive
    lent = client.post("/lend", json=GRANT, headers=admin).json()
    assert client.post(f"/lend/{lent['grant_id']}/revoke", headers=_agent_session(app, secrets)).status_code == 403
    assert client.post(f"/lend/{lent['grant_id']}/revoke", headers=admin).status_code == 200
    assert client.post(f"/lend/{lent['grant_id']}/revoke", headers=admin).status_code == 200
    assert client.post("/lend/nope/revoke", headers=admin).status_code == 404
    assert client.post("/lend/redeem", json={"code": lent["redeem_code"]}).status_code == 403
