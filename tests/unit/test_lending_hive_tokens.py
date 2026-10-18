"""Lending slice 2, part B: the agent honours grants the hive holds (issue #19).

The hive puts the state of every grant naming an agent (epoch, revoked,
expiry) on that agent's heartbeat ack, and on nobody else's. The agent records
it, and accepts a hive-issued borrower token only when:
  - the token verifies under the agent's own enrolment secret and names it;
  - the recorded state is fresher than max_grant_state_age_seconds (data), so
    a hive the agent cannot hear from fails closed;
  - the grant is in that state, not revoked, at the token's epoch.
Scopes, domains and the mission quota then apply exactly as for agent-held
grants. A revoke that lands while a code is being redeemed issues no token.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 40
MISSION = "Calculate berth fee for a dry container"


class Queue:
    """Stands in for the comms worker: holds what POST /mission enqueues."""

    def __init__(self) -> None:
        self.profiles: dict[str, object] = {}

    def queue_depth(self) -> int:
        return len(self.profiles)

    def enqueue(self, profile) -> bool:
        self.profiles[profile.mission_id] = profile
        return True

    def is_queued(self, mission_id: str) -> bool:
        return mission_id in self.profiles

    def waiting_reason(self, mission_id: str):
        return None


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A hive with two enrolled agents, and agent-1's API wired to its store."""
    from agent import bootstrapper
    from agent.memory import LocalStore, SyncEngine
    from agent.swarm.registry import BlockRegistry
    from common.models.schemas import BlockDef
    from common.storage.db import open_db
    from domain_kits.compiler.engine import compile_sheet
    from hive import admin_cli

    hive_db = str(tmp_path / "hive.db")
    secrets = {a: admin_cli.enroll_agent(a, db_path=hive_db) for a in ("agent-1", "agent-2")}
    monkeypatch.setenv("AGENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_ID", "agent-1")
    monkeypatch.setenv("AGENT_SECRET", secrets["agent-1"])
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EVIDENCE_INTAKE_DIRS", str(tmp_path / "intake"))
    registry = BlockRegistry()
    compiled = compile_sheet(ROOT / "domain-kits" / "sheets" / "port_ops.yaml",
                             out_dir=tmp_path / "blocks", tests_out_dir=tmp_path / "kit-tests")
    for path in compiled["blocks"]:
        registry.register(BlockDef.model_validate(json.loads(Path(path).read_text(encoding="utf-8"))))
    store = LocalStore(str(tmp_path / "data" / "agent.db"))
    engine = SyncEngine(store, "agent-1", lambda: None)
    runtime = {"comms": Queue(), "store": store, "registry": registry}
    with TestClient(bootstrapper._create_api(runtime)) as client:
        yield {"client": client, "hive": open_db(hive_db), "engine": engine, "store": store, "secrets": secrets}


def _lend(w, agent_id="agent-1", scopes=("mission:submit", "mission:read"), max_missions=5):
    from hive import lending as hive_lending

    lent = hive_lending.create_grant(w["hive"], agent_id=agent_id, borrower="visiting team", scopes=list(scopes),
                                     domains=["port_terminal"], ttl_seconds=3600, max_missions=max_missions)
    token = hive_lending.redeem(w["hive"], lent["redeem_code"])["token"]
    return lent["grant_id"], {"Authorization": f"Bearer {token}"}


def _heartbeat(w, agent_id="agent-1"):
    """One heartbeat round trip: the hive's frames, handled by agent-1."""
    from hive import sync as hive_sync

    frames = hive_sync.heartbeat_response(w["hive"], agent_id, {})
    for frame in frames:
        w["engine"]._handle_message(frame.model_dump(mode="json"))
    return frames


def _mission(w, auth):
    return w["client"].post("/mission", headers=auth,
                            json={"natural_language": MISSION, "domain_hint": "port_terminal"})


