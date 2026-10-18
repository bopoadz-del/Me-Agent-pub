.PHONY: build up run downtest test-unit test-integration test-e2e compile-domain verify-airgap detect clean mutation-gate

build:
	docker compose build

up:
	sh scripts/local_up.sh

# Docker only: the stack brings its own Ollama, which pulls the model named in
# config/model.env (the path CI's live job takes).
run:
	COMPOSE_PROFILES=bundled-ollama OLLAMA_URL=http://ollama:11434 sh scripts/local_up.sh

down:
	sh scripts/local_down.sh

detect:
	python3 scripts/stub_detector.py
	python3 scripts/secret_scan.py

test-unit:
	python3 -m pytest tests/unit/ -v

test-integration:
	python3 -m pytest tests/integration/ -v -m "not airgap"

test-e2e:
	python3 -m pytest tests/e2e/ -v

test: detect test-unit test-integration test-e2e

# Every sheet, as the hive image compiles them; no kit is named here.
compile-domain:
	for sheet in domain-kits/sheets/*.yaml domain-kits/sheets/*.yml; do \
		[ -f "$$sheet" ] || continue; \
		python3 -m domain_kits.compiler.engine --sheet "$$sheet" || exit 1; \
	done

# Phase B: runs in CI (job "Phase B verify-airgap"). Needs Linux + Docker.
verify-airgap:
	python3 tests/fixtures/preload/build_memory_sqlite.py
	docker build -f agent/Dockerfile.airgap -t self_agent_airgap .
	docker run --rm --network none -e AIRGAP=true \
		-v $(PWD)/tests/fixtures/preload:/data/preload \
		self_agent_airgap python3 -m agent.security.airgap

mutation-gate:
	python3 scripts/mutation_gate.py --targets \
		agent/orchestrator/engine.py agent/swarm/spawner.py hive/sync.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
