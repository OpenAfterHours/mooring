"""Best-effort scan of the ONE free-text slot in a notebook entry: the markdown summary.

A markdown cell is prose a human wrote, so unlike a path or an import name it can carry
a value. It is the catalog analogue of a code-library docstring: kept because it is the
point of the feature ("what does this notebook do?"), but scanned at extraction and
withheld on a high-confidence hit. This is DEFENCE IN DEPTH, not a guarantee — a
customer name or an internal code a regex can't match survives, exactly as for a
docstring. Findings are value-free (a kind, never the matched value).

Deliberately self-contained (``secrets.scan`` OR ``pii.scan``), mirroring
:mod:`mooring.ai.codelib.docscan`: the catalog must not depend on another feature
package's internals to stay value-blind.
"""

from __future__ import annotations

from mooring.ai import pii, secrets


def scan_summary(text: str) -> str | None:
    """The first secret-or-PII kind in ``text``, or ``None`` when it is clean."""
    hits = secrets.scan(text) or pii.scan(text)
    return hits[0].kind if hits else None
