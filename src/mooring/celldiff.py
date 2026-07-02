"""Cell-aware diff of a workspace file against its last-synced base — PURE.

The "Review changes" panel's engine: given the base (last-synced) bytes and the
local bytes of one file, describe what a push would publish. For a marimo
notebook the change is described per cell, matched exact-source-first and then
by similarity — marimo persists no cell identity (see marimo_rt), so matching
is heuristic and anything it cannot pair confidently lands in an explicit
``unmatched`` bucket rather than a guessed pairing. Anything that defeats the
cell view degrades loudly: an unparseable notebook or a non-``.py`` text file
gets a whole-file unified line diff (the same text shape the version-history
endpoint returns, so one renderer serves both); undecodable or oversized
content gets sizes only.

No file IO and no network — callers hand in bytes (the hub route reads the
local side via ``gitsha.read_for_push`` and fetches the base blob itself), and
``.py`` bytes are LF-normalized here exactly like the push path, so a Windows
CRLF flip never shows as a phantom whole-file change. Imports stay at
``marimo_rt`` + the ``notebook_template`` sniff + stdlib: this module sits at
L2 beside the marimo bridge, and the sync domain core must never import it
(celldiff → marimo_rt → marimo would break the frozen-core-is-lean contract —
see ``.importlinter``).
"""

from __future__ import annotations

import difflib
from collections import defaultdict, deque
from dataclasses import dataclass, field

from mooring import marimo_rt, notebook_template

# Cells pairing at or above this SequenceMatcher ratio count as "the same cell,
# edited"; anything below is never claimed as a match (the unmatched bucket).
SIMILARITY_THRESHOLD = 0.5

# Diffing is quadratic-ish CPU work on the hub's thread pool; above this cap
# the panel shows sizes only (the pushguard scan-cap posture, pushguard.py).
MAX_TEXT_BYTES = 4 * 1024 * 1024


@dataclass
class CellEntry:
    """One cell's fate. ``index_base``/``index_local`` are 0-based document
    positions (None on the side the cell does not exist on); ``diff`` is a
    unified diff of the cell's source ("" for an unchanged cell)."""

    status: str  # "unchanged" | "changed" | "added" | "removed" | "unmatched"
    index_base: int | None = None
    index_local: int | None = None
    diff: str = ""


@dataclass
class DiffResult:
    """What the review panel renders: per-cell entries (kind="cells"), a
    whole-file unified diff (kind="lines"), or sizes only (kind="binary")."""

    kind: str  # "cells" | "lines" | "binary"
    cells: list[CellEntry] = field(default_factory=list)
    line_diff: str = ""
    note: str = ""


def diff(
    base: bytes | None,
    local: bytes | None,
    path: str,
    *,
    max_bytes: int = MAX_TEXT_BYTES,
) -> DiffResult:
    """Diff ``base`` (the last-synced blob; None = new local file) against
    ``local`` (the bytes a push would upload; None = deleted locally).

    Raises ``ValueError`` when both sides are None — there is nothing to show.
    """
    if base is None and local is None:
        raise ValueError("nothing to compare: no base and no local content")
    if max(len(base or b""), len(local or b"")) > max_bytes:
        return DiffResult(kind="binary", note=_sizes_note(base, local))
    if path.endswith(".py"):  # the push view: .py is LF-normalized (gitsha)
        base = base.replace(b"\r\n", b"\n") if base is not None else None
        local = local.replace(b"\r\n", b"\n") if local is not None else None
    try:
        base_text = base.decode("utf-8") if base is not None else None
        local_text = local.decode("utf-8") if local is not None else None
    except UnicodeDecodeError:
        return DiffResult(kind="binary", note=_sizes_note(base, local))
    # The cell view only for real marimo notebooks (the canonical sniff — see
    # notebook_template.is_marimo_app): marimo's converter happily WRAPS a plain
    # helper module into a one-cell notebook, which would render a misleading
    # "Cell 1 — changed" for what is honestly a whole-file line diff. Every
    # side that exists must carry the marker.
    if path.endswith(".py") and all(
        notebook_template.is_marimo_app(t) for t in (base_text, local_text) if t is not None
    ):
        try:
            return _cell_result(base_text, local_text)
        except (ValueError, marimo_rt.MarimoTooOld, marimo_rt.MarimoTransportError):
            # Looked like a notebook but did not parse — honest beats clever.
            return _line_result(
                base_text,
                local_text,
                path,
                note="not readable as marimo cells — showing a whole-file line diff",
            )
    return _line_result(base_text, local_text, path)


def _human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _sizes_note(base: bytes | None, local: bytes | None) -> str:
    if base is None:
        return f"new file, {_human_size(len(local or b''))} — contents not shown"
    if local is None:
        return f"deleted locally, was {_human_size(len(base))} — contents not shown"
    return f"changed, {_human_size(len(base))} → {_human_size(len(local))} — contents not shown"


