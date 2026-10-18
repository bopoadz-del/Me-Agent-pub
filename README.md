# Me-Agent

A self-hosted agent and its hive, running locally with docker compose on your
own Ollama server. Nothing is hosted and no API key is needed.

You give the agent a task in plain words plus the documents it may use. It
answers only from those documents: every value it returns is cited from a
line of your evidence, and a task it cannot ground is refused rather than
guessed. What it knows how to do comes from **domain kits**, YAML sheets
compiled into blocks (see below).

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

## Give it a mission

The agent's API is on http://localhost:8000. Every call needs the
`AGENT_API_TOKEN` that `make up` wrote to `.env`, as a bearer token.

The agent reads evidence only from its own data directory. Upload each
document there over the API: the body is the file itself (UTF-8 text, up to
1 MB), the path segment its file name. The reply's `path` is what you attach
to the mission.

```sh
TOKEN=$(sed -n 's/^AGENT_API_TOKEN=//p' .env)
curl -s -X POST http://localhost:8000/evidence/tariff.txt \
  -H "Authorization: Bearer $TOKEN" --data-binary @tariff.txt
# {"path": "/data/evidence/upload-<id>/tariff.txt", "type": "txt", "bytes": 52}
```

Each upload gets a directory of its own, so two files of one name never
overwrite each other. A name with a directory part, a leading dot or dash, an
empty body or a body that is not UTF-8 text is refused. For a larger file,
copy it into the container instead:

```sh
docker compose exec -T agent-instance mkdir -p /data/evidence/my-job
docker compose cp tariff.txt agent-instance:/data/evidence/my-job/tariff.txt
```

Then post the mission, attaching the `path` you got back (or the path you
copied to). The call returns at once with a `mission_id`; the
mission runs in a queue.

```sh
TOKEN=$(sed -n 's/^AGENT_API_TOKEN=//p' .env)
curl -s -X POST http://localhost:8000/mission \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"natural_language": "Calculate berth fee for a dry container using attached tariff evidence",
       "domain_hint": "port_terminal",
       "attached_evidence": [{"type": "txt", "path": "/data/evidence/upload-<id>/tariff.txt"}]}'
```

Read the result with `GET /mission/{mission_id}` (same header). Poll until
the body carries `result`: before that it says `"status": "queued"` (waiting
for a worker) or `"running"` (with the latest `checkpoint`). `result` holds
the outcome `status` (`success`, `partial` or `failure`), each step's output,
the `citations` (the exact evidence lines used) and a `confidence`.

`domain_hint` names the kit to use. Leave it out and the agent chooses among every kit's blocks by the
words of the task.

## Grounding and refusal

- Every value in a result must appear in a cited line of your evidence: a
  number beside its own label (a column header counts), a word as a whole
  word. If one value is not held by any line, the step is refused and the
  reason names the field.
- A value a kit defines by a formula (for example `berth_fee_usd = dwell_hours
  * base_rate_usd_per_hour`) is computed from the cited operands, not taken
  from the model.
- A mission whose kit needs evidence and that has none is refused, with
  `confidence` 0.0, no citations, and a reason that says evidence is required.

## Domain kits

A kit is one YAML sheet in `domain-kits/sheets/`: lookup tables, calculations
(with their formulas), decision rules and failure modes. The hive image and
`make compile-domain` compile every sheet there into blocks; no kit is named
in code. Two kits ship:

- `port_ops.yaml` (domain `port_terminal`): berth tariff lookup, berth fee,
  maximum dwell gate.
- `concrete_mix.yaml` (domain `concrete_mix`): EN 206 Annex F exposure class
  limits, water/cement ratio, exposure limit gate.

To add a kit, add a sheet. `tests/unit/test_every_domain_kit.py` checks every
sheet, and the live CI check (`tests/live/evidence_missions.json`) needs at
least one evidence mission and one refusal for each kit's domain.
