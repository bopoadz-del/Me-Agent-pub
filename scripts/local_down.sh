#!/bin/sh
# One command: stop hive + agent, and the bundled Ollama if `make run` started
# it. Data stays on the named volumes; an Ollama server on the host and its
# models are never touched.
set -eu
cd "$(dirname "$0")/.."
docker compose --profile bundled-ollama down
