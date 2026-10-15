"""Evidence grounding: citations are chosen by the model, worded by the evidence.

The model never writes an excerpt. Evidence is split into numbered chunks, the
model answers with the numbers of the chunks it used, and every citation's
excerpt is copied from the chunk itself. A citation is therefore verbatim
evidence by construction; a number that names no chunk is dropped.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")
_DROP_KEYS = ("cite", "citations", "confidence")


@dataclass(frozen=True)
class Chunk:
    source: str
    text: str
    #: For a row of a delimited table: (column header, cell) pairs. A number in
    #: a cell is labelled by its header, not by the words on its own line.
    cells: tuple[tuple[str, str], ...] = ()


_DELIMITERS = (",", "\t", ";", "|")


def _table(text: str) -> Optional[tuple[str, list[str], list[str]]]:
    """(delimiter, header cells, data lines) when ``text`` is a delimited table:
    two or more non-empty lines that all split into the same number (>= 2) of
    cells on one delimiter. Prose never qualifies by accident twice over."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    for delim in _DELIMITERS:
        widths = {len(ln.split(delim)) for ln in lines}
        if len(widths) == 1 and widths.pop() >= 2:
            header = [c.strip() for c in lines[0].split(delim)]
            return delim, header, lines[1:]
    return None


def chunk_evidence(documents: list[tuple[str, str]]) -> list[Chunk]:
    """Split each (source, text) into verbatim chunks: a table's data rows, or
    the line/sentence pieces of prose."""
    chunks: list[Chunk] = []
    for source, text in documents:
        table = _table(text)
        if table is not None:
            delim, header, rows = table
            for row in rows:
                cells = tuple(zip(header, (c.strip() for c in row.split(delim))))
                chunks.append(Chunk(source, row.strip(), cells))
            continue
        for line in text.splitlines():
            for piece in _SENTENCE_END.split(line):
                piece = piece.strip()
                if piece:
                    chunks.append(Chunk(source, piece))
    return chunks


def build_prompt(
    task: str,
    chunks: list[Chunk],
    upstream: dict[str, Any],
    output_schema: dict[str, Any],
    feedback: str = "",
) -> str:
    lines = [task, "", "Evidence (cite by number):"]
    if chunks:
        # A table row is shown with its column headers so each cell is labelled;
        # its citation excerpt is still the row exactly as the document has it.
        lines.extend(
            f"[{i}] " + ("; ".join(f"{h}: {v}" for h, v in c.cells) if c.cells else c.text)
            for i, c in enumerate(chunks)
        )
    else:
        lines.append("(none attached)")
    if upstream:
        lines += ["", "Results from earlier steps:", json.dumps(upstream, sort_keys=True)]
    lines += [
        "",
        'Reply with one JSON object: {"result": <object matching the output schema>, '
        '"cite": [<numbers of every evidence line you used>]}',
        "Output schema: " + json.dumps(output_schema, sort_keys=True),
    ]
    if feedback:
        lines += ["", "Your previous reply was rejected: " + feedback]
    return "\n".join(lines)


