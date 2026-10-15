"""FastAPI Hive app: health, auth, message, admin blocks, WebSocket /sync."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common.http_limits import REQUEST_BODY_MAX_BYTES, BodySizeLimit
from common.models.schemas import AuthProof, BlockDef, HiveMessage
from common.storage.db import open_db
from hive import auth as hive_auth
from hive import blocks as hive_blocks
from hive import sync as hive_sync

REPO_ROOT = Path(__file__).resolve().parents[1]
WS_AUTH_TIMEOUT_SECONDS = 5.0


def _read_version() -> str:
    version_path = REPO_ROOT / "VERSION"
    if version_path.is_file():
        return version_path.read_text(encoding="utf-8").strip() or "1.0.0"
    return "1.0.0"


def _resolve_db_path(db_path: Optional[str], data_dir: Optional[str]) -> str:
    if db_path:
        return db_path
    env_db = os.environ.get("HIVE_DB_PATH")
    if env_db:
        return env_db
    base = data_dir or os.environ.get("DATA_DIR", "/data")
    return str(Path(base) / "hive.db")


#: Largest command payload /message stores (serialized JSON bytes).
MESSAGE_PAYLOAD_MAX_BYTES = 64 * 1024


class MessageBody(BaseModel):
    target_agent_id: str
    payload: dict[str, Any]


def create_app(
    db_path: Optional[str] = None,
    jwt_key: Optional[str] = None,
    data_dir: Optional[str] = None,
) -> FastAPI:
    key = hive_auth.require_jwt_key(jwt_key or os.environ.get("HIVE_JWT_KEY"))
    resolved_db = _resolve_db_path(db_path, data_dir)
    if data_dir:
        os.environ["DATA_DIR"] = str(data_dir)
    os.environ["HIVE_DB_PATH"] = resolved_db
    os.environ["HIVE_JWT_KEY"] = key

    conn = open_db(resolved_db)
    limiter = hive_auth.ChallengeRateLimiter()
    version = _read_version()

    app = FastAPI(title="self-agent-hive", version=version)
    # Bounded before routing: no body over the cap is read, authenticated or not.
    app.add_middleware(BodySizeLimit, max_bytes=REQUEST_BODY_MAX_BYTES)
    app.state.db = conn
    app.state.jwt_key = key
    app.state.db_path = resolved_db
    app.state.rate_limiter = limiter
    app.state.version = version
    app.state.test_broker = None

    def bearer_claims(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail=hive_auth.AUTH_FAIL_DETAIL)
        token = authorization.split(" ", 1)[1].strip()
        try:
            claims = jwt.decode(token, app.state.jwt_key, algorithms=[hive_auth.JWT_ALGORITHM])
        except Exception as exc:
            raise HTTPException(status_code=401, detail=hive_auth.AUTH_FAIL_DETAIL) from exc
        # An agent's token is good only while it is that agent's current session;
        # an admin token only while the epoch it was minted under is current.
        if claims.get("role") == "admin":
            try:
                epoch = int(claims.get("epoch", 0))
            except (TypeError, ValueError):
                epoch = -1
            if epoch != hive_auth.admin_epoch(app.state.db):
                raise HTTPException(status_code=401, detail=hive_auth.AUTH_FAIL_DETAIL)
        elif not hive_auth.is_current_session(app.state.db, str(claims.get("sub")), claims.get("jti")):
            raise HTTPException(status_code=401, detail=hive_auth.AUTH_FAIL_DETAIL)
        return claims

    async def _publish(message: dict[str, Any]) -> None:
        broker = getattr(app.state, "test_broker", None)
        if broker is not None and hasattr(broker, "publish"):
            await broker.publish(message)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": app.state.version}

    @app.get("/auth/challenge")
    async def auth_challenge(agent_id: str, request: Request):
        db: sqlite3.Connection = app.state.db
        client = request.client.host if request.client else "unknown"
        # Unauthenticated: charged to its sender only. Charging the agent id it
        # names would let anyone spend that agent's budget and lock it out.
        if not app.state.rate_limiter.allow(f"client:{client}"):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        challenge = hive_auth.issue_challenge(db, agent_id)
        return challenge.model_dump(mode="json")

    @app.post("/auth/prove")
    async def auth_prove(body: AuthProof):
        grant, status, detail = hive_auth.verify_and_grant(app.state.db, body, app.state.jwt_key)
        if grant is None:
            return JSONResponse(status_code=status, content={"detail": detail})
        return grant.model_dump(mode="json")

    @app.post("/message")
    async def post_message(
        body: MessageBody,
        claims: dict[str, Any] = Depends(bearer_claims),
    ):
        # A stored message reaches its target as a command: admin only, the
        # same rule /admin/blocks applies.
        if claims.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin role required")
        payload = json.dumps(body.payload)
        if len(payload.encode("utf-8")) > MESSAGE_PAYLOAD_MAX_BYTES:
            raise HTTPException(status_code=413, detail="message payload too large")
        db: sqlite3.Connection = app.state.db
        msg_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO messages(id, target_agent_id, payload, delivered) VALUES (?, ?, ?, 0)",
            (msg_id, body.target_agent_id, payload),
        )
        db.commit()
        return {"id": msg_id, "status": "stored"}

    @app.post("/admin/blocks")
    async def admin_blocks(
        blocks: list[BlockDef],
        claims: dict[str, Any] = Depends(bearer_claims),
    ):
        if claims.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin role required")
        db: sqlite3.Connection = app.state.db
        changed = hive_blocks.upsert_blocks(db, blocks)
        if changed:
            msg = {
                "msg_type": "block_update",
                "session_id": "admin",
                "payload": {"blocks": [b.model_dump(mode="json") for b in changed]},
                "vector_clock": hive_sync.hive_vector_clock(db),
            }
            await _publish(msg)
        return {"status": "ok", "changed": bool(changed)}

    @app.post("/lend")
    async def lend() -> JSONResponse:
        return JSONResponse(status_code=501, content={"detail": "V2"})

    @app.post("/lend/redeem")
    async def lend_redeem() -> JSONResponse:
        return JSONResponse(status_code=501, content={"detail": "V2"})

    @app.post("/billing/{path:path}")
    async def billing_any(path: str) -> JSONResponse:
        _ = path
        return JSONResponse(status_code=501, content={"detail": "V2"})

    @app.websocket("/sync")
    async def sync_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        db: sqlite3.Connection = app.state.db
        agent_id: Optional[str] = None
        session_id: Optional[str] = None
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, WebSocketDisconnect):
            await websocket.close(code=4401)
            return
        try:
            frame = json.loads(raw)
            token = frame.get("auth")
            if not token:
                await websocket.close(code=4401)
                return
            claims = jwt.decode(token, app.state.jwt_key, algorithms=[hive_auth.JWT_ALGORITHM])
            agent_id = str(claims["sub"])
            session_id = claims.get("jti")
        except Exception:
            await websocket.close(code=4401)
            return

        try:
            while True:
                text = await websocket.receive_text()
                # Checked on every frame: a rotated secret or a newer login ends
                # this session even while its socket is open.
                if not hive_auth.is_current_session(db, agent_id, session_id):
                    await websocket.close(code=4401)
                    return
                data = json.loads(text)
                msg = HiveMessage.model_validate(data)
                if msg.session_id != agent_id:
                    await websocket.close(code=4403)
                    return
                outbound: list[HiveMessage] = []
                if msg.msg_type == "heartbeat":
                    outbound = hive_sync.heartbeat_response(
                        db, agent_id, msg.vector_clock or {}
                    )
                elif msg.msg_type == "memory_delta":
                    outbound = hive_sync.handle_agent_delta(
                        db, agent_id, msg.payload, msg.vector_clock or {}
                    )
                elif msg.msg_type == "block_update":
                    outbound = [
                        HiveMessage(
                            msg_type="ack",
                            session_id=agent_id,
                            payload={},
                            vector_clock=hive_sync.hive_vector_clock(db),
                        )
                    ]
                    payload = msg.model_dump(mode="json")
                    await _publish(payload)
                else:
                    outbound = [
                        HiveMessage(
                            msg_type="ack",
                            session_id=agent_id,
                            payload={},
                            vector_clock=hive_sync.hive_vector_clock(db),
                        )
                    ]
                for out in outbound:
                    payload = out.model_dump(mode="json")
                    await websocket.send_text(json.dumps(payload))
                    await _publish(payload)
        except WebSocketDisconnect:
            return
        except Exception:
            await websocket.close(code=4401)
            return
        finally:
            # The session is over: the agent no longer holds the single-agent lock.
            if agent_id is not None:
                hive_auth.release_agent(db, agent_id, session_id)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        try:
            app.state.db.close()
        except Exception:
            pass

    return app

