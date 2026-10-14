"""Turn values the model copied from a page into skill variables.

When a trajectory types "1234+5678=" and the page the model was looking at
said "First: 1234 ... Second: 5678", the skill should not remember 1234 and
5678. It should remember "the number after 'First:'" and "the number after
'Second:'", so a replay on a page with different numbers still does the right
thing. No LLM is involved: a variable is a value that occurred verbatim in the
page text, and its locator is the text immediately before it on that line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NUMBER = re.compile(r"\d[\d,.]*\d|\d")
WORD = re.compile(r"[A-Za-z][\w@.-]{2,}|[一-鿿]{2,}")
MAX_PREFIX = 40


@dataclass
class Variable:
    pattern: str  # regex with one capture group, applied to the page text
    kind: str  # number | word
    example: str  # the value seen when the skill was learned


def _candidates(value: str) -> list[tuple[int, int, str, str]]:
    """(start, end, token, kind) for substrings worth parameterizing, longest first."""
    found: list[tuple[int, int, str, str]] = []
    for m in NUMBER.finditer(value):
        found.append((m.start(), m.end(), m.group(), "number"))
    for m in WORD.finditer(value):
        found.append((m.start(), m.end(), m.group(), "word"))
    found.sort(key=lambda t: (-(t[1] - t[0]), t[0]))
    return found


def _locator(context: str, token: str, kind: str) -> str | None:
    """Regex that finds `token` in `context` by the text preceding it on the same line."""
    idx = context.find(token)
    if idx == -1:
        return None
    line_start = context.rfind("\n", 0, idx) + 1
    prefix = context[max(line_start, idx - MAX_PREFIX) : idx]
    prefix = prefix.lstrip()
    value_re = r"([\d,.]+)" if kind == "number" else r"(\S+)"
    pattern = (re.escape(prefix) + r"\s*" if prefix else r"(?m)^\s*") + value_re
    try:
        m = re.search(pattern, context)
    except re.error:
        return None
    if not m or m.group(1) != token:
        return None
    # the prefix must identify the value uniquely, otherwise a replay could pick the wrong one
    if prefix and len(re.findall(pattern, context)) != 1:
        return None
    return pattern


def parameterize(value: str, context: str) -> tuple[str, dict[str, Variable]] | None:
    """Return (template, variables) if some part of `value` came from `context`, else None."""
    if not value or not context:
        return None
    taken: list[tuple[int, int]] = []
    variables: dict[str, Variable] = {}
    pieces: list[tuple[int, int, str]] = []  # (start, end, var name)
    for start, end, token, kind in _candidates(value):
        if any(s < end and start < e for s, e in taken):
            continue
        if len(token) < 2 and kind == "number":
            continue
        if token not in context:
            continue
        pattern = _locator(context, token, kind)
        if pattern is None:
            continue
        name = f"v{len(variables) + 1}"
        variables[name] = Variable(pattern=pattern, kind=kind, example=token)
        taken.append((start, end))
        pieces.append((start, end, name))
    if not variables:
        return None
    pieces.sort()
    out, pos = [], 0
    for start, end, name in pieces:
        out.append(value[pos:start].replace("{", "{{").replace("}", "}}"))
        out.append("{" + name + "}")
        pos = end
    out.append(value[pos:].replace("{", "{{").replace("}", "}}"))
    return "".join(out), variables


def resolve(template: str, variables: dict[str, Variable], context: str) -> tuple[str | None, str]:
    """Fill the template from the current page text. Returns (value, error)."""
    values: dict[str, str] = {}
    for name, var in variables.items():
        m = re.search(var.pattern, context or "")
        if not m:
            return None, f"could not find {name} (was {var.example!r}) on the current page"
        values[name] = m.group(1)
    try:
        return template.format(**values), ""
    except (KeyError, IndexError, ValueError) as e:
        return None, f"bad template {template!r}: {e}"


def variables_to_json(variables: dict[str, Variable]) -> dict[str, dict]:
    return {k: {"pattern": v.pattern, "kind": v.kind, "example": v.example} for k, v in variables.items()}


def variables_from_json(data: dict[str, dict] | None) -> dict[str, Variable]:
    return {k: Variable(**v) for k, v in (data or {}).items()}
