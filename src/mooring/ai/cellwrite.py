"""Apply a proposed change by writing it into the notebook's `.py` source.

This is how the copilot's "Apply" lands a change in the analyst's open notebook:
mooring rewrites the cell(s) via marimo's own codegen, and the editor (launched
with ``--watch``) reloads it so the change appears (and, with
``watcher_on_save = "autorun"``, re-runs only the changed cells).

Why not the HTTP control API: ``POST /api/kernel/run`` executes code in the
kernel but never adds/edits a cell in the *frontend document*, so it never
becomes visible; ``POST /api/document/transaction`` is broadcast to every
consumer EXCEPT the originating tab. The file-watch reload is broadcast to all
tabs, so it's the one channel that makes a change appear. (Verified:
scripts/spike_marimo_filewatch.py.)

Append, edit, delete and whole-notebook rewrite all reduce to: read the `.py`,
transform it through the marimo IR (in :mod:`mooring.marimo_rt`), and write the
result atomically. The hub snapshots the pre-edit bytes first (see
:mod:`mooring.notebook_undo`) so any Apply can be rolled back.

Privacy: this only ever writes value-free *source code*. mooring still never
reads cell outputs or opens a marimo websocket.
"""

from __future__ import annotations

from pathlib import Path

from mooring import marimo_rt
from mooring.paths import safe_write_text


class CellWriteError(Exception):
    """The notebook source could not be read/parsed/written."""


class CellApplyConflict(CellWriteError):
    """A targeted edit/delete no longer matches the notebook (it changed since read).

    A :class:`CellWriteError` subclass (so existing callers still catch it) but a
    distinct type so the hub can answer 409 ("the cell changed") rather than 502.
    """


def apply_patch(notebook_path: str | Path, ops) -> None:
    """Apply a list of :class:`mooring.marimo_rt.CellOp` to the notebook at
    ``notebook_path``: read the source, transform it through the marimo IR seam,
    and write the result atomically as plain UTF-8 (no BOM — the marimo parser
    rejects one).

    Raises :class:`CellApplyConflict` if a targeted cell changed since it was read,
    or :class:`CellWriteError` on any other failure (unreadable/unparseable source,
    a too-old marimo, or a result that would not parse).
    """
    path = Path(notebook_path)
    try:
        source = path.read_text("utf-8")
        result = marimo_rt.apply_cell_patch(source, ops)
        safe_write_text(path, result)
    except marimo_rt.CellPatchConflict as exc:
        raise CellApplyConflict(str(exc)) from exc
    except (OSError, ValueError, SyntaxError, marimo_rt.MarimoTooOld, marimo_rt.MarimoTransportError) as exc:
        raise CellWriteError(f"could not apply the change to {path.name}: {exc}") from exc


def append_cell(notebook_path: str | Path, code: str) -> None:
    """Append a new cell containing ``code`` (a one-op :func:`apply_patch`)."""
    apply_patch(notebook_path, [marimo_rt.CellOp(op="append", code=code)])


def apply_wire_patch(notebook_path: str | Path, op_dicts) -> None:
    """:func:`apply_patch` for the JSON op-dicts the chat UI echoes back on Apply.

    The hub stays at this seam (it never imports marimo); the dicts are converted to
    :class:`mooring.marimo_rt.CellOp` here. An unknown op is a :class:`CellWriteError`.
    """
    apply_patch(notebook_path, _ops_from_wire(op_dicts))


def _ops_from_wire(op_dicts) -> list[marimo_rt.CellOp]:
    ops: list[marimo_rt.CellOp] = []
    for d in op_dicts or []:
        if not isinstance(d, dict):
            raise CellWriteError("a patch operation must be an object")
        kind = str(d.get("op", ""))
        if kind == "append":
            ops.append(marimo_rt.CellOp(op="append", code=str(d.get("code", ""))))
        elif kind in ("edit", "delete"):
            anchor = d.get("anchor")
            ops.append(
                marimo_rt.CellOp(
                    op=kind,
                    index=_as_int(d.get("index")),
                    anchor=str(anchor) if anchor is not None else None,
                    code=str(d.get("code", "")),
                )
            )
        elif kind == "replace_all":
            ops.append(
                marimo_rt.CellOp(
                    op="replace_all", cells=tuple(str(c) for c in (d.get("cells") or []))
                )
            )
        else:
            raise CellWriteError(f"unknown patch operation: {kind!r}")
    return ops


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
