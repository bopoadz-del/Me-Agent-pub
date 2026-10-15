"""Every file run through its shebang is checked out with LF line endings.

The containers are Linux. A tracked script checked out with CRLF (git's
core.autocrlf=true on a Windows clone) carries "#!/bin/sh\\r" as its first
line, and the kernel fails to exec it: "no such file or directory". The rule
is held by .gitattributes, so it holds for every clone on every OS; this test
checks it for every tracked file that starts with a shebang, not a list of
names.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def _shebang_files() -> list[str]:
    files = []
    for rel in _git("ls-files", "-z").split("\0"):
        path = ROOT / rel
        if rel and path.is_file():
            with path.open("rb") as fh:
                if fh.read(2) == b"#!":
                    files.append(rel)
    return files


def test_repo_has_shebang_files():
    assert _shebang_files(), "no tracked shebang files found; the check below would pass vacuously"


def test_every_shebang_file_checks_out_lf():
    files = _shebang_files()
    out = _git("check-attr", "eol", "--", *files)
    eol = {line.split(": ")[0]: line.rsplit(": ", 1)[1] for line in out.splitlines()}
    not_lf = sorted(f for f in files if eol.get(f) != "lf")
    assert not not_lf, f"shebang files without eol=lf in .gitattributes: {not_lf}"