def test_the_ack_carries_the_state_of_this_agents_grants_only(world):
    mine, _auth = _lend(world)
    theirs, _ = _lend(world, agent_id="agent-2")
    ack = _heartbeat(world)[-1]
    assert ack.msg_type == "ack"
    grants = ack.payload["grants"]
    assert mine in grants and theirs not in grants
    assert grants[mine]["revoked"] is False and grants[mine]["epoch"] == 0


def test_a_hive_token_opens_the_agent_once_the_state_is_heard(world):
    _gid, auth = _lend(world)
    assert _mission(world, auth).status_code == 401  # nothing heard from the hive yet: closed
    _heartbeat(world)
    assert _mission(world, auth).status_code == 200


def test_a_revoke_on_the_hive_closes_the_agent_on_the_next_heartbeat(world):
    from hive import lending as hive_lending

    gid, auth = _lend(world)
    _heartbeat(world)
    assert _mission(world, auth).status_code == 200
    hive_lending.revoke(world["hive"], gid)
    _heartbeat(world)
    assert _mission(world, auth).status_code == 401


def test_a_stale_state_fails_closed(world):
    from agent import lending
    from common.logging import OPERATIONAL_CONSTRAINTS

    _gid, auth = _lend(world)
    _heartbeat(world)
    age = int(OPERATIONAL_CONSTRAINTS["max_grant_state_age_seconds"])
    old = (datetime.now(timezone.utc) - timedelta(seconds=age + 1)).isoformat()
    world["store"].conn.execute(f"UPDATE {lending.HIVE_STATE_TABLE} SET heard_at = ?", (old,))
    world["store"].conn.commit()
    assert _mission(world, auth).status_code == 401


def test_a_token_for_another_agent_does_not_open_this_one(world):
    _gid, auth = _lend(world, agent_id="agent-2")
    _heartbeat(world)
    _heartbeat(world, agent_id="agent-2")
    assert _mission(world, auth).status_code == 401


def test_scopes_and_the_quota_apply_to_hive_grants(world):
    _gid, read_only = _lend(world, scopes=("mission:read",))
    _gid2, one = _lend(world, max_missions=1)
    _heartbeat(world)
    assert _mission(world, read_only).status_code == 403
    assert _mission(world, one).status_code == 200
    assert _mission(world, one).status_code == 403


def test_a_revoke_during_redeem_issues_no_token(world):
    """The revoke lands between the redeem's read of the grant and its write."""
    from hive import lending as hive_lending

    lent = hive_lending.create_grant(world["hive"], agent_id="agent-1", borrower="visiting team",
                                     scopes=["mission:submit"], domains=["port_terminal"],
                                     ttl_seconds=3600, max_missions=1)
    real = world["hive"]

    class RevokeAfterRead:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, params=()):
            cur = real.execute(sql, params)
            if sql.startswith("SELECT * FROM hive_grants"):
                row = cur.fetchone()
                hive_lending.revoke(real, lent["grant_id"])
                return type("Done", (), {"fetchone": staticmethod(lambda: row)})()
            return cur

    with pytest.raises(hive_lending.GrantRefused):
        hive_lending.redeem(RevokeAfterRead(), lent["redeem_code"])


# --- Each refusal on its own (grade of #21) ---------------------------------

def _state(w, grant_id, **over):
    """Record the hive's state of one grant, with fields overridden."""
    from agent import lending
    from hive import lending as hive_lending

    state = {**hive_lending.grant_state(w["hive"], "agent-1")[grant_id], **over}
    lending.record_hive_state(w["store"].conn, {grant_id: state})


def _forged(w, grant_id, **claims):
    """A token signed with agent-1's own secret, with chosen claims."""
    from common import lending_tokens

    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    base = {"sub": f"grant:{grant_id}", "grant": grant_id, "agent": "agent-1", "borrower": "visiting team",
            "scope": ["mission:submit"], "domains": ["port_terminal"], "max_missions": 5, "epoch": 0, "exp": exp}
    token = lending_tokens.encode({**base, **claims}, w["secrets"]["agent-1"])
    return {"Authorization": f"Bearer {token}"}


