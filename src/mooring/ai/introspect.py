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

**The second question the probe answers** (see :func:`observe`) is "are THESE
names bound in the kernel, and what are they?" — asked so the copilot can learn
whether the cell it just wrote actually ran, instead of proposing blind. The
names are mooring's own (they come from :func:`mooring.marimo_rt.cell_defs`
static analysis, so the asker already knows them), and the answer per name is a
bool plus ``type(obj).__name__`` — the runtime CLASS name and nothing else.
Never ``repr``, never ``str(obj)``, never a length, never dict keys: a class
name is authored code, of exactly the kind the model already reads in the
notebook source. :func:`_parse_names` is fail-closed the same way
:func:`_parse_frames` is, and additionally drops any name it did not ask for.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import tempfile
import time
from dataclasses import dataclass
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

_COLLECT_SRC = """
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


def _mooring_collect_names(_ns, _wanted):
    # Answers ONLY "is this name bound, and what class is it?" for names mooring
    # asked about. type(obj).__name__ is the whole of what an object contributes —
    # no repr(), no str(), no len(), no keys, no attribute walk. An object that
    # lies about its type (a __class__ property) can only lie with another name.
    _out = []
    for _n in _wanted:
        if not isinstance(_n, str):
            continue
        if _n in _ns:
            try:
                _tn = type(_ns[_n]).__name__
            except Exception:
                _tn = ""
            _out.append({"name": _n, "present": True, "type": str(_tn)})
        else:
            _out.append({"name": _n, "present": False, "type": None})
    return _out
"""

_PROBE_WRAPPER = """
def _mooring_probe(_path, _wanted):
    import json as _json, os as _os
    _g = dict(globals())
    try:
        _data = _mooring_collect_schemas(_g)
    except Exception:
        _data = {"frames": []}
    try:
        _data["names"] = _mooring_collect_names(_g, _wanted)
    except Exception:
        _data["names"] = []
    try:
        _tmp = _path + ".mooring.tmp"
        with open(_tmp, "w", encoding="utf-8") as _f:
            _json.dump(_data, _f)
        _os.replace(_tmp, _path)
    except Exception:
        pass
"""

# The collection logic, exec'd here so the SAME source the kernel runs is also
# importable + unit-testable (no drift between the tested and injected code).
_collect_ns: dict = {}
exec(_COLLECT_SRC, _collect_ns)  # noqa: S102  # our own constant, no external input
collect_schemas = _collect_ns["_mooring_collect_schemas"]
collect_names = _collect_ns["_mooring_collect_names"]


def probe_source(out_path: str | Path, expect_names=()) -> str:
    """The full kernel snippet: define the collectors, then write their result to
    ``out_path`` as value-free JSON.

    ``expect_names`` is the list of names to answer "bound?" for. It is filtered
    to plain Python identifiers before being embedded (a name that is not an
    identifier cannot be a binding, so nothing is lost — and nothing that is not
    an identifier ever reaches the kernel snippet)."""
    wanted = tuple(_askable_names(expect_names))
    return (
        f"{_COLLECT_SRC}\n{_PROBE_WRAPPER}\n"
        f"_mooring_probe({str(out_path)!r}, {wanted!r})\n"
    )


def _askable_names(names) -> list[str]:
    """The subset of ``names`` the probe can meaningfully answer about, de-duplicated.

    Drops anything that is not a plain identifier, and anything ``_``-prefixed:
    marimo treats an underscore name as CELL-LOCAL, so it is deliberately absent
    from the kernel's globals and would read as "missing" for a cell that ran
    perfectly. Not asking is the only honest answer for those.
    """
    out: list[str] = []
    seen: set[str] = set()
    # A bare string is iterable one CHARACTER at a time — a caller that passes one
    # name instead of a list must not end up asking about `s`, `a`, `l`, `e`, `s`.
    names = (names,) if isinstance(names, str) else names
    for name in names or ():
        if not isinstance(name, str) or name in seen:
            continue
        if not name.isidentifier() or name.startswith("_"):
            continue
        seen.add(name)
        out.append(name)
    return out


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
            if isinstance(c, list)
            and len(c) == 2
            and isinstance(c[0], str)
            and isinstance(c[1], str)
        )
        if not clean:
            continue
        n_rows = item.get("n_rows")
        n_rows = n_rows if isinstance(n_rows, int) and not isinstance(n_rows, bool) else None
        frames.append(DatasetSchema(name=name, columns=clean, n_rows=n_rows))
    return frames


