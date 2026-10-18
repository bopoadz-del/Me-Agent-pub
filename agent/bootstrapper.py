"""Agent bootstrapper — env validation, auth, API server."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agent import lending
from agent.comms import CommsLink
from agent.memory import LocalStore, Retriever, SyncEngine
from agent.orchestrator.engine import HeadlessOrchestrator, disk_over_limit
from agent.swarm.registry import BlockRegistry
from agent.swarm.spawner import SubAgentSpawner
from agent.swarm.tools import evidence_file, intake_evidence, list_evidence, store_upload
from common.http_limits import REQUEST_BODY_MAX_BYTES, BodySizeLimit
from common.logging import OPERATIONAL_CONSTRAINTS, get_logger
from agent.model_server import ModelServerGate
from common.models.llm_client import MockLLMClient, OllamaClient, configured_model, replay_client_from_env
from common.models.schemas import BlockDef, MemoryDelta, MissionProfile, SessionGrant

_logger = get_logger("agent.bootstrapper")
_RUNTIME: dict[str, Any] = {}


class LendBody(BaseModel):
    borrower: str = Field(min_length=1, max_length=200)
    scopes: list[str]
    domains: list[str]
    ttl_seconds: int
    max_missions: int


class RedeemBody(BaseModel):
    code: str = Field(min_length=1, max_length=200)


class MissionCreateBody(BaseModel):
    # The MissionProfile bounds, checked here too so the API answers 422.
    natural_language: str = Field(max_length=OPERATIONAL_CONSTRAINTS["max_mission_text_chars"])
    domain_hint: Optional[str] = None
    attached_evidence: list[dict[str, str]] = Field(
        default_factory=list, max_length=OPERATIONAL_CONSTRAINTS["max_evidence_items"]
    )
    required_confidence: float = 0.9
    max_cost_usd: float = 1.0
    priority: int = 5


def _mission_queue_max_depth() -> int:
    raw = os.environ.get("MISSION_QUEUE_MAX_DEPTH")
    if raw is not None and str(raw).strip() != "":
        return max(0, int(raw))
    return int(OPERATIONAL_CONSTRAINTS["mission_queue_max_depth"])


def _build_llm():
    """MockLLM when LLM_CLIENT=mock, replayed replies when LLM_CLIENT=replay;
    otherwise the configured local model."""
    if os.environ.get("LLM_CLIENT", "").lower() == "mock":
        return MockLLMClient()
    return replay_client_from_env() or OllamaClient(model=configured_model())



def _model_gate(llm: Any) -> Optional[ModelServerGate]:
    """Share the real Ollama server (wait while it is busy); a mock needs none."""
    if isinstance(llm, OllamaClient):
        return ModelServerGate(llm.base_url, llm.model)
    return None

def _fail(message: str) -> None:
    sys.stderr.write(message + "\n")
    raise SystemExit(1)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if val is None or not str(val).strip():
        _fail(f"missing required environment variable: {name}")
    return str(val).strip()


MIN_SECRET_LENGTH = 32


def _require_secret(name: str) -> str:
    """A secret is required, at least MIN_SECRET_LENGTH characters."""
    val = _require(name)
    if len(val) < MIN_SECRET_LENGTH:
        _fail(f"{name} must be at least {MIN_SECRET_LENGTH} characters")
    return val


def _validate_env() -> None:
    _require("AGENT_ID")
    api_token = _require_secret("AGENT_API_TOKEN")
    airgap = os.environ.get("AIRGAP", "false").lower() == "true"
    if not airgap:
        _require("HIVE_URL")
        if _require_secret("AGENT_SECRET") == api_token:
            _fail("AGENT_SECRET and AGENT_API_TOKEN must be different keys")
    test_mode = os.environ.get("TEST_MODE", "").lower() == "true"
    if os.environ.get("LLM_CLIENT", "").lower() in ("mock", "replay") and not test_mode:
        _fail(f"LLM_CLIENT={os.environ['LLM_CLIENT'].lower()} requires TEST_MODE=true")
    if os.environ.get("INPROCESS_BROKER", "").lower() == "true" and not test_mode:
        _fail("INPROCESS_BROKER requires TEST_MODE=true")


def _data_paths() -> tuple[str, str, Path]:
    """(data dir, agent db, preload dir), read from the environment at call time."""
    data_dir = os.environ.get("DATA_DIR", "/data")
    preload = Path(os.environ.get("PRELOAD_DIR", str(Path(data_dir) / "preload")))
    return data_dir, str(Path(data_dir) / "agent.db"), preload


def _load_preload(store: LocalStore) -> None:
    preload = _data_paths()[2]
    mem_file = preload / "memory.sqlite"
    blocks_file = preload / "blocks.json"
    if mem_file.is_file():
        src = sqlite3.connect(str(mem_file))
        src.row_factory = sqlite3.Row
        rows = src.execute(
            "SELECT delta_id, key, value, origin, vector_clock, operation, timestamp FROM memory_deltas"
        ).fetchall()
        deltas = []
        for row in rows:
            deltas.append(
                MemoryDelta(
                    delta_id=row["delta_id"],
                    key=row["key"],
                    value=json.loads(row["value"]) if row["value"] else None,
                    origin=row["origin"],
                    vector_clock=json.loads(row["vector_clock"]),
                    operation=row["operation"],
                )
            )
        store.apply_memory_dump(deltas)
        src.close()
    if blocks_file.is_file():
        raw = json.loads(blocks_file.read_text(encoding="utf-8"))
        blocks = [BlockDef.model_validate(item) for item in raw]
        store.apply_blocks(blocks)


def _hive_login() -> SessionGrant:
    """Challenge/prove against the hive; RuntimeError when it refuses."""
    hive_url = _require("HIVE_URL").rstrip("/")
    agent_id = _require("AGENT_ID")
    secret = _require("AGENT_SECRET")
    with httpx.Client(base_url=hive_url, timeout=30.0) as client:
        challenge = client.get("/auth/challenge", params={"agent_id": agent_id})
        if challenge.status_code != 200:
            raise RuntimeError(f"hive challenge refused: {challenge.status_code}")
        try:
            nonce = str(challenge.json()["nonce"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("hive challenge malformed: no nonce") from exc
        proof = hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        response = client.post(
            "/auth/prove",
            json={"agent_id": agent_id, "nonce": nonce, "proof": proof},
        )
        if response.status_code != 200:
            raise RuntimeError(f"hive auth failed: {response.status_code} {response.text}")
        return SessionGrant.model_validate(response.json())


def _authenticate_online() -> SessionGrant:
    try:
        return _hive_login()
    except RuntimeError as exc:
        _fail(str(exc))
        raise


def _build_runtime(store: LocalStore, grant: Optional[SessionGrant] = None) -> dict[str, Any]:
    registry = BlockRegistry()
    for block in store.load_blocks():
        registry.register(block)
    if grant is not None:
        for block in grant.blocks:
            registry.register(block)
    llm = _build_llm()
    spawner = SubAgentSpawner(registry, llm_client=llm)
    retriever = Retriever(store)
    orchestrator = HeadlessOrchestrator(registry, retriever, spawner, store)
    comms = CommsLink(orchestrator, model_gate=_model_gate(llm))
    sync_engine: Optional[SyncEngine] = None
    if grant is not None:
        sync_engine = SyncEngine(
            store,
            _require("AGENT_ID"),
            _require("HIVE_URL"),
            grant.session_token,
            comms=comms,
            reauth=lambda: _hive_login().session_token,
        )

        def _publish_to_hive(message: dict[str, Any]) -> None:
            agent_id = _require("AGENT_ID")
            clock = dict(sync_engine.vector_clock)
            clock[agent_id] = clock.get(agent_id, 0) + 1
            sync_engine.vector_clock = clock
            frame = {
                "msg_type": message.get("msg_type", "block_update"),
                "session_id": agent_id,
                "payload": message.get("payload", message),
                "vector_clock": clock,
            }
            sync_engine.send_outbound(frame)

        comms.publish_fn = _publish_to_hive
    return {
        "store": store,
        "registry": registry,
        "orchestrator": orchestrator,
        "comms": comms,
        "sync_engine": sync_engine,
        "grant": grant,
    }


def _create_api(runtime: dict[str, Any]) -> FastAPI:
    api_token = _require("AGENT_API_TOKEN")
    app = FastAPI(title="self-agent")
    # Bounded before routing: no body over the cap is read, authenticated or not.
    app.add_middleware(BodySizeLimit, max_bytes=REQUEST_BODY_MAX_BYTES)

    def _auth(authorization: Optional[str] = Header(default=None)) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="unauthorized")
        token = authorization.split(" ", 1)[1].strip()
        if not secrets.compare_digest(token.encode(), api_token.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.post("/lend", dependencies=[Depends(_auth)])
    async def post_lend(body: LendBody) -> dict[str, str]:
        try:
            return lending.create_grant(
                runtime["store"].conn,
                borrower=body.borrower,
                scopes=body.scopes,
                domains=body.domains,
                ttl_seconds=body.ttl_seconds,
                max_missions=body.max_missions,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/lend/redeem")
    async def post_lend_redeem(body: RedeemBody) -> dict[str, str]:
        try:
            return lending.redeem(runtime["store"].conn, body.code, api_token)
        except lending.GrantRefused as exc:
            raise HTTPException(status_code=403, detail="code does not stand for a live grant") from exc

    @app.post("/lend/{grant_id}/revoke", dependencies=[Depends(_auth)])
    async def post_lend_revoke(grant_id: str) -> dict[str, str]:
        if not lending.revoke(runtime["store"].conn, grant_id):
            raise HTTPException(status_code=404, detail="not found")
        return {"grant_id": grant_id, "status": "revoked"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/mission", dependencies=[Depends(_auth)])
    async def post_mission(payload: MissionCreateBody) -> dict[str, Any]:
        # Re-read constraints at request time (subprocess env / seams).
        if disk_over_limit(_data_paths()[0]):
            raise HTTPException(status_code=429, detail="disk limit exceeded")
        comms: CommsLink = runtime["comms"]
        max_depth = _mission_queue_max_depth()
        if hasattr(comms, "_max_depth"):
            comms._max_depth = max_depth
        if comms.queue_depth() >= max_depth:
            raise HTTPException(status_code=429, detail="queue saturated")
        profile = MissionProfile(
            natural_language=payload.natural_language,
            domain_hint=payload.domain_hint,
            attached_evidence=list(payload.attached_evidence or []),
            required_confidence=payload.required_confidence,
            max_cost_usd=payload.max_cost_usd,
            priority=payload.priority,
        )
        try:
            # Ruling 1: evidence is copied into the whitelist; never read in place.
            profile.attached_evidence = intake_evidence(
                profile.attached_evidence, profile.mission_id
            )
        except PermissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime["store"].last_profile = profile
        if not comms.enqueue(profile):
            raise HTTPException(status_code=429, detail="queue saturated")
        return {"mission_id": profile.mission_id, "status": "queued"}

    # {name:path} so a name with a separator reaches the name rule and is refused
    # there (400), the same as every other bad name.
    @app.post("/evidence/{name:path}", dependencies=[Depends(_auth)])
    async def post_evidence(name: str, request: Request) -> dict[str, Any]:
        # One document as the raw body, under the same cap as every route; the
        # reply is the path to attach to a mission.
        if disk_over_limit(_data_paths()[0]):
            raise HTTPException(status_code=429, detail="disk limit exceeded")
        data = await request.body()
        try:
            path = store_upload(name, data)
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="evidence must be UTF-8 text") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"path": str(path), "type": path.suffix.lstrip(".").lower() or "txt", "bytes": len(data)}

    @app.get("/evidence", dependencies=[Depends(_auth)])
    async def get_evidence_list(offset: int = 0, limit: Optional[int] = None) -> dict[str, Any]:
        max_page = int(OPERATIONAL_CONSTRAINTS["max_evidence_list_page"])
        limit = max_page if limit is None else limit
        if offset < 0 or not 1 <= limit <= max_page:
            raise HTTPException(status_code=422, detail=f"offset >= 0 and 1 <= limit <= {max_page}")
        every = list_evidence()
        items = [{"ref": ref, "path": str(path), "bytes": size} for ref, path, size in every[offset:offset + limit]]
        return {"items": items, "total": len(every), "offset": offset, "limit": limit}

    @app.get("/evidence/{ref:path}", dependencies=[Depends(_auth)])
    async def get_evidence_file(ref: str) -> FileResponse:
        try:
            return FileResponse(evidence_file(ref))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="not found") from exc

    @app.get("/mission/{mission_id}", dependencies=[Depends(_auth)])
    async def get_mission(mission_id: str) -> dict[str, Any]:
        store = runtime["store"]
        row = store.conn.execute(
            "SELECT status, checkpoint FROM missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if row is None:
            comms = runtime["comms"]
            if comms.is_queued(mission_id):
                queued: dict[str, Any] = {"mission_id": mission_id, "status": "queued"}
                reason = comms.waiting_reason(mission_id)
                if reason:
                    queued["waiting"] = reason
                return queued
            raise HTTPException(status_code=404, detail="not found")
        body: dict[str, Any] = {
            "mission_id": mission_id,
            "status": row["status"],
            "checkpoint": json.loads(row["checkpoint"] or "{}"),
        }
        # Terminal ExecutionResult once the worker has finished: this is what carries
        # citations and confidence (spec Section 11.4 step 7). Absent while running.
        result = store.get_mission_result(mission_id)
        if result is not None:
            body["result"] = result
        return body

    return app


def _resume_running_missions(runtime: dict[str, Any], online: bool) -> None:
    store: LocalStore = runtime["store"]
    for state in store.running_missions():
        state.resumed_from = state.mission_id
        store.save_mission_state(state)
        if online and runtime.get("sync_engine") is not None:
            runtime["comms"].handle_command({"action": "resume", "mission_id": state.mission_id})


def boot(*, serve: bool = True) -> dict[str, Any]:
    _validate_env()
    data_dir, db_path, _preload = _data_paths()
    os.makedirs(data_dir, exist_ok=True)
    if disk_over_limit(data_dir):
        _logger.warning("disk usage over limit — read-only inspection mode")
    airgap = os.environ.get("AIRGAP", "false").lower() == "true"
    store = LocalStore(db_path)
    grant: Optional[SessionGrant] = None
    hive_connected = False
    if airgap:
        _load_preload(store)
        # SKIP_AIRGAP_VERIFY is honored only under TEST_MODE (ruling 2).
        skip = (
            os.environ.get("SKIP_AIRGAP_VERIFY", "false").lower() == "true"
            and os.environ.get("TEST_MODE", "").lower() == "true"
        )
        if not skip:
            from agent.security.airgap import AirGapVerifier

            report = AirGapVerifier().verify()
            if not report.verified:
                _fail("airgap verification failed")
    else:
        grant = _authenticate_online()
        hive_connected = True
        store.apply_memory_dump(grant.memory_dump)
        store.apply_blocks(grant.blocks)
    runtime = _build_runtime(store, grant)
    _RUNTIME.clear()
    _RUNTIME.update(runtime)
    if runtime["sync_engine"] is not None:
        runtime["sync_engine"].start()
    runtime["comms"].start()
    _resume_running_missions(runtime, online=not airgap)
    result = {"airgap": airgap, "hive_connected": hive_connected}
    if not serve:
        return result
    port = int(os.environ.get("AGENT_API_PORT") or os.environ.get("PORT", "8000"))
    app = _create_api(runtime)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()
    return result


main = boot  # the module entry point: boot(serve=True)


if __name__ == "__main__":
    main()
