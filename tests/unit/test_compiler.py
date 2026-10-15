"""Acceptance tests for the domain kit compiler (spec Section 9.2)."""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

import pytest


@pytest.fixture
def tmp_generated_dir(tmp_path):
    """Section 9.2 uses this fixture; Section 12 omits it — defined locally."""
    out = tmp_path / "generated"
    out.mkdir()
    return str(out)


def test_compiler_generates_valid_blocks(tmp_generated_dir):
    from common.models.schemas import BlockDef
    from domain_kits.compiler.engine import compile_sheet

    compile_sheet("domain-kits/sheets/port_ops.yaml", out_dir=tmp_generated_dir)
    names = {os.path.basename(p) for p in glob.glob(f"{tmp_generated_dir}/*.json")}
    assert {
        "port_terminal_tariffs_lookup.json",
        "port_terminal_berth_fee.json",
        "port_terminal_max_dwell_gate.json",
    } <= names
    for p in glob.glob(f"{tmp_generated_dir}/*.json"):
        with open(p) as f:
            BlockDef.model_validate(json.load(f))
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/generated/", "-q"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_calculation_block_carries_its_formula_as_data(tmp_generated_dir):
    """The kit's formula travels on the block (BlockDef.formulas) so grounding
    can verify a computed value by recomputing it from cited operands -- for
    any kit, from the sheet, never from code."""
    from domain_kits.compiler.engine import compile_sheet

    compile_sheet("domain-kits/sheets/port_ops.yaml", out_dir=tmp_generated_dir)
    import yaml

    sheet = yaml.safe_load(open("domain-kits/sheets/port_ops.yaml", encoding="utf-8"))
    for calc in sheet.get("calculations") or []:
        lhs, rhs = (part.strip() for part in calc["formula"].split("=", 1))
        path = os.path.join(tmp_generated_dir, f"{sheet['domain']}_{calc['name']}.json")
        block = json.load(open(path, encoding="utf-8"))
        assert block["formulas"] == {lhs: rhs}
