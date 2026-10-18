"""What the agent and the hive share about lending (docs/LENDING.md).

The scopes a grant can carry, the rules every grant's fields follow, and the
borrower token format: an HS256 JWT signed with a key derived from a secret
(the agent's owner token for agent-held grants, the agent's enrolment secret
for hive-held ones), never with the secret itself. Bounds are data
(INPUT_BOUNDS).
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Optional

import jwt

from common.logging import OPERATIONAL_CONSTRAINTS

#: What a grant can let a borrower do.
SCOPES = ("mission:submit", "mission:read", "evidence:upload", "evidence:read")

_ALGORITHM = "HS256"
_LABEL = b"me-agent lending borrower tokens v1"


class TokenRefused(Exception):
    """A borrower token that does not verify."""


def derive_key(secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), _LABEL, hashlib.sha256).hexdigest()


def check_fields(
    scopes: Any, domains: Any, ttl_seconds: Any, max_missions: Any, known_domains: Optional[set[str]] = None
) -> None:
    """ValueError when a grant field is outside its rule or bound."""
    if not isinstance(scopes, list) or not scopes or not set(scopes) <= set(SCOPES):
        raise ValueError(f"scopes must be a non-empty subset of {list(SCOPES)}")
    if (
        not isinstance(domains, list)
        or not 1 <= len(domains) <= int(OPERATIONAL_CONSTRAINTS["max_grant_domains"])
        or not all(isinstance(d, str) and d for d in domains)
        or len(set(domains)) != len(domains)
    ):
        raise ValueError("domains must be 1..max_grant_domains distinct kit domain names")
    if known_domains is not None and not set(domains) <= known_domains:
        raise ValueError(f"unknown kit domains: {sorted(set(domains) - known_domains)}")
    if not 1 <= int(ttl_seconds) <= int(OPERATIONAL_CONSTRAINTS["max_grant_ttl_seconds"]):
        raise ValueError("ttl_seconds is outside 1..max_grant_ttl_seconds")
    if not 1 <= int(max_missions) <= int(OPERATIONAL_CONSTRAINTS["max_grant_missions"]):
        raise ValueError("max_missions is outside 1..max_grant_missions")


def encode(claims: dict[str, Any], secret: str) -> str:
    return jwt.encode({**claims, "role": "borrower"}, derive_key(secret), algorithm=_ALGORITHM)


def decode(token: str, secret: str) -> dict[str, Any]:
    """The claims of a borrower token signed with ``secret``'s derived key."""
    try:
        claims = jwt.decode(
            token, derive_key(secret), algorithms=[_ALGORITHM], options={"require": ["exp", "grant", "epoch"]}
        )
    except jwt.PyJWTError as exc:
        raise TokenRefused(f"token not valid: {exc}") from exc
    if claims.get("role") != "borrower":
        raise TokenRefused("not a borrower token")
    return claims
