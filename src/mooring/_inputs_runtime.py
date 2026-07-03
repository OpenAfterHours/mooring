"""mooring_inputs — value-free input-data fingerprints for a notebook cell.

mooring INJECTS this module into ``<workspace>/.mooring/pylib/mooring_inputs.py``
and puts that directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can::

    import mooring_inputs as mi
    sales = pl.read_csv("data/sales.csv")
    mi.fingerprint(sales, "sales", path="data/sales.csv")

to pin the exact inputs a run read. Each call records a VALUE-FREE fingerprint of the
input — its file content HASH, its SHAPE (row/column counts), and its SCHEMA (column
names + dtypes) — and compares it to the previous run's, so a changed input is flagged
(the reproducibility question "same inputs, same numbers?"). It answers the auditor
without ever storing a data value.

Everything recorded is value-free: a per-notebook receipt under
``<workspace>/.mooring/inputs/`` holding ``{path, sha, rows, cols, schema, changed}``
per input — a hash, two counts, and column names/types, never a cell value. The receipt
lives in the sync-excluded ``.mooring`` directory and is NEVER sent to the AI copilot.

Standalone by design: it imports only the standard library and duck-types the dataframe
you pass (polars OR pandas), so it works in the team's locked uv env and in the frozen
bundle where mooring itself is not importable. Do not import mooring here.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_STATE_DIR = ".mooring"
_INPUTS_DIRNAME = "inputs"
_HASH_CHUNK = 1 << 20  # 1 MiB


class Result:
    """The outcome of fingerprinting one input. Truthy when the input is UNCHANGED
    since the last run (so ``assert mi.fingerprint(df, "sales", path=...)`` reads as
    "assert this input hasn't moved"); a first sighting counts as unchanged. ``repr``
    is the one-line summary printed into the cell output."""

    __slots__ = ("name", "changed", "seen_before", "note")

    def __init__(self, name: str, changed: bool, seen_before: bool, note: str = "") -> None:
        self.name = name
        self.changed = bool(changed)
        self.seen_before = bool(seen_before)
        self.note = note

    def __bool__(self) -> bool:
        return not self.changed

    def __repr__(self) -> str:
        if not self.seen_before:
            mark = "NEW"
        elif self.changed:
            mark = "CHANGED"
        else:
            mark = "SAME"
        extra = f" — {self.note}" if self.note else ""
        return f"[{mark}] input {self.name}{extra}"


# -- dataframe introspection (duck-typed: polars OR pandas, never imported) ------


def _shape(df) -> tuple[int, int]:
    """(rows, cols) of a polars or pandas dataframe — counts only, never a value."""
    shape = getattr(df, "shape", None)
    if isinstance(shape, tuple) and len(shape) >= 2:
        try:
            return int(shape[0]), int(shape[1])
        except (TypeError, ValueError):
            pass
    try:
        rows = int(len(df))
    except TypeError:
        rows = 0
    try:
        cols = len(list(df.columns))
    except Exception:  # noqa: BLE001  # best-effort duck-typing
        cols = 0
    return rows, cols


def _schema(df) -> list[list[str]]:
    """``[[name, dtype], ...]`` for a polars or pandas dataframe — column NAMES and
    type names only (both are schema, never a data value)."""
    try:
        names = [str(c) for c in df.columns]
    except Exception:  # noqa: BLE001
        return []
    # polars: df.schema is an ordered {name: dtype} mapping.
    sch = getattr(df, "schema", None)
    if sch is not None and hasattr(sch, "items"):
        try:
            return [[str(n), str(t)] for n, t in sch.items()]
        except Exception:  # noqa: BLE001
            pass
    # pandas: df.dtypes is a Series (name -> dtype).
    dtypes = getattr(df, "dtypes", None)
    if dtypes is not None and hasattr(dtypes, "items"):
        try:
            return [[str(n), str(t)] for n, t in dtypes.items()]
        except Exception:  # noqa: BLE001
            pass
    # polars .dtypes is a list aligned with columns.
    if dtypes is not None:
        try:
            return [[str(n), str(t)] for n, t in zip(names, list(dtypes))]
        except Exception:  # noqa: BLE001
            pass
    return [[n, ""] for n in names]


def _file_sha(path) -> str | None:
    """A content hash of the input FILE (sha256 of its bytes, streamed). Value-free —
    a digest, never the parsed data. ``None`` if the file can't be read. Data files are
    hashed byte-faithfully (no line-ending normalisation), matching how git/mooring hash
    non-``.py`` blobs; note a container format (xlsx/parquet) can re-compress to different
    bytes for the same logical data, so this is a FILE fingerprint, backed up by the
    shape+schema below."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


# -- the public API -------------------------------------------------------------


