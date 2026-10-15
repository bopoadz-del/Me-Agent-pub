"""Hive block registry writes shared by POST /admin/blocks and hive-admin."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from common.models.schemas import BlockDef


def _definition(payload: dict) -> dict:
    """Block definition without its clock: what decides whether a block changed."""
    return {key: value for key, value in payload.items() if key != "version_clock"}


def upsert_blocks(conn: sqlite3.Connection, blocks: Iterable[BlockDef]) -> list[BlockDef]:
    """Insert new blocks; bump version_clock only when a definition changed.

    Returns the blocks as stored after the write, for the ones that changed.
    Re-registering an identical definition is a no-op, so a boot-time
    registration can run on every start without advancing clocks.
    """
    changed: list[BlockDef] = []
    for block in blocks:
        payload = block.model_dump(mode="json")
        existing = conn.execute(
            "SELECT def_json, version_clock FROM block_registry WHERE block_id = ?",
            (block.block_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO block_registry(block_id, def_json, version_clock) VALUES (?, ?, ?)",
                (block.block_id, json.dumps(payload), block.version_clock),
            )
            changed.append(block)
            continue
        if _definition(json.loads(existing["def_json"])) == _definition(payload):
            continue
        payload["version_clock"] = int(existing["version_clock"]) + 1
        conn.execute(
            "UPDATE block_registry SET def_json = ?, version_clock = ? WHERE block_id = ?",
            (json.dumps(payload), payload["version_clock"], block.block_id),
        )
        changed.append(BlockDef.model_validate(payload))
    conn.commit()
    return changed


def load_block_files(paths: Iterable[str | Path]) -> list[BlockDef]:
    """Read BlockDefs from JSON files or directories of JSON files.

    A file may hold one block object or a list of them. Any file that is not a
    valid BlockDef raises: a registration run never half-applies a bad kit.
    """
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(p for p in path.glob("*.json") if p.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(f"block path not found: {path}")
    blocks: list[BlockDef] = []
    for file in files:
        data = json.loads(file.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        blocks.extend(BlockDef.model_validate(item) for item in items)
    return blocks
