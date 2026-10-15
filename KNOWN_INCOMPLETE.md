# KNOWN_INCOMPLETE

Registered deferred work only. Hollow reachable production code is forbidden
unless listed under **Path allow-list**. CI and `make detect` read this file.

## Path allow-list (stub_detector completeness gate)

Paths below are exempt from stub findings while registered incomplete.
Format (one per line): `- path: relative/path.py`

(none — zero hollow paths permitted on this branch)

## Phase B — executed in CI (Linux + Docker runners)

Both Phase B items now run on every code change; neither is skipped.

### verify-airgap

- Status: **runs in CI**: job "Phase B verify-airgap (spec 8.7)", command `make verify-airgap`
- The verifier (`agent/security/airgap.py`) keeps every check unconditional (amendment A4): no
  `TEST_MODE` relaxation. Its Ollama probe needs a model server answering inside the
  `--network none` container, so `agent/Dockerfile.airgap` carries the Ollama binary from the
  same digest-pinned image the stack's bundled server uses, and `agent/airgap_entrypoint.sh`
  starts it on loopback before the command runs.

### Live E2E — SELF_AGENT_SPEC_V1.md §11.4 step 7

- Status: **runs in CI**: job "Phase B live E2E (spec 11.4 step 7)": `make up` on the bundled
  Ollama, then `scripts/live_e2e_check.py` (every evidence mission plus the refusal).
- The spec's single curl mission is a subset of what that job checks.

### Windows spawn caveat

- Status: **WEAK** evidence on Windows host (subprocess / ProactorEventLoop teardown warnings observed in integration runs)
- Action: re-run Phase A spawn/integration suite and Phase B airgap/live E2E on Linux+Docker before claiming full §11.4 completion

### Critical-path triviality rule (`TRIVIALITY_ALLOWLIST`)

- Status: **enforced** (owner ruling 2). The `<2` meaningful-statement rule applies to every critical-path file; statements nested in compound statements count. Route handlers (`@<router>.<verb>("/path")`) are exempt as a class, by structure; no file or function name is exempt. The one-line helpers on these paths were folded into their callers.
