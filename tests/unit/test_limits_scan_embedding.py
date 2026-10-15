"""Phase 2: rate limits by agent and by client, nonces pruned, the secret scan
sees real key names and committed .env files, and a wrong-sized embedding
leaves no orphan row."""
from __future__ import annotations

import importlib.util
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from common.storage.db import embedding_dim, open_db
from hive import auth as hive_auth
from hive import sync as hive_sync

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_rotating_agent_ids_from_one_client_is_still_limited():
    limiter = hive_auth.ChallengeRateLimiter(limit=3, window=60)
    assert all(limiter.allow(f"agent:a{i}", "client:1.2.3.4") for i in range(3))
    assert not limiter.allow("agent:fresh", "client:1.2.3.4")
    assert limiter.allow("agent:fresh", "client:5.6.7.8")


def test_one_agent_id_is_limited_across_clients():
    limiter = hive_auth.ChallengeRateLimiter(limit=2, window=60)
    assert limiter.allow("agent:a", "client:1")
    assert limiter.allow("agent:a", "client:2")
    assert not limiter.allow("agent:a", "client:3")


def test_idle_keys_are_forgotten(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(hive_auth.time, "monotonic", lambda: clock[0])
    limiter = hive_auth.ChallengeRateLimiter(limit=2, window=60)
    for i in range(50):
        limiter.allow(f"agent:{i}", "client:x")
    clock[0] += 61
    limiter.allow("agent:new", "client:y")
    assert set(limiter._hits) == {"agent:new", "client:y"}


def test_used_and_expired_nonces_are_pruned(tmp_path):
    conn = open_db(str(tmp_path / "hive.db"))
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    conn.executemany(
        "INSERT INTO nonces(nonce, agent_id, expires_at, used) VALUES (?, 'a', ?, ?)",
        [("expired", past, 0), ("used", future, 1), ("live", future, 0)],
    )
    conn.commit()
    fresh = hive_auth.issue_challenge(conn, "a").nonce
    left = {r[0] for r in conn.execute("SELECT nonce FROM nonces")}
    assert left == {"live", fresh}


def test_a_wrong_sized_embedding_leaves_no_delta(tmp_path):
    conn = open_db(str(tmp_path / "hive.db"))
    with pytest.raises(ValueError, match="dimension mismatch"):
        hive_sync.insert_delta(
            conn, key="k", value={"v": 1}, origin="a", vector_clock={"a": 1},
            embedding=[0.0] * (embedding_dim() + 1),
        )
    assert conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 0


def test_a_right_sized_embedding_is_stored_with_its_delta(tmp_path):
    conn = open_db(str(tmp_path / "hive.db"))
    hive_sync.insert_delta(
        conn, key="k", value={"v": 1}, origin="a", vector_clock={"a": 1},
        embedding=[0.1] * embedding_dim(),
    )
    assert conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 1


# -- secret scan -------------------------------------------------------------------


@pytest.fixture(scope="module")
def scan():
    spec = importlib.util.spec_from_file_location("secret_scan", REPO_ROOT / "scripts" / "secret_scan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE = "f3" * 20  # 40 key-like characters, not a real secret


@pytest.mark.parametrize(
    "name,text",
    [
        ("config.yml", f"HIVE_JWT_KEY: '{FAKE}'\n"),
        ("client.py", f'api_token = "{FAKE}"\n'),
        ("deploy.sh", f"export AGENT_SECRET={FAKE}\n"),
        ("settings.ini", f"db_password={FAKE}\n"),
    ],
)
def test_key_names_and_unquoted_values_are_caught(scan, tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")
    assert scan.scan_file(tmp_path / name, set())


def test_a_committed_env_file_is_a_finding_but_the_example_is_not(scan, tmp_path):
    (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("HIVE_JWT_KEY=change-me\n", encoding="utf-8")
    assert scan.scan_file(Path(".env"), set()) == [".env: environment file committed"]
    assert scan.scan_file(tmp_path / ".env.example", set()) == []


def test_the_scan_reads_what_git_would_commit_not_ignored_files(scan, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"HIVE_JWT_KEY={FAKE}\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    names = {p.name for p in scan.committed_files(str(tmp_path))}
    assert names == {".gitignore", "app.py"}
