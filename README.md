# Me-Agent

A self-hosted agent and its hive, running locally with docker compose on your
own Ollama server. Nothing is hosted and no API key is needed.

## Run it

You need Docker and a running Ollama server on this machine.

```sh
make up      # start hive + agent
make down    # stop them (your data stays)
```

`make up` does everything else:

- On first run it writes fresh random credentials to `.env`. That file stays
  on this machine and is never committed.
- It checks your Ollama server and downloads the model named in
  `config/model.env` if the server doesn't have it yet. This is a download
  only: it never loads or unloads a model, and it never starts a second
  Ollama.
- It starts the hive (http://localhost:8001) and the agent
  (http://localhost:8000), and waits until both are healthy.

Data lives on the docker volumes `hive-data` and `agent-data`. `make down`
keeps them.

The model is named in one place: `config/model.env`.

If your Ollama server is busy with another model or job, missions wait in the
queue and report "waiting for the model server". The agent never evicts
another model.