def _parse_names(data: object, asked=None):
    """Fail-closed: accept ONLY ``{names:[{name:str, present:bool, type:str|None}]}``.

    Same discipline as :func:`_parse_frames`, plus two extra locks:

    * ``asked`` (the names mooring actually put in the probe) filters the readback,
      so a name mooring never asked about cannot ride back in — the readback can
      only ever answer the question that was posed;
    * a ``type`` that is not a plain identifier of sane length is dropped to ``""``.
      ``type(obj).__name__`` is always an identifier for a real class, so this costs
      nothing and means the one free-form string in the payload cannot carry a
      sentence, a path or a serialized value. The ``present`` bool survives, because
      dropping a suspect type name must not also lose the fact that the name is bound.

    Returns ``(present, missing, types)`` — sorted names, and ``(name, type)`` pairs
    for the present ones that reported a usable type.
    """
    present: list[str] = []
    missing: list[str] = []
    types: list[tuple[str, str]] = []
    if not isinstance(data, dict):
        return (), (), ()
    raw = data.get("names")
    if not isinstance(raw, list):
        return (), (), ()
    wanted = None if asked is None else frozenset(asked)
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        is_present = item.get("present")
        if not isinstance(name, str) or not name.isidentifier():
            continue
        if wanted is not None and name not in wanted:
            continue
        if not isinstance(is_present, bool):
            continue  # an int/str "present" is not an answer we accept
        if not is_present:
            missing.append(name)
            continue
        present.append(name)
        type_name = item.get("type")
        if isinstance(type_name, str) and type_name.isidentifier() and len(type_name) <= 64:
            types.append((name, type_name))
    return tuple(sorted(set(present))), tuple(sorted(set(missing))), tuple(sorted(set(types)))


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


# --- observe: did the cell mooring just wrote actually run? -----------------
#
# The settle problem, and what is actually KNOWN about it (measured against a real
# headless `marimo edit --watch` with runtime.watcher_on_save = "autorun", marimo
# 0.23.9, Windows — a throwaway spike, not a committed script):
#
# * `/api/kernel/run` is FIRE-AND-FORGET over HTTP (the POST returns in ~10 ms) but
#   the kernel executes control requests ONE AT A TIME, in the order they were
#   queued. Measured: a probe posted immediately after a 4-second cell had its
#   sidecar written 4.17 s later, and it saw that cell's definitions. So a readback
#   is never a stale namespace — while a cascade is in flight the probe has simply
#   not run yet, and the sidecar does not appear at all.
# * The file-watch reload takes the SAME queue: marimo's file-change handler calls
#   `session.put_control_request(SyncGraphCommand(...))` when watcher_on_save is
#   "autorun", exactly as `/api/kernel/run` does (read in
#   marimo/_session/file_change_handler.py and _server/api/utils.py). So once the
#   reload is queued, a probe posted after it necessarily runs after the whole
#   re-run cascade.
# * That leaves ONE window in which a probe can honestly answer "not bound" about a
#   cell that is going to run perfectly: between mooring writing the .py and the
#   watcher noticing it. Measured latencies for that window on this machine: 0.5 s,
#   0.6 s and 1.5 s.
#
# So "not bound" is only ever reported when the namespace has stopped changing (two
# identical readbacks), the kernel reports itself idle, AND at least
# :data:`SETTLE_FLOOR_SECONDS` have passed since the observation began. Note the
# first two conditions are BOTH true during that window — a kernel that has not yet
# noticed the file is idle and unchanging — so the floor is the one thing standing
# between the watcher's latency and a false "your cell failed". That is why it is
# 2x the slowest latency measured rather than a snug fit, and why it is only ever
# paid on the missing path: a name that IS bound settles immediately. The whole rule
# is deliberately lopsided — "I could not see" costs the model one turn of
# ignorance, a false failure report sends it off to repair a cell that was fine.
#
# `GET /api/kernel/status` is a whole-SESSION busy signal, so it cannot prove the
# reload happened (mooring's own probe runs make it "running" too, and a fast cell's
# running window can be missed entirely between polls — observed). It is used only
# in the direction that is safe: "not idle" BLOCKS a missing verdict, it never
# grants one.

