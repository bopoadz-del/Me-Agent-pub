"""Every sheet in domain-kits/sheets is a working kit, and kits do not collide.

The compiler, the hive image and `make compile-domain` take every sheet the
same way; none is named in code. These checks hold for each sheet that is
there, so a new kit is covered by adding its sheet, not a test.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

SHEETS_DIR = Path(__file__).resolve().parents[2] / "domain-kits" / "sheets"
SHEETS = sorted(SHEETS_DIR.glob("*.yaml")) + sorted(SHEETS_DIR.glob("*.yml"))


def _compile(sheet: Path, tmp_path: Path) -> tuple[dict, list[dict]]:
    from domain_kits.compiler.engine import compile_sheet

    out = compile_sheet(sheet, out_dir=tmp_path / "blocks", tests_out_dir=tmp_path / "tests")
    return out, [json.loads(Path(p).read_text(encoding="utf-8")) for p in out["blocks"]]


def test_there_is_more_than_one_kit():
    assert len(SHEETS) >= 2, [s.name for s in SHEETS]


@pytest.mark.parametrize("sheet", SHEETS, ids=lambda p: p.stem)
def test_each_sheet_compiles_to_valid_blocks_carrying_its_formulas(sheet, tmp_path):
    from common.models.schemas import BlockDef

    out, blocks = _compile(sheet, tmp_path)
    spec = yaml.safe_load(sheet.read_text(encoding="utf-8"))
    for block in blocks:
        BlockDef.model_validate(block)
        assert block["domain"] == out["domain"]
    by_id = {b["block_id"]: b for b in blocks}
    for calc in spec.get("calculations") or []:
        lhs, rhs = (part.strip() for part in calc["formula"].split("=", 1))
        assert by_id[f"{out['domain']}_{calc['name']}"]["formulas"] == {lhs: rhs}
    for block in blocks:
        for dep in block["dependencies"]:
            assert dep in by_id, f"{block['block_id']} depends on {dep}, which its own kit lacks"
    assert len(out["tests"]) == len(spec.get("failure_modes") or []) >= 1


def test_no_two_kits_share_a_domain_or_a_block_id(tmp_path):
    domains: list[str] = []
    ids: list[str] = []
    for n, sheet in enumerate(SHEETS):
        out, blocks = _compile(sheet, tmp_path / str(n))
        domains.append(out["domain"])
        ids.extend(b["block_id"] for b in blocks)
    assert len(set(domains)) == len(domains), domains
    assert len(set(ids)) == len(ids), sorted(ids)
