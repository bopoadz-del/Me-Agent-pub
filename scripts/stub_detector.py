#!/usr/bin/env python3
"""Zero-tolerance stub detector. Exit 1 on any finding.

Completeness (FINDINGS B / b1): reads KNOWN_INCOMPLETE.md for a path allow-list
and required Phase-B registry markers. Missing registry or hollow code outside
the allow-list fails the gate.
"""
import ast
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", "tests", ".venv", "generated", "scripts"}
# Critical-path files that ALSO face the <2-statement triviality rule.
TRIVIALITY_ALLOWLIST = {
    "agent/orchestrator/engine.py", "agent/swarm/spawner.py",
    "agent/memory.py", "agent/comms.py", "agent/bootstrapper.py",
    "hive/main.py", "hive/sync.py", "hive/auth.py",
    "domain-kits/compiler/engine.py",
}
# Owner ruling 2: route handlers are exempt from the triviality rule as a class,
# recognised by structure -- a decorator call <router>.<verb>("/path", ...).
# A one-line endpoint (health, a 501 for a V2 path) is the API's shape, not a
# hollow body. No file or function name is exempt.
ROUTE_DECORATORS = {
    "get", "post", "put", "patch", "delete", "head", "options", "trace",
    "route", "api_route", "websocket",
}
BANNED_CALLS = {"eval", "exec", "print"}
BANNED_RAISES = {"NotImplementedError", "Exception", "RuntimeError"}

KNOWN_INCOMPLETE_PATH = Path("KNOWN_INCOMPLETE.md")
ALLOW_PATH_RE = re.compile(r"(?m)^\s*-\s*path:\s+(\S+)\s*$")
REQUIRED_REGISTRY_SNIPPETS = (
    "make verify-airgap",
    "11.4",
    "Windows spawn",
)


def _is_docstring(node):
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _bare_return(node):
    return isinstance(node, ast.Return) and (
        node.value is None
        or (isinstance(node.value, ast.Dict) and not node.value.keys)
        or (isinstance(node.value, ast.List) and not node.value.elts)
        or (isinstance(node.value, ast.Constant)
            and node.value.value in (None, "", 0)))


def is_stub_body(body):
    core = [n for n in body if not _is_docstring(n)]
    if not core:
        return "docstring-only"
    if len(core) == 1:
        n = core[0]
        if isinstance(n, ast.Pass):
            return "pass"
        if _bare_return(n):
            return "bare-return"
        if (isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                and isinstance(n.exc.func, ast.Name)
                and n.exc.func.id in BANNED_RAISES):
            return "raise-marker"
    return None


def _meaningful(node):
    return not (_is_docstring(node)
                or isinstance(node, (ast.Pass, ast.Import, ast.ImportFrom)))


def is_trivial(body):
    """Fewer than two meaningful statements, counting the statements nested in
    compound ones: a loop or a try with a real body is not a one-liner."""
    count = sum(1 for top in body if _meaningful(top)
                for n in ast.walk(top)
                if isinstance(n, ast.stmt) and _meaningful(n))
    return count < 2      # ast.Expr(Call) survives this filter: it counts


def is_route_handler(node):
    """Decorated as an HTTP/websocket route: @<obj>.<verb>("/path", ...)."""
    for deco in node.decorator_list:
        if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                and deco.func.attr in ROUTE_DECORATORS and deco.args
                and isinstance(deco.args[0], ast.Constant)
                and isinstance(deco.args[0].value, str)
                and deco.args[0].value.startswith("/")):
            return True
    return False


def load_known_incomplete(path: Path | None = None) -> tuple[set[str], list[str]]:
    """Return (allowlisted relative paths, registry errors)."""
    p = path or KNOWN_INCOMPLETE_PATH
    errors: list[str] = []
    if not p.is_file():
        return set(), [f"missing {p.as_posix()} — completeness registry required"]
    text = p.read_text(encoding="utf-8")
    for snippet in REQUIRED_REGISTRY_SNIPPETS:
        if snippet not in text:
            errors.append(
                f"{p.as_posix()} missing required registry marker: {snippet!r}"
            )
    allow: set[str] = set()
    for m in ALLOW_PATH_RE.finditer(text):
        rel = m.group(1).replace("\\", "/").lstrip("./")
        if rel.lower() in {"(none", "(none)", "none"}:
            continue
        allow.add(rel)
    return allow, errors


def _allowlisted(rel_norm: str, allow: set[str]) -> bool:
    if rel_norm in allow:
        return True
    return any(rel_norm.startswith(a.rstrip("/") + "/") for a in allow)


def scan(path, rel, allow: set[str] | None = None):
    rel_norm = rel.replace("\\", "/").lstrip("./")
    if rel_norm == "scripts/stub_detector.py":
        return []
    if allow is not None and _allowlisted(rel_norm, allow):
        return []
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            reason = is_stub_body(node.body)
            if reason:
                findings.append(f"{rel}:{node.lineno} {node.name} [{reason}]")
            elif (rel_norm in TRIVIALITY_ALLOWLIST and is_trivial(node.body)
                  and not is_route_handler(node)):
                findings.append(f"{rel}:{node.lineno} {node.name} [trivial]")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in BANNED_CALLS:
            findings.append(f"{rel}:{node.lineno} banned call [{node.func.id}]")
    return findings


def collect_findings(root: str = ".", allow: set[str] | None = None) -> list[str]:
    found: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(dirpath, f)
                found.extend(scan(p, os.path.relpath(p, root), allow=allow))
    return found


def main(argv: list[str] | None = None) -> int:
    _ = argv
    allow, reg_errors = load_known_incomplete()
    if reg_errors:
        print("STUB DETECTOR FAILED (completeness registry):")
        print("\n".join(f"  {x}" for x in reg_errors))
        return 1
    found = collect_findings(".", allow=allow)
    if found:
        print("STUB DETECTOR FAILED:")
        print("\n".join(f"  {x}" for x in found))
        return 1
    print("STUB DETECTOR PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
