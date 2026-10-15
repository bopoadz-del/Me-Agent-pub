#!/bin/sh
# Airgap image entrypoint (spec section 8.7): the verifier and the agent need a
# local Ollama answering inside the --network none container. Start it on
# loopback, wait until it answers (or give up after the wait), then run the
# command. The container has no network: nothing here can reach out.
set -e
export OLLAMA_HOST="127.0.0.1:11434"
ollama serve >/tmp/ollama.log 2>&1 &
tries=0
until curl -fsS "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -ge 60 ]; then
    echo "airgap entrypoint: local ollama did not answer within 60 s" >&2
    cat /tmp/ollama.log >&2 || true
    break
  fi
  sleep 1
done
exec "$@"
