"""Headless mission orchestrator."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import deque
from typing import Any, Optional

from agent.swarm.registry import BlockRegistry
from agent.swarm.spawner import SubAgentSpawner
from common.logging import OPERATIONAL_CONSTRAINTS, get_logger
from common.models.schemas import (
    Citation,
    ExecutionPlan,
    ExecutionResult,
    MissionProfile,
    MissionState,
    PlanStep,
    StepOutput,
)

_logger = get_logger("agent.orchestrator")


def _topological_sort(blocks: list[Any]) -> list[Any]:
    by_id = {b.block_id: b for b in blocks}
    indeg: dict[str, int] = {bid: 0 for bid in by_id}
    graph: dict[str, list[str]] = {bid: [] for bid in by_id}
    for block in blocks:
        for dep in block.dependencies:
            if dep not in by_id:
                continue
            graph[dep].append(block.block_id)
            indeg[block.block_id] += 1
    queue = deque([bid for bid, deg in indeg.items() if deg == 0])
    ordered: list[Any] = []
    while queue:
        bid = queue.popleft()
        ordered.append(by_id[bid])
        for nxt in graph[bid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(ordered) != len(blocks):
        raise ValueError("dependency cycle detected")
    return ordered


_WORD = re.compile(r"[a-z0-9]+")


def _select_blocks(natural_language: str, candidates: list[Any]) -> list[Any]:
    """The candidates the mission names most strongly. Empty when none is named.

    A block's relevance is the number of mission words (3+ letters) that name it,
    read from its id and name only; a word names it up to inflection, one being
    a prefix of the other ("tariff"/"tariffs")."""
    mission_words = {w for w in _WORD.findall(natural_language.lower()) if len(w) >= 3}
    scored = []
    for block in candidates:
        identity = [w for w in _WORD.findall(f"{block.block_id} {block.name}".lower()) if len(w) >= 3]
        score = sum(1 for w in mission_words if any(w.startswith(i) or i.startswith(w) for i in identity))
        scored.append((score, block))
    best = max((score for score, _ in scored), default=0)
    if best == 0:
        return []
    return [b for score, b in scored if score == best]


def _with_dependencies(selected: list[Any], registry: Any) -> tuple[list[Any], list[str]]:
    """Close the selection over declared dependencies; report any that are missing."""
    by_id = {b.block_id: b for b in selected}
    missing: list[str] = []
    pending = [dep for b in selected for dep in b.dependencies]
    while pending:
        dep = pending.pop()
        if dep in by_id or dep in missing:
            continue
        block = registry.get(dep)
        if block is None:
            missing.append(dep)
            continue
        by_id[dep] = block
        pending.extend(block.dependencies)
    return list(by_id.values()), missing


class HeadlessOrchestrator:
    def __init__(self, registry: BlockRegistry, retriever: Any, spawner: SubAgentSpawner, store: Any) -> None:
        self.registry = registry if isinstance(registry, BlockRegistry) else _wrap_registry(registry)
        self.retriever = retriever
        self.spawner = spawner
        self.store = store
        self._last_profile: Optional[MissionProfile] = None

    def resolve(self, profile: MissionProfile) -> ExecutionPlan:
        self._last_profile = profile
        domain = profile.domain_hint
        candidates = self.registry.all_blocks()
        # A hint that selects its domain's blocks confines the whole plan to them.
        confined_to: Optional[str] = None
        if domain:
            domain_blocks = self.registry.blocks_for_domain(domain)
            if domain_blocks:
                candidates = domain_blocks
                confined_to = domain
        elif self.retriever is not None and hasattr(self.retriever, "search_blocks"):
            candidates = self.retriever.search_blocks(profile.natural_language) or candidates
        evidence_required = any(b.evidence_standard for b in candidates)
        if evidence_required and not profile.attached_evidence:
            standard = next(
                (b.evidence_standard for b in candidates if b.evidence_standard),
                "evidence required",
            )
            refusal = PlanStep(
                block_id="__refusal__",
                prompt_scope=f"refuse: {standard}",
                tool_set=[],
                timeout_seconds=1,
            )
            return ExecutionPlan(mission_id=profile.mission_id, steps=[refusal])
        selected, missing = _with_dependencies(
            _select_blocks(profile.natural_language, candidates), self.registry
        )
        if missing:
            refusal = PlanStep(
                block_id="__refusal__",
                prompt_scope=f"missing dependency block: {', '.join(sorted(missing))}",
                tool_set=[],
                timeout_seconds=1,
            )
            return ExecutionPlan(mission_id=profile.mission_id, steps=[refusal])
        outside = sorted({b.domain for b in selected if confined_to and b.domain != confined_to})
        if outside:
            refusal = PlanStep(
                block_id="__refusal__",
                prompt_scope=f"dependency outside the mission's domain {confined_to}: {', '.join(outside)}",
                tool_set=[],
                timeout_seconds=1,
            )
            return ExecutionPlan(mission_id=profile.mission_id, steps=[refusal])
        if not selected:
            refusal = PlanStep(
                block_id="__refusal__",
                prompt_scope="no matching blocks",
                tool_set=[],
                timeout_seconds=1,
            )
            return ExecutionPlan(mission_id=profile.mission_id, steps=[refusal])
        ordered = _topological_sort(selected)
        steps: list[PlanStep] = []
        total_cost = 0.0
        for block in ordered:
            cost = 0.05
            total_cost += cost
            steps.append(
                PlanStep(
                    block_id=block.block_id,
                    prompt_scope=profile.natural_language,
                    tool_set=list(block.tools),
                    timeout_seconds=OPERATIONAL_CONSTRAINTS["subagent_default_timeout_sec"],
                    estimated_cost_usd=cost,
                )
            )
        if total_cost > profile.max_cost_usd:
            refusal = PlanStep(
                block_id="__refusal__",
                prompt_scope="cost",
                tool_set=[],
                timeout_seconds=1,
            )
            return ExecutionPlan(mission_id=profile.mission_id, steps=[refusal])
        return ExecutionPlan(mission_id=profile.mission_id, steps=steps)

    def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        if not plan.steps:
            return ExecutionResult(
                mission_id=plan.mission_id,
                status="failure",
                outputs=[],
                citations=[],
                confidence=0.0,
            )
        if plan.steps[0].block_id == "__refusal__":
            reason = plan.steps[0].prompt_scope
            if reason == "cost":
                text = "cost limit exceeded"
            elif reason.startswith("refuse:"):
                text = f"evidence required: {reason.split(':', 1)[1].strip()}"
            else:
                text = reason
            out = StepOutput(
                step_id=plan.steps[0].step_id,
                block_id="__refusal__",
                output={"reason": text},
                validation_status="failed",
                execution_time_ms=0,
            )
            self._checkpoint(plan.mission_id, out)
            return ExecutionResult(
                mission_id=plan.mission_id,
                status="failure",
                outputs=[out],
                citations=[],
                confidence=0.0,
            )
        outputs: list[StepOutput] = []
        citations: list[Citation] = []
        confidences: list[float] = []
        evidence_paths = self._evidence_paths(plan)
        done: dict[str, dict[str, Any]] = {}
        state = MissionState(mission_id=plan.mission_id, status="running")
        if self.store is not None:
            self.store.save_mission_state(state)
        for step in plan.steps:
            block = self.registry.get(step.block_id)
            if block is None:
                out = StepOutput(
                    step_id=step.step_id,
                    block_id=step.block_id,
                    output={"reason": "missing block"},
                    validation_status="failed",
                    execution_time_ms=0,
                )
                outputs.append(out)
                self._checkpoint(plan.mission_id, out)
                if self.store is not None:
                    self.store.update_mission_status(plan.mission_id, "failed")
                return ExecutionResult(
                    mission_id=plan.mission_id,
                    status="failure",
                    outputs=outputs,
                    citations=citations,
                    confidence=0.0,
                )
            if block.evidence_standard and not evidence_paths:
                out = StepOutput(
                    step_id=step.step_id,
                    block_id=step.block_id,
                    output={"reason": f"evidence required: {block.evidence_standard}"},
                    validation_status="failed",
                    execution_time_ms=0,
                )
                outputs.append(out)
                self._checkpoint(plan.mission_id, out)
                return ExecutionResult(
                    mission_id=plan.mission_id,
                    status="failure",
                    outputs=outputs,
                    citations=[],
                    confidence=0.0,
                )
            upstream = {
                dep: done[dep] for dep in block.dependencies if dep in done
            }
            out = self.spawner.run_block(step, upstream, evidence_paths)
            outputs.append(out)
            self._checkpoint(plan.mission_id, out)
            # Rule 9 / V1.1: confidence is orchestrator-assigned from validation,
            # never echoed from LLM top-level or citation confidence floats.
            step_conf = 1.0 if out.validation_status == "passed" else 0.0
            confidences.append(step_conf)
            raw = out.output.get("citations")
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    key = (str(item.get("source", "evidence")), str(item.get("excerpt", "")))
                    if any((c.source, c.excerpt) == key for c in citations):
                        continue
                    citations.append(
                        Citation(
                            source=str(item.get("source", "evidence")),
                            excerpt=str(item.get("excerpt", "")),
                            confidence=step_conf,
                        )
                    )
            done[step.block_id] = out.output
            if out.validation_status == "failed":
                if self.store is not None:
                    self.store.update_mission_status(plan.mission_id, "failed")
                return ExecutionResult(
                    mission_id=plan.mission_id,
                    status="failure",
                    outputs=outputs,
                    citations=citations,
                    confidence=0.0,
                )
        if not confidences:
            return ExecutionResult(
                mission_id=plan.mission_id,
                status="failure",
                outputs=outputs,
                citations=citations,
                confidence=0.0,
            )
        confidence = sum(confidences) / len(confidences)
        if self.store is not None:
            self.store.update_mission_status(plan.mission_id, "completed")
        return ExecutionResult(
            mission_id=plan.mission_id,
            status="success",
            outputs=outputs,
            citations=citations,
            confidence=confidence,
        )

    def _evidence_paths(self, plan: ExecutionPlan) -> list[str]:
        profile = self._last_profile
        if profile is None and self.store is not None:
            profile = getattr(self.store, "last_profile", None)
        if profile is None:
            return []
        paths: list[str] = []
        for item in profile.attached_evidence:
            path = item.get("path")
            if path:
                paths.append(path)
        return paths

    def _checkpoint(self, mission_id: str, step_output: StepOutput) -> None:
        if self.store is None:
            return
        self.store.checkpoint(mission_id, step_output)


def _wrap_registry(raw: Any) -> BlockRegistry:
    reg = BlockRegistry()
    if isinstance(raw, dict):
        for bid, block in raw.items():
            reg.register(block)
    return reg


def _data_dir_bytes(path: str) -> int:
    """Bytes consumed by the agent's own data directory."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                # Vanished mid-walk or unreadable: it cannot be counted, and a
                # stat error must not wedge the mission API shut.
                continue
    return total


def disk_over_limit(path: str = "/data") -> bool:
    """True when the AGENT'S data directory exceeds max_disk_gb.

    Previously this compared ``shutil.disk_usage(path).used`` -- a volume-wide
    figure -- against the limit. A Docker named volume reports the host filesystem,
    so on any ordinary host that total is already past 10 GB and the mission API
    answered 429 to everything. TEST_MODE returned False unconditionally, which hid
    it from the suite: the real computation never ran in a test.

    Measuring the agent's own footprint is what Section 10 means by max_disk_gb,
    and it is correct regardless of how /data is mounted.
    """
    if not os.path.isdir(path):
        return False
    limit_bytes = OPERATIONAL_CONSTRAINTS["max_disk_gb"] * (1024**3)
    return _data_dir_bytes(path) > limit_bytes
