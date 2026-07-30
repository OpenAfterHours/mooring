"""mooring_deliver — send a notebook's result tables to a stakeholder ``.xlsx``.

mooring INJECTS this module into ``<workspace>/.mooring/pylib/mooring_deliver.py``
and puts that directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can::

    import mooring_deliver as md
    md.reset()                          # start fresh each run
    md.table(summary, "Summary")        # one sheet per call
    md.table(by_region, "By region")

and the **Deliver as Excel** action (or ``mooring deliver <path> --excel``) turns
that into one workbook in the sync-excluded ``.mooring/outbox/``. HTML delivery
suits a manager who reads a chart; a finance stakeholder wants the numbers in
Excel, and this is that last mile.

Unlike its sibling runtimes (``mooring_checks`` / ``mooring_inputs``), what this one
writes is deliberately NOT value-free — a workbook of real numbers is the entire
point. That is exactly why it may only ever write under ``.mooring``, which
:func:`mooring.sync.is_synced_path` excludes on both scan sides: the data cannot
ride a push, and nothing here reaches the AI copilot. The one path mooring supplies
from outside (the target, via ``MOORING_DELIVER_XLSX``) is re-checked against that
directory here, so even a rogue environment cannot redirect the values somewhere
syncable. The value-free RECEIPT this leaves behind is what mooring reads back.

**mooring ships no Excel writer.** A frozen ``.pyz`` has no pip at runtime and the
base install is nine dependencies; the workbook is therefore written by the
NOTEBOOK's own environment (the repo's ``pyproject.toml``/``uv.lock``), using
whichever engine is already there — detected at call time. Both ``xlsxwriter`` and
``openpyxl`` are checked, which also covers analysts who reach Excel through polars'
``write_excel`` (xlsxwriter) or pandas' ``to_excel`` (either): the engine is what
actually has to exist. With neither, ``table()`` prints an actionable message and
returns falsy — it never raises, because losing an artifact is a much smaller harm
than breaking the run that produced the numbers.

Standalone by design: it imports only the standard library plus the Excel engine it
finds, and duck-types the table you pass (polars OR pandas OR plain Python), so it
works in the team's locked uv env and in the frozen bundle where mooring itself is
not importable. Do not import mooring here.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import tempfile
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path

_STATE_DIR = ".mooring"
_WORKBOOKS_DIRNAME = "workbooks"
_OUTBOX_DIRNAME = "outbox"

# What mooring's Deliver-as-Excel run tells the kernel. All optional: an INTERACTIVE
# run (the analyst just editing in marimo) sets none of them, and then the workbook
# goes to the default outbox path and claims no repo or commit at all — see
# _provenance_rows, which is where the "never claim provenance we can't stand
# behind" rule lands.
_ENV_TARGET = "MOORING_DELIVER_XLSX"
_ENV_ORIGIN = "MOORING_DELIVER_ORIGIN"
_ENV_LINK = "MOORING_DELIVER_LINK"
_ENV_NOTEBOOK = "MOORING_DELIVER_NOTEBOOK"
_ENV_DAY = "MOORING_DELIVER_DAY"

# Excel's own limits, not ours: 31 characters, and these characters are illegal in a
# sheet name. A workbook that breaks either is rejected by Excel on open, so the
# sanitiser below is load-bearing rather than cosmetic.
_MAX_SHEET_NAME = 31
_BAD_SHEET_CHARS = set("[]:*?/\\")
_PROVENANCE_SHEET = "Provenance"

# The one message an analyst with no Excel engine sees. It names the exact command,
# because "pip install openpyxl" is not actionable inside a synced repo — the package
# has to land in the REPO's pyproject.toml for the whole team, which is what
# `mooring deps add` does.
NO_WRITER_HINT = (
    "no Excel writer in this notebook's environment. Add one to the repo with "
    "`mooring deps add openpyxl` (or xlsxwriter) and run Deliver as Excel again. "
    "mooring ships no Excel writer of its own — the workbook is written by the "
    "notebook's environment."
)

# Registered sheets for this kernel, in insertion order: label -> (columns, rows).
# A dict (not a list) so re-running one cell REPLACES its sheet instead of adding a
# second copy — marimo re-executes cells freely, and a duplicated sheet would be a
# silently wrong deliverable.
_SHEETS: dict[str, tuple[list[str], list[list]]] = {}

# What actually reached disk, so a later failure reports the truth ("two sheets are
# in the workbook, the third could not be written") instead of erasing the record of
# the sheets that succeeded.
_DELIVERED: dict[str, object] = {"workbook": "", "sheets": []}


class Result:
    """The outcome of one :func:`table` call. Truthy when the sheet reached the
    workbook on disk; ``repr`` is the one-line summary printed into the cell output."""

    __slots__ = ("name", "rows", "cols", "path", "written", "note")

    def __init__(
        self,
        name: str,
        rows: int = 0,
        cols: int = 0,
        path: str = "",
        written: bool = False,
        note: str = "",
    ) -> None:
        self.name = name
        self.rows = rows
        self.cols = cols
        self.path = path
        self.written = bool(written)
        self.note = note

    def __bool__(self) -> bool:
        return self.written

    def __repr__(self) -> str:
        if not self.written:
            return f"[NOT DELIVERED] sheet {self.name} — {self.note or 'not written'}"
        where = os.path.basename(self.path) if self.path else "the outbox"
        return f"[SHEET] {self.name} — {self.rows}x{self.cols} -> {where}"


class _WriteError(Exception):
    """Internal: the workbook could not be written. ``reason`` is the mooring-authored
    classification safe to RECORD; ``str(exc)`` may carry engine detail that is only
    ever printed into the analyst's own cell (an engine message can quote a value —
    the same rule that keeps marimo's stderr out of a verify receipt)."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason or message


