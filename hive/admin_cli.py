"""Hive admin CLI — agent enrollment, block registration, and admin JWT minting."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from common.storage.db import open_db
from hive.auth import admin_epoch, advance_admin_epoch, mint_admin_token, require_jwt_key, set_agent_secret
from hive.blocks import load_block_files, upsert_blocks


def _db_path_from_env(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env_db = os.environ.get("HIVE_DB_PATH")
    if env_db:
        return env_db
    data_dir = os.environ.get("DATA_DIR", "/data")
    return str(Path(data_dir) / "hive.db")


def enroll_agent(
    agent_id: str,
    db_path: str | None = None,
    jwt_key: str | None = None,
) -> str:
    """Enroll an agent; return per-agent secret (hex string)."""
    _ = jwt_key  # enrollment does not require JWT; accepted for harness signature
    path = _db_path_from_env(db_path)
    conn = open_db(path)
    secret = secrets.token_hex(32)
    now = datetime.now(timezone.utc).isoformat()
    set_agent_secret(conn, agent_id, secret, now)
    conn.close()
    return secret


def add_agent(agent_id: str, db_path: str | None = None, jwt_key: str | None = None) -> str:
    return enroll_agent(agent_id, db_path=db_path, jwt_key=jwt_key)


def agent_add(agent_id: str, db_path: str | None = None, jwt_key: str | None = None) -> str:
    return enroll_agent(agent_id, db_path=db_path, jwt_key=jwt_key)


def register_blocks(paths: list[str], db_path: str | None = None) -> list[str]:
    """Register BlockDef JSON files (or directories of them); return changed ids."""
    blocks = load_block_files(paths)
    conn = open_db(_db_path_from_env(db_path))
    try:
        changed = upsert_blocks(conn, blocks)
    finally:
        conn.close()
    return [block.block_id for block in changed]


def mint_admin(jwt_key: str | None = None, db_path: str | None = None) -> str:
    """An admin token under the hive's current admin epoch."""
    key = require_jwt_key(jwt_key or os.environ.get("HIVE_JWT_KEY"))
    conn = open_db(_db_path_from_env(db_path))
    try:
        return mint_admin_token(key, epoch=admin_epoch(conn))
    finally:
        conn.close()


def revoke_admin_tokens(db_path: str | None = None) -> int:
    """End every admin token issued so far; returns the new epoch."""
    conn = open_db(_db_path_from_env(db_path))
    try:
        return advance_admin_epoch(conn)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hive-admin")
    sub = parser.add_subparsers(dest="command", required=True)

    agent_p = sub.add_parser("agent", help="agent enrollment")
    agent_sub = agent_p.add_subparsers(dest="agent_cmd", required=True)
    add_p = agent_sub.add_parser("add", help="enroll a new agent")
    add_p.add_argument("agent_id")
    add_p.add_argument("--db", dest="db_path", default=None)

    blocks_p = sub.add_parser("blocks", help="block registration")
    blocks_sub = blocks_p.add_subparsers(dest="blocks_cmd", required=True)
    reg_p = blocks_sub.add_parser("register", help="register BlockDef JSON files or directories")
    reg_p.add_argument("paths", nargs="+")
    reg_p.add_argument("--db", dest="db_path", default=None)

    admin_p = sub.add_parser("token", help="mint an admin JWT, or revoke every admin JWT issued so far")
    admin_p.add_argument("action", nargs="?", choices=["mint", "revoke"], default="mint")
    admin_p.add_argument("--jwt-key", dest="jwt_key", default=None)
    admin_p.add_argument("--db", dest="db_path", default=None)

    args = parser.parse_args(argv)
    if args.command == "agent" and args.agent_cmd == "add":
        secret = enroll_agent(args.agent_id, db_path=args.db_path)
        sys.stdout.write(secret + "\n")
        return 0
    if args.command == "blocks" and args.blocks_cmd == "register":
        changed = register_blocks(args.paths, db_path=args.db_path)
        sys.stdout.write(f"registered {len(changed)} changed block(s)\n")
        return 0
    if args.command == "token" and args.action == "revoke":
        epoch = revoke_admin_tokens(db_path=args.db_path)
        sys.stdout.write(f"admin tokens revoked; admin epoch is now {epoch}\n")
        return 0
    if args.command == "token":
        token = mint_admin(args.jwt_key, db_path=args.db_path)
        sys.stdout.write(token + "\n")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
