"""Lending, slice 1 (docs/LENDING.md, issue #12): grants held by the agent.

An owner creates a grant (scopes, domains, window, mission quota); the reply
carries a one-time redeem code, of which only the hash is stored. The code
redeems once for a short-lived borrower token, signed with the agent's owner
token. A token is good only while its grant is inside its window, unrevoked
and on the same revocation epoch. Every bound is data (INPUT_BOUNDS).

In this slice the grants live in the agent's own store and the agent signs and
verifies borrower tokens itself; moving them to the hive (asymmetric signing)
is a later slice.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from common.logging import OPERATIONAL_CONSTRAINTS

#: What a grant can let a borrower do.
SCOPES = ("mission:submit", "mission:read", "evidence:upload", "evidence:read")

#: Every route the agent serves: open to anyone, owner-only, or the scope a
#: borrower needs. A route missing here fails tests/unit/test_lending_grants.py.
ROUTE_ACCESS: dict[tuple[str, str], str] = {
    ("GET", "/health"): "public",
    ("POST", "/mission"): "mission:submit",
    ("GET", "/mission/{mission_id}"): "mission:read",
    ("POST", "/evidence/{name:path}"): "evidence:upload",
    ("GET", "/evidence"): "evidence:read",
    ("GET", "/evidence/{ref:path}"): "evidence:read",
    ("POST", "/lend"): "owner",
    ("POST", "/lend/redeem"): "public",
    ("POST", "/lend/{grant_id}/revoke"): "owner",
}

_ALGORITHM = "HS256"


class GrantRefused(Exception):
    """A code or token that does not stand for a live grant."""


@dataclass(frozen=True)
class Grant:
    grant_id: str
    borrower: str
    scopes: tuple[str, ...]
    domains: list[str]
    expires_at: datetime
    max_missions: int
    missions_used: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grants (
            grant_id TEXT PRIMARY KEY,
            borrower TEXT NOT NULL,
            scopes TEXT NOT NULL,
            domains TEXT NOT NULL,
            not_before TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            max_missions INTEGER NOT NULL,
            missions_used INTEGER NOT NULL DEFAULT 0,
            redeem_hash TEXT NOT NULL UNIQUE,
            redeemed INTEGER NOT NULL DEFAULT 0,
            epoch INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT
        )
        """
    )
    conn.commit()


def create_grant(
    conn: sqlite3.Connection,
    *,
    borrower: str,
    scopes: list[str],
    domains: list[str],
    ttl_seconds: int,
    max_missions: int,
) -> dict[str, str]:
    """Store a grant; return its id, the one-time redeem code and its expiry.
    ValueError when a field is outside its bound."""
    if not isinstance(scopes, list) or not scopes or not set(scopes) <= set(SCOPES):
        raise ValueError(f"scopes must be a non-empty subset of {list(SCOPES)}")
    if not isinstance(domains, list) or not all(isinstance(d, str) and d for d in domains):
        raise ValueError("domains must be a list of kit domain names")
    if not 1 <= int(ttl_seconds) <= int(OPERATIONAL_CONSTRAINTS["max_grant_ttl_seconds"]):
        raise ValueError("ttl_seconds is outside 1..max_grant_ttl_seconds")
    if not 1 <= int(max_missions) <= int(OPERATIONAL_CONSTRAINTS["max_grant_missions"]):
        raise ValueError("max_missions is outside 1..max_grant_missions")
    ensure_schema(conn)
    now = _now()
    grant_id = uuid.uuid4().hex
    code = secrets.token_urlsafe(24)
    expires_at = now + timedelta(seconds=int(ttl_seconds))
    conn.execute(
        "INSERT INTO grants(grant_id, borrower, scopes, domains, not_before, expires_at, max_missions, redeem_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (grant_id, borrower, json.dumps(sorted(set(scopes))), json.dumps(domains), now.isoformat(),
         expires_at.isoformat(), int(max_missions), _hash(code)),
    )
    conn.commit()
    return {"grant_id": grant_id, "redeem_code": code, "expires_at": expires_at.isoformat()}


def _row(conn: sqlite3.Connection, where: str, value: str) -> Any:
    if where not in ("grant_id", "redeem_hash"):
        raise ValueError(f"not a grant lookup column: {where}")
    ensure_schema(conn)
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row  # this query only; the shared connection is untouched
    return cur.execute(f"SELECT * FROM grants WHERE {where} = ?", (value,)).fetchone()


def _live(row: Any, now: datetime) -> bool:
    return (
        row is not None
        and row["revoked_at"] is None
        and datetime.fromisoformat(row["not_before"]) <= now < datetime.fromisoformat(row["expires_at"])
    )


def redeem(conn: sqlite3.Connection, code: str, key: str) -> dict[str, str]:
    """Turn a one-time code into a borrower token. GrantRefused for an unknown,
    used, revoked or out-of-window code."""
    now = _now()
    row = _row(conn, "redeem_hash", _hash(code))
    if not _live(row, now):
        raise GrantRefused("code does not stand for a live grant")
    took = conn.execute(
        "UPDATE grants SET redeemed = 1 WHERE grant_id = ? AND redeemed = 0", (row["grant_id"],)
    ).rowcount
    conn.commit()
    if took != 1:
        raise GrantRefused("code already redeemed")
    ttl = int(OPERATIONAL_CONSTRAINTS["borrower_token_ttl_seconds"])
    exp = min(now + timedelta(seconds=ttl), datetime.fromisoformat(row["expires_at"]))
    token = jwt.encode(
        {
            "sub": f"grant:{row['grant_id']}",
            "role": "borrower",
            "grant": row["grant_id"],
            "scope": json.loads(row["scopes"]),
            "domains": json.loads(row["domains"]),
            "epoch": row["epoch"],
            "jti": uuid.uuid4().hex,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        },
        key,
        algorithm=_ALGORITHM,
    )
    return {"token": token, "expires_at": exp.isoformat()}


def verify(conn: sqlite3.Connection, token: str, key: str) -> Grant:
    """The live grant a borrower token stands for; GrantRefused otherwise."""
    try:
        claims = jwt.decode(token, key, algorithms=[_ALGORITHM], options={"require": ["exp", "grant", "epoch"]})
    except jwt.PyJWTError as exc:
        raise GrantRefused(f"token not valid: {exc}") from exc
    now = _now()
    if claims.get("role") != "borrower" or claims["exp"] <= now.timestamp():
        raise GrantRefused("not a live borrower token")
    row = _row(conn, "grant_id", str(claims["grant"]))
    if not _live(row, now) or row["epoch"] != claims["epoch"]:
        raise GrantRefused("grant is revoked, expired or unknown")
    return Grant(
        grant_id=row["grant_id"],
        borrower=row["borrower"],
        scopes=tuple(json.loads(row["scopes"])),
        domains=json.loads(row["domains"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        max_missions=row["max_missions"],
        missions_used=row["missions_used"],
    )


def revoke(conn: sqlite3.Connection, grant_id: str) -> bool:
    """End a grant and every token it issued; False when there is no such grant."""
    ensure_schema(conn)
    changed = conn.execute(
        "UPDATE grants SET revoked_at = ?, epoch = epoch + 1 WHERE grant_id = ?", (_now().isoformat(), grant_id)
    ).rowcount
    conn.commit()
    return changed == 1
