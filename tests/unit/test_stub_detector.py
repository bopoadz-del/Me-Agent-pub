"""Meta-tests for scripts/stub_detector.py (spec Section 11.1)."""
from __future__ import annotations

from pathlib import Path

import pytest


def _load_detector():
    import importlib.util

    path = Path("scripts/stub_detector.py").resolve()
    spec = importlib.util.spec_from_file_location("stub_detector", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def detector():
    return _load_detector()


def test_stub_detector_flags_pass_docstring_raise_eval_not_legit(detector, tmp_path):
    cases = {
        "a_pass.py": "def f():\n    pass\n",
        "b_docstring.py": 'def f():\n    """only docs"""\n',
        "c_raise.py": 'def f():\n    raise NotImplementedError("BLOCKED")\n',
        "d_eval.py": "def f(x):\n    return eval(x)\n",
        "e_legit.py": "def f(db):\n    db.begin()\n    db.commit()\n",
    }
    for name, src in cases.items():
        (tmp_path / name).write_text(src, encoding="utf-8")

    findings_a = detector.scan(str(tmp_path / "a_pass.py"), "a_pass.py")
    findings_b = detector.scan(str(tmp_path / "b_docstring.py"), "b_docstring.py")
    findings_c = detector.scan(str(tmp_path / "c_raise.py"), "c_raise.py")
    findings_d = detector.scan(str(tmp_path / "d_eval.py"), "d_eval.py")
    findings_e = detector.scan(str(tmp_path / "e_legit.py"), "e_legit.py")

    assert any("pass" in f for f in findings_a), findings_a
    assert any("docstring-only" in f for f in findings_b), findings_b
    assert any("raise-marker" in f for f in findings_c), findings_c
    assert any("eval" in f for f in findings_d), findings_d
    assert findings_e == [], findings_e


def test_stub_detector_expr_call_counts_as_meaningful(detector, tmp_path):
    """Pins ast.Expr(Call) surviving the triviality filter."""
    src = "def f(db):\n    db.begin()\n    db.commit()\n"
    p = tmp_path / "legit_two_calls.py"
    p.write_text(src, encoding="utf-8")
    findings = detector.scan(str(p), sorted(detector.TRIVIALITY_ALLOWLIST)[0])
    assert findings == [], findings
    assert detector.is_trivial(
        __import__("ast").parse(src).body[0].body
    ) is False


def test_planted_hollow_makes_detect_exit_nonzero(detector, tmp_path, monkeypatch):
    """Completeness meta-test: hollow reachable module → non-zero exit."""
    repo = tmp_path / "repo"
    (repo / "agent").mkdir(parents=True)
    (repo / "agent" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "agent" / "hollow.py").write_text(
        "def incomplete():\n    pass\n",
        encoding="utf-8",
    )
    (repo / "KNOWN_INCOMPLETE.md").write_text(
        "# KNOWN_INCOMPLETE\n\n"
        "## Path allow-list\n\n(none)\n\n"
        "make verify-airgap\n"
        "11.4\n"
        "Windows spawn\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    rc = detector.main([])
    assert rc == 1


def test_allowlisted_hollow_does_not_fail(detector, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "agent").mkdir(parents=True)
    (repo / "agent" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "agent" / "hollow.py").write_text(
        "def incomplete():\n    pass\n",
        encoding="utf-8",
    )
    (repo / "KNOWN_INCOMPLETE.md").write_text(
        "# KNOWN_INCOMPLETE\n\n"
        "## Path allow-list\n\n"
        "- path: agent/hollow.py\n\n"
        "make verify-airgap\n"
        "11.4\n"
        "Windows spawn\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    rc = detector.main([])
    assert rc == 0


def test_missing_known_incomplete_fails(detector, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = detector.main([])
    assert rc == 1


# -- ruling 2: the triviality rule is enforced; route handlers are exempt by structure


def _scan_critical(detector, tmp_path, src):
    p = tmp_path / "critical.py"
    p.write_text(src, encoding="utf-8")
    return detector.scan(str(p), sorted(detector.TRIVIALITY_ALLOWLIST)[0])


def test_triviality_rule_is_enforced_on_the_critical_paths(detector):
    assert {"agent/bootstrapper.py", "hive/main.py", "agent/swarm/spawner.py"} <= detector.TRIVIALITY_ALLOWLIST
    assert not hasattr(detector, "TRIVIALITY_ALLOWLIST_PENDING")


def test_a_one_line_helper_on_a_critical_path_is_flagged(detector, tmp_path):
    findings = _scan_critical(detector, tmp_path, "def helper(x):\n    return x + 1\n")
    assert any("helper" in f and "[trivial]" in f for f in findings), findings


def test_route_handlers_are_exempt_by_structure_whatever_their_name(detector, tmp_path):
    src = (
        "@app.get('/health')\nasync def anything():\n    return {'status': 'ok'}\n\n"
        "@router.post('/x/{id}')\ndef other(id):\n    return id\n\n"
        "@app.websocket('/ws')\nasync def ws(sock):\n    await sock.accept()\n"
    )
    assert _scan_critical(detector, tmp_path, src) == []


def test_other_decorators_do_not_exempt(detector, tmp_path):
    src = (
        "@app.on_event('shutdown')\nasync def down():\n    app.close()\n\n"
        "@property\ndef value(self):\n    return self._v\n\n"
        "@cache.get('key')\ndef cached():\n    return 1\n"  # not a path: not a route
    )
    findings = _scan_critical(detector, tmp_path, src)
    assert {f.split()[1] for f in findings} == {"down", "value", "cached"}, findings


def test_statements_nested_in_compound_statements_count(detector, tmp_path):
    src = (
        "def loop(q):\n    while q:\n        item = q.pop()\n        handle(item)\n\n"
        "def guarded(lock, q):\n    with lock:\n        return len(q)\n"
    )
    assert _scan_critical(detector, tmp_path, src) == []
