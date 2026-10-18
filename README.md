# Me-Agent

A self-hosted agent and its hive, running locally with docker compose on your
own Ollama server. Nothing is hosted and no API key is needed.

## What you need

- **Python 3.11** (the package pins `>=3.11,<3.12`), for the tests and the
  setup scripts.
- **Docker** with the **compose v2** plugin (`docker compose version` must
  answer): <https://docs.docker.com/get-docker/>.
- **Ollama**, running on this machine: <https://ollama.com/download>.
- **make** (on Debian/Ubuntu: `sudo apt install make`).

## Five-minute path: run the tests, no Docker or model needed

```sh
python3.11 -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install --require-hashes -r requirements.txt
pip install -e . --no-deps
python -m pytest tests/unit/test_supply_chain.py tests/unit/test_challenge_budget.py
```

The whole unit suite is the same command on the directory:

```sh
python -m pytest tests/unit
```

`pip install -e .` matters: it makes the `domain-kits/` directory importable
as `domain_kits`. The tests write their logs to a fresh temporary directory
unless you set `DATA_DIR`. CI runs exactly these steps on a clean checkout
(job "Fresh clone, README steps").

## Run it

```sh
make up      # start hive + agent
make down    # stop them (your data stays)
```

`make up` does everything else:

- On first run it writes fresh random credentials to `.env`. That file stays
  on this machine and is never committed.
- It checks your Ollama server and downloads the model named in
  `config/model.env` (`llama3.2:3b`, about 2 GB) if the server doesn't have
  it yet. Expect a few minutes on first run, depending on your connection.
  This is a download only: it never loads or unloads a model, and it never
  starts a second Ollama.
- It starts the hive (http://localhost:8001) and the agent
  (http://localhost:8000), and waits until both are healthy.

Data lives on the docker volumes `hive-data` and `agent-data`. `make down`
keeps them.

The model is named in one place: `config/model.env`.

If your Ollama server is busy with another model or job, missions wait in the
queue and report "waiting for the model server". The agent never evicts
another model.
