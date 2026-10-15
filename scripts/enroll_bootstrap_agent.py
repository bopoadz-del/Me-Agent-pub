"""One-shot bootstrap agent enrollment at hive boot (secret from env).

Operational utility invoked by scripts/hive_docker_entrypoint.sh before
uvicorn. Not imported by hive/agent/common runtime modules.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from common.logging import get_logger, log_event
from common.storage.db import open_db
from hive.auth import set_agent_secret


def _logger() -> logging.Logger:
    """File JSON logger plus stderr mirror so Docker boot logs stay visible."""
    logger = get_logger("enroll_bootstrap_agent")
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr for h in logger.handlers):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(stream)
    return logger


def main() -> int:
    log = _logger()
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    agent_id = os.environ.get("BOOTSTRAP_AGENT_ID", "agent-1").strip()
    secret = os.environ.get("BOOTSTRAP_AGENT_SECRET", "").strip()
    if not secret or len(secret) < 32:
        log_event(
            log,
            "ERROR",
            "BOOTSTRAP_AGENT_SECRET must be set and at least 32 characters",
        )
        return 1
    if secret == os.environ.get("HIVE_JWT_KEY", "").strip():
        log_event(log, "ERROR", "BOOTSTRAP_AGENT_SECRET and HIVE_JWT_KEY must be different keys")
        return 1
    conn = open_db(str(data_dir / "hive.db"))
    now = datetime.now(timezone.utc).isoformat()
    set_agent_secret(conn, agent_id, secret, now)
    conn.close()
    log_event(log, "INFO", f"enrolled {agent_id}", metrics={"enrolled": 1.0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