def reply_format(output_schema: dict[str, Any]) -> dict[str, Any]:
    """The JSON schema the model's reply must follow, for constrained decoding:
    {"result": <the block's output schema>, "cite": [<line numbers>]}. A block's
    "citations" are written by grounding from the evidence, never by the model,
    so they are left out of what it must generate."""
    result = dict(output_schema) if isinstance(output_schema, dict) else {"type": "object"}
    props = result.get("properties")
    if isinstance(props, dict) and "citations" in props:
        result["properties"] = {k: v for k, v in props.items() if k != "citations"}
        if isinstance(result.get("required"), list):
            result["required"] = [k for k in result["required"] if k != "citations"]
    return {
        "type": "object",
        "properties": {
            "result": result,
            "cite": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["result", "cite"],
    }


def _cited_indices(raw: dict[str, Any], chunks: list[Chunk]) -> list[int]:
    indices: list[int] = []

    def add(i: int) -> None:
        if 0 <= i < len(chunks) and i not in indices:
            indices.append(i)

    cite = raw.get("cite")
    if isinstance(cite, (int, str)):
        cite = [cite]
    if isinstance(cite, list):
        for item in cite:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                add(item)
            elif isinstance(item, str) and item.strip().strip("[]").isdigit():
                add(int(item.strip().strip("[]")))
    # Excerpts quoted by the model only select chunks; the chunk supplies the words.
    quoted = raw.get("citations")
    if isinstance(quoted, list):
        for item in quoted:
            text = item.get("excerpt") if isinstance(item, dict) else item
            if not isinstance(text, str) or not text.strip():
                continue
            needle = text.strip()
            for i, chunk in enumerate(chunks):
                if needle in chunk.text or chunk.text in needle:
                    add(i)
    return indices




# -- every value held ---------------------------------------------------------------
#
# Owner rule (M-PHASE-1-CLOSE), the same for every kit: each scalar the step
# returns must be held by a line it cites. Numbers are held in full and beside
# their own label; strings as whole words; booleans and nulls are not values.

_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w]|\.\d)")
_WORD = re.compile(r"[a-z]+")
#: Field-name words that label nothing on their own.
_NOT_LABELS = frozenset({"per", "the", "of", "and", "or", "in", "to", "for", "by", "a", "an", "is", "id"})


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text.lower())}


def _label(field: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(field.lower()) if w not in _NOT_LABELS and len(w) > 1}


def _numbers(text: str) -> list[float]:
    out = []
    for token in _NUMBER.findall(text):
        try:
            out.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return out


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def _labelled(label: set[str], words: set[str]) -> bool:
    """A strict majority of the field's label words appear (a word and its
    longer form count: rate/rates, hour/hourly). One shared word is not a
    label: base_rate_usd_per_hour is not labelled by "Dwell hours"."""
    if not label:
        return True

    def present(term: str) -> bool:
        return any(w == term or (min(len(w), len(term)) >= 3 and (w.startswith(term) or term.startswith(w))) for w in words)

    return sum(1 for term in label if present(term)) * 2 > len(label)


def _holds_number(chunk: Chunk, field: str, value: float) -> bool:
    label = _label(field)
    if chunk.cells:
        return any(
            any(_same(n, value) for n in _numbers(cell)) and _labelled(label, _words(header))
            for header, cell in chunk.cells
        )
    if not any(_same(n, value) for n in _numbers(chunk.text)):
        return False
    return _labelled(label, _words(chunk.text))


def _holds_string(chunk: Chunk, value: str) -> bool:
    word = value.strip()
    if len(word) < 2:
        return False
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", chunk.text, re.IGNORECASE) is not None


def _values(result: Any, path: str = "") -> list[tuple[str, Any]]:
    """(dotted field path, scalar) for every value in ``result``."""
    if isinstance(result, bool) or result is None:
        return []
    if isinstance(result, (int, float, str)):
        return [(path, result)]
    if isinstance(result, dict):
        out = []
        for key, val in result.items():
            if not path and key in _DROP_KEYS:
                continue
            out.extend(_values(val, f"{path}.{key}" if path else str(key)))
        return out
    if isinstance(result, list):
        return [pair for item in result for pair in _values(item, path)]
    return []


