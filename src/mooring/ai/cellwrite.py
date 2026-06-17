"""Apply a proposed cell by writing it into the notebook's `.py` source.

This is how the copilot's "Apply" lands a cell in the analyst's open notebook:
mooring appends the cell to the `.py` file via marimo's own codegen, and the
editor (launched with ``--watch``) reloads it so the cell appears (and, with
``watcher_on_save = "autorun"``, runs).

Why not the HTTP control API: ``POST /api/kernel/run`` executes code in the
kernel but never adds a cell to the *frontend document*, so the cell never
becomes visible; ``POST /api/document/transaction`` is broadcast to every
consumer EXCEPT the originating tab. The file-watch reload is broadcast to all
tabs, so it's the one channel that makes a cell appear. (Verified:
scripts/spike_marimo_filewatch.py.)

Privacy: this only ever writes value-free *source code*. mooring still never
reads cell outputs or opens a marimo websocket.
"""

from __future__ import annotations

from pathlib import Path

from mooring import marimo_rt


class CellWriteError(Exception):
    """The notebook source could not be read/parsed/written."""


def append_cell(notebook_path: str | Path, code: str) -> None:
    """Append a new cell containing ``code`` to the marimo notebook at ``notebook_path``.

    The marimo codegen (so the cell's ``def`` signature and ``return`` are derived
    correctly) lives in :mod:`mooring.marimo_rt`; this only owns the FILE concern —
    read the source, hand it to the seam, write plain UTF-8 (no BOM — the marimo
    parser rejects a BOM). Raises :class:`CellWriteError` on any failure (including
    a too-old marimo, surfaced by the seam's loud floor check).
    """
    path = Path(notebook_path)
    try:
        source = path.read_text("utf-8")
        result = marimo_rt.append_cell_source(source, code)
        path.write_text(result, encoding="utf-8")
    except (OSError, ValueError, SyntaxError, marimo_rt.MarimoTooOld, marimo_rt.MarimoTransportError) as exc:
        raise CellWriteError(f"could not apply the cell to {path.name}: {exc}") from exc
