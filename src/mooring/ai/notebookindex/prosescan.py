"""Best-effort scan of the ONE authored-prose slot in a catalog entry: the H1 title.

A heading is prose a human wrote, so unlike a path or an import name it can carry a
value. It is kept because a repo of ``q3_recon_v2.py`` files is otherwise illegible, and
it is the *only* prose the catalog carries — the free-prose summary an earlier draft
included was cut outright (see :mod:`mooring.ai.notebookindex.model`), because no scanner
makes an arbitrary markdown paragraph value-free.

Scanning is DEFENCE IN DEPTH, not a guarantee: a value typed into a heading can survive,
exactly as one typed into a notebook cell can. Findings are value-free (a kind, never the
matched value), and a hit withholds the title WHOLE rather than trimming around it.

Unlike :mod:`mooring.ai.codelib.docscan`, this uses the NER-capable
:func:`mooring.ai.pii.scan_prose` — the same scanner ``ai/scan.py`` and the
notebook-source banner use — so a person or organisation name in a heading is caught when
the operator has turned name detection on. The caller injects that configuration
(:func:`make_scanner`), because the extractor sits below config and must not read it.
"""

from __future__ import annotations

from typing import Callable

from mooring.ai import pii, secrets

# The scanner signature the extractor accepts: text -> a value-free kind, or None.
Scanner = Callable[[str], "str | None"]


def scan_title(text: str) -> str | None:
    """The first secret-or-PII kind in ``text``, or ``None`` when it is clean.

    The default (structured-only) scanner: checksum-validated identifiers, shape-anchored
    emails/NINOs, and high-confidence secrets. Local callers that never egress — the hub's
    per-row listing — use this, because running a NER model per file on every
    ``/api/state`` poll would be unacceptable and the hub renders to the analyst's own
    browser, not to a model.
    """
    hits = secrets.scan(text) or pii.scan(text)
    return hits[0].kind if hits else None


def make_scanner(
    *,
    names: bool = False,
    labels: tuple[str, ...] | None = None,
    threshold: float = 0.7,
    model=None,
    backend: str = "gliner",
) -> Scanner:
    """A :data:`Scanner` that also runs the optional local NER name pass.

    Used on the path that actually egresses (chat context assembly), where the operator's
    ``[ai.pii]`` configuration is known. ``pii.scan_prose`` degrades silently to
    structured-only when the extra is missing or the model is not ready, so this never
    fails extraction — it only ever catches MORE than :func:`scan_title`.
    """

    def scan(text: str) -> str | None:
        if hits := secrets.scan(text):
            return hits[0].kind
        hits = pii.scan_prose(
            text, names=names, labels=labels, threshold=threshold, model=model, backend=backend
        )
        return hits[0].kind if hits else None

    return scan
