"""SQLite connection helpers and numbered schema migrations."""
from __future__ import annotations

import os
import sqlite3
from typing import Callable

import sqlite_vec

# Local only (owner ruling 6): stock sqlite3 + sqlite-vec, no hosted fallback.

CODE_SCHEMA_VERSION = 4


def embedding_dim() -> int:
    raw = os.environ.get("EMBEDDING_DIM", "384")
    return int(raw)


def connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not hasattr(conn, "enable_load_extension"):
        raise RuntimeError(
            "SQLite loadable extensions unavailable; use Python with extension support"
        )
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    ver = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if ver is None:
        return 0
    return int(ver[0])


def _migration_001(conn: sqlite3.Connection) -> None:
    dim = embedding_dim()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY,
            secret TEXT NOT NULL,
            enrolled_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS memory_deltas (
            delta_id TEXT PRIMARY KEY,
            key TEXT NOT NULL,
            value TEXT,
            origin TEXT NOT NULL,
            vector_clock TEXT NOT NULL,
            operation TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            conflict_id TEXT PRIMARY KEY,
            key TEXT NOT NULL,
            hive_value TEXT,
            agent_value TEXT,
            hive_clock TEXT NOT NULL,
            agent_clock TEXT NOT NULL,
            winner TEXT NOT NULL DEFAULT 'hive',
            resolved_at TEXT NOT NULL,
            replayed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS block_registry (
            block_id TEXT PRIMARY KEY,
            def_json TEXT NOT NULL,
            version_clock INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            target_agent_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nonces (
            nonce TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(embedding float[{dim}])"
    )
    existing = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    if int(existing) == 0:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (1,))
    else:
        conn.execute("UPDATE schema_version SET version = ?", (1,))


def _migration_002(conn: sqlite3.Connection) -> None:
    """The single-agent lock is a lease: an agent holds it until its session
    expires (active_until) or its sync socket closes."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
    if "active_until" not in columns:
        conn.execute("ALTER TABLE agents ADD COLUMN active_until TEXT")


def _migration_003(conn: sqlite3.Connection) -> None:
    """An agent's current session (the jti of its last grant): a token is good
    only while it names it, and only it may release the lease."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
    if "session_id" not in columns:
        conn.execute("ALTER TABLE agents ADD COLUMN session_id TEXT")


def _migration_004(conn: sqlite3.Connection) -> None:
    """Hive-wide settings (the admin token epoch): one value per key."""
    conn.execute("CREATE TABLE IF NOT EXISTS hive_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_001),
    (2, _migration_002),
    (3, _migration_003),
    (4, _migration_004),
]


def migrate(conn: sqlite3.Connection) -> None:
    current = get_schema_version(conn)
    if current > CODE_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than code "
            f"version {CODE_SCHEMA_VERSION}; refuse to boot"
        )
    for version, fn in MIGRATIONS:
        if current < version:
            fn(conn)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
            conn.commit()
            current = version
    conn.commit()


def open_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn
