"""Best-effort scan of the ONE free-text slot in a code skeleton: docstrings.

A docstring is prose a human wrote, so unlike a signature it can carry a value. It
is the code-library analogue of a data-dictionary ``description``: kept because it
is the point of the feature, but scanned at extraction and withheld on a
high-confidence hit. This is DEFENCE IN DEPTH, not a guarantee — a customer name or
an internal code a regex can't match survives, exactly as for dictionary
descriptions. Findings are value-free (a kind, never the matched value).

Deliberately self-contained (``secrets.scan`` OR ``pii.scan``) rather than reusing
``mooring.ai.context``'s private ``_desc_kind`` — codelib does not ride the
``context/`` reader and must not depend on its internals.
"""

from __future__ import annotations

from mooring.ai import pii, secrets


def scan_docstring(text: str) -> str | None:
    """The first secret-or-PII kind in ``text``, or ``None`` when it is clean."""
    hits = secrets.scan(text) or pii.scan(text)
    return hits[0].kind if hits else None
