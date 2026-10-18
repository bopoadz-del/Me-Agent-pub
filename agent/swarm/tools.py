"""Tool registry: calculator, file_reader, web_search, vector_search."""
from __future__ import annotations

import ast
import json
import operator as op
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from common.logging import OPERATIONAL_CONSTRAINTS
from common.models.schemas import require_mission_id

_OPS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,
    ast.Mod: op.mod,
    ast.USub: op.neg,
    ast.UAdd: op.pos,
}


class AirGapError(RuntimeError):
    """Raised when a network tool is invoked in airgap mode."""


def calculator(expression: str) -> float:
    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return float(_OPS[type(node.op)](ev(node.left), ev(node.right)))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return float(_OPS[type(node.op)](ev(node.operand)))
        raise ValueError(f"disallowed expression node: {type(node).__name__}")

    return ev(ast.parse(expression, mode="eval"))


def _allowed_evidence_roots() -> list[Path]:
    """The one directory the reader trusts. Evidence from anywhere else is
    copied in here on intake (``intake_evidence``); this list is never widened."""
    data_dir = os.environ.get("DATA_DIR", "/data")
    return [(Path(data_dir) / "evidence").resolve()]


def _intake_dirs() -> list[Path]:
    raw = os.environ.get("EVIDENCE_INTAKE_DIRS", "/tmp/evidence")
    return [Path(d).resolve() for d in raw.split(os.pathsep) if d.strip()]


def _under(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def intake_evidence(items: list[dict[str, Any]], mission_id: str) -> list[dict[str, Any]]:
    """Bring a mission's attached evidence inside the whitelist.

    A file already in the whitelist is kept where it is. A file in an intake
    directory is copied to DATA_DIR/evidence/<mission id>/ and the item points
    at the copy. Anything else -- including a link that resolves outside both --
    is refused with PermissionError.
    """
    import shutil

    allowed = _allowed_evidence_roots()
    intake = _intake_dirs()
    out: list[dict[str, Any]] = []
    for item in items:
        path = item.get("path") if isinstance(item, dict) else None
        if not path:
            out.append(item)
            continue
        real = Path(os.path.realpath(path))
        if _under(real, allowed):
            out.append({**item, "path": str(real)})
            continue
        if not _under(real, intake) or not real.is_file():
            raise PermissionError(f"evidence not in an intake directory: {path}")
        max_bytes = int(OPERATIONAL_CONSTRAINTS["max_evidence_file_mb"]) * 1024 * 1024
        if real.stat().st_size > max_bytes:
            raise PermissionError(f"evidence file larger than {max_bytes} bytes: {path}")
        target_dir = (allowed[0] / require_mission_id(mission_id)).resolve()
        target = (target_dir / real.name).resolve()
        if target_dir.parent != allowed[0] or target.parent != target_dir:
            raise PermissionError(f"evidence copy would leave its mission directory: {path}")
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(real, target)
        out.append({**item, "path": str(target.resolve())})
    return out


#: One plain file name: no directory part, no leading dot or dash.
_UPLOAD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: Names a filesystem reserves for devices, with or without an extension.
_DEVICE_NAME = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", re.IGNORECASE)


def store_upload(name: str, data: bytes) -> Path:
    """Write one uploaded document into the whitelist, in a directory of its own.

    The document must be UTF-8 text, the only thing file_reader reads; a body
    that is not raises UnicodeDecodeError. A name that is not one plain file
    name, or an empty or oversized body, raises ValueError. Nothing is written
    outside DATA_DIR/evidence."""
    import uuid

    if not _UPLOAD_NAME.fullmatch(name) or _DEVICE_NAME.fullmatch(name):
        raise ValueError(f"evidence name must be one plain file name: {name!r}")
    if not data:
        raise ValueError("evidence document is empty")
    max_bytes = int(OPERATIONAL_CONSTRAINTS["max_evidence_file_mb"]) * 1024 * 1024
    if len(data) > max_bytes:
        raise ValueError(f"evidence document larger than {max_bytes} bytes")
    data.decode("utf-8")
    root = _allowed_evidence_roots()[0]
    target_dir = (root / f"upload-{uuid.uuid4().hex}").resolve()
    target = (target_dir / name).resolve()
    if target_dir.parent != root or target.parent != target_dir:
        raise ValueError(f"evidence upload would leave its directory: {name!r}")
    target_dir.mkdir(parents=True)
    target.write_bytes(data)
    return target


def list_evidence() -> list[tuple[str, Path, int]]:
    """(reference, path, bytes) for every file inside the whitelist, sorted by
    reference: its path relative to DATA_DIR/evidence. A link that resolves
    outside the whitelist is not evidence and is left out."""
    root = _allowed_evidence_roots()[0]
    if not root.is_dir():
        return []
    out = []
    for path in root.rglob("*"):
        real = Path(os.path.realpath(path))
        if real.is_file() and _under(real, [root]):
            out.append((path.relative_to(root).as_posix(), real, real.stat().st_size))
    return sorted(out)


def evidence_file(ref: str) -> Path:
    """The whitelist file a reference names; FileNotFoundError for anything
    that does not resolve to a file inside the whitelist."""
    root = _allowed_evidence_roots()[0]
    if not ref or Path(ref).is_absolute() or "\\" in ref:
        raise FileNotFoundError(ref)
    real = Path(os.path.realpath(root / ref))
    if not _under(real, [root]) or not real.is_file():
        raise FileNotFoundError(ref)
    return real


def file_reader(path: str) -> str:
    real = Path(os.path.realpath(path))
    if not _under(real, _allowed_evidence_roots()):
        raise PermissionError(f"path not in evidence whitelist: {path}")
    return real.read_text(encoding="utf-8")


def web_search(query: str) -> list[dict[str, Any]]:
    if os.environ.get("AIRGAP", "false").lower() == "true":
        raise AirGapError("web_search blocked in airgap mode")
    import httpx

    url = "https://api.duckduckgo.com/"
    params = {"q": query, "format": "json", "no_html": 1}
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
    related = data.get("RelatedTopics") or []
    results: list[dict[str, Any]] = []
    for item in related[:5]:
        if isinstance(item, dict) and "Text" in item:
            results.append({"text": item.get("Text"), "url": item.get("FirstURL")})
    return results


def vector_search(query_embedding: list[float], db_path: str, limit: int = 5) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        rows = conn.execute(
            """
            SELECT m.delta_id, m.key, m.value, v.distance
            FROM memories_vec v
            JOIN memory_deltas m ON m.rowid = v.rowid
            WHERE v.embedding MATCH ?
            ORDER BY v.distance
            LIMIT ?
            """,
            (json.dumps(query_embedding), limit),
        ).fetchall()
    except sqlite3.Error:
        rows = conn.execute(
            "SELECT delta_id, key, value FROM memory_deltas LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        value = row["value"]
        if isinstance(value, str):
            value = json.loads(value)
        out.append({"delta_id": row["delta_id"], "key": row["key"], "value": value})
    return out


TOOL_REGISTRY: dict[str, Any] = {
    "calculator": calculator,
    "file_reader": file_reader,
    "web_search": web_search,
    "vector_search": vector_search,
}
