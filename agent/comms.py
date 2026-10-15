"""Comms link — hive commands and mission completion reporting."""
from __future__ import annotations

import os
import threading
from collections import deque
from typing import Any, Callable, Optional

from common.logging import OPERATIONAL_CONSTRAINTS, get_logger
from agent.orchestrator.engine import disk_over_limit
from agent.swarm.tools import intake_evidence
from common.models.schemas import ExecutionResult, MissionProfile, StepOutput

_logger = get_logger("agent.comms")


def _mission_queue_max_depth() -> int:
    raw = os.environ.get("MISSION_QUEUE_MAX_DEPTH")
    if raw is not None and str(raw).strip() != "":
        return max(0, int(raw))
    return int(OPERATIONAL_CONSTRAINTS["mission_queue_max_depth"])


class CommsLink:
    def __init__(
        self,
        orchestrator: Any,
        publish_fn: Optional[Callable[[dict[str, Any]], None]] = None,
        model_gate: Any = None,
        poll_seconds: float = 5.0,
    ) -> None:
        self.orchestrator = orchestrator
        self.publish_fn = publish_fn
        # agent.model_server.ModelServerGate: a mission waits while the shared
        # Ollama server is busy with another model, and never evicts it.
        self.model_gate = model_gate
        self._poll_seconds = poll_seconds
        self._waiting: dict[str, str] = {}
        self._max_depth = _mission_queue_max_depth()
        # Manual capacity check — refuse rather than silently drop (deque maxlen).
        self.queue: deque[MissionProfile] = deque()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._process_loop, name="comms-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=5.0)

    def enqueue(self, profile: MissionProfile) -> bool:
        with self._lock:
            if len(self.queue) >= self._max_depth:
                return False
            self.queue.append(profile)
            return True

    def waiting_reason(self, mission_id: str) -> Optional[str]:
        """Why a queued mission has not started yet, while it waits."""
        with self._lock:
            return self._waiting.get(mission_id)

    def is_queued(self, mission_id: str) -> bool:
        with self._lock:
            return mission_id in self._waiting or any(p.mission_id == mission_id for p in self.queue)

    def _mission_id_in_use(self, mission_id: str) -> bool:
        if self.is_queued(mission_id):
            return True
        store = getattr(self.orchestrator, "store", None)
        exists = getattr(store, "mission_exists", None)
        return bool(exists is not None and exists(mission_id))

    def queue_depth(self) -> int:
        with self._lock:
            return len(self.queue)

    def handle_command(self, payload: dict[str, Any]) -> None:
        action = payload.get("action")
        if action == "run_mission":
            raw = payload.get("profile") or payload
            profile = MissionProfile.model_validate(raw)
            try:
                # The disk limit POST /mission applies holds for a hive-sent mission too.
                if disk_over_limit(os.environ.get("DATA_DIR", "/data")):
                    raise PermissionError("disk limit exceeded")
                # A new mission takes a new id: reusing one would overwrite that
                # mission's state, result and evidence directory.
                if self._mission_id_in_use(profile.mission_id):
                    raise PermissionError(f"mission id already used: {profile.mission_id}")
                # Ruling 1: hive-sent evidence enters the whitelist the same way.
                profile.attached_evidence = intake_evidence(
                    profile.attached_evidence, profile.mission_id
                )
            except PermissionError as exc:
                _logger.warning(f"mission {profile.mission_id} refused at intake: {exc}")
                self._emit_block_update(
                    ExecutionResult(
                        mission_id=profile.mission_id,
                        status="failure",
                        outputs=[
                            StepOutput(
                                step_id="intake",
                                block_id="intake",
                                output={"reason": str(exc)},
                                validation_status="failed",
                                execution_time_ms=0,
                            )
                        ],
                        citations=[],
                        confidence=0.0,
                    )
                )
                return
            if not self.enqueue(profile):
                _logger.warning("mission queue saturated")
                return
        elif action == "status":
            status = {"queue_depth": self.queue_depth()}
            self._publish({"msg_type": "ack", "payload": status})
        elif action == "resume":
            _logger.info(f"resume notification: {payload}")

    def _process_loop(self) -> None:
        while not self._stop.is_set():
            profile: Optional[MissionProfile] = None
            with self._lock:
                if self.queue:
                    profile = self.queue.popleft()
            if profile is None:
                self._stop.wait(0.1)
                continue
            if not self._wait_for_model_server(profile.mission_id):
                return
            plan = self.orchestrator.resolve(profile)
            result = self.orchestrator.execute(plan)
            self._persist_result(result)
            self._emit_block_update(result)

    def _wait_for_model_server(self, mission_id: str) -> bool:
        """Hold the mission while the model server is busy. False when stopping."""
        if self.model_gate is None:
            return True
        while not self._stop.is_set():
            reason = self.model_gate.busy()
            if reason is None:
                with self._lock:
                    self._waiting.pop(mission_id, None)
                return True
            with self._lock:
                first = mission_id not in self._waiting
                self._waiting[mission_id] = reason
            if first:
                _logger.info(f"mission {mission_id}: {reason}")
            self._stop.wait(self._poll_seconds)
        return False

    def _persist_result(self, result: ExecutionResult) -> None:
        """Store the terminal result so the agent API can report it (Section 11.4 step 7).

        Publishing to the Hive alone left the agent unable to answer for its own
        missions. A store that cannot persist must not kill the worker thread.
        """
        store = getattr(self.orchestrator, "store", None)
        saver = getattr(store, "save_mission_result", None)
        if saver is None:
            return
        try:
            saver(result)
        except Exception as exc:  # noqa: BLE001 - worker must survive a bad write
            _logger.error(f"failed to persist mission result {result.mission_id}: {exc}")

    def _emit_block_update(self, result: ExecutionResult) -> None:
        # Payload MUST be a full ExecutionResult dump (schemas.py) — not a blocks registry push.
        payload = result.model_dump(mode="json")
        message = {
            "msg_type": "block_update",
            "session_id": "-",
            "payload": {
                "mission_id": payload["mission_id"],
                "status": payload["status"],
                "outputs": payload["outputs"],
                "citations": payload["citations"],
                "confidence": payload["confidence"],
            },
            "vector_clock": {},
        }
        self._publish(message)

    def _publish(self, message: dict[str, Any]) -> None:
        if self.publish_fn is not None:
            self.publish_fn(message)
