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