def fingerprint(df=None, name: str | None = None, *, path: str | None = None) -> Result:
    """Record a value-free fingerprint of an input and flag it if it changed.

    ``df`` — the loaded dataframe (polars/pandas), inspected for shape + schema (names
    and types only). ``path`` — the source file, hashed for a content fingerprint.
    ``name`` — the logical input name the receipt is keyed by (defaults to the file's
    basename). Pass ``df`` and/or ``path``; at least one, plus a name or a path.

    Returns a :class:`Result` that is falsy when the input CHANGED since the previous
    run — so you can ``assert`` a run read the same inputs as before."""
    if name is None and path is not None:
        name = os.path.basename(str(path)) or str(path)
    if not name:
        raise ValueError("fingerprint() needs a name or a path")
    rows, cols = (_shape(df) if df is not None else (0, 0))
    schema = _schema(df) if df is not None else []
    sha = _file_sha(path) if path is not None else None
    entry = {
        "path": str(path).replace(os.sep, "/") if path is not None else "",
        "sha": sha or "",
        "rows": rows,
        "cols": cols,
        "schema": schema,
    }
    prior = _load_entry(name)
    seen_before = prior is not None
    changed = seen_before and _differs(prior, entry)
    entry["changed"] = changed
    note = _describe(prior, entry, seen_before, changed)
    _write_receipt(name, entry)
    result = Result(name, changed, seen_before, note)
    print(repr(result))
    return result


def reset(name: str | None = None) -> None:
    """Clear this notebook's recorded input fingerprints — call at the top of the cell
    so a renamed or dropped input does not linger. With ``name``, clear only that one."""
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
        inputs = data.get("inputs")
        if isinstance(inputs, dict) and name in inputs:
            del inputs[name]
            _atomic_write(path, json.dumps(data, ensure_ascii=False))
    except (OSError, ValueError):
        pass


# -- change detection (all value-free) ------------------------------------------


def _differs(prior: dict, entry: dict) -> bool:
    """Whether ``entry`` differs from the stored ``prior`` fingerprint. Prefer the
    content hash when both have one; otherwise fall back to shape + schema (so a
    df-only fingerprint still detects a moved input)."""
    if prior.get("sha") and entry.get("sha"):
        return prior["sha"] != entry["sha"]
    return (
        prior.get("rows") != entry.get("rows")
        or prior.get("cols") != entry.get("cols")
        or prior.get("schema") != entry.get("schema")
    )


def _describe(prior, entry, seen_before: bool, changed: bool) -> str:
    """A value-free one-line note (counts and structural facts only)."""
    shape = f"{entry['rows']}x{entry['cols']}"
    if not seen_before:
        return f"first fingerprint ({shape})"
    if not changed:
        return f"unchanged ({shape})"
    bits = []
    if prior.get("sha") and entry.get("sha") and prior["sha"] != entry["sha"]:
        bits.append("content changed")
    if prior.get("rows") != entry.get("rows"):
        bits.append(f"rows {prior.get('rows')}->{entry.get('rows')}")
    if prior.get("cols") != entry.get("cols") or prior.get("schema") != entry.get("schema"):
        bits.append("schema changed")
    return "; ".join(bits) or "changed"


# -- value-free receipt (local only; never sent to the AI) ----------------------


def _workspace() -> Path | None:
    # <ws>/.mooring/pylib/mooring_inputs.py -> parents[2] == <ws>
    try:
        return Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        return None


def _detect_notebook(ws: Path) -> str | None:
    """The workspace-relative path of the notebook that called us — via the ``__file__``
    global marimo sets in each cell's namespace (its ``frame.filename`` is a temporary
    compiled path). Best-effort; ``None`` outside marimo."""
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
                continue  # our own module lives at .mooring/pylib/mooring_inputs.py
            return str(rel).replace(os.sep, "/")
    except Exception:  # noqa: BLE001  # detection is best-effort; never break a run
        pass
    return None


def _slug(rel: str) -> str:
    """An INJECTIVE per-notebook receipt filename: escape ``_`` first so the ``__`` that
    encodes ``/`` is unambiguous (``a/b`` and ``a__b`` map to different files)."""
    return rel.replace("_", "_u").replace("/", "__")


def _receipt_path() -> Path | None:
    ws = _workspace()
    if ws is None:
        return None
    rel = _detect_notebook(ws) or "_notebook"
    return ws / _STATE_DIR / _INPUTS_DIRNAME / (_slug(rel) + ".json")


def _load_entry(name: str) -> dict | None:
    path = _receipt_path()
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    inputs = data.get("inputs") if isinstance(data, dict) else None
    entry = inputs.get(name) if isinstance(inputs, dict) else None
    return entry if isinstance(entry, dict) else None


def _write_receipt(name: str, entry: dict) -> None:
    ws = _workspace()
    if ws is None:
        return
    rel = _detect_notebook(ws) or "_notebook"
    path = ws / _STATE_DIR / _INPUTS_DIRNAME / (_slug(rel) + ".json")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data: dict = {"notebook": rel, "updated": now, "inputs": {}}
    try:
        if path.is_file():
            existing = json.loads(path.read_text("utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("inputs"), dict):
                data = existing
    except (OSError, ValueError):
        pass
    data["notebook"] = rel
    data["updated"] = now
    data.setdefault("inputs", {})[name] = {**entry, "ts": now}
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
