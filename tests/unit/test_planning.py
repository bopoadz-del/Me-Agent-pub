"""Planning selects the blocks a mission names, closes over dependencies, fails honestly."""
from __future__ import annotations

from agent.orchestrator.engine import HeadlessOrchestrator
from agent.swarm.registry import BlockRegistry
from common.models.schemas import BlockDef, ExecutionPlan, MissionProfile, PlanStep


def _block(block_id: str, deps: list[str] | None = None) -> BlockDef:
    return BlockDef(
        block_id=block_id,
        name=block_id,
        domain="kit",
        input_schema={"type": "object"},
        output_json_schema={"type": "object"},
        system_prompt_template="x",
        tools=[],
        dependencies=deps or [],
        version_clock=1,
    )


def _orch(*blocks: BlockDef) -> HeadlessOrchestrator:
    registry = BlockRegistry()
    for block in blocks:
        registry.register(block)
    return HeadlessOrchestrator(registry, retriever=None, spawner=None, store=None)


def _ids(plan: ExecutionPlan) -> list[str]:
    return [s.block_id for s in plan.steps]


def test_named_block_runs_after_its_dependency_and_unnamed_blocks_do_not_run():
    orch = _orch(
        _block("kit_widget_total", ["kit_rates_table"]),
        _block("kit_rates_table"),
        _block("kit_unrelated_gate"),
    )
    plan = orch.resolve(MissionProfile(natural_language="Compute widget total", domain_hint="kit"))
    assert _ids(plan) == ["kit_rates_table", "kit_widget_total"]


def test_inflected_mission_word_still_names_the_block():
    orch = _orch(_block("kit_rates_table"), _block("kit_other"))
    plan = orch.resolve(MissionProfile(natural_language="look up the rate", domain_hint="kit"))
    assert _ids(plan) == ["kit_rates_table"]


def test_missing_dependency_is_refused_not_skipped():
    orch = _orch(_block("kit_widget_total", ["kit_absent"]))
    result = orch.execute(
        orch.resolve(MissionProfile(natural_language="widget total", domain_hint="kit"))
    )
    assert result.status == "failure" and result.confidence == 0.0
    assert "kit_absent" in result.outputs[0].output["reason"]


def test_missing_block_at_execution_is_failure_never_success():
    orch = _orch(_block("kit_present"))
    plan = ExecutionPlan(
        mission_id="m1",
        steps=[PlanStep(block_id="kit_vanished", prompt_scope="x", tool_set=[], timeout_seconds=5)],
    )
    result = orch.execute(plan)
    assert result.status == "failure"
    assert result.confidence == 0.0
    assert result.outputs[0].output["reason"] == "missing block"
