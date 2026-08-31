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
bool plus a KIND from :data:`KINDS` — a closed vocabulary mooring chose
(``dataframe``, ``int``, ``other``, …), decided by identity checks against real
type objects.

That is deliberate and it is the security property of this section. It used to
report ``type(obj).__name__``, which reads like "the identifier from a ``class``
statement" but is not: ``__name__`` is a *writable* class attribute, and can be a
metaclass property computed at read time — so it is an arbitrary string the
executing cell controls, and a cell can smuggle roughly 64 characters of anything
per asked name through it (``c = type("T", (), {}); c.__name__ = "c" + chunk``).
With auto-apply on, the cell doing that is one the model wrote and no human need
ever see. No reader-side filter on a free string can close that; a closed
vocabulary can, because nothing read off the object is ever part of an answer.
:func:`_parse_names` validates the readback against the SAME closed set and fails
closed to the catch-all, and additionally drops any name it did not ask for.
"""

from __future__ import annotations

import contextlib
import json
import os
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


# The CLOSED vocabulary the probe may answer a "what is this name?" question with.
# Every entry is MOORING'S OWN word. Nothing read off the object — no attribute, no
# class name, no repr — is ever part of an answer, so this field cannot be made to
# carry anything the executing cell chose. (See _mooring_kind.)
_MOORING_KIND_OTHER = "other"
_MOORING_KIND_NAMES = (
    "dataframe",
    "lazyframe",
    "series",
    "str",
    "int",
    "float",
    "bool",
    "bytes",
    "list",
    "dict",
    "tuple",
    "set",
    "none",
    "function",
    "class",
    _MOORING_KIND_OTHER,
)


def _mooring_exact_kinds():
    # (type object, mooring's word) pairs, matched by IDENTITY below. A subclass
    # never matches, so it lands in the catch-all rather than inheriting a label.
    return (
        (str, "str"),
        (int, "int"),
        (float, "float"),
        (bool, "bool"),
        (bytes, "bytes"),
        (list, "list"),
        (dict, "dict"),
        (tuple, "tuple"),
        (set, "set"),
        (frozenset, "set"),
        (type(None), "none"),
        (type(_mooring_safe_dtype), "function"),
    )


def _mooring_frame_kind(_t):
    # Frames by CLASS IDENTITY against the library the kernel already imported —
    # never by a module/class NAME, which the object controls. sys.modules.get so a
    # library the notebook does not use is not imported just to answer this.
    import sys as _sys

    _pl = _sys.modules.get("polars")
    if _pl is not None:
        if _t is getattr(_pl, "DataFrame", None):
            return "dataframe"
        if _t is getattr(_pl, "LazyFrame", None):
            return "lazyframe"
        if _t is getattr(_pl, "Series", None):
            return "series"
    _pd = _sys.modules.get("pandas")
    if _pd is not None:
        if _t is getattr(_pd, "DataFrame", None):
            return "dataframe"
        if _t is getattr(_pd, "Series", None):
            return "series"
    return ""


def _mooring_kind(_obj):
    # WHY this is a classification and not `type(obj).__name__`: __name__ is a
    # WRITABLE class attribute (and can be a metaclass property computed at read
    # time), so it is an arbitrary string the executing cell controls — reporting it
    # would turn this readback into a data channel of ~64 chars per asked name. So
    # the probe decides, from a fixed table, and answers with mooring's own word. A
    # misclassification is harmless by construction: the worst it can do is pick the
    # wrong CONSTANT.
    _t = type(_obj)
    for _k, _label in _mooring_exact_kinds():
        if _t is _k:
            return _label
    _frame = _mooring_frame_kind(_t)
    if _frame:
        return _frame
    try:
        if isinstance(_obj, type):
            return "class"
    except Exception:
        pass
    return _MOORING_KIND_OTHER


def _mooring_collect_names(_ns, _wanted):
    # Answers ONLY "is this name bound, and what KIND of thing is it?" for names
    # mooring asked about. The kind is one of _MOORING_KIND_NAMES — mooring's own
    # closed vocabulary — never a string derived from the object: no __name__, no
    # repr(), no str(), no len(), no keys, no attribute walk.
    _out = []
    for _n in _wanted:
        if not isinstance(_n, str):
            continue
        if _n in _ns:
            try:
                _kind = _mooring_kind(_ns[_n])
            except Exception:
                _kind = _MOORING_KIND_OTHER
            if _kind not in _MOORING_KIND_NAMES:
                _kind = _MOORING_KIND_OTHER
            _out.append({"name": _n, "present": True, "kind": _kind})
        else:
            _out.append({"name": _n, "present": False, "kind": None})
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

# The closed vocabulary, read OUT of the probe source rather than restated here, so the
# writer and the reader of the ``kind`` field cannot drift apart: :func:`_parse_names`
# accepts these words and nothing else.
KINDS = frozenset(_collect_ns["_MOORING_KIND_NAMES"])
KIND_OTHER = _collect_ns["_MOORING_KIND_OTHER"]


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
    """Fail-closed: accept ONLY ``{names:[{name:str, present:bool, kind:str|None}]}``.

    Same discipline as :func:`_parse_frames`, plus two extra locks:

    * ``asked`` (the names mooring actually put in the probe) filters the readback,
      so a name mooring never asked about cannot ride back in — the readback can
      only ever answer the question that was posed;
    * ``kind`` is checked for MEMBERSHIP of :data:`KINDS` — the closed vocabulary the
      probe classifies into — and anything else fails closed to :data:`KIND_OTHER`.
      That is the whole guarantee of this field, and it is why the probe no longer
      reports ``type(obj).__name__``: a free-form string here would be an
      exfiltration channel no reader-side filter could close (``__name__`` is
      writable, so "an identifier of sane length" passes base32 and raw non-ASCII
      text alike). The ``present`` bool survives an unrecognised kind, because
      failing a kind closed must not also lose the fact that the name is bound.

    Returns ``(present, missing, kinds)`` — sorted names, and ``(name, kind)`` pairs
    for the present ones.
    """
    present: list[str] = []
    missing: list[str] = []
    kinds: list[tuple[str, str]] = []
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
        kind = item.get("kind")
        kinds.append((name, kind if isinstance(kind, str) and kind in KINDS else KIND_OTHER))
    return tuple(sorted(set(present))), tuple(sorted(set(missing))), tuple(sorted(set(kinds)))


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
# So NO verdict — bound or not bound — is reported until the namespace has stopped
# changing (two identical readbacks), the kernel reports itself idle, and there is
# some POSITIVE reason to believe the reload has already happened.
#
# The symmetry matters and was got wrong first time round. "Every asked-about name is
# bound" looks self-evidently final, and is not: for an EDIT of a working cell those
# names are ALREADY bound from before the write, so the first readback — taken inside
# that watcher-latency window, measured settling in 0.014 s — describes the PRE-EDIT
# namespace. Reporting it says "your edit is fine" about a cell that has not run, and
# the system prompt actively steers the model towards editing existing cells, so that
# is the common path, not a corner. A false "it worked" and a false "it failed" are
# both answers the model acts on.
#
# "Positive reason" is one of three, in decreasing strength:
#
# 1. a readback that DIFFERS from the first one — the namespace moved, so the reload
#    ran (a re-run deletes a cell's defs before rebinding them, so a real reload is
#    usually visible even when the end state matches);
# 2. the kernel seen RUNNING since the write. `GET /api/kernel/status` is a
#    whole-SESSION busy signal, so it is only evidence at a moment when mooring's own
#    probe is not what is running — hence the check sits after the previous probe's
#    sidecar landed AND a poll interval has passed, never right after a post. Honest
#    residual: on a loaded machine our own probe's tail could still be finishing then,
#    which would grant this evidence early. It is the weakest of the three and the
#    other two conditions (a still namespace, an idle kernel) still have to hold;
# 3. :data:`SETTLE_FLOOR_SECONDS` elapsed since the observation began — the fallback,
#    calibrated at 2x the slowest watcher latency measured on one machine. It is a
#    guess about someone else's hardware, so it is a floor that can be raised
#    (``settle_floor=``, or MOORING_OBSERVE_FLOOR), not a constant: a loaded box, a
#    network drive or an AV scan can all beat it.
#
# None of the three is proof, which is why "could not see" is the answer whenever the
# budget runs out first. That costs the model one turn of ignorance; a wrong verdict
# either way sends it off to rewrite something on a false premise.

OBSERVE_TIMEOUT = 20.0
# The default floor. Overridable per call (``observe(settle_floor=…)``) and by the
# MOORING_OBSERVE_FLOOR environment variable, because the right value is a property of
# the machine mooring is running on, not of this file.
SETTLE_FLOOR_SECONDS = 3.0
SETTLE_FLOOR_ENV = "MOORING_OBSERVE_FLOOR"
_OBSERVE_POLL_INTERVAL = 0.35
_OBSERVE_READ_SLICE = 1.0


def settle_floor_seconds(explicit: float | None = None) -> float:
    """The settle floor for one observation: explicit, else the env override, else
    :data:`SETTLE_FLOOR_SECONDS`. Never negative, and never raises — an unreadable
    override falls back to the default rather than failing the observation."""
    for value in (explicit, os.environ.get(SETTLE_FLOOR_ENV)):
        if value is None or value == "":
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return SETTLE_FLOOR_SECONDS


@dataclass(frozen=True)
class Observation:
    """What the running kernel looked like after a change — value-free throughout.

    ``present``/``missing`` are the names mooring ASKED about — normally
    :func:`mooring.marimo_rt.cell_defs` for the cell just written — split by whether
    the kernel has them bound. ``kinds`` pairs a present name with its :data:`KINDS`
    classification.

    ``frames`` are the live dataframe schemas of the names that were ASKED about, and
    only those. The session's other frames are deliberately dropped here: a frame's
    variable name and its column names are strings the executing cell can set from
    data (``globals()[secret] = df``, ``df.rename(...)``), and with the model writing
    the code that executes, every readback whose KEYS it controls is a channel. That
    cannot be closed by filtering — but this path has no need of the extra frames, and
    a channel not opened needs no filter. The live-schema context channel
    (:func:`live_dataset_schemas`, gated by ``[ai] live_schema``) is where the
    session's frames are reported, unchanged.

    ``observed`` is the load-bearing field. ``False`` means mooring could not see the
    kernel settle, and NOTHING may be concluded from the rest: ``missing`` is always
    empty in that case, because "I could not see" and "it failed" must never be the
    same result. ``detail`` says, value-free, why.
    """

    frames: tuple[DatasetSchema, ...] = ()
    present: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    kinds: tuple[tuple[str, str], ...] = ()
    observed: bool = False
    detail: str = ""


def observe(
    editor,
    notebook_rel: str,
    expect_names,
    *,
    timeout: float = OBSERVE_TIMEOUT,
    settle_floor: float | None = None,
) -> Observation:
    """Watch ``notebook_rel``'s live kernel until it settles, then report what is there.

    Call it immediately after writing the notebook source: the settle floor is
    measured from here, and it is calibrated against how long marimo's watcher takes
    to notice a file change (see the notes above). ``settle_floor`` overrides that
    floor for this call — see :func:`settle_floor_seconds`.

    ``expect_names`` is what the change was supposed to bind. Names that are not
    plain identifiers, and ``_``-prefixed names (which marimo keeps CELL-LOCAL, so
    they are absent from the kernel globals however well the cell ran), are dropped
    before asking — see :func:`_askable_names`. With NOTHING left to ask about (a
    markdown cell, or a source marimo could not read) there is no question the probe
    can answer, so this returns "could not see" rather than probing: settling on "the
    kernel is fine" would be a verdict about a change nothing was checked against.

    Never raises, and never guesses: every failure — no editor, no session, an
    unreachable kernel, a kernel that will not settle — comes back as
    ``observed=False`` with a fixed, value-free ``detail``. Those details are
    constant strings from this module, never ``str(exc)``, so nothing from the
    workspace can ride out on one.
    """
    names = tuple(_askable_names(expect_names))
    if editor is None or not getattr(editor, "running", False) or not getattr(editor, "port", None):
        return Observation(detail="the marimo editor is not running")
    if not names:
        return Observation(detail="the change binds no name mooring could check")
    try:
        kc = marimo_rt.KernelControl(editor.port, editor.token, timeout=_DEFAULT_TIMEOUT)
        session_id = kc.session_for(notebook_rel)
    except _LIVE_ERRORS:
        return Observation(detail="the notebook's kernel could not be reached")
    if not session_id:
        return Observation(detail="this notebook has no running kernel session")
    try:
        return _observe_loop(kc, session_id, names, timeout, settle_floor_seconds(settle_floor))
    except Exception:  # noqa: BLE001  # an observation must never break its caller
        return Observation(detail="the observation failed")


def _observe_loop(
    kc, session_id: str, names: tuple[str, ...], timeout: float, floor_seconds: float
) -> Observation:
    """Probe until the kernel settles, the budget runs out, or the transport fails."""
    start = time.monotonic()
    deadline = start + timeout
    # NOT capped by `timeout`: the floor is a safety guard, and a caller asking for a
    # quick answer must not get a cheaper standard of proof. A budget shorter than the
    # floor simply means this call can only settle on the other two kinds of evidence
    # (a moved namespace, a kernel seen running), and otherwise says it could not see.
    floor = start + floor_seconds
    # Paths whose probe may still be queued when we give up: `_poll_read` deletes the
    # file it waited for, but a probe that runs later writes it again. Swept at the end.
    written: list[Path] = []
    first: dict | None = None
    previous: dict | None = None
    saw_running = False
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
                if first is None:
                    first = reading
                moved = reading != first
                settled = _settled(
                    kc, session_id, reading, previous, names, saw_running or moved, floor
                )
                if settled is not None:
                    return settled
                previous = reading
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_OBSERVE_POLL_INTERVAL, remaining))
            # The busy signal read POSITIVELY, and this is the only place it is safe to:
            # our own probe's sidecar has already landed and a poll interval has passed,
            # so "running" is (very probably) the kernel's OTHER work — the watcher's
            # reload cascade. Sticky, because the cascade we are waiting for is exactly
            # the thing that will have finished by the time we ask again.
            if not saw_running:
                try:
                    saw_running = kc.kernel_state(session_id) == "running"
                except _LIVE_ERRORS:
                    saw_running = False
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


def _settled(kc, session_id, reading, previous, names, evidence, floor) -> Observation | None:
    """The verdict for one readback, or ``None`` for "keep watching".

    ONE rule, for both answers. It used to be two: "every name is bound" settled on
    the first readback, and only "a name is missing" had to prove anything. That is
    the asymmetry this function no longer has — for an EDIT the expected names are
    already bound from before the write, so the cheap branch settled inside the
    watcher-latency window and reported the pre-edit namespace as the result of the
    edit. Both answers now need the same three things:

    * the namespace unchanged since the previous readback (nothing is mid-cascade);
    * the kernel idle (its own word — an unknown state is never read as idle);
    * ``evidence`` that the reload happened, or the settle floor passed as a fallback.
    """
    present, missing, kinds = _parse_names(reading, names)
    asked = set(names)
    # Only the frames mooring ASKED about — see Observation.frames for why the rest of
    # the session's frames have no business on this path.
    frames = tuple(f for f in _parse_frames(reading) if f.name in asked)
    if not (evidence or time.monotonic() >= floor):
        return None
    if previous is None or reading != previous:
        return None
    try:
        idle = kc.kernel_state(session_id) == "idle"
    except _LIVE_ERRORS:
        idle = False  # no busy signal is "not idle": it can only block a verdict
    if not idle:
        return None
    return Observation(
        frames=frames, present=present, missing=missing, kinds=kinds, observed=True
    )


def format_observation(obs: Observation) -> str:
    """Render an :class:`Observation` as the text handed to the MODEL as a tool result.

    Strictly value-free: the variable names mooring asked about, their column names,
    dtypes and row counts, a kind from mooring's own closed vocabulary, and this
    module's own words. It states what is and is not bound and stops there — no cause
    is offered for a name that is missing, because none is known.
    """
    if not obs.observed:
        detail = f" ({obs.detail})" if obs.detail else ""
        return (
            f"mooring could not observe the running notebook{detail}. This is NOT a "
            "report that anything failed — nothing is known about whether the change "
            "ran. Do not change working code on the strength of it."
        )
    kind_of = dict(obs.kinds)
    frame_of = {f.name: f for f in obs.frames}
    lines = ["Observed the running notebook (schema only — never values):"]
    for name in obs.present:
        frame = frame_of.get(name)
        # KIND_OTHER is "mooring has no word for this", which is not worth a label.
        kind = kind_of.get(name, "")
        kind = "" if kind == KIND_OTHER else kind
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
    if len(lines) == 1:
        lines.append("nothing mooring asked about is bound.")
    return "\n".join(lines)