OBSERVE_TIMEOUT = 20.0
SETTLE_FLOOR_SECONDS = 3.0
_OBSERVE_POLL_INTERVAL = 0.35
_OBSERVE_READ_SLICE = 1.0


@dataclass(frozen=True)
class Observation:
    """What the running kernel looked like after a change — value-free throughout.

    ``frames`` are the live dataframe schemas (exactly what
    :func:`live_dataset_schemas` returns). ``present``/``missing`` are the names
    mooring ASKED about — normally :func:`mooring.marimo_rt.cell_defs` for the cell
    just written — split by whether the kernel has them bound. ``types`` pairs a
    present name with its runtime class name.

    ``observed`` is the load-bearing field. ``False`` means mooring could not see the
    kernel settle, and NOTHING may be concluded from the rest: ``missing`` is always
    empty in that case, because "I could not see" and "it failed" must never be the
    same result. ``detail`` says, value-free, why.
    """

    frames: tuple[DatasetSchema, ...] = ()
    present: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    types: tuple[tuple[str, str], ...] = ()
    observed: bool = False
    detail: str = ""


def observe(
    editor, notebook_rel: str, expect_names, *, timeout: float = OBSERVE_TIMEOUT
) -> Observation:
    """Watch ``notebook_rel``'s live kernel until it settles, then report what is there.

    Call it immediately after writing the notebook source: the settle floor is
    measured from here, and it is calibrated against how long marimo's watcher takes
    to notice a file change (see the notes above).

    ``expect_names`` is what the change was supposed to bind. Names that are not
    plain identifiers, and ``_``-prefixed names (which marimo keeps CELL-LOCAL, so
    they are absent from the kernel globals however well the cell ran), are dropped
    before asking — see :func:`_askable_names`.

    Never raises, and never guesses: every failure — no editor, no session, an
    unreachable kernel, a kernel that will not settle — comes back as
    ``observed=False`` with a fixed, value-free ``detail``. Those details are
    constant strings from this module, never ``str(exc)``, so nothing from the
    workspace can ride out on one.
    """
    names = tuple(_askable_names(expect_names))
    if editor is None or not getattr(editor, "running", False) or not getattr(editor, "port", None):
        return Observation(detail="the marimo editor is not running")
    try:
        kc = marimo_rt.KernelControl(editor.port, editor.token, timeout=_DEFAULT_TIMEOUT)
        session_id = kc.session_for(notebook_rel)
    except _LIVE_ERRORS:
        return Observation(detail="the notebook's kernel could not be reached")
    if not session_id:
        return Observation(detail="this notebook has no running kernel session")
    try:
        return _observe_loop(kc, session_id, names, timeout)
    except Exception:  # noqa: BLE001  # an observation must never break its caller
        return Observation(detail="the observation failed")


