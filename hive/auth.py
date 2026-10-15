"""Challenge-response authentication and JWT session issuance."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt

from common.models.schemas import AuthChallenge, AuthProof, BlockDef, MemoryDelta, SessionGrant

AUTH_FAIL_DETAIL = "unauthorized"
NONCE_TTL_SECONDS = 60
SESSION_TTL_SECONDS = 24 * 60 * 60
CHALLENGE_RATE_LIMIT = 10
CHALLENGE_RATE_WINDOW = 60.0

#: The one signing algorithm: tokens are issued with it and decoded only with it.
JWT_ALGORITHM = "HS256"


class ChallengeRateLimiter:
    """Sliding-window limiter: CHALLENGE_RATE_LIMIT challenges per window per
    key. A request is charged to every key it names (the agent_id it claims
    and the client address it comes from), and refused when any key is spent:
    rotating agent ids from one address does not reset the budget."""

    def __init__(self, limit: int = CHALLENGE_RATE_LIMIT, window: float = CHALLENGE_RATE_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, *keys: str) -> bool:
        now = time.monotonic()
        for stale in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
            del self._hits[stale]  # forget idle keys: the table stays bounded
        queues = [self._hits[k] for k in keys]
        for q in queues:
            while q and (now - q[0]) > self.window:
                q.popleft()
        if any(len(q) >= self.limit for q in queues):
            return False
        for q in queues:
            q.append(now)
        return True


def require_jwt_key(jwt_key: Optional[str]) -> str:
    if jwt_key is None or not str(jwt_key).strip() or len(str(jwt_key)) < 32:
        raise RuntimeError(
            "HIVE_JWT_KEY must be set, non-empty, and at least 32 characters"
        )
    return str(jwt_key)


def issue_challenge(conn: sqlite3.Connection, agent_id: str) -> AuthChallenge:
    nonce = secrets.token_hex(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=NONCE_TTL_SECONDS)
    # Prune as we go: a used or expired nonce can never be redeemed again.
    conn.execute("DELETE FROM nonces WHERE used = 1 OR expires_at < ?", (now.isoformat(),))
    conn.execute(
        "INSERT INTO nonces(nonce, agent_id, expires_at, used) VALUES (?, ?, ?, 0)",
        (nonce, agent_id, expires_at.isoformat()),
    )
    conn.commit()
    return AuthChallenge(agent_id=agent_id, nonce=nonce, expires_at=expires_at)


def _load_memory_dump(conn: sqlite3.Connection) -> list[MemoryDelta]:
    rows = conn.execute(
        "SELECT delta_id, key, value, origin, vector_clock, operation, timestamp "
        "FROM memory_deltas ORDER BY timestamp ASC"
    ).fetchall()
    dump: list[MemoryDelta] = []
    for row in rows:
        value = json.loads(row["value"]) if row["value"] is not None else None
        clock = json.loads(row["vector_clock"])
        ts_raw = row["timestamp"]
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw)
        else:
            ts = datetime.now(timezone.utc)
        dump.append(
            MemoryDelta(
                delta_id=row["delta_id"],
                key=row["key"],
                value=value,
                origin=row["origin"],
                vector_clock=clock,
                operation=row["operation"],
                timestamp=ts,
            )
        )
    return dump


def _load_blocks(conn: sqlite3.Connection) -> list[BlockDef]:
    rows = conn.execute(
        "SELECT def_json FROM block_registry ORDER BY block_id ASC"
    ).fetchall()
    return [BlockDef.model_validate(json.loads(r["def_json"])) for r in rows]


def mint_session_token(
    agent_id: str,
    jwt_key: str,
    *,
    role: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": agent_id,
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
    }
    if role is not None:
        payload["role"] = role
    if session_id is not None:
        payload["jti"] = session_id
    return jwt.encode(payload, jwt_key, algorithm=JWT_ALGORITHM)


def set_agent_secret(conn: sqlite3.Connection, agent_id: str, secret: str, enrolled_at: str) -> None:
    """Enrol ``agent_id`` with ``secret``. A changed secret ends every session
    issued under the old one (and its lease); the same secret keeps them."""
    row = conn.execute("SELECT secret FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO agents(agent_id, secret, enrolled_at, active) VALUES (?, ?, ?, 0)",
            (agent_id, secret, enrolled_at),
        )
    elif hmac.compare_digest(str(row["secret"]), secret):
        conn.execute("UPDATE agents SET enrolled_at = ? WHERE agent_id = ?", (enrolled_at, agent_id))
    else:
        conn.execute(
            "UPDATE agents SET secret = ?, enrolled_at = ?, active = 0, active_until = NULL, "
            "session_id = NULL WHERE agent_id = ?",
            (secret, enrolled_at, agent_id),
        )
    conn.commit()


def is_current_session(conn: sqlite3.Connection, agent_id: str, session_id: Optional[str]) -> bool:
    """True only while ``session_id`` is the agent's current session: a new
    login replaces it and re-enrolment clears it."""
    if not session_id:
        return False
    row = conn.execute("SELECT session_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return row is not None and row["session_id"] is not None and hmac.compare_digest(
        str(row["session_id"]), str(session_id)
    )


def release_agent(conn: sqlite3.Connection, agent_id: str, session_id: Optional[str]) -> None:
    """End an agent's hold on the single-agent lock when the socket that closed
    belonged to its current session; a stale session releases nothing."""
    if not session_id:
        return
    conn.execute(
        "UPDATE agents SET active = 0, active_until = NULL, session_id = NULL "
        "WHERE agent_id = ? AND session_id = ?",
        (agent_id, session_id),
    )
    conn.commit()


def admin_epoch(conn: sqlite3.Connection) -> int:
    """The current admin token epoch (0 until the first revocation)."""
    row = conn.execute("SELECT value FROM hive_settings WHERE key = 'admin_epoch'").fetchone()
    return int(row["value"]) if row is not None else 0


def advance_admin_epoch(conn: sqlite3.Connection) -> int:
    """Revoke every admin token issued so far: tokens carry the epoch they were
    minted under and are accepted only while it is current."""
    new = admin_epoch(conn) + 1
    conn.execute(
        "INSERT INTO hive_settings(key, value) VALUES ('admin_epoch', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(new),),
    )
    conn.commit()
    return new


def mint_admin_token(jwt_key: str, ttl_seconds: int = 3600, epoch: int = 0) -> str:
    key = require_jwt_key(jwt_key)
    now = int(time.time())
    return jwt.encode(
        {"sub": "admin", "role": "admin", "epoch": int(epoch), "iat": now, "exp": now + ttl_seconds},
        key,
        algorithm=JWT_ALGORITHM,
    )


def verify_and_grant(
    conn: sqlite3.Connection,
    proof: AuthProof,
    jwt_key: str,
) -> tuple[Optional[SessionGrant], int, str]:
    """Return (grant, status_code, detail). status 200 on success."""
    now = datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT agent_id, expires_at, used FROM nonces WHERE nonce = ?",
        (proof.nonce,),
    ).fetchone()
    if row is None:
        return None, 401, AUTH_FAIL_DETAIL
    if int(row["used"]) == 1:
        return None, 401, AUTH_FAIL_DETAIL
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        return None, 401, AUTH_FAIL_DETAIL
    if row["agent_id"] != proof.agent_id:
        return None, 401, AUTH_FAIL_DETAIL

    conn.execute("UPDATE nonces SET used = 1 WHERE nonce = ?", (proof.nonce,))
    conn.commit()

    agent = conn.execute(
        "SELECT agent_id, secret, active FROM agents WHERE agent_id = ?",
        (proof.agent_id,),
    ).fetchone()
    if agent is None:
        return None, 401, AUTH_FAIL_DETAIL

    expected = hmac.new(agent["secret"].encode(), proof.nonce.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, proof.proof):
        return None, 401, AUTH_FAIL_DETAIL

    now_iso = datetime.now(timezone.utc).isoformat()
    other = conn.execute(
        "SELECT agent_id FROM agents WHERE active = 1 AND agent_id != ? "
        "AND (active_until IS NULL OR active_until > ?)",
        (proof.agent_id, now_iso),
    ).fetchone()
    if other is not None:
        return None, 409, "single-agent limit: another agent is active"

    # Everything the grant serves is built first: a grant that cannot be built
    # takes no lease, so it never locks the agent (or anyone) out.
    memory_dump = _load_memory_dump(conn)
    blocks = _load_blocks(conn)

    lease_end = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)
    session_id = secrets.token_hex(16)
    conn.execute(
        "UPDATE agents SET active = 1, active_until = ?, session_id = ? WHERE agent_id = ?",
        (lease_end.isoformat(), session_id, proof.agent_id),
    )
    conn.commit()

    token = mint_session_token(proof.agent_id, jwt_key, session_id=session_id)
    grant = SessionGrant(session_token=token, memory_dump=memory_dump, blocks=blocks)
    return grant, 200, "ok"
