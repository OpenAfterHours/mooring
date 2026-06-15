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


class CellWriteError(Exception):
    """The notebook source could not be read/parsed/written."""


def append_cell(notebook_path: str | Path, code: str) -> None:
    """Append a new cell containing ``code`` to the marimo notebook at ``notebook_path``.

    Uses marimo's codegen so the cell's ``def`` signature and ``return`` are
    derived correctly; writes plain UTF-8 (no BOM — the marimo parser rejects a
    BOM). Raises :class:`CellWriteError` on any failure.
    """
    path = Path(notebook_path)
    try:
        from marimo._ast import codegen
        from marimo._convert.converters import MarimoConvert
    except Exception as exc:  # noqa: BLE001 - marimo always present, but be explicit
        raise CellWriteError(f"marimo codegen unavailable: {exc}") from exc

    try:
        source = path.read_text("utf-8")
        ir = MarimoConvert.from_py(source).to_ir()
        ir.cells.append(_new_cell(ir, code))
        path.write_text(codegen.generate_filecontents_from_ir(ir), encoding="utf-8")
    except (OSError, ValueError, SyntaxError) as exc:
        raise CellWriteError(f"could not apply the cell to {path.name}: {exc}") from exc


def _new_cell(ir, code: str):
    """A fresh CellDef for ``code`` (reuse the notebook's cell class, or import it)."""
    if ir.cells:
        cell_cls = type(ir.cells[0])
    else:  # empty notebook — import the class directly
        from marimo._schemas.serialization import CellDef as cell_cls
    return cell_cls(code=code, name="_")
