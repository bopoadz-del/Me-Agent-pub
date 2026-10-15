"""Phase 2e: no request body larger than the cap is read, on either service,
before or after authentication.

FastAPI reads and parses a request's whole body before its dependencies
(the bearer check) run, and nothing bounded it: anyone who can reach the
agent or the hive could post a multi-gigabyte body with no credentials and
the service would hold it in memory first, then answer 401. One ASGI
middleware on both apps now refuses a body over REQUEST_BODY_MAX_BYTES with
413 -- from its Content-Length before reading anything, or as soon as a
streamed body passes the cap.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

KEY = "k" * 40
#: Well above any reasonable cap; the cap itself is checked separately.
TOO_BIG = b"x" * (8 * 1024 * 1024)


def test_the_cap_is_below_the_oversized_probe():
    from common.http_limits import REQUEST_BODY_MAX_BYTES

    assert 0 < REQUEST_BODY_MAX_BYTES < len(TOO_BIG)


def test_the_hive_refuses_an_oversized_body_before_auth(tmp_path):
    from hive.main import create_app

    with TestClient(create_app(db_path=str(tmp_path / "hive.db"), jwt_key=KEY)) as client:
        r = client.post("/auth/prove", content=TOO_BIG, headers={"content-type": "application/json"})
        assert r.status_code == 413


def test_the_agent_refuses_an_oversized_body_before_auth(monkeypatch):
    from agent import bootstrapper

    monkeypatch.setenv("AGENT_API_TOKEN", "t" * 40)
    app = bootstrapper._create_api({"comms": None, "store": None})
    with TestClient(app) as client:
        r = client.post("/mission", content=TOO_BIG, headers={"content-type": "application/json"})
        assert r.status_code == 413


def _limit(app, max_bytes):
    from common.http_limits import BodySizeLimit

    return BodySizeLimit(app, max_bytes=max_bytes)


def _drive(app, body_chunks: list[bytes], headers: list[tuple[bytes, bytes]]) -> tuple[int, int]:
    """Run one HTTP request through ``app``; (status, bytes the app consumed)."""
    sent: list[dict] = []
    chunks = list(body_chunks)
    consumed = 0

    async def receive():
        nonlocal consumed
        if chunks:
            chunk = chunks.pop(0)
            consumed += len(chunk)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], consumed


async def _reads_everything(scope, receive, send):
    while True:
        message = await receive()
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_a_declared_oversized_body_is_refused_without_reading_it():
    app = _limit(_reads_everything, 10)
    status, consumed = _drive(app, [b"x" * 100], [(b"content-length", b"100")])
    assert (status, consumed) == (413, 0)


def test_a_streamed_body_is_refused_once_it_passes_the_cap():
    app = _limit(_reads_everything, 10)
    status, consumed = _drive(app, [b"x" * 6, b"x" * 6, b"x" * 6], [])
    assert status == 413 and consumed <= 12


@pytest.mark.parametrize("size", [0, 10])
def test_a_body_within_the_cap_passes(size):
    app = _limit(_reads_everything, 10)
    status, _ = _drive(app, [b"x" * size], [(b"content-length", str(size).encode())])
    assert status == 200
