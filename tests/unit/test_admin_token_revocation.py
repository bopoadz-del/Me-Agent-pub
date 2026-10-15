"""Phase 2f: admin tokens can be revoked.

An admin token was checked for signature and expiry only, so a leaked one
(hive-admin token prints it) stayed good for its whole hour and could queue
commands for any agent; the only way to stop it was to rotate HIVE_JWT_KEY,
which also ends every agent session. The hive now keeps an admin epoch:
every admin token carries the epoch it was minted under (a token without one
is epoch 0, the starting epoch) and is accepted only while that epoch is
current. `hive-admin token revoke` advances the epoch, ending every admin
token issued before it; tokens minted afterwards work.
"""
from __future__ import annotations

import asyncio

import httpx

from hive import admin_cli
from hive import auth as hive_auth
from hive.main import create_app

KEY = "k" * 40


def _post(app, token: str) -> int:
    async def run() -> int:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hive") as c:
            r = await c.post(
                "/message",
                json={"target_agent_id": "agent-1", "payload": {"action": "status"}},
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code

    return asyncio.run(run())


def test_an_admin_token_works_until_revoked(tmp_path):
    db = str(tmp_path / "hive.db")
    app = create_app(db_path=db, jwt_key=KEY)
    token = admin_cli.mint_admin(KEY, db_path=db)
    assert _post(app, token) == 200
    admin_cli.revoke_admin_tokens(db_path=db)
    assert _post(app, token) == 401


def test_a_token_minted_after_a_revocation_works(tmp_path):
    db = str(tmp_path / "hive.db")
    app = create_app(db_path=db, jwt_key=KEY)
    admin_cli.revoke_admin_tokens(db_path=db)
    assert _post(app, admin_cli.mint_admin(KEY, db_path=db)) == 200


def test_a_revocation_ends_tokens_without_an_epoch_too(tmp_path):
    """A token minted with no epoch claim is epoch 0 and ends at the first revocation."""
    db = str(tmp_path / "hive.db")
    app = create_app(db_path=db, jwt_key=KEY)
    bare = hive_auth.mint_admin_token(KEY)
    assert _post(app, bare) == 200
    admin_cli.revoke_admin_tokens(db_path=db)
    assert _post(app, bare) == 401


def test_the_cli_revokes(tmp_path, capsys):
    db = str(tmp_path / "hive.db")
    app = create_app(db_path=db, jwt_key=KEY)
    token = admin_cli.mint_admin(KEY, db_path=db)
    assert admin_cli.main(["token", "revoke", "--db", db]) == 0
    assert _post(app, token) == 401
