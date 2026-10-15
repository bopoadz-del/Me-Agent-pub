"""Every value a grounded step returns is held by a line it cites.

Rule (owner, M-PHASE-1-CLOSE), the same for every kit: each scalar in the
result -- numbers as numbers, strings as whole words; booleans and nulls are
not values -- must be held by at least one cited evidence line. Explicit cites
are checked the same way. With no cite list, the lines holding the values
become the citations only when EVERY value is held. A value no line holds
refuses the step, naming the field.

A number is held only in full and beside its own label: a word of its field
name on the same line, or the column header of its cell in a table. A field
the kit computes (BlockDef.formulas) is held when the kit's formula, over
operands each held by its own label, gives the value.
"""
from __future__ import annotations

from agent.swarm.grounding import Chunk, chunk_evidence, parse_reply

TARIFF = chunk_evidence([("tariff.txt", "Container type: dry. Dwell hours: 24. Base rate: 10 USD/hr.\n")])
LOOKUP = {
    "type": "object",
    "properties": {"matched": {"type": "boolean"}, "row": {"type": "object"}, "citations": {"type": "array"}},
}


def _excerpts(citations):
    return [c["excerpt"] for c in citations]


# (a) one real string plus one invented number is refused ------------------------------


def test_a_real_string_and_an_invented_number_is_refused_naming_the_number():
    raw = {"result": {"matched": True, "row": {"container_type": "dry", "base_rate_usd_per_hour": 99.0}}}
    _result, _citations, unheld = parse_reply(raw, TARIFF, LOOKUP)
    assert unheld == ["row.base_rate_usd_per_hour"]


# (b) all values held, no cite list: accepted with the right lines ----------------------


def test_all_values_held_without_a_cite_list_cites_exactly_their_lines():
    raw = {"result": {"matched": True, "row": {"container_type": "dry", "base_rate_usd_per_hour": 10.0, "currency": "USD"}}}
    result, citations, unheld = parse_reply(raw, TARIFF, LOOKUP)
    assert unheld == []
    assert _excerpts(citations) == ["Container type: dry.", "Base rate: 10 USD/hr."]
    assert result["citations"] == citations


# (c) an explicit cite at a line that does not hold the value is refused ----------------


def test_an_explicit_cite_that_does_not_hold_the_value_is_refused():
    raw = {"result": {"row": {"base_rate_usd_per_hour": 10.0}}, "cite": [1]}  # [1] is "Dwell hours: 24."
    _result, _citations, unheld = parse_reply(raw, TARIFF, LOOKUP)
    assert unheld == ["row.base_rate_usd_per_hour"]


def test_an_explicit_cite_that_holds_every_value_is_kept_as_cited():
    raw = {"result": {"row": {"base_rate_usd_per_hour": 10.0}}, "cite": [2]}
    _result, citations, unheld = parse_reply(raw, TARIFF, LOOKUP)
    assert unheld == [] and _excerpts(citations) == ["Base rate: 10 USD/hr."]


# numbers: in full, beside their label ------------------------------------------------


def test_a_bare_small_integer_on_an_unrelated_line_grounds_nothing():
    chunks = [Chunk("e.txt", "3 cranes serve berth B."), Chunk("e.txt", "Dwell hours: 24.")]
    _r, _c, unheld = parse_reply({"result": {"dwell_hours": 3}}, chunks, {"type": "object", "properties": {}})
    assert unheld == ["dwell_hours"]


def test_a_number_must_match_in_full():
    chunks = [Chunk("e.txt", "Base rate: 100 USD/hr."), Chunk("e.txt", "Base rate: 10.5 USD/hr.")]
    _r, _c, unheld = parse_reply({"result": {"base_rate_usd_per_hour": 10}}, chunks, {"type": "object", "properties": {}})
    assert unheld == ["base_rate_usd_per_hour"]


def test_a_string_must_match_as_a_whole_word():
    chunks = [Chunk("e.txt", "Container type: drywall.")]
    _r, _c, unheld = parse_reply({"result": {"container_type": "dry"}}, chunks, {"type": "object", "properties": {}})
    assert unheld == ["container_type"]


def test_booleans_and_nulls_are_not_values():
    _r, citations, unheld = parse_reply({"result": {"matched": False, "row": None}}, TARIFF, LOOKUP)
    assert unheld == [] and citations == []


# tables: a cell's label is its column header ------------------------------------------


