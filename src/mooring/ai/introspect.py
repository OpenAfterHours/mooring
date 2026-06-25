"""Read the schema of the dataframes LIVE in the running marimo kernel.

Why this exists: :mod:`mooring.schema` can only inspect data files that sit
*inside* the workspace (it reads their parquet footer / csv+xlsx header). But an
analyst's real data usually lives OUTSIDE the workspace — a network share, a
warehouse export, a DB connection, a dynamically-built path — and the useful
schema for code-completion is often a *derived* frame (a join/filter result)
that exists in no file at all. The kernel already holds those frames, so we ask
*it* for their schema instead of re-reading files.

How it stays value-blind, the same promise as :mod:`mooring.schema`:

* We never open marimo's websocket and never read a cell *output* (the channel
  that carries data). ``POST /api/kernel/run`` executes code but its HTTP
  response carries no outputs (verified: scripts/spike_marimo_http_control.py).
* The code we run is the FROZEN probe below — never model-authored. It emits
  only ``{name, columns:[(name, dtype)], n_rows}`` for each polars/pandas frame
  in the kernel namespace, using schema-only accessors (``collect_schema()`` /
  ``.schema`` / ``.dtypes`` — never ``.head``/``.row``/``.collect`` of data),
  and it strips the one dtype that embeds author values (polars ``Enum``).
* The probe hands its value-free JSON back via a sidecar file the hub reads and
  deletes; the hub-side parser (:func:`_parse_frames`) is fail-closed.

Unlike :mod:`mooring.schema` (where mooring physically only reads a header), the
guarantee here is "mooring runs its own fixed, value-free code" — see the leak
test in tests/test_introspect.py and docs/admins/ai-privacy.md.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import tempfile
import time
from pathlib import Path

from mooring import marimo_rt
from mooring.schema import DatasetSchema

# The marimo HTTP control client + server-token scraping live in mooring.marimo_rt
# (the transport seam). Re-exported here so this module's public surface — and the
# tests that import these names — are unchanged.
_DEFAULT_TIMEOUT = marimo_rt.DEFAULT_TIMEOUT
_extract_server_token = marimo_rt.extract_server_token

# A transport failure from the seam (incl. a too-old marimo) means we fall back to
# the file-based schema — live introspection is best-effort and never raises.
_LIVE_ERRORS = (marimo_rt.MarimoTransportError, marimo_rt.MarimoTooOld, OSError, ValueError)

# --- the frozen probe ------------------------------------------------------
#
# Self-contained: the kernel runs in the team's env (uv project or frozen
# bundle), where `mooring` is NOT importable — so this is stdlib + whatever the
# user already imported (polars/pandas). Names are `_`-prefixed so marimo treats
# them as cell-local (no reactive-graph edges, no multiple-definition errors).

_COLLECT_SRC = '''
def _mooring_safe_dtype(_dt):
    _s = str(_dt)
    # polars Enum embeds author-defined category strings in its repr; keep the
    # type name, drop the values. Every other dtype str is pure type metadata.
    if "Enum" in _s:
        return "Enum"
    return _s


def _mooring_collect_schemas(_ns):
    _frames = []
    for _name, _obj in list(_ns.items()):
        if not isinstance(_name, str) or _name.startswith("_"):
            continue
        _t = type(_obj)
        _mod = (getattr(_t, "__module__", "") or "").split(".")[0]
        _cls = getattr(_t, "__name__", "")
        if _mod not in ("polars", "pandas"):
            continue
        try:
            if _mod == "polars" and _cls == "LazyFrame":
                _cols = [[str(_k), _mooring_safe_dtype(_v)]
                         for _k, _v in _obj.collect_schema().items()]
                _n = None
            elif _mod == "polars" and _cls == "DataFrame":
                _cols = [[str(_k), _mooring_safe_dtype(_v)]
                         for _k, _v in _obj.schema.items()]
                _n = int(_obj.height)
            elif _mod == "pandas" and _cls == "DataFrame":
                _cols = [[str(_c), _mooring_safe_dtype(_obj.dtypes[_c])]
                         for _c in list(_obj.columns)]
                _n = int(len(_obj))
            else:
                continue
        except Exception:
            continue
        _frames.append({"name": str(_name), "columns": _cols, "n_rows": _n})
    return {"frames": _frames}
'''

_PROBE_WRAPPER = '''
def _mooring_probe(_path):
    import json as _json, os as _os
    try:
        _data = _mooring_collect_schemas(dict(globals()))
    except Exception:
        _data = {"frames": []}
    try:
        _tmp = _path + ".mooring.tmp"
        with open(_tmp, "w", encoding="utf-8") as _f:
            _json.dump(_data, _f)
        _os.replace(_tmp, _path)
    except Exception:
        pass
'''

# The collection logic, exec'd here so the SAME source the kernel runs is also
# importable + unit-testable (no drift between the tested and injected code).
_collect_ns: dict = {}
exec(_COLLECT_SRC, _collect_ns)  # noqa: S102  # our own constant, no external input
collect_schemas = _collect_ns["_mooring_collect_schemas"]


def probe_source(out_path: str | Path) -> str:
    """The full kernel snippet: define the collector, then write its result to
    ``out_path`` as value-free JSON."""
    return f"{_COLLECT_SRC}\n{_PROBE_WRAPPER}\n_mooring_probe({str(out_path)!r})\n"


# --- public entry point ----------------------------------------------------


def live_dataset_schemas(editor, notebook_rel: str, *, timeout: float = _DEFAULT_TIMEOUT):
    """Schemas of the dataframes loaded in ``notebook_rel``'s running kernel.

    Best-effort: returns ``[]`` (and the caller falls back to file-based schema)
    if the editor isn't running, the notebook has no live session, the frames
    aren't loaded yet, or anything goes wrong. Never raises.
    """
    if editor is None or not getattr(editor, "running", False) or not getattr(editor, "port", None):
        return []
    try:
        # Construction asserts the marimo floor (MarimoTooOld), so it must be inside
        # the guard — this function must never raise; the caller falls back to file schema.
        kc = marimo_rt.KernelControl(editor.port, editor.token, timeout=timeout)
        session_id = kc.session_for(notebook_rel)
    except _LIVE_ERRORS:
        return []
    if not session_id:
        return []
    out = Path(tempfile.gettempdir()) / f"mooring-introspect-{secrets.token_hex(8)}.json"
    try:
        kc.run(session_id, probe_source(out))
    except _LIVE_ERRORS:
        with contextlib.suppress(OSError):
            out.unlink()
        return []
    return _parse_frames(_poll_read(out, timeout))


def _poll_read(path: Path, timeout: float) -> dict:
    """Wait (briefly) for the probe to write ``path``, read it, then delete it."""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if path.exists():
                try:
                    with path.open("r", encoding="utf-8") as f:
                        return json.load(f)
                except (OSError, ValueError):
                    return {}
            time.sleep(0.05)
        return {}
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def _parse_frames(data: object) -> list[DatasetSchema]:
    """Fail-closed: accept ONLY ``{frames:[{name:str, columns:[[str,str]], n_rows:int?}]}``.

    Anything else in the readback is dropped — a value can't ride in on a key we
    don't read."""
    frames: list[DatasetSchema] = []
    if not isinstance(data, dict):
        return frames
    raw = data.get("frames")
    if not isinstance(raw, list):
        return frames
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        cols = item.get("columns")
        if not isinstance(name, str) or not isinstance(cols, list):
            continue
        clean = tuple(
            (c[0], c[1])
            for c in cols
            if isinstance(c, list) and len(c) == 2 and isinstance(c[0], str) and isinstance(c[1], str)
        )
        if not clean:
            continue
        n_rows = item.get("n_rows")
        n_rows = n_rows if isinstance(n_rows, int) and not isinstance(n_rows, bool) else None
        frames.append(DatasetSchema(name=name, columns=clean, n_rows=n_rows))
    return frames


def format_live_schemas(frames) -> str:
    """Render the live frames for the system context — names + dtypes only."""
    if not frames:
        return ""
    lines = [
        "These dataframes are currently loaded in the running notebook session "
        "(variable name, then columns as name: dtype — never values):"
    ]
    for f in frames:
        rows = f" ({f.n_rows:,} rows)" if f.n_rows is not None else ""
        lines.append(f"`{f.name}`{rows}:")
        lines += [f"- {name}: {dtype}" for name, dtype in f.columns]
    return "\n".join(lines)
