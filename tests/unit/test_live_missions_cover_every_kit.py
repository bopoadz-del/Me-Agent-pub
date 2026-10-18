"""The live E2E check (spec 11.4 step 7) covers every kit, from data.

Each mission and each refusal in tests/live/evidence_missions.json names the
domain it runs in, the checker sends that domain and no other, and every
sheet's domain has at least one evidence mission and one refusal.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MISSIONS = json.loads((ROOT / "tests" / "live" / "evidence_missions.json").read_text(encoding="utf-8"))
SHEET_DOMAINS = {
    yaml.safe_load(p.read_text(encoding="utf-8"))["domain"]
    for p in sorted((ROOT / "domain-kits" / "sheets").glob("*.y*ml"))
}


def _checker():
    spec = importlib.util.spec_from_file_location("live_e2e_check", ROOT / "scripts" / "live_e2e_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_mission_and_refusal_names_a_kit_domain():
    for case in [*MISSIONS["missions"], *MISSIONS["refusals"]]:
        assert case.get("domain_hint") in SHEET_DOMAINS, case


def test_every_kit_has_an_evidence_mission_and_a_refusal():
    assert {c["domain_hint"] for c in MISSIONS["missions"]} == SHEET_DOMAINS
    assert {c["domain_hint"] for c in MISSIONS["refusals"]} == SHEET_DOMAINS


def test_the_checker_sends_the_domain_the_case_names():
    checker = _checker()
    body = checker._mission_body({"natural_language": "x", "domain_hint": "some_kit"}, ["/data/evidence/a.txt"])
    assert body["domain_hint"] == "some_kit"
    assert body["attached_evidence"] == [{"type": "txt", "path": "/data/evidence/a.txt"}]
    refusal = checker._mission_body({"natural_language": "x", "domain_hint": "other_kit"}, [])
    assert refusal["domain_hint"] == "other_kit" and refusal["attached_evidence"] == []
