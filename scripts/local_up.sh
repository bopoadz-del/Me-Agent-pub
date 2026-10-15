#!/bin/sh
# One command: hive + agent up from a clean clone, on the local Ollama server.
#
#   make up        (or: sh scripts/local_up.sh)
#
# First run writes fresh random credentials to .env (gitignored, never
# printed). Then it checks the Ollama server the agent will call: it must
# answer, and it must have the configured model (config/model.env). A missing
# model is downloaded -- a download only; nothing is loaded or unloaded, and
# no second Ollama is ever started. Then hive + agent come up and are waited
# on until healthy. State lives on the named volumes hive-data and agent-data.
set -eu
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  umask 077
  rand() { python3 -c 'import secrets; print(secrets.token_hex(32))'; }
  secret=$(rand)
  {
    echo "HIVE_JWT_KEY=$(rand)"
    echo "AGENT_API_TOKEN=$(rand)"
    echo "AGENT_SECRET=$secret"
    echo "AGENT_ID=agent-1"
  } > .env
  echo "wrote .env with fresh credentials (kept local, never committed)"
fi

model=$(sed -n 's/^OLLAMA_MODEL=//p' config/model.env | head -n 1)
[ -n "$model" ] || { echo "config/model.env names no OLLAMA_MODEL" >&2; exit 1; }

case ",${COMPOSE_PROFILES:-}," in
  *,bundled-ollama,*)
    # CI: the stack brings its own server and pulls the model itself.
    ;;
  *)
    host_url="${OLLAMA_HOST_URL:-http://127.0.0.1:11434}"
    if ! python3 scripts/ollama_ready.py --url "$host_url" --model "$model"; then
      exit 1
    fi
    ;;
esac

docker compose up -d --build --wait --wait-timeout "${UP_WAIT_SECONDS:-900}"
echo "Me-Agent is up: agent http://localhost:8000  hive http://localhost:8001"
