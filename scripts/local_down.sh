#!/bin/sh
# One command: stop hive + agent. Data stays on the named volumes; the local
# Ollama server and its models are never touched.
set -eu
cd "$(dirname "$0")/.."
docker compose down
