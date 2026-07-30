"""mooring_inputs — value-free fingerprints of what a notebook cell READS and WRITES.

mooring INJECTS this module into ``<workspace>/.mooring/pylib/mooring_inputs.py``
and puts that directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can::

    import mooring_inputs as mi
    sales = pl.read_csv("data/sales.csv")
    mi.fingerprint(sales, "sales", path="data/sales.csv")     # what this run READ
    ...
    monthly.write_csv("data/monthly.csv")
    mi.output(monthly, "monthly", path="data/monthly.csv")    # what this run WROTE

Each call records a VALUE-FREE fingerprint — the file's content HASH, its SHAPE
(row/column counts), and its SCHEMA (column names + dtypes) — and compares it to the
previous run's, so a moved input (or a moved output) is flagged: the reproducibility
question "same inputs, same numbers?". It answers the auditor without ever storing a
data value.

The two sides together are also the ONLY thing mooring knows about lineage. One
notebook's recorded output path matching another's recorded input path is an edge in
the graph :mod:`mooring.lineage` derives — which is what lets the hub say "3 notebooks
read this" before you overwrite a file. That graph is exactly as complete as these
calls are, and never more: nothing here infers a dependency from source or from the
filesystem, because a fingerprint you can trust has to be one you asked for.

Everything recorded is value-free: a per-notebook receipt under
``<workspace>/.mooring/inputs/`` holding ``{path, rel, sha, rows, cols, schema,
changed}`` per input and per output — a hash, two counts, and column names/types,
never a cell value. The receipt lives in the sync-excluded ``.mooring`` directory and
is NEVER sent to the AI copilot.

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

# The receipt's two sections. "inputs" is the original and only key older receipts have,
# so every reader must treat a missing "outputs" as empty rather than as malformed —
# the format only ever GROWS, and a receipt from a previous mooring must keep working.
_INPUTS = "inputs"
_OUTPUTS = "outputs"


class Result:
    """The outcome of fingerprinting one input or output. Truthy when it is UNCHANGED
    since the last run (so ``assert mi.fingerprint(df, "sales", path=...)`` reads as
    "assert this input hasn't moved"); a first sighting counts as unchanged. ``repr``
    is the one-line summary printed into the cell output."""

    __slots__ = ("name", "changed", "seen_before", "note", "kind")

    def __init__(
        self,
        name: str,
        changed: bool,
        seen_before: bool,
        note: str = "",
        kind: str = "input",
    ) -> None:
        self.name = name
        self.changed = bool(changed)
        self.seen_before = bool(seen_before)
        self.note = note
        self.kind = kind

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
        return f"[{mark}] {self.kind} {self.name}{extra}"


# -- dataframe introspection (duck-typed: polars OR pandas, never imported) ------


def _typename(dtype) -> str:
    """A VALUE-FREE dtype name: the leading identifier only, dropping any parenthesised
    detail. This is load-bearing for value-blindness — a polars ``Enum``/``Categorical``
    stringifies WITH its category labels (real data values), e.g.
    ``"Enum(categories=['EMEA', 'APAC'])"``; taking the part before ``(`` keeps ``"Enum"``
    and drops the values. (``"Int64"`` -> ``"Int64"``, ``"List(Int64)"`` -> ``"List"``.)"""
    return str(dtype).split("(", 1)[0].strip()


def _columns(df) -> list[str]:
    """Column names of a polars/pandas frame. Prefer a polars LazyFrame's
    ``collect_schema().names()`` (no data materialised, and no PerformanceWarning that
    ``LazyFrame.columns`` would raise); fall back to ``.columns`` for an eager frame."""
    collect = getattr(df, "collect_schema", None)
    if callable(collect):
        try:
            return [str(c) for c in collect().names()]
        except Exception:  # noqa: BLE001
            pass
    try:
        return [str(c) for c in df.columns]
    except Exception:  # noqa: BLE001
        return []


def _shape(df) -> tuple[int | None, int]:
    """``(rows, cols)`` — counts only, never a value. ``rows`` is ``None`` when it cannot
    be known cheaply (e.g. an un-materialised polars LazyFrame): NEVER fabricate ``0``,
    which would make a real row-count change compare equal to "no rows"."""
    shape = getattr(df, "shape", None)
    if isinstance(shape, tuple) and len(shape) >= 2:
        try:
            return int(shape[0]), int(shape[1])
        except (TypeError, ValueError):
            pass
    cols = len(_columns(df))
    try:
        rows: int | None = int(len(df))
    except TypeError:
        rows = None  # unknown (a lazy frame has no __len__) — not zero
    return rows, cols


def _schema(df) -> list[list[str]]:
    """``[[name, dtype], ...]`` — column NAMES and value-free type NAMES only (via
    :func:`_typename`). Handles a polars DataFrame, a polars LazyFrame (schema without
    materialising data), and a pandas DataFrame."""
    # polars LazyFrame: collect_schema() reads the schema without touching data.
    collect = getattr(df, "collect_schema", None)
    if callable(collect):
        try:
            return [[str(n), _typename(t)] for n, t in collect().items()]
        except Exception:  # noqa: BLE001
            pass
    # polars DataFrame: df.schema is an ordered {name: dtype} mapping.
    sch = getattr(df, "schema", None)
    if sch is not None and hasattr(sch, "items"):
        try:
            return [[str(n), _typename(t)] for n, t in sch.items()]
        except Exception:  # noqa: BLE001
            pass
    # pandas: df.dtypes is a Series (name -> dtype).
    dtypes = getattr(df, "dtypes", None)
    if dtypes is not None and hasattr(dtypes, "items"):
        try:
            return [[str(n), _typename(t)] for n, t in dtypes.items()]
        except Exception:  # noqa: BLE001
            pass
    # polars .dtypes is a list aligned with columns.
    names = _columns(df)
    if dtypes is not None:
        try:
            return [[str(n), _typename(t)] for n, t in zip(names, list(dtypes))]
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
    return _record(_INPUTS, "input", df, name, path)


def output(df=None, name: str | None = None, *, path: str | None = None) -> Result:
    """Record a value-free fingerprint of something this notebook WROTE.

    The mirror image of :func:`fingerprint`: same arguments, same value-free receipt
    (content hash + shape + schema, never a value), same falsy-when-it-changed
    :class:`Result` — so ``assert mi.output(...)`` reads as "assert my numbers didn't
    move". Call it AFTER the file is written, so the hash is of what actually landed.

    Pass ``path=`` even more religiously here than for an input. The path is the JOIN:
    it is what lets :mod:`mooring.lineage` match this output to the notebook downstream
    that fingerprints the same file as ITS input, and a name-only output is invisible to
    that graph."""
    return _record(_OUTPUTS, "output", df, name, path)


def _record(section: str, kind: str, df, name: str | None, path: str | None) -> Result:
    """The shared body of :func:`fingerprint` and :func:`output` — one implementation so
    the two sides of the graph can never drift into recording different things."""
    if name is None and path is not None:
        name = os.path.basename(str(path)) or str(path)
    if not name:
        raise ValueError(f"{kind}() needs a name or a path")
    rows, cols = (_shape(df) if df is not None else (None, 0))
    schema = _schema(df) if df is not None else []
    hashed = path is not None  # a content hash was INTENDED (a path was supplied)
    sha = _file_sha(path) if hashed else None
    entry = {
        "path": str(path).replace(os.sep, "/") if path is not None else "",
        "rel": _workspace_rel(path),  # the lineage join key; "" when not resolvable
        "hashed": hashed,
        "sha": sha,  # the hex digest, or None — NEVER "" (which would fake a hash)
        "rows": rows,  # int, or None when unknown (a lazy frame)
        "cols": cols,
        "schema": schema,
    }
    prior = _load_entry(section, name)
    seen_before = prior is not None
    changed = seen_before and _differs(prior, entry)
    entry["changed"] = changed
    note = _describe(prior, entry, seen_before, changed)
    _write_receipt(section, name, entry)
    result = Result(name, changed, seen_before, note, kind)
    print(repr(result))
    return result


def reset(name: str | None = None) -> None:
    """Clear this notebook's recorded input AND output fingerprints — call at the top of
    the cell so a renamed or dropped one does not linger (and, since lineage is derived
    from these receipts, so a dependency you removed stops being claimed). With ``name``,
    clear only that one, from whichever side recorded it."""
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
        if not isinstance(data, dict):
            return  # a foreign/corrupt receipt: nothing of ours to clear
        dropped = False
        for section in (_INPUTS, _OUTPUTS):
            bucket = data.get(section)
            if isinstance(bucket, dict) and name in bucket:
                del bucket[name]
                dropped = True
        if dropped:
            _atomic_write(path, json.dumps(data, ensure_ascii=False))
    except (OSError, ValueError):
        pass


# -- change detection (all value-free) ------------------------------------------


def _differs(prior: dict, entry: dict) -> bool:
    """Whether ``entry`` differs from the stored ``prior`` fingerprint — FAIL-CLOSED.

    When a content hash was INTENDED (a ``path`` was supplied) but could not be computed,
    report CHANGED rather than risk a false "unchanged". With a content hash on both sides
    the hash is definitive. A df-only fingerprint (no ``path``) can only compare shape +
    schema — which cannot see a same-shape VALUE change — so pass ``path=`` for the real
    content guarantee."""
    if entry.get("hashed"):
        es = entry.get("sha")
        if es is None:
            return True  # intended a content hash but it failed -> cannot confirm same
        ps = prior.get("sha")
        if ps:
            return ps != es  # both hashed: definitive
        return False  # first content hash for this input (prior was df-only) -> new baseline
    return (
        prior.get("rows") != entry.get("rows")
        or prior.get("cols") != entry.get("cols")
        or prior.get("schema") != entry.get("schema")
    )


def _describe(prior, entry, seen_before: bool, changed: bool) -> str:
    """A value-free one-line note (counts and structural facts only)."""
    rows = entry.get("rows")
    shape = f"{'?' if rows is None else rows}x{entry.get('cols')}"
    if not seen_before:
        return f"first fingerprint ({shape})"
    if not changed:
        return f"unchanged ({shape})"
    bits = []
    ps, es = prior.get("sha"), entry.get("sha")
    if entry.get("hashed") and es is None:
        bits.append("could not hash the file")
    elif ps and es and ps != es:
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
    """The workspace-relative path of the NOTEBOOK that triggered this call.

    marimo sets ``__file__`` in each cell's namespace to the real notebook ``.py`` (the
    caller's ``frame.filename`` is a temporary compiled path). We take the OUTERMOST
    workspace ``.py`` frame — the notebook cell is the outer frame, a helper module it
    calls is inner — so a ``fingerprint`` made from a helper is still attributed to the
    notebook that drove it, not the helper. Best-effort; ``None`` outside marimo."""
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
                continue  # our own module lives at .mooring/pylib/mooring_inputs.py
            found = str(rel).replace(os.sep, "/")  # keep walking: prefer the OUTERMOST
    except Exception:  # noqa: BLE001  # detection is best-effort; never break a run
        pass
    return found


def _workspace_rel(path) -> str:
    """``path`` as a workspace-relative POSIX path — the key lineage joins two notebooks
    on — or ``""`` when it resolves outside the workspace (or not at all).

    Resolved HERE, in the kernel, because only the kernel knows its own working
    directory: ``"data/sales.csv"`` names a different file depending on which notebook
    wrote it, and mooring cannot recover that later from the string alone. Resolution is
    non-strict, so an output can be fingerprinted whether or not the file exists yet."""
    ws = _workspace()
    if ws is None or path is None:
        return ""
    try:
        return str(Path(path).resolve().relative_to(ws)).replace(os.sep, "/")
    except (OSError, ValueError):
        return ""


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


def _load_entry(section: str, name: str) -> dict | None:
    path = _receipt_path()
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    bucket = data.get(section) if isinstance(data, dict) else None
    entry = bucket.get(name) if isinstance(bucket, dict) else None
    return entry if isinstance(entry, dict) else None


def _write_receipt(section: str, name: str, entry: dict) -> None:
    """Merge one entry into this notebook's receipt, leaving the OTHER section alone.

    The read-modify-write keeps a receipt that predates outputs intact when an output is
    first recorded into it (and vice versa); only the section being written is rebuilt
    when it is missing or malformed."""
    ws = _workspace()
    if ws is None:
        return
    rel = _detect_notebook(ws) or "_notebook"
    path = ws / _STATE_DIR / _INPUTS_DIRNAME / (_slug(rel) + ".json")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data: dict = {"notebook": rel, "updated": now}
    try:
        if path.is_file():
            existing = json.loads(path.read_text("utf-8"))
            if isinstance(existing, dict):
                data = existing
    except (OSError, ValueError):
        pass
    data["notebook"] = rel
    data["updated"] = now
    if not isinstance(data.get(section), dict):
        data[section] = {}
    data[section][name] = {**entry, "ts": now}
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
