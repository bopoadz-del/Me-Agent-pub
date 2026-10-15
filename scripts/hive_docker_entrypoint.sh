#!/bin/sh
set -eu
cd /app
export PYTHONPATH="${PYTHONPATH:-/app}"
python scripts/enroll_bootstrap_agent.py
# Register every compiled domain kit before serving, so the first agent to
# authenticate receives the blocks in its session grant.
BLOCK_KITS_DIR="${BLOCK_KITS_DIR:-/app/domain-kits/generated}"
if ls "$BLOCK_KITS_DIR"/*.json >/dev/null 2>&1; then
  python -m hive.admin_cli blocks register "$BLOCK_KITS_DIR"
fi
# --factory: the app is built at start-up, so a missing or short HIVE_JWT_KEY
# stops the hive instead of serving an unconfigured app.
exec python -m uvicorn --factory hive.main:create_app --host 0.0.0.0 --port "${PORT:-10000}" --ws-max-size 1048576