def test_an_epoch_behind_the_hive_is_refused_while_the_grant_is_not_revoked(world):
    gid, auth = _lend(world)
    _state(world, gid, epoch=1, revoked=False)
    assert _mission(world, auth).status_code == 401


def test_a_grant_the_hive_says_has_expired_is_refused(world):
    gid, auth = _lend(world)
    _state(world, gid, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    assert _mission(world, auth).status_code == 401


def test_a_token_naming_another_agent_is_refused_even_when_its_signature_verifies(world):
    gid, _auth = _lend(world)
    _heartbeat(world)
    assert _mission(world, _forged(world, gid)).status_code == 200  # the forge is otherwise sound
    gid2, _ = _lend(world)
    _heartbeat(world)
    assert _mission(world, _forged(world, gid2, agent="agent-2")).status_code == 401


def test_a_token_opens_only_the_path_whose_key_signed_it(world):
    from agent import lending

    _gid, hive_auth = _lend(world)
    _heartbeat(world)
    lent = world["client"].post("/lend", headers={"Authorization": f"Bearer {TOKEN}"}, json={
        "borrower": "visiting team", "scopes": ["mission:submit"], "domains": ["port_terminal"],
        "ttl_seconds": 3600, "max_missions": 5}).json()
    local = world["client"].post("/lend/redeem", json={"code": lent["redeem_code"]}).json()["token"]
    hive_token = hive_auth["Authorization"].split(" ", 1)[1]
    conn = world["store"].conn
    with pytest.raises(lending.GrantRefused):
        lending.verify_hive(conn, local, world["secrets"]["agent-1"], "agent-1")
    with pytest.raises(lending.GrantRefused):
        lending.verify(conn, hive_token, TOKEN)


def test_a_hive_grant_cannot_take_over_an_agent_held_grant_id(world):
    from agent import lending

    lent = world["client"].post("/lend", headers={"Authorization": f"Bearer {TOKEN}"}, json={
        "borrower": "visiting team", "scopes": ["mission:submit"], "domains": ["port_terminal"],
        "ttl_seconds": 3600, "max_missions": 1}).json()
    local_id = lent["grant_id"]
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    lending.record_hive_state(world["store"].conn,
                              {local_id: {"epoch": 0, "revoked": False, "expires_at": expires}})
    assert _mission(world, _forged(world, local_id, max_missions=1000)).status_code == 401
    row = world["store"].conn.execute(
        "SELECT max_missions, missions_used, redeem_hash FROM grants WHERE grant_id = ?", (local_id,)).fetchone()
    assert tuple(row)[:2] == (1, 0) and not str(row[2]).startswith("hive:")


def test_a_hive_grant_opens_only_its_own_domains(world):
    _gid, auth = _lend(world)
    _heartbeat(world)
    r = world["client"].post("/mission", headers=auth,
                             json={"natural_language": MISSION, "domain_hint": "some_other_domain"})
    assert r.status_code == 403


@pytest.mark.parametrize("grants", ["not a dict", {}, {"g": {"epoch": "x", "revoked": False}}])
def test_an_ack_that_carries_no_usable_state_opens_nothing(world, grants):
    gid, auth = _lend(world)
    bad = grants if not isinstance(grants, dict) or "g" not in grants else {gid: grants["g"]}
    world["engine"]._handle_message({"msg_type": "ack", "session_id": "agent-1", "payload": {"grants": bad},
                                     "vector_clock": {}})
    assert _mission(world, auth).status_code == 401


def test_an_empty_ack_withdraws_what_an_earlier_one_granted(world):
    _gid, auth = _lend(world)
    _heartbeat(world)
    assert _mission(world, auth).status_code == 200
    world["engine"]._handle_message({"msg_type": "ack", "session_id": "agent-1", "payload": {"grants": {}},
                                     "vector_clock": {}})
    assert _mission(world, auth).status_code == 401


def test_a_revoked_flag_is_refused_on_its_own_at_the_same_epoch(world):
    gid, auth = _lend(world)
    _state(world, gid, epoch=0, revoked=True)
    assert _mission(world, auth).status_code == 401