# -- the public API -------------------------------------------------------------


def table(data=None, name: str | None = None) -> Result:
    """Add ``data`` to the stakeholder workbook as one sheet called ``name``.

    ``data`` is a polars DataFrame/LazyFrame, a pandas DataFrame, a list of dicts, or
    a ``{column: values}`` mapping. Calling ``table`` twice with the same ``name``
    REPLACES that sheet, so re-running a cell is idempotent.

    The workbook is rewritten in full on every call, so it is complete and openable
    the moment the last table is registered — there is no ``save()`` to forget, and a
    cell that fails later cannot cost you the sheets that already succeeded.

    Never raises: any failure (no Excel engine, an unreadable table, a locked file)
    prints one line and returns a falsy :class:`Result`. Breaking the run that
    computed the numbers would be a far worse outcome than losing the artifact."""
    label = str(name).strip() if name else f"Sheet {len(_SHEETS) + 1}"
    try:
        columns, rows = _extract(data)
    except Exception as exc:  # noqa: BLE001  # a bad table must not sink the run
        return _report(Result(label, note=str(exc)), "could not read the table")
    _SHEETS[label] = (columns, rows)
    result = Result(label, len(rows), len(columns))
    try:
        result.path = str(_flush())
    except _WriteError as exc:
        result.note = str(exc)
        return _report(result, exc.reason)
    result.written = True
    print(repr(result))
    return result


def reset() -> None:
    """Clear the registered sheets and remove this notebook's workbook — call at the
    top of the run so a renamed or dropped table cannot linger into today's file.

    Removing the file matters more than clearing the dict: a stale workbook from
    yesterday sitting in the outbox looks exactly like a fresh one to whoever emails
    it, and that is the kind of mistake this product exists to prevent."""
    _SHEETS.clear()
    _DELIVERED["workbook"] = ""
    _DELIVERED["sheets"] = []
    try:
        _target().unlink()
    except OSError:
        pass
    _write_receipt("")


# -- table introspection (duck-typed: polars OR pandas OR plain Python) ----------