def _evaluate(expr: str, env: dict[str, float]) -> Optional[float]:
    """Arithmetic over named operands only: + - * / and parentheses."""
    import ast
    import operator

    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        if isinstance(node, ast.Name) and node.id in env:
            return env[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError("not arithmetic over the operands")

    try:
        return walk(ast.parse(expr, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError):
        return None


def _formula_holders(expr: str, value: float, pool: list[int], chunks: list[Chunk]) -> Optional[list[int]]:
    """Lines whose labelled operands make ``expr`` give ``value``, or None."""
    import ast
    import itertools

    try:
        names = sorted({n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)})
    except SyntaxError:
        return None
    candidates: list[list[tuple[float, int]]] = []
    for name in names:
        found = [
            (n, i) for i in pool for n in _numbers(chunks[i].text) if _holds_number(chunks[i], name, n)
        ]
        if not found:
            return None
        candidates.append(found)
    for combo in itertools.islice(itertools.product(*candidates), 4096):
        got = _evaluate(expr, {name: n for name, (n, _i) in zip(names, combo)})
        if got is not None and _same(got, float(value)):
            return sorted({i for _n, i in combo})
    return None


def _holders(
    field: str, value: Any, pool: list[int], chunks: list[Chunk], formulas: dict[str, str], chosen: list[int]
) -> list[int]:
    leaf = field.rsplit(".", 1)[-1]
    if isinstance(value, str):
        holding = [i for i in pool if _holds_string(chunks[i], value)]
        already = [i for i in holding if i in chosen]
        return (already or holding)[:1]
    direct = [i for i in pool if _holds_number(chunks[i], leaf, float(value))][:1]
    if direct:
        return direct
    expr = formulas.get(leaf) or formulas.get(field)
    if expr:
        return _formula_holders(expr, float(value), pool, chunks) or []
    return []


def ground(
    result: dict[str, Any],
    chunks: list[Chunk],
    cited: list[int],
    formulas: Optional[dict[str, str]] = None,
) -> tuple[list[int], list[str]]:
    """(lines to cite, fields no line holds). With a cite list the values must
    be held within it; without one, every line that holds a value is cited,
    and only if every value is held."""
    formulas = formulas or {}
    pool = list(cited) if cited else list(range(len(chunks)))
    chosen: list[int] = []
    unheld: list[str] = []
    # Numbers first: they pin rows, and a string then prefers a line already cited.
    values = sorted(_values(result), key=lambda fv: isinstance(fv[1], str))
    for field, value in values:
        lines = _holders(field, value, pool, chunks, formulas, chosen)
        if not lines:
            unheld.append(field)
        for i in lines:
            if i not in chosen:
                chosen.append(i)
    if cited:
        return list(cited), unheld
    return (sorted(chosen) if not unheld else []), unheld


def _operand(name: str, upstream: Any, chunks: list[Chunk]) -> Optional[float]:
    """The one value an operand can take: from an earlier step's verified
    result under that name, else the single number the evidence holds beside
    that label. None when there is no such value or more than one."""
    found: set[float] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == name and isinstance(val, (int, float)) and not isinstance(val, bool):
                    found.add(float(val))
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(upstream)
    if not found:
        found = {n for c in chunks for n in _numbers(c.text) if _holds_number(c, name, n)}
    return found.pop() if len(found) == 1 else None


def apply_formulas(
    result: dict[str, Any], chunks: list[Chunk], formulas: dict[str, str], upstream: Any
) -> dict[str, Any]:
    """A field the kit defines by a formula is computed, not taken from the
    model: when every operand has exactly one verified value, the formula
    gives the field's value. Otherwise the model's value stands and must be
    grounded like any other."""
    import ast

    out = dict(result)
    for field, expr in formulas.items():
        if field not in out:
            continue
        try:
            names = {n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)}
        except SyntaxError:
            continue
        env = {name: _operand(name, upstream, chunks) for name in names}
        if any(v is None for v in env.values()):
            continue
        value = _evaluate(expr, env)
        if value is not None:
            out[field] = int(value) if float(value).is_integer() else round(value, 6)
    return out


def parse_reply(
    raw: dict[str, Any],
    chunks: list[Chunk],
    output_schema: dict[str, Any],
    formulas: Optional[dict[str, str]] = None,
    upstream: Any = None,
) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """Return (result, citations, unheld fields) from a model reply."""
    if isinstance(raw.get("result"), dict):
        result = dict(raw["result"])
    else:
        result = {k: v for k, v in raw.items() if k not in _DROP_KEYS}
    if formulas:
        result = apply_formulas(result, chunks, formulas, upstream)
    indices, unheld = ground(result, chunks, _cited_indices(raw, chunks), formulas)
    citations = [{"source": chunks[i].source, "excerpt": chunks[i].text} for i in indices]
    properties = output_schema.get("properties") if isinstance(output_schema, dict) else None
    if isinstance(properties, dict) and "citations" in properties:
        # A block whose output declares citations gets the grounded ones, never
        # the model's own wording.
        result["citations"] = citations
    else:
        result.pop("citations", None)
    return result, citations, unheld
