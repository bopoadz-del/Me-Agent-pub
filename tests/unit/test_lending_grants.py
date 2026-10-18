"""Lending slice 1, part A: the grant lifecycle on the agent (issue #12).

The owner (AGENT_API_TOKEN) creates a grant and gets a one-time redeem code;
only the code's hash is stored. The code redeems once for a short-lived
borrower token, signed by the agent. A token is good only while its grant is
inside its window, unrevoked and on the same revocation epoch. Every limit is
data (INPUT_BOUNDS). Every route the agent serves has a row in the
route-access table: public, owner-only, or the scope a borrower needs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

TOKEN = "t" * 40
OWNER = {"Authorization": f"Bearer {TOKEN}"}
GRANT = {"borrower": "visiting team", "scopes": ["mission:submit", "mission:read"], "domains": ["port_terminal"],
         "ttl_seconds": 3600, "max_missions": 3}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    from agent import bootstrapper
    from agent.memory import LocalStore

    monkeypatch.setenv("AGENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    store = LocalStore(str(tmp_path / "data" / "agent.db"))
    with TestClient(bootstrapper._create_api({"comms": None, "store": store})) as client:
        yield client, store


def _lend(client, **over):
    return client.post("/lend", json={**GRANT, **over}, headers=OWNER)


def test_the_owner_creates_a_grant_and_only_the_code_hash_is_stored(agent):
    client, store = agent
    r = _lend(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["grant_id"] and body["redeem_code"] and body["expires_at"]
    dump = "\n".join(str(row) for row in store.conn.iterdump())
    assert body["redeem_code"] not in dump


def test_lending_is_owner_only(agent):
    client, _store = agent
    assert client.post("/lend", json=GRANT).status_code == 401
    code = _lend(client).json()["redeem_code"]
    borrower = client.post("/lend/redeem", json={"code": code}).json()["token"]
    assert client.post("/lend", json=GRANT, headers={"Authorization": f"Bearer {borrower}"}).status_code == 401


def test_a_code_redeems_once(agent):
    client, _store = agent
    code = _lend(client).json()["redeem_code"]
    first = client.post("/lend/redeem", json={"code": code})
    assert first.status_code == 200 and first.json()["token"]
    assert client.post("/lend/redeem", json={"code": code}).status_code == 403
    assert client.post("/lend/redeem", json={"code": "not-a-code"}).status_code == 403


def test_a_token_carries_its_grant_and_verifies(agent):
    from agent import lending

    client, store = agent
    lent = _lend(client).json()
    token = client.post("/lend/redeem", json={"code": lent["redeem_code"]}).json()["token"]
    grant = lending.verify(store.conn, token, TOKEN)
    assert grant.grant_id == lent["grant_id"]
    assert set(grant.scopes) == set(GRANT["scopes"]) and grant.domains == GRANT["domains"]


def test_a_token_signed_with_another_key_is_refused(agent):
    from agent import lending

    client, store = agent
    token = client.post("/lend/redeem", json={"code": _lend(client).json()["redeem_code"]}).json()["token"]
    with pytest.raises(lending.GrantRefused):
        lending.verify(store.conn, token, "k" * 40)


def test_revoking_ends_every_token_of_the_grant(agent):
    from agent import lending

    client, store = agent
    lent = _lend(client).json()
    token = client.post("/lend/redeem", json={"code": lent["redeem_code"]}).json()["token"]
    assert client.post(f"/lend/{lent['grant_id']}/revoke", headers=OWNER).status_code == 200
    with pytest.raises(lending.GrantRefused):
        lending.verify(store.conn, token, TOKEN)
    assert client.post("/lend/unknown/revoke", headers=OWNER).status_code == 404


def test_a_code_or_token_past_the_window_is_refused(agent, monkeypatch):
    from agent import lending

    client, store = agent
    later = datetime.now(timezone.utc) + timedelta(seconds=GRANT["ttl_seconds"] + 5)
    lent = _lend(client).json()
    token = client.post("/lend/redeem", json={"code": lent["redeem_code"]}).json()["token"]
    other = _lend(client).json()["redeem_code"]
    monkeypatch.setattr(lending, "_now", lambda: later)
    with pytest.raises(lending.GrantRefused):
        lending.verify(store.conn, token, TOKEN)
    assert client.post("/lend/redeem", json={"code": other}).status_code == 403


def test_a_borrower_token_outlives_neither_its_ttl_nor_its_grant(agent, monkeypatch):
    from common.logging import OPERATIONAL_CONSTRAINTS

    client, _store = agent
    monkeypatch.setitem(OPERATIONAL_CONSTRAINTS, "borrower_token_ttl_seconds", 60)
    lent = _lend(client).json()
    body = client.post("/lend/redeem", json={"code": lent["redeem_code"]}).json()
    granted_until = datetime.fromisoformat(lent["expires_at"])
    token_until = datetime.fromisoformat(body["expires_at"])
    assert token_until <= granted_until
    assert token_until <= datetime.now(timezone.utc) + timedelta(seconds=61)


@pytest.mark.parametrize("over", [
    {"scopes": ["mission:submit", "admin"]},
    {"scopes": []},
    {"domains": "port_terminal"},
    {"ttl_seconds": 0},
    {"max_missions": 0},
])
def test_a_malformed_grant_is_refused(agent, over):
    client, _store = agent
    assert _lend(client, **over).status_code == 422


def test_the_grant_bounds_are_data(agent, monkeypatch):
    from common.logging import OPERATIONAL_CONSTRAINTS

    client, _store = agent
    monkeypatch.setitem(OPERATIONAL_CONSTRAINTS, "max_grant_ttl_seconds", 100)
    monkeypatch.setitem(OPERATIONAL_CONSTRAINTS, "max_grant_missions", 2)
    assert _lend(client, ttl_seconds=101).status_code == 422
    assert _lend(client, max_missions=3).status_code == 422
    assert _lend(client, ttl_seconds=100, max_missions=2).status_code == 200


def test_every_route_has_an_access_row(agent):
    from agent import lending

    client, _store = agent
    served = {(m, r.path) for r in client.app.routes if hasattr(r, "methods") for m in r.methods
              if m not in ("HEAD", "OPTIONS") and not r.path.startswith(("/docs", "/openapi", "/redoc"))}
    missing = served - set(lending.ROUTE_ACCESS)
    assert not missing, sorted(missing)
    stale = set(lending.ROUTE_ACCESS) - served
    assert not stale, sorted(stale)
    allowed = {"public", "owner", *lending.SCOPES}
    assert set(lending.ROUTE_ACCESS.values()) <= allowed