def test_a_table_cell_is_labelled_by_its_column_header():
    doc = "container_type,base_rate_usd_per_hour,currency\nreefer,18.5,USD\ndry,10,USD\n"
    chunks = chunk_evidence([("tariff.csv", doc)])
    raw = {"result": {"row": {"container_type": "dry", "base_rate_usd_per_hour": 10, "currency": "USD"}}}
    _r, citations, unheld = parse_reply(raw, chunks, LOOKUP)
    assert unheld == [] and _excerpts(citations) == ["dry,10,USD"]


def test_a_table_number_in_another_column_is_not_held():
    doc = "container_type,dwell_hours,base_rate_usd_per_hour\ndry,10,12\n"
    chunks = chunk_evidence([("t.csv", doc)])
    _r, _c, unheld = parse_reply({"result": {"base_rate_usd_per_hour": 10}}, chunks, {"type": "object", "properties": {}})
    assert unheld == ["base_rate_usd_per_hour"]


# computed fields: the kit's formula over held operands --------------------------------

FEE = {"type": "object", "properties": {"berth_fee_usd": {"type": "number"}, "currency": {"type": "string"}}}
FORMULAS = {"berth_fee_usd": "dwell_hours * base_rate_usd_per_hour"}


def test_a_computed_value_is_held_by_its_formula_over_held_operands():
    _r, citations, unheld = parse_reply(
        {"result": {"berth_fee_usd": 240.0, "currency": "USD"}}, TARIFF, FEE, formulas=FORMULAS
    )
    assert unheld == []
    assert _excerpts(citations) == ["Dwell hours: 24.", "Base rate: 10 USD/hr."]


def test_a_computed_value_the_formula_does_not_give_is_recomputed():
    """Every operand has one held value, so the kit's formula sets the field."""
    result, citations, unheld = parse_reply(
        {"result": {"berth_fee_usd": 250.0, "currency": "USD"}}, TARIFF, FEE, formulas=FORMULAS
    )
    assert result["berth_fee_usd"] == 240 and unheld == []
    assert _excerpts(citations) == ["Dwell hours: 24.", "Base rate: 10 USD/hr."]


def test_a_computed_value_without_a_declared_formula_is_not_held():
    _r, _c, unheld = parse_reply({"result": {"berth_fee_usd": 240.0}}, TARIFF, FEE)
    assert unheld == ["berth_fee_usd"]


# -- a formula field is computed from verified operands, not taken from the model --

from agent.swarm.grounding import apply_formulas  # noqa: E402

FEE_FORMULA = {"berth_fee_usd": "dwell_hours * base_rate_usd_per_hour"}
TABLE = "| container_type | base_rate_usd_per_hour | currency |\n| reefer | 18.5 | USD |\n| dry | 10 | USD |\n"


def test_bad_model_arithmetic_is_replaced_by_the_kit_formula():
    chunks = chunk_evidence([("schedule.md", TABLE), ("stay.txt", "Dwell hours: 8.\n")])
    upstream = {"lookup": {"row": {"base_rate_usd_per_hour": 18.5, "container_type": "reefer"}}}
    result, citations, unheld = parse_reply(
        {"berth_fee_usd": 146, "currency": "USD"}, chunks, {"type": "object"}, FEE_FORMULA, upstream
    )
    assert result["berth_fee_usd"] == 148
    assert unheld == []
    assert {c["excerpt"] for c in citations} >= {"| reefer | 18.5 | USD |", "Dwell hours: 8."}


def test_an_ambiguous_operand_leaves_the_model_value_to_be_grounded():
    chunks = chunk_evidence([("schedule.md", TABLE), ("stay.txt", "Dwell hours: 8.\n")])
    # No upstream row: two base rates are labelled in the evidence, so nothing is computed.
    assert apply_formulas({"berth_fee_usd": 146}, chunks, FEE_FORMULA, {}) == {"berth_fee_usd": 146}
    _, _, unheld = parse_reply({"berth_fee_usd": 146}, chunks, {"type": "object"}, FEE_FORMULA, {})
    assert unheld == ["berth_fee_usd"]


def test_a_field_the_model_did_not_return_is_not_invented():
    chunks = chunk_evidence([("stay.txt", "Dwell hours: 8.\n")])
    upstream = {"row": {"base_rate_usd_per_hour": 10}}
    assert apply_formulas({"currency": "USD"}, chunks, FEE_FORMULA, upstream) == {"currency": "USD"}
