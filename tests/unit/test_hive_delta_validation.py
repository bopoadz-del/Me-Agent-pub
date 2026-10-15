"""Phase 2e: the hive stores only well-formed memory deltas, and an agent's
clock cannot claim events that never happened.

handle_agent_delta stored whatever an agent session sent. A delta the
MemoryDelta model rejects (a list value, an unknown operation) made every
later /auth/prove fail building the memory dump -- after the lease had been
committed, so the agent row stayed locked and no one could log in. A clock
claiming a huge "hive" (or any other node's) component became the hive's own
clock from then on, and every later honest write lost to it.

Now an inbound delta is validated as a MemoryDelta (bounded key, upsert or
delete, an object or no value) and refused otherwise; an agent may advance
its own clock component, while every other component is capped at what the
hive has actually issued; and the grant is built before the lease is taken.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from common.models.schemas import AuthProof
from common.storage.db import open_db
from hive import admin_cli
from hive import auth as hive_auth
from hive import sync as hive_sync

KEY = "k" * 40


@pytest.fixture()
def hive_db(tmp_path):
    db = str(tmp_path / "hive.db")
    secret = admin_cli.enroll_agent("a1", db_path=db)
    return open_db(db), secret


def _login(conn, secret):
    nonce = hive_auth.issue_challenge(conn, "a1").nonce
    proof = hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return hive_auth.verify_and_grant(conn, AuthProof(agent_id="a1", nonce=nonce, proof=proof), KEY)


def _stored(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0]


@pytest.mark.parametrize(
    "payload",
    [
        {"key": "k", "value": [1, 2], "operation": "upsert"},
        {"key": "k", "value": {"v": 1}, "operation": "drop"},
        {"key": "", "value": {"v": 1}},
        {"key": "k" * 257, "value": {"v": 1}},
        {"value": {"v": 1}},
    ],
)
def test_a_malformed_delta_is_refused_and_logins_still_work(hive_db, payload):
    conn, secret = hive_db
    out = hive_sync.handle_agent_delta(conn, "a1", payload, {"a1": 1})
    assert _stored(conn) == 0
    assert out and "refused" in json.dumps(out[0].payload)
    assert _login(conn, secret)[1] == 200


def test_a_well_formed_delta_is_stored_with_the_agents_own_clock(hive_db):
    conn, _secret = hive_db
    hive_sync.handle_agent_delta(conn, "a1", {"key": "k", "value": {"v": 1}}, {"a1": 5})
    assert _stored(conn) == 1
    row = conn.execute("SELECT vector_clock FROM memory_deltas").fetchone()
    assert json.loads(row["vector_clock"])["a1"] == 5


def test_an_agent_cannot_claim_hive_events_that_never_happened(hive_db):
    conn, _secret = hive_db
    hive_sync.handle_agent_delta(conn, "a1", {"key": "k", "value": {"v": 1}}, {"a1": 1, "hive": 10**12})
    assert hive_sync.hive_vector_clock(conn)["hive"] <= 1


def test_an_inflated_clock_does_not_make_honest_writes_lose(hive_db):
    """Inflating the hive or another agent's component once must not leave every
    later honest write unable to dominate the stored state."""
    conn, _secret = hive_db
    hive_sync.handle_agent_delta(
        conn, "a1", {"key": "k", "value": {"v": "forged"}}, {"a1": 1, "a2": 10**9, "hive": 10**9}
    )
    seen = hive_sync.hive_vector_clock(conn)  # what an honest agent learns from a heartbeat
    hive_sync.handle_agent_delta(conn, "a2", {"key": "k", "value": {"v": "honest"}}, {**seen, "a2": seen.get("a2", 0) + 1})
    assert hive_sync.latest_state_for_key(conn, "k")["value"] == {"v": "honest"}


def test_a_dump_that_cannot_be_built_takes_no_lease(hive_db):
    conn, secret = hive_db
    conn.execute(
        "INSERT INTO memory_deltas(delta_id, key, value, origin, vector_clock, operation, timestamp) "
        "VALUES ('d1', 'k', '[1]', 'a1', '{\"a1\": 1}', 'drop', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    with pytest.raises(Exception):
        _login(conn, secret)
    active = conn.execute("SELECT active FROM agents WHERE agent_id = 'a1'").fetchone()[0]
    assert active == 0
