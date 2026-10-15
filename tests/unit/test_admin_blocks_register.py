"""hive-admin blocks register: idempotent on identical kits, clock bump on change."""
from __future__ import annotations

import json

import pytest

from common.storage.db import open_db
from hive import admin_cli


def _write(path, block_id: str, prompt: str) -> None:
    path.write_text(
        json.dumps(
            {
                "block_id": block_id,
                "name": block_id,
                "domain": "kit",
                "input_schema": {"type": "object"},
                "output_json_schema": {"type": "object"},
                "system_prompt_template": prompt,
                "tools": [],
                "version_clock": 1,
            }
        ),
        encoding="utf-8",
    )


def _clocks(db_path: str) -> dict[str, int]:
    conn = open_db(db_path)
    rows = conn.execute("SELECT block_id, version_clock FROM block_registry").fetchall()
    conn.close()
    return {r["block_id"]: r["version_clock"] for r in rows}


def test_register_directory_then_reregister_is_noop_then_change_bumps(tmp_path):
    kits = tmp_path / "kits"
    kits.mkdir()
    _write(kits / "a.json", "kit_a", "one")
    _write(kits / "b.json", "kit_b", "one")
    db = str(tmp_path / "hive.db")

    assert admin_cli.main(["blocks", "register", str(kits), "--db", db]) == 0
    assert _clocks(db) == {"kit_a": 1, "kit_b": 1}

    assert admin_cli.register_blocks([str(kits)], db_path=db) == []
    assert _clocks(db) == {"kit_a": 1, "kit_b": 1}

    _write(kits / "a.json", "kit_a", "two")
    assert admin_cli.register_blocks([str(kits)], db_path=db) == ["kit_a"]
    assert _clocks(db) == {"kit_a": 2, "kit_b": 1}


def test_invalid_block_file_registers_nothing(tmp_path):
    kits = tmp_path / "kits"
    kits.mkdir()
    _write(kits / "a.json", "kit_a", "one")
    (kits / "z.json").write_text('{"block_id": "broken"}', encoding="utf-8")
    db = str(tmp_path / "hive.db")
    with pytest.raises(Exception):
        admin_cli.register_blocks([str(kits)], db_path=db)
    assert _clocks(db) == {}
