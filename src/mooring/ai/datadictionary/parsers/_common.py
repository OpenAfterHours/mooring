"""Helpers shared by every format parser.

All output-shaping is funnelled through these: a slot only ever receives a
scalar (:func:`_scalar_str`), descriptions are capped (:func:`_cap`), and every
unread source key is recorded (:func:`_record_dropped`) so a team sees exactly
what was withheld.
"""

from __future__ import annotations

from mooring.ai.datadictionary.model import DESC_CAP


def _scalar_str(value) -> str:
    """A slot may only ever hold a scalar. A nested list/dict placed under an
    allowed key is NOT stringified — that would leak the nested values; it
    degrades to empty so a mis-shaped field becomes a missing field, not a leak."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (str, int, float)):
        return str(value)
    return ""


def _as_list(value) -> list:
    """Coerce a value that may legally be a scalar, a list, or absent to a list,
    so 'x or []' style concatenation can't blow up on a mapping/scalar."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _first(node: dict, keys, default: str = "") -> str:
    for k in keys:
        if k in node:
            scalar = _scalar_str(node[k])
            if scalar != "":
                return scalar
    return default


def _cap(text) -> str:
    text = " ".join(_scalar_str(text).split())
    return text if len(text) <= DESC_CAP else text[: DESC_CAP - 1].rstrip() + "..."


def _record_dropped(node: dict, used: set[str], dropped: set[str]) -> None:
    for key in node:
        if key not in used:
            dropped.add(str(key))