def _observe_loop(kc, session_id: str, names: tuple[str, ...], timeout: float) -> Observation:
    """Probe until the kernel settles, the budget runs out, or the transport fails."""
    start = time.monotonic()
    deadline = start + timeout
    # NOT capped by `timeout`: the floor is a safety guard, and a caller asking for a
    # quick answer must not get a cheaper standard of proof. A budget shorter than the
    # floor simply means this call can never return a MISSING verdict — it can still
    # settle the moment every name is bound, which is the common case.
    floor = start + SETTLE_FLOOR_SECONDS
    # Paths whose probe may still be queued when we give up: `_poll_read` deletes the
    # file it waited for, but a probe that runs later writes it again. Swept at the end.
    written: list[Path] = []
    previous: dict | None = None
    try:
        while True:
            out = Path(tempfile.gettempdir()) / f"mooring-observe-{secrets.token_hex(8)}.json"
            written.append(out)
            try:
                kc.run(session_id, probe_source(out, names))
            except _LIVE_ERRORS:
                return Observation(detail="the notebook's kernel could not be reached")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            reading = _poll_read(out, min(_OBSERVE_READ_SLICE, remaining)) or None
            if reading is not None:
                settled = _settled(kc, session_id, reading, previous, names, floor)
                if settled is not None:
                    return settled
                previous = reading
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_OBSERVE_POLL_INTERVAL, remaining))
        return Observation(
            detail=(
                f"the notebook's kernel did not settle within {timeout:.0f}s — it may "
                "still be running the change"
            )
        )
    finally:
        for path in written:
            with contextlib.suppress(OSError):
                path.unlink()


def _settled(kc, session_id, reading, previous, names, floor) -> Observation | None:
    """The verdict for one readback, or ``None`` for "keep watching".

    Two ways to settle, and they are not symmetric. Every asked-about name being
    bound is self-evidently final — the kernel cannot un-run the cell. A name still
    NOT bound proves nothing on its own, so it needs the whole settle rule: the
    namespace unchanged since the previous readback, the kernel idle, and the floor
    passed.
    """
    present, missing, types = _parse_names(reading, names)
    frames = tuple(_parse_frames(reading))
    if not names or (not missing and set(present) == set(names)):
        return Observation(frames=frames, present=present, types=types, observed=True)
    if previous is not None and reading == previous and time.monotonic() >= floor:
        try:
            idle = kc.kernel_state(session_id) == "idle"
        except _LIVE_ERRORS:
            idle = False  # no busy signal is "not idle": it can only block a verdict
        if idle:
            return Observation(
                frames=frames, present=present, missing=missing, types=types, observed=True
            )
    return None


def format_observation(obs: Observation) -> str:
    """Render an :class:`Observation` as the text handed to the MODEL as a tool result.

    Strictly value-free: variable names, column names, dtypes, row counts, class
    names, and this module's own words. It states what is and is not bound and stops
    there — no cause is offered for a name that is missing, because none is known.
    """
    if not obs.observed:
        detail = f" ({obs.detail})" if obs.detail else ""
        return (
            f"mooring could not observe the running notebook{detail}. This is NOT a "
            "report that anything failed — nothing is known about whether the change "
            "ran. Do not change working code on the strength of it."
        )
    type_of = dict(obs.types)
    frame_of = {f.name: f for f in obs.frames}
    lines = ["Observed the running notebook (schema only — never values):"]
    for name in obs.present:
        frame = frame_of.get(name)
        kind = type_of.get(name, "")
        if frame is not None:
            rows = f", {frame.n_rows:,} rows" if frame.n_rows is not None else ""
            label = f" ({kind}{rows})" if kind else ""
            lines.append(f"- `{name}` is bound{label}:")
            lines += [f"    - {col}: {dtype}" for col, dtype in frame.columns]
        else:
            lines.append(f"- `{name}` is bound{f' ({kind})' if kind else ''}.")
    if obs.missing:
        named = ", ".join(f"`{name}`" for name in obs.missing)
        lines.append(
            f"NOT bound in the kernel: {named}. The code that defines them did not run "
            "to completion."
        )
    others = [f for f in obs.frames if f.name not in set(obs.present)]
    if others:
        listed = ", ".join(
            f"`{f.name}` ({len(f.columns)} column{'' if len(f.columns) == 1 else 's'})"
            for f in others
        )
        lines.append(f"Also loaded in this session: {listed}.")
    if len(lines) == 1:
        lines.append("nothing mooring asked about is bound, and no dataframes are loaded.")
    return "\n".join(lines)
