"""Phase 2d: what the images install and run is pinned, and the code is not
writable by the user that runs it.

requirements.txt held version ranges only, so each build installed whatever
was newest that day, unverified; the bundled Ollama ran ollama/ollama:latest;
and both images chowned /app to the runtime user, so a compromised process
could rewrite its own code. Now:

- requirements.in holds the ranges; requirements.txt is the compiled lock
  (uv pip compile --universal --generate-hashes): every requirement exact,
  every one with hashes, installed with --require-hashes.
- every image the stack pulls is pinned by digest.
- /app stays owned by root; the runtime user owns only /data.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = sorted(p for p in ROOT.rglob("Dockerfile*") if ".git" not in p.parts)


def _requirements() -> list[str]:
    """Logical requirement entries of the lock (continuation lines joined)."""
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8").replace("\\\n", " ")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_every_locked_requirement_is_exact_and_hashed():
    entries = _requirements()
    assert entries, "requirements.txt is empty"
    loose = [e.split()[0] for e in entries if "==" not in e.split(";")[0] or "--hash=sha256:" not in e]
    assert not loose, f"not exact and hashed: {loose}"


def test_every_direct_requirement_is_in_the_lock():
    locked = {re.split(r"[=;\s\[]", e, 1)[0].lower().replace("_", "-") for e in _requirements()}
    direct = [
        re.split(r"[<>=!~;\s\[]", ln.strip(), 1)[0].lower().replace("_", "-")
        for ln in (ROOT / "requirements.in").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    missing = [d for d in direct if d not in locked]
    assert not missing, f"requirements.in names packages the lock lacks (recompile): {missing}"


def test_images_install_with_require_hashes():
    for df in DOCKERFILES:
        for line in df.read_text(encoding="utf-8").splitlines():
            if "pip install" in line and "requirements" in line:
                assert "--require-hashes" in line, f"{df.relative_to(ROOT)}: {line.strip()}"


def test_every_pulled_image_is_pinned_by_digest():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    images = [s["image"] for s in (compose.get("services") or {}).values() if "image" in s]
    images += [
        ln.split(None, 1)[1].split()[0]
        for df in DOCKERFILES
        for ln in df.read_text(encoding="utf-8").splitlines()
        if ln.startswith("FROM ")
    ]
    assert images, "no images found; the check would pass vacuously"
    unpinned = [i for i in images if "@sha256:" not in i]
    assert not unpinned, f"not pinned by digest: {unpinned}"


def test_the_runtime_user_does_not_own_the_code():
    assert DOCKERFILES
    for df in DOCKERFILES:
        text = df.read_text(encoding="utf-8")
        for m in re.finditer(r"chown\s+(?:-R\s+)?\S+\s+([^\n&\\]+)", text):
            targets = m.group(1).split()
            assert not any(t.rstrip("/") == "/app" or t.startswith("/app/") for t in targets), (
                f"{df.relative_to(ROOT)} gives the runtime user /app: {m.group(0)}"
            )
        users = re.findall(r"^USER\s+(\S+)", text, re.M)
        assert users and users[-1] not in ("root", "0"), f"{df.relative_to(ROOT)} runs as root"
