"""The mock LLM test seam answers the same way whatever domain a prompt is in.

A test that needs a domain-shaped answer passes its own handler; the seam's
default reads only the numbers in the text, so no kit's vocabulary can change
what it returns.
"""
from __future__ import annotations

import pytest

from common.models.llm_client import MockLLMClient


@pytest.mark.parametrize("word", ["berth", "cement", "crane", "invoice"])
def test_the_default_answer_does_not_depend_on_the_domain_word(word):
    plain = MockLLMClient().generate("values 24 10", "system")
    worded = MockLLMClient().generate(f"{word} values 24 10", f"{word} system")
    assert worded == plain
