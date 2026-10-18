#!/usr/bin/env python3
"""Live E2E verification — spec Section 11.4 step 7. Exit 1 on any failure.

Runs the spec's assertions against a live stack (real LLM, real HTTP), for
every evidence mission in tests/live/evidence_missions.json (synthetic
documents that differ in layout, row position and wording):

  positive: each mission WITH its attached documents must reach status
            success|partial, return the expected values (the right row), carry
            non-empty citations whose every excerpt appears VERBATIM in its
            documents, and confidence >= 0.9.
Each case names its kit's domain; there is one refusal case per kit.

  negative: each refusal case, sent WITHOUT evidence, must be refused per Section 8.3 --
            status "failure", confidence 0.0, no citations, and a reason naming
            evidence.

POST /mission is asynchronous (it returns a queue receipt), so the terminal
ExecutionResult is read back from GET /mission/{id}.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import httpx

MISSIONS = Path(__file__).resolve().parents[1] / "tests" / "live" / "evidence_missions.json"


def _post_mission(base: str, token: str, body: dict[str, Any]) -> str:
    response = httpx.post(
        f"{base}/mission",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["mission_id"]


def _await_result(base: str, token: str, mission_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        response = httpx.get(
            f"{base}/mission/{mission_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        if response.status_code == 404:
            time.sleep(1.0)
            continue
        response.raise_for_status()
        last = response.json()
        if last.get("result") is not None:
            return last["result"]
        time.sleep(1.0)
    raise TimeoutError(
        f"mission {mission_id} produced no result within {timeout_s}s; last body: {json.dumps(last)[:600]}"
    )


def _found(result: dict[str, Any], field: str) -> list[Any]:
    """Every value the mission's step outputs hold under ``field``, at any depth."""
    out: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == field and isinstance(val, (int, float)) and not isinstance(val, bool):
                    out.append(val)
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    # Only passed steps count: a refused step may carry the model's unverified answer.
    walk([o.get("output") for o in result.get("outputs") or [] if o.get("validation_status") == "passed"])
    return out


def _check_expected(result: dict[str, Any], expect: dict[str, Any], failures: list[str], tag: str) -> None:
    for field, want in expect.items():
        got = _found(result, field)
        if not any(abs(float(v) - float(want)) < 1e-6 for v in got):
            failures.append(f"{tag}: {field} expected {want}, got {got or 'nothing'}")


def _check_positive(result: dict[str, Any], evidence: str, failures: list[str]) -> None:
    status = result.get("status")
    if status not in ("success", "partial"):
        failures.append(f"status was {status!r}, expected success|partial")

    citations = result.get("citations") or []
    if not citations:
        failures.append("citations were empty; spec requires non-empty citations")

    for citation in citations:
        excerpt = (citation or {}).get("excerpt", "")
        if not excerpt or excerpt not in evidence:
            failures.append(
                f"citation excerpt not verbatim in evidence: {excerpt!r}"
            )

    confidence = result.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < 0.9:
        failures.append(f"confidence was {confidence!r}, expected >= 0.9")


def _check_refusal(result: dict[str, Any], failures: list[str]) -> None:
    if result.get("status") != "failure":
        failures.append(f"refusal status was {result.get('status')!r}, expected 'failure'")
    if result.get("confidence") != 0.0:
        failures.append(f"refusal confidence was {result.get('confidence')!r}, expected 0.0")
    if result.get("citations"):
        failures.append("refusal carried citations; spec Section 8.3 requires none")
    reasons = " ".join(
        str((output or {}).get("output", {}).get("reason", ""))
        for output in (result.get("outputs") or [])
    ).lower()
    if "evidence" not in reasons:
        failures.append(f"refusal reason did not name evidence: {reasons[:200]!r}")


def _stage(service: str, mission_id: str, documents: dict[str, str]) -> list[str]:
    """Write a mission's documents into the agent container; their paths there."""
    paths = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in documents.items():
            local = Path(tmp) / name
            local.write_text(text, encoding="utf-8")
            remote = f"/data/evidence/{mission_id}/{name}"
            subprocess.run(
                ["docker", "compose", "exec", "-T", service, "mkdir", "-p", f"/data/evidence/{mission_id}"],
                check=True,
            )
            subprocess.run(["docker", "compose", "cp", str(local), f"{service}:{remote}"], check=True)
            paths.append(remote)
    return paths


def _mission_body(case: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """POST /mission body for a case: its own words, its own kit's domain."""
    return {
        "natural_language": case["natural_language"],
        "domain_hint": case["domain_hint"],
        "attached_evidence": [{"type": _evidence_type(p), "path": p} for p in paths],
        "required_confidence": 0.9,
    }


def _evidence_type(name: str) -> str:
    suffix = Path(name).suffix.lstrip(".").lower()
    return suffix if suffix in ("txt", "csv", "pdf", "json") else "txt"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Spec 11.4 step 7 live E2E check")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--service", default="agent-instance", help="compose service the documents are staged into")
    parser.add_argument("--missions", default=str(MISSIONS))
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    spec = json.loads(Path(args.missions).read_text(encoding="utf-8"))
    failures: list[str] = []
    total = len(spec["missions"]) + len(spec["refusals"])
    slowest_ms = 0

    for n, case in enumerate(spec["missions"], start=1):
        paths = _stage(args.service, case["id"], case["documents"])
        body = _mission_body(case, paths)
        sys.stdout.write(f"[{n}/{total}] {case['id']}: mission WITH evidence\n")
        mission_id = _post_mission(args.base_url, args.token, body)
        result = _await_result(args.base_url, args.token, mission_id, args.timeout)
        sys.stdout.write(json.dumps(result, indent=2)[:3000] + "\n")
        step_ms = [int(o.get("execution_time_ms") or 0) for o in result.get("outputs") or []]
        slowest_ms = max([slowest_ms, *step_ms])
        sys.stdout.write(f"timing {case['id']}: step ms {step_ms}\n")
        before = len(failures)
        _check_positive(result, "".join(case["documents"].values()), failures)
        _check_expected(result, case["expect"], failures, case["id"])
        for i in range(before, len(failures)):
            if not failures[i].startswith(case["id"]):
                failures[i] = f"{case['id']}: {failures[i]}"

    for n, case in enumerate(spec["refusals"], start=len(spec["missions"]) + 1):
        sys.stdout.write(f"[{n}/{total}] {case['domain_hint']}: mission WITHOUT evidence (must be refused)\n")
        refusal_id = _post_mission(args.base_url, args.token, _mission_body(case, []))
        refusal = _await_result(args.base_url, args.token, refusal_id, args.timeout)
        sys.stdout.write(json.dumps(refusal, indent=2)[:2000] + "\n")
        before = len(failures)
        _check_refusal(refusal, failures)
        for i in range(before, len(failures)):
            failures[i] = f"refusal {case['domain_hint']}: {failures[i]}"

    sys.stdout.write(f"\nslowest step: {slowest_ms} ms\n")
    if failures:
        sys.stdout.write("\nLIVE E2E FAILED (spec 11.4 step 7):\n")
        for failure in failures:
            sys.stdout.write(f"  - {failure}\n")
        return 1
    sys.stdout.write(
        f"\nLIVE E2E PASSED (spec 11.4 step 7): {len(spec['missions'])} evidence missions"
        f" + {len(spec['refusals'])} refusals.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
