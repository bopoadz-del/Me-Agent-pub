"""A grounded block never takes a retry answer made without its evidence.

Live E2E on 1d57213 (csv-middle-row): the child's reply failed the output
schema, the parent retried the LLM with no evidence, accepted a schema-valid
invented row (base rate 0, no citations) and marked the step passed.
"""
from __future__ import annotations

import queue
import threading

import pytest

from agent.swarm import spawner as spawner_mod
from agent.swarm.registry import BlockRegistry
from common.models.schemas import BlockDef, PlanStep

SCHEMA = {
    "type": "object",
    "properties": {"matched": {"type": "boolean"}, "rate": {"type": "number"}},
    "required": ["matched", "rate"],
    "additionalProperties": False,
}


class _InlineProcess:
    def __init__(self, target, args):
        self._thread = threading.Thread(target=target, args=args)
        self.pid = 1

    def start(self):
        self._thread.start()

    def join(self, timeout=None):
        self._thread.join(timeout)

    def is_alive(self):
        return self._thread.is_alive()


class _InlineContext:
    Process = _InlineProcess
    Queue = queue.Queue


def _child_with_invalid_reply(block, step, paths, context, env, result_queue):
    result_queue.put({"ok": True, "output": {"matched": "yes"}, "citations": [], "unheld": []})


class _ParentLLM:
    calls = 0

    def generate(self, *a, **k):
        _ParentLLM.calls += 1
        return {"matched": True, "rate": 0}  # schema-valid, grounded in nothing


def _block(standard: str) -> BlockDef:
    return BlockDef(
        block_id="lookup", name="lookup", domain="d", version_clock=1,
        system_prompt_template="look it up", input_schema={}, output_json_schema=SCHEMA,
        tools=[], evidence_standard=standard,
    )


@pytest.fixture()
def inline(monkeypatch):
    monkeypatch.setattr(spawner_mod, "_SPAWN", _InlineContext)
    monkeypatch.setattr(spawner_mod, "_child_worker", _child_with_invalid_reply)
    _ParentLLM.calls = 0


def _run(block, evidence):
    registry = BlockRegistry()
    registry.register(block)
    sp = spawner_mod.SubAgentSpawner(registry, llm_client=_ParentLLM())
    step = PlanStep(step_id="s", block_id="lookup", prompt_scope="find the rate", tool_set=[])
    return sp._spawn_block(block, step, evidence, {}, None)


def test_a_grounded_block_is_refused_not_retried_without_evidence(inline):
    out = _run(_block("rates must be quoted from the evidence"), ["/data/evidence/m/t.csv"])
    assert out.validation_status == "failed"
    assert "schema" in out.output["reason"]
    assert _ParentLLM.calls == 0


def test_a_block_without_evidence_keeps_the_schema_retry(inline):
    out = _run(_block(""), [])
    assert _ParentLLM.calls == 1
    assert out.validation_status == "passed"
