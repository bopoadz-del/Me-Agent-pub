"""Red proof: a pull request's new or changed tests must fail before its fix.

For a base and a head commit, this takes ONLY the test files the head adds or
changes (tests/**/test_*.py) since its fork point -- the git merge-base, which
a pull request's own diff is computed against, never the target's tip -- lays
them over a checkout of that fork point, and runs them there: at least one
must fail (or error) on the base. It then runs the
same files on the head, where every one must pass. The names of the tests
that were red on the base are printed, one per line, under "red on base:".

It knows nothing about any particular pull request, test or fault: the
files come from the diff, the verdict from the test outcomes. A change that
touches no test file is skipped with that reason.

    python scripts/red_proof.py --base <sha> --head <sha>

Exit 0: proof holds (or nothing to prove). Exit 1: no test was red on the
base, or a test is not green on the head.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TEST_FILE = re.compile(r"^tests/(?:.+/)?test_[^/]+\.py$")


def changed_test_files(paths: list[str]) -> list[str]:
    """The test modules among a diff's added or changed paths."""
    return sorted({p for p in paths if _TEST_FILE.match(p.replace("\\", "/"))})


def outcomes(junit_xml: str) -> dict[str, str]:
    """test id -> passed | failed | error | skipped, from a pytest junit report."""
    result: dict[str, str] = {}
    for case in ET.parse(junit_xml).getroot().iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        if case.find("failure") is not None:
            result[name] = "failed"
        elif case.find("error") is not None:
            result[name] = "error"
        elif case.find("skipped") is not None:
            result[name] = "skipped"
        else:
            result[name] = "passed"
    return result


def pytest_command(files: list[str], report: Path) -> list[str]:
    """One file that cannot even be imported on the base must not hide the
    outcomes of the others: collection errors are reported, the rest still run."""
    return [
        sys.executable, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider",
        "--continue-on-collection-errors", f"--junitxml={report}",
    ]


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def proof_base(base: str, head: str, cwd: Path = ROOT) -> str:
    """The fork point of the head from the target branch (git merge-base), the
    same base a pull request's own diff is computed against. Proving against
    the target's tip instead would count tests the target changed after the
    fork as the pull request's."""
    return _git("merge-base", base, head, cwd=cwd).strip()


def files_under_proof(base: str, head: str, cwd: Path = ROOT) -> list[str]:
    """The test files the head adds or changes since its fork point."""
    fork = proof_base(base, head, cwd=cwd)
    return changed_test_files(_git("diff", "--name-only", "--diff-filter=AMR", fork, head, cwd=cwd).split())


def _run_tests(checkout: Path, files: list[str], report: Path) -> dict[str, str]:
    env = {**os.environ, "PYTHONPATH": str(checkout)}
    data = Path(tempfile.mkdtemp(prefix="red-proof-data-"))
    (data / "logs").mkdir()
    env["DATA_DIR"] = str(data)
    # The package layout (domain-kits -> domain_kits) of the tree under test.
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(checkout), "--no-deps"], check=True)
    subprocess.run(pytest_command(files, report), cwd=checkout, env=env)
    if not report.exists():
        return {f: "error" for f in files}
    return outcomes(str(report))


def _run_at(
    commit: str, files: list[str], tree: Path, report: Path, overlay_from: str | None = None
) -> dict[str, str]:
    _git("worktree", "add", "--detach", str(tree), commit)
    try:
        for f in files if overlay_from else []:
            target = tree / f
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.run(
                ["git", "show", f"{overlay_from}:{f}"], cwd=ROOT, capture_output=True, check=True
            ).stdout)
        return _run_tests(tree, files, report)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT)
        # The checkout's own package layout again for anything run after.
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(ROOT), "--no-deps"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    args = ap.parse_args(argv)

    fork = proof_base(args.base, args.head)
    files = files_under_proof(args.base, args.head)
    print(f"red-proof: fork point {fork[:12]} (merge-base of {args.base[:12]} and {args.head[:12]})")
    if not files:
        print("red-proof: skipped -- this change adds or changes no test file (tests/**/test_*.py).")
        return 0
    print("red-proof: test files under proof:\n  " + "\n  ".join(files))

    work = Path(tempfile.mkdtemp(prefix="red-proof-"))
    try:
        # The base with only the head's test files laid over it, then the head
        # itself: each in its own worktree, whatever is checked out here.
        on_base = _run_at(fork, files, work / "base", work / "base.xml", overlay_from=args.head)
        on_head = _run_at(args.head, files, work / "head", work / "head.xml")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    red = sorted(n for n, o in on_base.items() if o in ("failed", "error"))
    green_on_base = sorted(n for n, o in on_base.items() if o == "passed")
    not_green = sorted(n for n, o in on_head.items() if o in ("failed", "error"))

    print("\nred on base: " + (", ".join(red) if red else "(none)"))
    if green_on_base:
        print("green on base (no proof by themselves): " + ", ".join(green_on_base))
    ok = True
    if not red:
        print("red-proof: FAIL -- no new or changed test fails on the base; the tests do not show the fault.")
        ok = False
    if not_green:
        print("red-proof: FAIL -- not green on the head: " + ", ".join(not_green))
        ok = False
    if not on_head:
        print("red-proof: FAIL -- no test outcome on the head.")
        ok = False
    if ok:
        print(f"red-proof: OK -- {len(red)} red on base, all {len(on_head)} green or skipped on head.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
