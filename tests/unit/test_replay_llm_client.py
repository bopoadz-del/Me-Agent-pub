"""The replay LLM client: model replies from a data file, for CI runs with no model.

Selected by LLM_CLIENT=replay with LLM_REPLAY_FILE, only under TEST_MODE (like
the mock seam). Each entry answers the first request whose prompt+system holds
all of its 'when_all' strings; a request no entry matches raises, so a changed
prompt cannot pass on a stale default. Block subprocesses select it the same way.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def replay_file(tmp_path):
    path = tmp_path / "replies.json"
    path.write_text(json.dumps({"replies": [
        {"when_all": ["alpha", "beta"], "reply": {"x": 1}},
        {"when_all": ["alpha"], "reply": {"x": 2}},
    ]}), encoding="utf-8")
    return path


def test_the_first_entry_whose_strings_all_appear_answers(replay_file):
    from common.models.llm_client import ReplayLLMClient

    client = ReplayLLMClient(replay_file)
    assert client.generate("alpha and beta", "sys") == {"x": 1}
    assert client.generate("alpha only", "beta in system") == {"x": 1}
    assert client.generate("alpha only", "sys") == {"x": 2}


def test_a_request_no_entry_matches_is_an_error(replay_file):
    from common.models.llm_client import ReplayLLMClient

    with pytest.raises(LookupError):
        ReplayLLMClient(replay_file).generate("gamma", "sys")


def test_a_reply_is_a_fresh_copy(replay_file):
    from common.models.llm_client import ReplayLLMClient

    client = ReplayLLMClient(replay_file)
    client.generate("alpha", "sys")["x"] = 99
    assert client.generate("alpha", "sys") == {"x": 2}


def test_the_factory_selects_replay_from_env(replay_file, monkeypatch):
    from common.models.llm_client import ReplayLLMClient, build_llm_client

    monkeypatch.setenv("LLM_CLIENT", "replay")
    monkeypatch.setenv("LLM_REPLAY_FILE", str(replay_file))
    assert isinstance(build_llm_client(), ReplayLLMClient)


def test_block_subprocesses_select_it_the_same_way(replay_file, monkeypatch):
    from agent.swarm import spawner
    from common.models.llm_client import ReplayLLMClient

    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("LLM_CLIENT", "replay")
    monkeypatch.setenv("LLM_REPLAY_FILE", str(replay_file))
    env = spawner._scrubbed_env()
    assert env["LLM_CLIENT"] == "replay" and env["LLM_REPLAY_FILE"] == str(replay_file)
    assert isinstance(spawner._child_llm(), ReplayLLMClient)


def test_replay_outside_test_mode_is_refused(replay_file, monkeypatch):
    from agent import bootstrapper

    for key, value in {
        "AGENT_ID": "a", "AGENT_API_TOKEN": "t" * 40, "AIRGAP": "true",
        "LLM_CLIENT": "replay", "LLM_REPLAY_FILE": str(replay_file),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TEST_MODE", raising=False)
    with pytest.raises(SystemExit):
        bootstrapper._validate_env()
