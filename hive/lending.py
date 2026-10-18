"""Lending, slice 2 (docs/LENDING.md, issue #19): grants held by the hive.

An admin lends one enrolled agent. The grant row names the agent; the one-time
redeem code is stored only as its hash. Redeeming signs a borrower token with
a key derived from THAT agent's enrolment secret (common/lending_tokens.py):
the agent can verify it, no other agent can, and no agent holds the hive's
key. Revoking advances the grant's epoch. Field rules and bounds are shared
with agent-held grants.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from common import lending_tokens
from common.logging import OPERATIONAL_CONSTRAINTS


class GrantRefused(Exception):
    """A code that does not stand for a live grant."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hive_grants (
            grant_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            borrower TEXT NOT NULL,
            scopes TEXT NOT NULL,
            domains TEXT NOT NULL,
            not_before TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            max_missions INTEGER NOT NULL,
            redeem_hash TEXT NOT NULL UNIQUE,
            redeemed INTEGER NOT NULL DEFAULT 0,
            epoch INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT
        )
        """
    )
    conn.commit()


def _known_domains(conn: sqlite3.Connection) -> Optional[set[str]]:
    """The kit domains of the blocks the hive serves; None while it serves none."""
    domains = {json.loads(row[0]).get("domain") for row in conn.execute("SELECT def_json FROM block_registry")}
    domains.discard(None)
    return domains or None


def create_grant(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    borrower: str,
    scopes: list[str],
    domains: list[str],
    ttl_seconds: int,
    max_missions: int,
) -> dict[str, str]:
    """Store a grant for an enrolled agent; return its id, agent, the one-time
    redeem code and its expiry. ValueError for an unknown agent or a field
    outside its rule."""
    if conn.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone() is None:
        raise ValueError(f"agent is not enrolled: {agent_id}")
    lending_tokens.check_fields(scopes, domains, ttl_seconds, max_missions, _known_domains(conn))
    ensure_schema(conn)
    now = _now()
    grant_id = uuid.uuid4().hex
    code = secrets.token_urlsafe(24)
    expires_at = now + timedelta(seconds=int(ttl_seconds))
    conn.execute(
        "INSERT INTO hive_grants(grant_id, agent_id, borrower, scopes, domains, not_before, expires_at, "
        "max_missions, redeem_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (grant_id, agent_id, borrower, json.dumps(sorted(set(scopes))), json.dumps(domains), now.isoformat(),
         expires_at.isoformat(), int(max_missions), _hash(code)),
    )
    conn.commit()
    return {"grant_id": grant_id, "agent_id": agent_id, "redeem_code": code, "expires_at": expires_at.isoformat()}


def _live(row: Any, now: datetime) -> bool:
    return (
        row is not None
        and row["revoked_at"] is None
        and datetime.fromisoformat(row["not_before"]) <= now < datetime.fromisoformat(row["expires_at"])
    )


def redeem(conn: sqlite3.Connection, code: str) -> dict[str, str]:
    """Turn a one-time code into a borrower token for the grant's agent.
    GrantRefused for an unknown, used, revoked or out-of-window code."""
    ensure_schema(conn)
    now = _now()
    row = conn.execute("SELECT * FROM hive_grants WHERE redeem_hash = ?", (_hash(code),)).fetchone()
    if not _live(row, now):
        raise GrantRefused("code does not stand for a live grant")
    agent = conn.execute("SELECT secret FROM agents WHERE agent_id = ?", (row["agent_id"],)).fetchone()
    if agent is None:
        raise GrantRefused("the grant's agent is no longer enrolled")
    # The write re-checks what the read saw: a revoke landing in between wins.
    took = conn.execute(
        "UPDATE hive_grants SET redeemed = 1 "
        "WHERE grant_id = ? AND redeemed = 0 AND revoked_at IS NULL AND epoch = ?",
        (row["grant_id"], row["epoch"]),
    ).rowcount
    conn.commit()
    if took != 1:
        raise GrantRefused("code already redeemed or revoked")
    ttl = int(OPERATIONAL_CONSTRAINTS["borrower_token_ttl_seconds"])
    exp = min(now + timedelta(seconds=ttl), datetime.fromisoformat(row["expires_at"]))
    token = lending_tokens.encode(
        {
            "sub": f"grant:{row['grant_id']}",
            "grant": row["grant_id"],
            "agent": row["agent_id"],
            "borrower": row["borrower"],
            "scope": json.loads(row["scopes"]),
            "domains": json.loads(row["domains"]),
            "max_missions": row["max_missions"],
            "epoch": row["epoch"],
            "jti": uuid.uuid4().hex,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        },
        str(agent["secret"]),
    )
    return {"token": token, "expires_at": exp.isoformat()}


def grant_state(conn: sqlite3.Connection, agent_id: str) -> dict[str, dict[str, Any]]:
    """What ``agent_id`` needs to honour its borrowers' tokens: epoch, revoked
    and expiry of every unexpired grant naming it, keyed by grant id."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT grant_id, epoch, revoked_at, expires_at FROM hive_grants WHERE agent_id = ? AND expires_at > ?",
        (agent_id, _now().isoformat()),
    ).fetchall()
    return {
        row["grant_id"]: {"epoch": row["epoch"], "revoked": row["revoked_at"] is not None,
                          "expires_at": row["expires_at"]}
        for row in rows
    }


def revoke(conn: sqlite3.Connection, grant_id: str) -> bool:
    """End a grant and every token it issued; False when there is no such grant."""
    ensure_schema(conn)
    changed = conn.execute(
        "UPDATE hive_grants SET revoked_at = COALESCE(revoked_at, ?), epoch = epoch + 1 WHERE grant_id = ?",
        (_now().isoformat(), grant_id),
    ).rowcount
    conn.commit()
    return changed == 1
