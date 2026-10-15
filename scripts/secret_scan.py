#!/usr/bin/env python3
"""Secret scan (spec Section 11.2). Exit 1 on findings. Allowlist must stay empty in V1."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# A 32+ char key-like value assigned to any name that contains secret, token,
# key or password -- HIVE_JWT_KEY, api_token, AGENT_SECRET as well as a bare
# "key" -- quoted, or unquoted as in a .env file.
ASSIGN_SECRET = re.compile(
    r"(?i)\b[A-Za-z0-9_]*(?:secret|token|key|passw(?:or)?d)[A-Za-z0-9_]*\b"
    r"\s*[=:]\s*['\"]?([A-Za-z0-9+/=_-]{32,})['\"]?"
)
# A committed environment file holds real values; only the example may be committed.
ENV_FILE = re.compile(r"(^|/)\.env(\.[^/]*)?$")
ENV_EXAMPLE = re.compile(r"(^|/)\.env\.example$")
AKIA = re.compile(r"AKIA[0-9A-Z]{16}")
# PEM headers (not the prose pattern string "BEGIN.*PRIVATE KEY" in docs)
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


def _load_allowlist(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }


def _rel(path: Path) -> str:
    rel = path.as_posix()
    if rel.startswith("./"):
        rel = rel[2:]
    return rel


def scan_file(path: Path, allowlist: set[str]) -> list[str]:
    rel = _rel(path)
    if rel in allowlist:
        return []
    if ENV_FILE.search(rel) and not ENV_EXAMPLE.search(rel):
        return [f"{rel}: environment file committed"]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"{rel}: unreadable ({exc})"]
    findings: list[str] = []
    for pattern, label in (
        (ASSIGN_SECRET, "secret-like assignment"),
        (AKIA, "AWS access key"),
        (PRIVATE_KEY, "private key block"),
    ):
        for match in pattern.finditer(text):
            findings.append(f"{rel}: {label}: {match.group(0)[:80]}")
    return findings


def committed_files(root: str = ".") -> list[Path]:
    """Every file git tracks or would add (untracked, not ignored): what a
    commit can carry. The owner's own .env is gitignored and never read."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    return [Path(root) / name for name in out.split("\0") if name]


def main() -> int:
    allow_path = Path("scripts/secret_scan_allow.txt")
    allowlist = _load_allowlist(allow_path)
    if allowlist:
        sys.stderr.write(
            "SECRET SCAN FAILED: secret_scan_allow.txt must stay empty in V1\n"
        )
        return 1
    findings: list[str] = []
    for path in committed_files():
        if path.is_file():
            findings.extend(scan_file(path, allowlist))
    if findings:
        sys.stderr.write("SECRET SCAN FAILED:\n")
        for item in findings:
            sys.stderr.write(f"  {item}\n")
        return 1
    sys.stdout.write("SECRET SCAN PASSED.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
