"""Citations are selected by the model but worded by the evidence."""
from __future__ import annotations

from agent.swarm.grounding import build_prompt, chunk_evidence, parse_reply

DOC = "Alpha: 3. Beta: 7.\nGamma rate: 2 units/h."
SCHEMA = {
    "type": "object",
    "properties": {"total": {"type": "number"}},
    "required": ["total"],
    "additionalProperties": False,
}


def test_chunks_are_verbatim_substrings():
    chunks = chunk_evidence([("doc.txt", DOC)])
    assert [c.text for c in chunks] == ["Alpha: 3.", "Beta: 7.", "Gamma rate: 2 units/h."]
    assert all(c.text in DOC and c.source == "doc.txt" for c in chunks)


def test_cited_numbers_become_exact_excerpts_and_bad_numbers_drop():
    chunks = chunk_evidence([("doc.txt", DOC)])
    result, citations, unheld = parse_reply(
        {"result": {"total": 7}, "cite": [1, "2", 9, -1, True]}, chunks, SCHEMA
    )
    assert result == {"total": 7}
    assert [c["excerpt"] for c in citations] == ["Beta: 7.", "Gamma rate: 2 units/h."]
    assert unheld == ["total"]  # 7 sits beside "Beta", not beside its own label


def test_model_worded_excerpt_never_reaches_a_citation():
    chunks = chunk_evidence([("doc.txt", DOC)])
    _, citations, _unheld = parse_reply(
        {"total": 21, "citations": [{"excerpt": "Beta: 7"}, {"excerpt": "Delta: 99"}]},
        chunks,
        SCHEMA,
    )
    assert [c["excerpt"] for c in citations] == ["Beta: 7."]


def test_schema_with_citations_gets_grounded_citations_only():
    chunks = chunk_evidence([("doc.txt", DOC)])
    schema = {
        "type": "object",
        "properties": {"citations": {"type": "array"}, "ok": {"type": "boolean"}},
    }
    result, _, _unheld = parse_reply(
        {"result": {"ok": True, "citations": [{"excerpt": "invented"}]}, "cite": [0]},
        chunks,
        schema,
    )
    assert result["citations"] == [{"source": "doc.txt", "excerpt": "Alpha: 3."}]


def test_prompt_numbers_every_chunk_and_carries_feedback():
    chunks = chunk_evidence([("doc.txt", DOC)])
    prompt = build_prompt("task", chunks, {"up": {"x": 1}}, SCHEMA, feedback="bad")
    assert "[0] Alpha: 3." in prompt and "[2] Gamma rate: 2 units/h." in prompt
    assert '"up"' in prompt and "rejected: bad" in prompt


def test_a_table_row_is_shown_labelled_but_cited_verbatim():
    chunks = chunk_evidence([("t.csv", "kind,rate_usd\ndry,10\nreefer,18.5\n")])
    prompt = build_prompt("task", chunks, {}, SCHEMA)
    assert "[1] kind: reefer; rate_usd: 18.5" in prompt
    _, citations, _ = parse_reply({"result": {"total": 1}, "cite": [1]}, chunks, SCHEMA)
    assert citations == [{"source": "t.csv", "excerpt": "reefer,18.5"}]