def _unified(old: str, new: str, fromfile: str, tofile: str) -> str:
    """A unified diff in exactly the shape the version-history endpoint emits
    (splitlines + lineterm="" + newline-joined), so one renderer serves both."""
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile=fromfile, tofile=tofile, lineterm=""
        )
    )


def _line_result(
    base_text: str | None, local_text: str | None, path: str, note: str = ""
) -> DiffResult:
    if not note:
        if base_text is None:
            note = "new local file — every line is added"
        elif local_text is None:
            note = "deleted locally — a push removes every line"
    return DiffResult(
        kind="lines",
        line_diff=_unified(
            base_text or "", local_text or "", f"{path} (last synced)", f"{path} (local)"
        ),
        note=note,
    )


def _match_cells(
    base_codes: list[str], local_codes: list[str]
) -> tuple[dict[int, int], set[int]]:
    """Pair local cells with base cells: (local index -> base index, used base
    indices). Exact source at the same position first (stability when the same
    code appears twice), then exact source anywhere (a moved cell keeps its
    identity), then the best remaining counterpart at >= the similarity
    threshold. Whatever stays unpaired is the caller's unmatched/added/removed."""
    pair: dict[int, int] = {}
    used: set[int] = set()
    for j, code in enumerate(local_codes):  # pass 1: same position, same source
        if j < len(base_codes) and base_codes[j] == code:
            pair[j] = j
            used.add(j)
    by_code: dict[str, deque[int]] = defaultdict(deque)
    for i, code in enumerate(base_codes):
        if i not in used:
            by_code[code].append(i)
    for j, code in enumerate(local_codes):  # pass 2: exact source anywhere
        if j in pair:
            continue
        bucket = by_code.get(code)
        if bucket:
            i = bucket.popleft()
            pair[j] = i
            used.add(i)
    for j, code in enumerate(local_codes):  # pass 3: similarity
        if j in pair:
            continue
        best_i, best_ratio = None, 0.0
        for i, base_code in enumerate(base_codes):
            if i in used:
                continue
            ratio = difflib.SequenceMatcher(None, base_code, code).ratio()
            if ratio > best_ratio:
                best_i, best_ratio = i, ratio
        if best_i is not None and best_ratio >= SIMILARITY_THRESHOLD:
            pair[j] = best_i
            used.add(best_i)
    return pair, used


def _cell_result(base_text: str | None, local_text: str | None) -> DiffResult:
    """The per-cell view. Raises like marimo_rt.read_cells_checked on a side
    marimo cannot fully parse (the caller degrades to the whole-file line diff
    rather than rendering cells that silently dropped content)."""
    base_codes = (
        [code for _, code in marimo_rt.read_cells_checked(base_text)]
        if base_text is not None
        else []
    )
    local_codes = (
        [code for _, code in marimo_rt.read_cells_checked(local_text)]
        if local_text is not None
        else []
    )
    pair, used = _match_cells(base_codes, local_codes)
    leftover_base = [i for i in range(len(base_codes)) if i not in used]
    leftover_local = [j for j in range(len(local_codes)) if j not in pair]
    # Leftovers on BOTH sides are ambiguous — a rewrite? an add plus a remove?
    # Never guess: the explicit unmatched bucket renders them removed-then-added.
    ambiguous = bool(leftover_base) and bool(leftover_local)

    entries: list[CellEntry] = []
    for j, code in enumerate(local_codes):
        if j in pair:
            i = pair[j]
            if base_codes[i] == code:
                entries.append(CellEntry("unchanged", i, j))
            else:
                d = _unified(
                    base_codes[i], code, f"cell {i + 1} (last synced)", f"cell {j + 1} (local)"
                )
                entries.append(CellEntry("changed", i, j, d))
        else:
            status = "unmatched" if ambiguous else "added"
            d = _unified("", code, "(no cell)", f"cell {j + 1} (local)")
            entries.append(CellEntry(status, None, j, d))
    for i in leftover_base:
        status = "unmatched" if ambiguous else "removed"
        d = _unified(base_codes[i], "", f"cell {i + 1} (last synced)", "(no cell)")
        entries.append(CellEntry(status, i, None, d))
    # Approximate document order; a removed cell renders before an added one at
    # the same position (the classic removed-then-added diff reading order).
    entries.sort(
        key=lambda e: (
            e.index_local if e.index_local is not None else e.index_base,
            0 if e.index_local is None else 1,
        )
    )

    note = ""
    if base_text is None:
        note = "new local notebook — every cell is new"
    elif local_text is None:
        note = "deleted locally — a push removes every cell"
    elif ambiguous:
        note = "some cells could not be matched confidently — shown as removed + added"
    return DiffResult(kind="cells", cells=entries, note=note)
