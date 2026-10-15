"""Grounded extraction is deterministic: every call samples at temperature 0
with a fixed seed.

Live (PR #10 head 93df503, spec 11.4 step 7): the same commit passed the live
E2E on one CI run (37249938043) and failed it on the other (37249934773). The
stack, auth, block load and the no-evidence refusal were identical; only the
evidence mission differed -- the 3B model answered port_terminal_tariffs_lookup
with matched=false, no row, no citations, on all three generate calls. The
client sent no `options`, so Ollama sampled at its default temperature (0.8)
with a random seed: a grounded lookup that passes about half the time.
"""
from __future__ import annotations

import json

import httpx
import pytest

from common.models.llm_client import OllamaClient


@pytest.fixture
def bodies(monkeypatch):
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"response": json.dumps({"ok": True})})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    return seen


def test_every_call_samples_at_temperature_zero(bodies):
    OllamaClient(model="m").generate(prompt="p", system="s")
    assert bodies[0]["options"]["temperature"] == 0


def test_every_call_uses_the_same_seed(bodies):
    OllamaClient(model="m").generate(prompt="p", system="s")
    OllamaClient(model="m").generate(prompt="p", system="s")
    seeds = [b["options"]["seed"] for b in bodies]
    assert isinstance(seeds[0], int) and seeds[0] == seeds[1]


# -- constrained decoding: the reply can only take the block's shape -------------------
#
# Live E2E on f663959, csv-middle-row: the 3B model found the right row and
# cited it, but returned the row's fields flat instead of {matched, row}; the
# step was refused for not matching the output schema.

from agent.swarm.grounding import reply_format  # noqa: E402

LOOKUP = {
    "type": "object",
    "properties": {
        "matched": {"type": "boolean"},
        "row": {"type": "object", "properties": {"rate": {"type": "number"}}},
        "citations": {"type": "array"},
    },
    "required": ["matched", "row", "citations"],
}


def test_the_reply_format_wraps_the_output_schema_without_citations():
    fmt = reply_format(LOOKUP)
    assert fmt["required"] == ["result", "cite"]
    assert fmt["properties"]["cite"] == {"type": "array", "items": {"type": "integer"}}
    result = fmt["properties"]["result"]
    assert set(result["properties"]) == {"matched", "row"}
    assert result["required"] == ["matched", "row"]
    assert "citations" in LOOKUP["properties"]  # the block's own schema is untouched


def test_a_schema_format_goes_to_ollama_as_is(bodies):
    fmt = reply_format(LOOKUP)
    OllamaClient(model="m").generate(prompt="p", system="s", format=fmt)
    assert bodies[0]["format"] == fmt


def test_the_spawner_asks_for_the_block_reply_format():
    import inspect

    from agent.swarm import spawner

    assert "format=reply_format(block.output_json_schema)" in inspect.getsource(spawner._child_worker)
