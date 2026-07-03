"""mooring_checks — value-free tie-out / data-quality checks for a notebook cell.

mooring INJECTS this module into ``<workspace>/.mooring/pylib/mooring_checks.py``
and puts that directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can::

    import mooring_checks as mc
    mc.reset()
    mc.reconciles(segment_total, control_total, tol=0.01)
    mc.unique_key(loans, "loan_id")
    mc.no_fanout(loans, rates, on="rate_id")

and assert that its numbers hang together — segment totals reconcile to a control,
a key is unique, a join won't fan out, row counts didn't move unexpectedly.

Every check runs LOCALLY in the kernel (it can see your data — that is your own
machine). What it RECORDS is value-free: a per-notebook receipt under
``<workspace>/.mooring/checks/`` holding only ``{name, kind, passed, note, ts}`` —
counts and booleans, never a cell value — which the mooring hub reads to show a
green/red badge on the notebook's row. The receipt lives in the sync-excluded
``.mooring`` directory and is NEVER sent to the AI copilot.

Standalone by design: it imports only the standard library and duck-types the
dataframe you pass (polars OR pandas), so it works in the team's locked uv env and
in the frozen bundle where mooring itself is not importable. Do not import mooring
here.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_STATE_DIR = ".mooring"
_CHECKS_DIRNAME = "checks"


class Result:
    """The outcome of one check. Truthy when it passed, so checks can be chained or
    asserted; ``repr`` is the one-line summary printed into the cell output."""

    __slots__ = ("name", "kind", "passed", "note")

    def __init__(self, name: str, kind: str, passed: bool, note: str = "") -> None:
        self.name = name
        self.kind = kind
        self.passed = bool(passed)
        self.note = note

    def __bool__(self) -> bool:
        return self.passed

    def __repr__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        extra = f" — {self.note}" if self.note else ""
        return f"[{mark}] {self.name}{extra}"


# -- dataframe helpers (duck-typed: polars OR pandas, never imported) -----------


def _height(df) -> int:
    """Row count of a polars or pandas dataframe (both support ``len``)."""
    try:
        return int(len(df))
    except TypeError:
        shape = getattr(df, "shape", None)
        return int(shape[0]) if shape else 0


def _unique_rows(df, cols) -> int:
    """Distinct-row count over ``cols`` (all columns when empty)."""
    cols = list(cols)
    if hasattr(df, "unique") and hasattr(df, "height"):  # polars DataFrame
        sub = df.select(cols) if cols else df
        return int(sub.unique().height)
    if hasattr(df, "drop_duplicates"):  # pandas DataFrame
        sub = df[cols] if cols else df
        return int(sub.drop_duplicates().shape[0])
    # Generic fallback: count distinct hashable rows.
    seen = {tuple(row) if isinstance(row, (list, tuple)) else row for row in df}
    return len(seen)


def _null_count(df, col) -> int:
    series = df[col]
    if hasattr(series, "null_count"):  # polars Series
        return int(series.null_count())
    if hasattr(series, "isna"):  # pandas Series
        return int(series.isna().sum())
    return sum(1 for value in series if value is None)


# -- the checks -----------------------------------------------------------------


def reconciles(actual, expected, *, tol=0.0, name: str = "reconciles") -> Result:
    """Two totals agree within ``tol`` (``|actual - expected| <= tol``)."""
    diff = abs(float(actual) - float(expected))
    passed = diff <= float(tol)
    # Value-free note ONLY: the receipt must never carry a data magnitude (the raw
    # difference of two confidential totals is a data value). The analyst sees the
    # actual figures in their own notebook cells.
    note = "within tolerance" if passed else "outside tolerance"
    return _record(Result(name, "reconciles", passed, note))


def unique_key(df, *columns, name: str | None = None) -> Result:
    """No duplicate keys: the rows are unique over ``columns`` (or whole rows)."""
    cols = list(columns)
    name = name or (f"unique_key({', '.join(cols)})" if cols else "unique_rows")
    dupes = _height(df) - _unique_rows(df, cols)
    passed = dupes == 0
    note = "no duplicates" if passed else f"{dupes} duplicate row(s)"
    return _record(Result(name, "unique_key", passed, note))


def no_fanout(left, right, *, on, name: str | None = None) -> Result:
    """A left-join ``left`` -> ``right`` on ``on`` won't multiply rows — i.e. the
    ``right`` (lookup) side has unique join keys. ``left`` is accepted for a
    readable call site and to future-proof the signature; only ``right`` is
    inspected."""
    del left  # only the lookup side's key cardinality determines fan-out
    keys = [on] if isinstance(on, str) else list(on)
    name = name or f"no_fanout({', '.join(keys)})"
    dupes = _height(right) - _unique_rows(right, keys)
    passed = dupes == 0
    note = "lookup keys unique" if passed else f"lookup has {dupes} duplicate key(s) — may fan out"
    return _record(Result(name, "no_fanout", passed, note))


def row_delta(df, prior, *, tol=0, name: str = "row_delta") -> Result:
    """Row count moved within ``tol`` since ``prior`` (an int row count, or a df)."""
    now = _height(df)
    before = prior if isinstance(prior, int) else _height(prior)
    delta = now - before
    passed = abs(delta) <= int(tol)
    note = "unchanged" if delta == 0 else f"{'+' if delta > 0 else ''}{delta} row(s)"
    return _record(Result(name, "row_delta", passed, note))


def not_null(df, *columns, name: str | None = None) -> Result:
    """No nulls in ``columns``."""
    cols = list(columns)
    name = name or f"not_null({', '.join(cols)})"
    total = sum(_null_count(df, col) for col in cols)
    passed = total == 0
    note = "no nulls" if passed else f"{total} null(s)"
    return _record(Result(name, "not_null", passed, note))


def expect(condition, *, name: str, note: str = "") -> Result:
    """A custom boolean the analyst has already computed (only the bool is recorded)."""
    return _record(Result(name, "expect", bool(condition), note))


def reset(name: str | None = None) -> None:
    """Clear this notebook's recorded results — call at the top of your checks cell
    so a renamed or removed check does not linger as a stale badge. With ``name``,
    clear only that one check."""
    path = _receipt_path()
    if path is None:
        return
    if name is None:
        try:
            path.unlink()
        except OSError:
            pass
        return
    try:
        data = json.loads(path.read_text("utf-8"))
        checks = data.get("checks")
        if isinstance(checks, dict) and name in checks:
            del checks[name]
            _atomic_write(path, json.dumps(data, ensure_ascii=False))
    except (OSError, ValueError):
        pass


# -- value-free receipt (local only; never sent to the AI) ----------------------


def _record(result: Result) -> Result:
    print(repr(result))
    _write_receipt(result)
    return result


def _workspace() -> Path | None:
    # <ws>/.mooring/pylib/mooring_checks.py -> parents[2] == <ws>
    try:
        return Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        return None


def _detect_notebook(ws: Path) -> str | None:
    """The workspace-relative path of the notebook that called us.

    marimo executes each cell with a temporary compiled-cell filename (so the
    caller's ``frame.filename`` is NOT the notebook), but it sets ``__file__`` in the
    cell's globals to the real notebook ``.py``. So we walk the caller frames and read
    ``f_globals["__file__"]``, taking the first that resolves under the workspace and
    is not our own module (which lives under ``.mooring``). Best-effort — returns
    ``None`` if it cannot be resolved (e.g. run outside marimo)."""
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
                continue  # our own module lives at .mooring/pylib/mooring_checks.py
            return str(rel).replace(os.sep, "/")
    except Exception:  # noqa: BLE001  # detection is best-effort; never break a check
        pass
    return None


def _slug(rel: str) -> str:
    """A per-notebook receipt filename that is INJECTIVE — distinct notebook paths
    never collide. Escape ``_`` first so the ``__`` that encodes ``/`` is
    unambiguous (``a/b`` and ``a__b`` map to different files). Common paths with no
    underscores are unchanged (``notebooks/x`` -> ``notebooks__x``), so it stays
    readable."""
    return rel.replace("_", "_u").replace("/", "__")


def _receipt_path() -> Path | None:
    ws = _workspace()
    if ws is None:
        return None
    rel = _detect_notebook(ws) or "_notebook"
    return ws / _STATE_DIR / _CHECKS_DIRNAME / (_slug(rel) + ".json")


def _write_receipt(result: Result) -> None:
    ws = _workspace()
    if ws is None:
        return
    rel = _detect_notebook(ws) or "_notebook"
    path = ws / _STATE_DIR / _CHECKS_DIRNAME / (_slug(rel) + ".json")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data: dict = {"notebook": rel, "updated": now, "checks": {}}
    try:
        if path.is_file():
            existing = json.loads(path.read_text("utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("checks"), dict):
                data = existing
    except (OSError, ValueError):
        pass
    data["notebook"] = rel
    data["updated"] = now
    data.setdefault("checks", {})[result.name] = {
        "kind": result.kind,
        "passed": result.passed,
        "note": result.note,
        "ts": now,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(data, ensure_ascii=False))
    except OSError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
