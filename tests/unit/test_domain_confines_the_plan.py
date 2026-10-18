"""A domain hint confines the plan to that domain (issue #15).

When a mission's domain hint selects that domain's blocks, every block the
plan pulls in through dependencies must belong to the same domain; a
dependency in another domain refuses the plan, naming the domain. Blocks can
reach the registry from any source (kit sheets, the hive grant, admin
uploads), so the rule is checked at planning time, not only by kit tests.
"""
from __future__ import annotations

from common.models.schemas import BlockDef, MissionProfile


def _block(block_id: str, domain: str, deps: list[str] | None = None) -> BlockDef:
    return BlockDef(
        block_id=block_id,
        name=block_id,
        domain=domain,
        input_schema={"type": "object"},
        output_json_schema={"type": "object"},
        system_prompt_template="x",
        tools=[],
        dependencies=deps or [],
        version_clock=1,
    )


def _orchestrator(*blocks: BlockDef):
    from agent.orchestrator.engine import HeadlessOrchestrator
    from agent.swarm.registry import BlockRegistry

    registry = BlockRegistry()
    for block in blocks:
        registry.register(block)
    return HeadlessOrchestrator(registry, retriever=None, spawner=None, store=None)


def test_a_dependency_in_another_domain_refuses_a_hinted_plan():
    orch = _orchestrator(_block("alpha_fee", "alpha", deps=["beta_rates"]), _block("beta_rates", "beta"))
    plan = orch.resolve(MissionProfile(natural_language="compute the alpha fee", domain_hint="alpha"))
    assert [s.block_id for s in plan.steps] == ["__refusal__"]
    assert "beta" in plan.steps[0].prompt_scope and "domain" in plan.steps[0].prompt_scope


def test_dependencies_inside_the_domain_still_plan():
    orch = _orchestrator(_block("alpha_fee", "alpha", deps=["alpha_rates"]), _block("alpha_rates", "alpha"),
                         _block("beta_rates", "beta"))
    plan = orch.resolve(MissionProfile(natural_language="compute the alpha fee", domain_hint="alpha"))
    assert [s.block_id for s in plan.steps] == ["alpha_rates", "alpha_fee"]


def test_without_a_hint_dependencies_may_cross_domains_as_before():
    orch = _orchestrator(_block("alpha_fee", "alpha", deps=["beta_rates"]), _block("beta_rates", "beta"))
    plan = orch.resolve(MissionProfile(natural_language="compute the alpha fee"))
    assert [s.block_id for s in plan.steps] == ["beta_rates", "alpha_fee"]