def _extract(data) -> tuple[list[str], list[list]]:
    """``(columns, rows)`` from whatever the analyst passed.

    Duck-typed in the order that resolves each library unambiguously, and never
    importing polars/pandas — the kernel may have one, both, or neither."""
    if data is None:
        raise ValueError("nothing to write (pass a dataframe, a list of dicts, or a mapping)")
    # A polars LazyFrame: materialise once, then fall through to the DataFrame branch.
    if hasattr(data, "collect") and hasattr(data, "collect_schema"):
        data = data.collect()
    columns = getattr(data, "columns", None)
    if columns is not None and hasattr(data, "rows"):  # polars DataFrame
        return [str(c) for c in columns], [list(r) for r in data.rows()]
    if columns is not None and hasattr(data, "itertuples"):  # pandas DataFrame
        return [str(c) for c in columns], [list(r) for r in data.itertuples(index=False, name=None)]
    if hasattr(data, "keys") and hasattr(data, "values"):  # {column: values}
        names = [str(key) for key in data.keys()]
        cols = [list(values) for values in data.values()]
        height = max((len(col) for col in cols), default=0)
        return names, [[col[i] if i < len(col) else None for col in cols] for i in range(height)]
    try:
        items = list(data)
    except TypeError:
        items = []
    if items and all(hasattr(item, "keys") for item in items):  # a list of dicts
        names = list(dict.fromkeys(str(key) for item in items for key in item.keys()))
        return names, [[item.get(n) for n in names] for item in items]
    raise TypeError(
        "unsupported table: pass a polars/pandas dataframe, a list of dicts, "
        "or a {column: values} mapping"
    )


