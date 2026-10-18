"""The second kit (concrete mix design) runs on the same grounding and refusal
rules as the first, with no code of its own, and loading it changes nothing
for the port kit.

Its numbers are the recommended limiting values of EN 206 Annex F (Table F.1);
the tests use them only as evidence text, never as code paths.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.swarm.grounding import chunk_evidence, parse_reply

SHEETS_DIR = Path(__file__).resolve().parents[2] / "domain-kits" / "sheets"
CONCRETE = SHEETS_DIR / "concrete_mix.yaml"
PORT = SHEETS_DIR / "port_ops.yaml"


@pytest.fixture
def blocks(tmp_path):
    from common.models.schemas import BlockDef
    from domain_kits.compiler.engine import compile_sheet

    out: dict[str, BlockDef] = {}
    for sheet in (PORT, CONCRETE):
        compiled = compile_sheet(sheet, out_dir=tmp_path / sheet.stem, tests_out_dir=tmp_path / "t")
        for path in compiled["blocks"]:
            block = BlockDef.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
            out[block.block_id] = block
    return out


@pytest.fixture
def orchestrator(blocks):
    from agent.orchestrator.engine import HeadlessOrchestrator
    from agent.swarm.registry import BlockRegistry

    registry = BlockRegistry()
    for block in blocks.values():
        registry.register(block)
    return HeadlessOrchestrator(registry, retriever=None, spawner=None, store=None)


def _ratio_block(blocks):
    return next(b for b in blocks.values() if b.domain == "concrete_mix" and b.formulas)


MIX = "Mix design for exposure class XC3.\nFree water: 165 kg/m3.\nCement: 330 kg/m3.\n"


def test_the_ratio_is_computed_from_its_cited_operands_not_taken_from_the_model(blocks):
    block = _ratio_block(blocks)
    chunks = chunk_evidence([("mix.txt", MIX)])
    result, citations, unheld = parse_reply(
        {"water_cement_ratio": 0.6}, chunks, block.output_json_schema, block.formulas
    )
    assert unheld == []
    assert result["water_cement_ratio"] == 0.5
    assert {c["excerpt"] for c in citations} == {"Free water: 165 kg/m3.", "Cement: 330 kg/m3."}


def test_a_ratio_the_evidence_cannot_give_is_refused(blocks):
    block = _ratio_block(blocks)
    chunks = chunk_evidence([("mix.txt", "Mix design for exposure class XC3.\nFree water: 165 kg/m3.\n")])
    _result, citations, unheld = parse_reply(
        {"water_cement_ratio": 0.5}, chunks, block.output_json_schema, block.formulas
    )
    assert unheld == ["water_cement_ratio"]
    assert citations == []


def test_a_concrete_mission_without_evidence_is_refused_naming_evidence(orchestrator):
    from common.models.schemas import MissionProfile

    profile = MissionProfile(
        natural_language="Calculate the water cement ratio for an XC3 mix using attached mix design evidence",
        domain_hint="concrete_mix",
    )
    result = orchestrator.execute(orchestrator.resolve(profile))
    assert result.status == "failure" and result.confidence == 0.0 and result.citations == []
    assert "evidence" in result.outputs[0].output["reason"].lower()


@pytest.mark.parametrize("domain_hint", ["concrete_mix", None])
def test_a_concrete_mission_plans_only_concrete_blocks(orchestrator, domain_hint):
    from common.models.schemas import MissionProfile

    profile = MissionProfile(
        natural_language="Calculate the water cement ratio for an XC3 mix using attached mix design evidence",
        domain_hint=domain_hint,
        attached_evidence=[{"type": "txt", "path": "/data/evidence/mix.txt"}],
    )
    steps = orchestrator.resolve(profile).steps
    assert [s.block_id for s in steps] == ["concrete_mix_water_cement_ratio"]


@pytest.mark.parametrize("domain_hint", ["port_terminal", None])
def test_with_both_kits_loaded_a_port_mission_plans_what_it_did_alone(orchestrator, domain_hint):
    from common.models.schemas import MissionProfile

    profile = MissionProfile(
        natural_language="Calculate berth fee for a dry container using attached tariff evidence",
        domain_hint=domain_hint,
        attached_evidence=[{"type": "txt", "path": "/data/evidence/tariff.txt"}],
    )
    steps = orchestrator.resolve(profile).steps
    assert [s.block_id for s in steps] == ["port_terminal_tariffs_lookup", "port_terminal_berth_fee"]
