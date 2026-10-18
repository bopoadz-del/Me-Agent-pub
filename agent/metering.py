"""Metering, slice A (issue #22): an append-only usage record per borrowed mission.

One row per mission, written when it ends: grant, mission, start and end,
steps run, tool calls, model calls, model tokens (only when the model adapter
reports them), the outcome code, and whether it counted against the grant's
quota. Which outcomes count is data (USAGE_OUTCOMES): only customer-caused
terminal states count, and a mission whose outcome does not count gives its
quota slot back, once. Rows are never updated or deleted; the table's own
triggers refuse both.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from common.models.schemas import METERING_UNITS, USAGE_OUTCOMES, ExecutionResult

USAGE_TABLE = "mission_usage"

_COLUMNS = ("mission_id", "grant_id", "started_at", "ended_at", "steps", "tool_calls", "model_calls",
            "model_tokens", "outcome", "counted")

#: The columns of a usage export, in order.
EXPORT_COLUMNS = _COLUMNS

#: A CSV cell starting with one of these runs as a formula in a spreadsheet.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {USAGE_TABLE} (
            mission_id TEXT PRIMARY KEY,
            grant_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            steps INTEGER NOT NULL,
            tool_calls INTEGER NOT NULL,
            model_calls INTEGER NOT NULL,
            model_tokens INTEGER,
            outcome TEXT NOT NULL,
            counted INTEGER NOT NULL
        )
        """
    )
    for event in ("UPDATE", "DELETE"):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {USAGE_TABLE}_no_{event.lower()} BEFORE {event} ON {USAGE_TABLE} "
            "BEGIN SELECT RAISE(ABORT, 'usage records are append-only'); END"
        )
    conn.execute(f"CREATE INDEX IF NOT EXISTS {USAGE_TABLE}_by_grant ON {USAGE_TABLE}(grant_id, ended_at)")
    conn.commit()


def outcome(result: ExecutionResult) -> str:
    """The outcome code of a finished mission, from its status and the
    structured cause on its last step (never from the step's text)."""
    if result.status == "success":
        return "completed"
    last = result.outputs[-1] if result.outputs else None
    if last is not None and last.output.get("cause") == "input":
        return "refused_input"
    if last is not None and last.block_id == "__refusal__":
        return "refused_by_agent"
    return "failed_by_agent"


def _ran(result: ExecutionResult) -> list[Any]:
    """The steps that ran: a refusal or intake marker is not a step."""
    return [o for o in result.outputs if not o.block_id.startswith("__")]


def record(
    conn: sqlite3.Connection,
    *,
    grant_id: str,
    mission_id: str,
    started_at: str,
    ended_at: str,
    result: ExecutionResult,
    code: Optional[str] = None,
) -> bool:
    """Write the mission's usage row; False when it already has one. An
    outcome that does not count gives back the slot the mission still holds."""
    ensure_schema(conn)
    code = code or outcome(result)
    counts = bool(USAGE_OUTCOMES[code]["counts"])
    ran = _ran(result)
    usages = [o.usage for o in result.outputs]
    tokens = [int(u["model_tokens"]) for u in usages if u.get("model_tokens") is not None]
    row = (mission_id, grant_id, started_at, ended_at, len(ran),
           sum(int(u.get("tool_calls", 0)) for u in usages),
           sum(int(u.get("model_calls", 0)) for u in usages),
           sum(tokens) if tokens else None, code, 1 if counts else 0)
    try:
        with conn:
            conn.execute(
                f"INSERT INTO {USAGE_TABLE}({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})", row
            )
            if not counts:
                # Only a slot the mission still holds: a queue refusal already gave it back.
                conn.execute(
                    "UPDATE grants SET missions_used = missions_used - 1 WHERE grant_id = ? AND missions_used > 0 "
                    "AND EXISTS (SELECT 1 FROM grant_missions WHERE mission_id = ? AND grant_id = ?)",
                    (grant_id, mission_id, grant_id),
                )
    except sqlite3.IntegrityError:
        return False
    return True


def usage(conn: sqlite3.Connection, grant_id: str, limit: Optional[int] = None, offset: int = 0) -> list[dict]:
    """The grant's usage rows, oldest first."""
    ensure_schema(conn)
    sql = f"SELECT {', '.join(_COLUMNS)} FROM {USAGE_TABLE} WHERE grant_id = ? ORDER BY ended_at, mission_id"
    params: tuple[Any, ...] = (grant_id,)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += (int(limit), int(offset))
    rows = []
    for values in conn.execute(sql, params).fetchall():
        item = dict(zip(_COLUMNS, tuple(values)))
        item["counted"] = bool(item["counted"])
        rows.append(item)
    return rows


def count(conn: sqlite3.Connection, grant_id: str) -> int:
    ensure_schema(conn)
    return int(conn.execute(f"SELECT COUNT(*) FROM {USAGE_TABLE} WHERE grant_id = ?", (grant_id,)).fetchone()[0])


def parse_instant(value: Optional[str]) -> Optional[datetime]:
    """An ISO 8601 instant (UTC when it names no offset); ValueError otherwise."""
    if value is None:
        return None
    instant = datetime.fromisoformat(value)
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)


def window(
    conn: sqlite3.Connection, grant_id: str, since: Optional[datetime] = None, until: Optional[datetime] = None
) -> list[dict]:
    """The grant's usage rows whose mission ended in [since, until)."""
    return [
        row for row in usage(conn, grant_id)
        if (since is None or datetime.fromisoformat(row["ended_at"]) >= since)
        and (until is None or datetime.fromisoformat(row["ended_at"]) < until)
    ]


def summary(rows: list[dict]) -> dict[str, Any]:
    """Missions by outcome, and each METERING_UNITS unit summed over its rows.
    A unit whose column no row reported (model tokens) is None."""
    outcomes: dict[str, int] = {}
    for row in rows:
        outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
    units: dict[str, Any] = {}
    for name, unit in METERING_UNITS.items():
        billed = [row for row in rows if row["counted"] or not unit.get("counted_only", True)]
        if unit["column"] == "*":
            units[name] = len(billed)
            continue
        values = [row[unit["column"]] for row in billed if row[unit["column"]] is not None]
        units[name] = sum(values) if values else None
    return {"missions": len(rows), "outcomes": outcomes, "units": units}


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def to_csv(rows: list[dict]) -> str:
    """Usage rows as CSV, one line per row, formula-leading cells escaped."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([_cell(row.get(column)) for column in EXPORT_COLUMNS])
    return out.getvalue()