def _cell(value):
    """Coerce one value into something an Excel engine accepts.

    Two of these conversions are not merely defensive. ``Decimal`` — what a finance
    notebook reads out of a warehouse — is rejected by both engines, so it becomes a
    float; lossy past 15 significant digits, but a text cell in a currency column
    would be worse. And Excel has no concept of a timezone: openpyxl REFUSES an aware
    datetime outright, so the wall-clock time is kept and the offset dropped, which is
    what the reader sees on screen anyway."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        try:
            return float(value)
        except (ValueError, ArithmeticError):
            return str(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, (date, time)):
        return value
    return str(value)


# -- sheet names ----------------------------------------------------------------


def _sheet_names(labels) -> list[str]:
    """Map the analyst's labels to legal, unique Excel sheet names, in order.

    ``_PROVENANCE_SHEET`` is reserved BEFORE the labels are mapped: mooring's
    provenance record has to be findable under a predictable name, so a data sheet
    the analyst happened to call "Provenance" is the one that gets suffixed. Names
    that collide only after truncation are suffixed too — collapsing two sheets into
    one would silently drop a table."""
    taken = {_PROVENANCE_SHEET.lower()}
    out = []
    for label in labels:
        base = "".join(" " if ch in _BAD_SHEET_CHARS else ch for ch in label).strip()
        base = base.strip("'")[:_MAX_SHEET_NAME].strip() or "Sheet"
        name, n = base, 1
        while name.lower() in taken:
            n += 1
            suffix = f" ({n})"
            name = base[: _MAX_SHEET_NAME - len(suffix)].strip() + suffix
        taken.add(name.lower())
        out.append(name)
    return out


# -- writing the workbook -------------------------------------------------------


def _engine() -> str:
    """The name of the Excel engine available in THIS kernel, or "".

    Probed at call time rather than at import, so an analyst who runs ``mooring deps
    add openpyxl`` mid-session need not restart anything, and so importing
    ``mooring_deliver`` never fails in an environment without a writer."""
    for name in ("xlsxwriter", "openpyxl"):
        try:
            importlib.import_module(name)
        except ImportError:
            continue
        return name
    return ""


def _flush() -> Path:
    """Write every registered sheet plus the provenance record, and return the path.

    Written to a sibling temp file and moved into place, so a reader who opens the
    outbox mid-run never finds a half-written workbook (Excel would call it corrupt)
    and a crashed write leaves the previous file untouched."""
    engine = _engine()
    if not engine:
        raise _WriteError(NO_WRITER_HINT)
    labels = list(_SHEETS)
    names = _sheet_names(labels)
    sheets = [(names[i], *_SHEETS[label]) for i, label in enumerate(labels)]
    sheets.append((_PROVENANCE_SHEET, *_provenance_rows(labels)))

    path = _target()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
        )
        os.close(handle)  # the engines open the path themselves
    except OSError as exc:
        raise _WriteError(f"could not create the workbook: {exc}", "could not create it") from exc
    try:
        if engine == "xlsxwriter":
            _write_xlsxwriter(tmp, sheets)
        else:
            _write_openpyxl(tmp, sheets)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001  # an engine error must not sink the run
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise _WriteError(f"could not write the workbook: {exc}", "could not write it") from exc
    _DELIVERED["workbook"] = _relative(path)
    _DELIVERED["sheets"] = names
    _write_receipt("")
    return path


def _write_xlsxwriter(path: str, sheets) -> None:
    import xlsxwriter

    book = xlsxwriter.Workbook(path, {"default_date_format": "yyyy-mm-dd"})
    try:
        header = book.add_format({"bold": True})
        for name, columns, rows in sheets:
            sheet = book.add_worksheet(name)
            for col, title in enumerate(columns):
                sheet.write(0, col, str(title), header)
            for index, row in enumerate(rows, start=1):
                for col, value in enumerate(row):
                    sheet.write(index, col, _cell(value))
            sheet.freeze_panes(1, 0)  # the header stays put while a reader scrolls
    finally:
        book.close()


def _write_openpyxl(path: str, sheets) -> None:
    import openpyxl
    from openpyxl.styles import Font

    book = openpyxl.Workbook()
    book.remove(book.active)  # drop the default empty sheet
    bold = Font(bold=True)
    for name, columns, rows in sheets:
        sheet = book.create_sheet(title=name)
        sheet.append([str(title) for title in columns])
        for cell in sheet[1]:
            cell.font = bold
        for row in rows:
            sheet.append([_cell(value) for value in row])
        sheet.freeze_panes = "A2"  # the header stays put while a reader scrolls
    book.save(path)


# -- provenance -----------------------------------------------------------------


def _provenance_rows(labels) -> tuple[list[str], list[list]]:
    """The provenance sheet: where this workbook came from, mirroring the footer
    :func:`mooring.app.deliver.stamp_provenance` stamps into the HTML.

    mooring computes the origin and the GitHub link (it knows the manifest; the kernel
    does not) and passes them in. When they are absent — an interactive run, or a
    notebook that has never been pushed, for which mooring deliberately sends no
    origin — those rows are simply omitted. Claiming a repo or a commit we cannot
    stand behind would make the record worse than useless on the one occasion a reader
    actually checks it."""
    origin = os.environ.get(_ENV_ORIGIN, "").strip()
    link = os.environ.get(_ENV_LINK, "").strip()
    day = os.environ.get(_ENV_DAY, "").strip() or f"{datetime.now():%Y-%m-%d}"
    rows = [["Generated by", "mooring"]]
    if origin:
        rows.append(["Source", origin])
    rows.append(["Notebook", _notebook_rel()])
    rows.append(["Date", day])
    if link:
        rows.append(["View on GitHub", link])
    rows.append(["Sheets", ", ".join(labels)])
    return ["Field", "Value"], rows


# -- where things land ----------------------------------------------------------


def _workspace() -> Path | None:
    # <ws>/.mooring/pylib/mooring_deliver.py -> parents[2] == <ws>
    try:
        return Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        return None


def _notebook_rel() -> str:
    """The workspace-relative notebook this call belongs to. mooring's own run states
    it outright; otherwise it is detected from the call stack."""
    given = os.environ.get(_ENV_NOTEBOOK, "").strip().replace("\\", "/")
    if given:
        return given
    ws = _workspace()
    return (_detect_notebook(ws) if ws is not None else None) or "notebook"


def _detect_notebook(ws: Path) -> str | None:
    """The workspace-relative path of the NOTEBOOK that triggered this call.

    marimo sets ``__file__`` in each cell's namespace to the real notebook ``.py``. We
    take the OUTERMOST workspace ``.py`` frame — the notebook cell is the outer frame,
    a helper module it calls is inner — so a table written from a helper is still
    attributed to the notebook that drove it. Best-effort; None outside marimo.
    (Mirrors the same walk in ``mooring_inputs``.)"""
    found: str | None = None
    try:
        for frame_info in inspect.stack(0)[1:]:  # context=0: don't read source lines
            filename = frame_info.frame.f_globals.get("__file__")
            if not isinstance(filename, str) or not filename.endswith(".py"):
                continue
            try:
                rel = Path(filename).resolve().relative_to(ws)
            except (ValueError, OSError):
                continue
            if _STATE_DIR in rel.parts:
                continue  # our own module lives at .mooring/pylib/mooring_deliver.py
            found = str(rel).replace(os.sep, "/")  # keep walking: prefer the OUTERMOST
    except Exception:  # noqa: BLE001  # detection is best-effort; never break a run
        pass
    return found


def _target() -> Path:
    """The workbook's absolute path.

    mooring names it via ``MOORING_DELIVER_XLSX`` so both sides agree on one file
    without re-deriving a date across midnight. That path is CONTAINMENT-CHECKED
    against ``<ws>/.mooring`` before it is honoured: this file carries real data
    values and the sync exclusion is the only thing stopping them riding a push, so an
    environment variable must not be able to move them outside it. Anything else falls
    back to the same dated outbox name the HTML delivery uses."""
    ws = _workspace() or Path.cwd()
    given = os.environ.get(_ENV_TARGET, "").strip()
    if given:
        try:
            candidate = Path(given).resolve()
            if candidate.is_relative_to((ws / _STATE_DIR).resolve()):
                return candidate
        except OSError:
            pass
    rel = _notebook_rel()
    stem = rel.rsplit("/", 1)[-1]
    stem = stem[:-3] if stem.endswith(".py") else stem
    folder = (rel[:-3] if rel.endswith(".py") else rel).replace("/", "__")
    return ws / _STATE_DIR / _OUTBOX_DIRNAME / folder / f"{stem}-{datetime.now():%Y%m%d}.xlsx"


def _relative(path: Path) -> str:
    ws = _workspace()
    if ws is None:
        return str(path)
    try:
        return str(path.relative_to(ws)).replace(os.sep, "/")
    except ValueError:
        return str(path)


# -- the receipt mooring reads back ---------------------------------------------


def _slug(rel: str) -> str:
    """An INJECTIVE per-notebook receipt filename: escape ``_`` first so the ``__``
    that encodes ``/`` is unambiguous (``a/b`` and ``a__b`` map to different files)."""
    return rel.replace("_", "_u").replace("/", "__")


def _write_receipt(reason: str) -> None:
    """Record what this run delivered, so mooring can report it without opening the
    workbook (it has no Excel reader either — see :mod:`mooring.workbook`).

    Value-free: the workbook's path, the sheet NAMES the analyst chose, and a
    mooring-authored reason — never an engine exception string. Best-effort; never
    raises, since a lost receipt costs a nicer message, not the artifact."""
    ws = _workspace()
    if ws is None:
        return
    rel = _notebook_rel()
    path = ws / _STATE_DIR / _WORKBOOKS_DIRNAME / (_slug(rel) + ".json")
    data = {
        "notebook": rel,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workbook": _DELIVERED["workbook"],
        "sheets": list(_DELIVERED["sheets"]),  # type: ignore[arg-type]
        "reason": reason,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
        )
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, path)
    except OSError:
        pass


def _report(result: Result, reason: str) -> Result:
    """Print the one-line outcome and record the value-free reason it failed."""
    _write_receipt(reason)
    print(repr(result))
    return result
