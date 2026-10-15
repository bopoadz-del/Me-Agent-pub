"""Phase 3: the spec's Phase B verification runs in CI instead of being skipped.

`make verify-airgap` (SELF_AGENT_SPEC_V1.md section 8.7 / 11.4) was only
registered as deferred: CI printed a SKIP notice. The verifier needs an
Ollama answering inside the --network none container, and the airgap image
had none, so it could only fail closed. The airgap image now carries the
Ollama binary from the same digest-pinned image the stack uses, its
entrypoint starts it on loopback before running the command, and CI runs
`make verify-airgap` as a job. Nothing in Phase B is skipped any more.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _ci() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def _steps(ci: dict) -> list[dict]:
    return [step for job in ci["jobs"].values() for step in job.get("steps", [])]


def test_ci_runs_make_verify_airgap():
    runs = [str(step.get("run", "")) for step in _steps(_ci())]
    assert any(re.search(r"^\s*make verify-airgap\s*$", run, re.M) for run in runs)


def test_no_phase_b_step_is_a_skip():
    skips = [step.get("name", "") for step in _steps(_ci()) if str(step.get("name", "")).lower().startswith("skip")]
    assert not skips, f"Phase B steps still skipped: {skips}"


def test_the_airgap_image_brings_its_own_pinned_ollama():
    dockerfile = (ROOT / "agent" / "Dockerfile.airgap").read_text(encoding="utf-8")
    copy = re.search(r"^COPY --from=(\S+) \S*ollama\S* ", dockerfile, re.M)
    assert copy, "the airgap image copies no ollama binary"
    assert "@sha256:" in copy.group(1), "the ollama source image is not pinned by digest"
    assert re.search(r"^ENTRYPOINT .*airgap_entrypoint\.sh", dockerfile, re.M)


def test_the_entrypoint_starts_ollama_on_loopback_before_the_command():
    script = (ROOT / "agent" / "airgap_entrypoint.sh").read_text(encoding="utf-8")
    assert "ollama serve" in script
    assert "127.0.0.1" in script
    assert script.rstrip().endswith('exec "$@"')


def test_known_incomplete_no_longer_defers_verify_airgap():
    text = (ROOT / "KNOWN_INCOMPLETE.md").read_text(encoding="utf-8")
    section = text.split("### verify-airgap", 1)[1].split("###", 1)[0]
    assert "deferred" not in section.lower()
