"""The marimo transport seam — the one module allowed to touch marimo's internals.

mooring couples to three volatile, undocumented marimo surfaces:

* the **private codegen** API (``marimo._ast.codegen``,
  ``marimo._convert.converters.MarimoConvert``, ``marimo._schemas.serialization``)
  used to append a cell to a notebook's ``.py`` source,
* the **HTTP control API** (the ``<marimo-server-token>`` scrape, the
  ``/?access_token`` 303+cookie dance, ``/api/home/running_notebooks``,
  ``/api/kernel/run``) used to read live-kernel schemas, and
* the **lint engine** (``marimo._lint.rule_engine.RuleEngine``,
  ``marimo._lint.rules.RULE_CODES``, ``marimo._lint.context.LintContext``) used by
  :func:`validate_notebook_source` to statically check a CANDIDATE notebook before
  an analyst is ever asked to apply it.

Both can break on any marimo upgrade. Concentrating them HERE makes a marimo
upgrade a one-file event, and lets a too-old marimo fail **loud** (:class:`MarimoTooOld`)
at first use instead of degrading silently. ``ai/cellwrite.py`` and
``ai/introspect.py`` are thin wrappers that call this module and import nothing
from marimo; ``editor.py`` keeps marimo's subprocess + ``.marimo.toml`` (an editor
concern, not transport). The .importlinter ``marimo-internals-isolated`` contract
enforces that boundary.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import http.cookiejar
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

# The single source of truth for the asserted runtime floor: the minimum marimo
# everything in-tree was verified against (the dedicated <marimo-server-token>
# element + value-free /api/kernel/run, the private codegen IR shape, and the
# editor's --watch / runtime.watcher_on_save). Kept in sync with the declared
# floors in pyproject.toml and pyproject_env.MARIMO_REQUIREMENT by a test.
MARIMO_FLOOR = (0, 23, 9)
MARIMO_FLOOR_STR = "0.23.9"

# A fixed cell id: reusing it means repeated probes replace, never accumulate.
# /api/kernel/run does not add a cell to the frontend document (see ai/cellwrite),
# so this never becomes visible in the analyst's tab.
PROBE_CELL_ID = "mooring-introspect"

# Introspection is best-effort context enrichment, so it is bounded and never
# blocks chat-open for long.
DEFAULT_TIMEOUT = 4.0


class MarimoTooOld(RuntimeError):
    """The installed marimo is older than mooring's asserted floor (or unparseable)."""


class MarimoTransportError(RuntimeError):
    """A marimo control-API / codegen call failed at runtime (the seam's error surface)."""


class CellPatchConflict(ValueError):
    """A targeted edit/delete no longer matches the notebook (it changed since it was read).

    Raised by :func:`apply_cell_patch` when an op's ``index`` is out of range or its
    captured ``anchor`` (the cell's source at propose time) no longer equals the cell
    on disk — so the analyst edited or reran the notebook between proposal and Apply.
    A ``ValueError`` subclass so the thin ``cellwrite`` wrapper still catches it, but a
    distinct type so the hub can surface it as a 409 ("the cell changed") rather than a
    generic failure.
    """


_floor_checked = False


def _require_marimo_floor() -> None:
    """Assert the installed marimo meets :data:`MARIMO_FLOOR`, loudly, exactly once.

    Reads the stable PUBLIC ``marimo.__version__`` and compares its leading
    ``MAJOR.MINOR.PATCH`` triple (tolerating ``.dev``/``rc``/``.post`` suffixes).
    An unparseable version is treated as too old (fail loud, never silently pass).
    Called at the first use of either marimo-internal path, NOT at import time, so
    importing this module (or running the value-free probe) never trips it. The
    once-flag is set only on success, so a failing check always re-asserts loudly.
    """
    global _floor_checked
    if _floor_checked:
        return
    import marimo

    version = getattr(marimo, "__version__", "") or ""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise MarimoTooOld(
            f"mooring requires marimo>={MARIMO_FLOOR_STR}, but the installed marimo "
            f"version {version!r} could not be parsed; upgrade marimo."
        )
    if tuple(int(g) for g in match.groups()) < MARIMO_FLOOR:
        raise MarimoTooOld(
            f"mooring requires marimo>={MARIMO_FLOOR_STR}, found {version}; upgrade marimo."
        )
    _floor_checked = True  # only after a successful check


# --- private-codegen: read + patch notebook cells --------------------------

# A marimo `.py` persists NO stable per-cell id (a cell is identified positionally
# / by its source — verified against marimo 0.23.9: CellDef carries only code/name/
# options, and codegen emits `@app.cell def <name>` with no id or marker). So an
# edit/delete targets a cell by its INDEX plus an ``anchor`` (its source captured at
# propose time): on Apply we re-read the file and require the anchor still matches,
# turning the "the analyst changed it meanwhile" race into a loud conflict instead of
# a silent clobber. marimo's own --watch reload then reconciles BY cell similarity
# (exact-code keeps the cell's identity + output; only changed cells re-run).


@dataclass(frozen=True)
class CellOp:
    """One operation in a notebook patch (see :func:`apply_cell_patch`).

    ``op`` is ``"append"`` | ``"edit"`` | ``"delete"`` | ``"replace_all"``. ``index``
    and ``anchor`` locate an existing cell for edit/delete (``anchor`` is the cell's
    source at propose time, checked to detect a meanwhile-edit). ``code`` is the new
    source for append/edit. ``cells`` is the full new cell list for ``replace_all``
    (the whole-notebook rewrite). Indices always refer to the ORIGINAL cell order.
    """

    op: str
    index: int | None = None
    anchor: str | None = None
    code: str = ""
    cells: tuple[str, ...] = ()


def _codegen_api():
    """The private marimo codegen entrypoints, or raise :class:`MarimoTransportError`."""
    try:
        from marimo._ast import codegen
        from marimo._convert.converters import MarimoConvert
    except ImportError as exc:  # marimo present + new enough, but the private API moved
        raise MarimoTransportError(f"marimo codegen API unavailable: {exc}") from exc
    return codegen, MarimoConvert


def _parse_ir(MarimoConvert, source: str):
    """Parse ``.py`` source to the marimo IR, NORMALIZING marimo's own parse errors
    (e.g. ``MarimoFileError`` on a non-notebook file) into a plain ``ValueError``.

    The ``ai/`` layer may not import marimo (the import-linter seam), so it cannot
    catch marimo-internal exception types — concentrating that translation here keeps
    callers handling only the documented mooring/stdlib errors.
    """
    try:
        return MarimoConvert.from_py(source).to_ir()
    except (MarimoTooOld, MarimoTransportError):
        raise
    except Exception as exc:  # noqa: BLE001  # marimo parse failures surface as ValueError
        raise ValueError(f"could not parse the notebook source: {exc}") from exc


def _cell_class(ir):
    """The notebook's CellDef class (reuse an existing cell's, or import it)."""
    if ir.cells:
        return type(ir.cells[0])
    from marimo._schemas.serialization import CellDef

    return CellDef


def _with_code(cell, code: str):
    """A copy of ``cell`` with new ``code`` — preserves its name + config (so marimo's
    reload keeps the cell's identity and only re-runs it)."""
    return replace(cell, code=code)


def is_markdown_cell(code: str) -> bool:
    """True if ``code`` is a single bare ``mo.md(...)`` expression — marimo's own
    markdown-cell shape.

    Mirrors the core of ``marimo._ast.compiler._extract_markdown``: exactly one
    statement, a bare expression (not an assignment), whose value is a call to the
    attribute ``md`` on the name ``mo``. Deliberately conservative — an assignment,
    a second statement, a chained ``mo.md(...).callout()``, or any other call all
    return ``False``, so a normal code cell is never mistaken for markdown. Covers
    ``mo.md(r"...")`` and ``mo.md(f"...")`` alike (both parse to a ``Call`` of ``mo.md``).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    if len(tree.body) != 1:
        return False
    node = tree.body[0]
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "md"
        and isinstance(func.value, ast.Name)
        and func.value.id == "mo"
    )


def _new_cell(cls, code: str):
    """Build a fresh cell for ``code``, auto-HIDING the source for a markdown cell.

    A ``mo.md(...)`` cell renders its output AND shows the ``mo.md`` source in
    marimo's edit view (the view mooring launches with ``--watch``), so the analyst
    reads the same prose twice. marimo only sets ``hide_code`` on Jupyter import,
    never in native edit — so mooring sets it here for the markdown cells it (or the
    copilot) appends/rewrites. Non-markdown cells get the default (visible) config.
    Only brand-new cells flow through here; an existing cell keeps its own config via
    :func:`_with_code`, so a markdown cell the analyst chose to un-hide stays un-hidden.
    """
    options = {"hide_code": True} if is_markdown_cell(code) else {}
    return cls(code=code, name="_", options=options)


def _check_parses(code: str) -> None:
    """Raise ``ValueError`` if ``code`` is not parseable Python.

    marimo's codegen does NOT reject a syntactically-broken cell — it wraps it in
    ``app._unparsable_cell(...)`` and re-parses as "valid", so a bad edit would write
    silently and then no-op in the editor. Compile-checking the cell body here catches
    it precisely (a cross-cell name reference still compiles — that's a runtime, not a
    syntax, concern).

    ``PyCF_ALLOW_TOP_LEVEL_AWAIT`` is set because marimo cells MAY use top-level
    ``await`` / ``async for`` / ``async with`` (a supported marimo feature) — without
    it this would wrongly reject a legitimate async cell.
    """
    try:
        compile(code, "<cell>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    except SyntaxError as exc:
        raise ValueError(f"the cell would not parse: {exc}") from exc


def normalize_cell_code(code: str) -> str:
    """Best-effort cleanup of a model-provided cell body so common format mistakes
    don't fail the parse check.

    A marimo cell body is top-level statements WITHOUT the trailing ``return`` (marimo
    auto-generates each cell's return from the names it defines) and WITHOUT the
    ``@app.cell`` / ``def _()`` wrapper. Models often copy those back from the FILE
    source they see (which shows the wrapped, return-carrying form). This:
      * unwraps a single ``@app.cell``-decorated ``def _()`` if the model included it, and
      * strips a trailing top-level ``return ...`` (marimo regenerates it).
    Anything it can't confidently clean is returned untouched, so a genuinely broken
    cell still surfaces a clear error from :func:`_check_parses`.
    """
    text = code.strip("\n")
    if not text.strip():
        return code
    unwrapped = _unwrap_app_cell(text)
    if unwrapped is not None:
        text = unwrapped
    return _drop_trailing_return(text)


def _is_app_cell_decorator(dec) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    return isinstance(target, ast.Attribute) and target.attr == "cell"


def _unwrap_app_cell(code: str) -> str | None:
    """If ``code`` is exactly a ``@app.cell``-decorated ``def _(...)`` (the marimo
    wrapper the model may have pasted), return its dedented body; else ``None``.

    Strictly gated on the ``@app.cell`` decorator + the ``_`` name so a legitimate
    single-function cell (``def load_data(): ...``) is never unwrapped.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    if len(tree.body) != 1:
        return None
    node = tree.body[0]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "_":
        return None
    if not any(_is_app_cell_decorator(d) for d in node.decorator_list):
        return None
    segments = [ast.get_source_segment(code, stmt) for stmt in node.body]
    if not segments or any(seg is None for seg in segments):
        return None
    return "\n".join(s for s in segments if s is not None)


def _drop_trailing_return(code: str) -> str:
    """Strip a trailing top-level ``return ...`` (only when the result still parses).

    Wraps the body in a synthetic ``async def`` so a top-level return/await is legal
    to analyze, checks the LAST statement is a ``Return``, and cuts the original
    source from that statement on (handles a multi-line parenthesized return). Nested
    returns inside the cell's own ``def`` are untouched — only the cell's own trailing
    return is removed.
    """
    body_lines = code.split("\n")
    wrapped = "async def __mooring_cell__():\n" + "\n".join("    " + ln for ln in body_lines)
    try:
        tree = ast.parse(wrapped)
    except SyntaxError:
        return code  # can't analyze safely — leave it for _check_parses to report
    func = tree.body[0]
    assert isinstance(func, ast.AsyncFunctionDef)  # we wrapped the body in an async def
    if not func.body or not isinstance(func.body[-1], ast.Return):
        return code
    cut = func.body[-1].lineno - 2  # wrapped line N maps to body_lines index N-2
    if cut < 0:
        return code
    return "\n".join(body_lines[:cut]).rstrip("\n")


def read_cells(source: str) -> list[tuple[int, str]]:
    """The notebook's cells as ``(index, code)`` pairs, in document order. PURE.

    The indices are what an edit/delete op targets; the code strings are the exact
    anchors to capture. Raises like :func:`apply_cell_patch` on a too-old/missing
    marimo or an unparseable source.
    """
    _require_marimo_floor()
    _, MarimoConvert = _codegen_api()
    ir = _parse_ir(MarimoConvert, source)
    return [(i, cell.code) for i, cell in enumerate(ir.cells)]


def read_cells_checked(source: str) -> list[tuple[int, str]]:
    """:func:`read_cells`, but LOUD when marimo could not fully parse the source.

    marimo's converter never fails on bad input — it swallows what it cannot
    parse into the IR's "header" and records a violation, returning ZERO cells
    (verified against marimo 0.23.9: a syntax-error file yields ``cells == []``
    with ``violations == ['Only able to extract header.']``). For an edit
    target that lenience is fine; for a DIFF it silently drops content, so this
    variant raises ``ValueError`` whenever the IR carries violations. Callers
    degrade to a whole-file view instead of lying per cell (see celldiff).
    """
    _require_marimo_floor()
    _, MarimoConvert = _codegen_api()
    ir = _parse_ir(MarimoConvert, source)
    violations = list(getattr(ir, "violations", None) or ())
    if violations:
        raise ValueError(f"marimo could not fully parse the notebook source: {violations[0]}")
    return [(i, cell.code) for i, cell in enumerate(ir.cells)]


def read_notebook_frame(source: str) -> tuple[str, dict]:
    """Everything about a notebook that is NOT a cell: its header text (the leading
    comment block — a PEP 723 ``# /// script`` dependency pin lives here) and the
    ``marimo.App(...)`` options (width, app_title, css_file, …). PURE.

    A caller that rebuilds a notebook from cells must decide what happens to this
    frame explicitly; reading it back is how they compare two notebooks' frames.
    Raises like :func:`read_cells`.
    """
    _require_marimo_floor()
    _, MarimoConvert = _codegen_api()
    ir = _parse_ir(MarimoConvert, source)
    header = getattr(ir.header, "value", "") or ""
    options = dict(getattr(ir.app, "options", None) or {})
    return header, options


def compose_notebook(frame_source: str, picks) -> str:
    """Build notebook ``.py`` source from ``frame_source``'s frame (see
    :func:`read_notebook_frame`) and cells taken WHOLE from other notebooks. PURE.

    ``picks`` is an iterable of ``(notebook source, cell index)``. Each picked cell
    is carried over as the IR object it is, so it keeps its own name and its
    ``@app.cell(...)`` options — a cell the author deliberately marked
    ``disabled=True`` or ``hide_code=True`` must not silently lose that just because
    it came from the other side of a merge. (:func:`apply_cell_patch`'s
    ``replace_all`` cannot express this: it takes code strings, so a cell that is not
    byte-identical to one already in the target notebook is emitted with default
    name and options.)

    Raises :class:`MarimoTooOld`, :class:`MarimoTransportError`, or ``ValueError``
    (an unparseable source, an out-of-range pick, no cells, or a result that would
    not parse).
    """
    _require_marimo_floor()
    codegen, MarimoConvert = _codegen_api()
    ir = _parse_ir(MarimoConvert, frame_source)
    parsed: dict[str, list] = {}
    chosen = []
    for source, index in picks:
        cells = parsed.get(source)
        if cells is None:
            cells = list(_parse_ir(MarimoConvert, source).cells)
            parsed[source] = cells
        if not 0 <= index < len(cells):
            raise ValueError(f"cell {index} does not exist in that notebook")
        chosen.append(cells[index])
    if not chosen:
        raise ValueError("a notebook must have at least one cell")
    ir.cells[:] = chosen
    return _finish(codegen, MarimoConvert, ir)


def apply_cell_patch(source: str, ops) -> str:
    """Apply a list of :class:`CellOp` to notebook ``.py`` ``source``, returning the
    new source. PURE — no file IO; the private marimo IR object never escapes here.

    append/edit/delete may be combined (indices refer to the original order);
    ``replace_all`` is exclusive (a whole-notebook rewrite). The result is re-parsed
    before returning, because marimo's --watch SILENTLY IGNORES a malformed write —
    failing loud here beats writing something that no-ops in the editor.

    Raises :class:`MarimoTooOld`, :class:`MarimoTransportError`, :class:`CellPatchConflict`
    (a stale anchor / out-of-range index), or ``ValueError``/``SyntaxError`` (bad source,
    empty/duplicate op, or a result that would not parse).
    """
    _require_marimo_floor()
    codegen, MarimoConvert = _codegen_api()
    ir = _parse_ir(MarimoConvert, source)
    original = list(ir.cells)
    ops = list(ops)

    rewrites = [o for o in ops if o.op == "replace_all"]
    if rewrites:
        if len(ops) != 1:
            raise ValueError("a whole-notebook rewrite cannot be combined with other edits")
        return _apply_replace_all(codegen, MarimoConvert, ir, original, rewrites[0])

    edits, deletes, appends = _collect_ops(ops, original)

    new_cells = [
        _with_code(cell, edits[i]) if i in edits else cell
        for i, cell in enumerate(original)
        if i not in deletes
    ]
    cls = _cell_class(ir)
    new_cells.extend(_new_cell(cls, code) for code in appends)
    if not new_cells:
        raise ValueError("the patch would empty the notebook")
    ir.cells[:] = new_cells
    return _finish(codegen, MarimoConvert, ir)


def _apply_replace_all(codegen, MarimoConvert, ir, original, rewrite) -> str:
    """Whole-notebook rewrite: replace every cell with ``rewrite.cells``. Preserves a
    cell's NAME + config when its new code is byte-identical to an existing cell."""
    cls = _cell_class(ir)
    codes = [normalize_cell_code(str(c)) for c in rewrite.cells]
    codes = [c for c in codes if c.strip()]
    if not codes:
        raise ValueError("a rewrite must contain at least one cell")
    for code in codes:
        _check_parses(code)
    # Preserve a cell's NAME + config when its code is byte-identical to an existing
    # cell, so a rewrite that leaves a cell unchanged doesn't silently rename a
    # `def load_customers()` cell to `_`. New/changed cells get the default name.
    by_code = {}
    for cell in original:
        by_code.setdefault(cell.code, cell)
    ir.cells[:] = [_with_code(by_code[c], c) if c in by_code else _new_cell(cls, c) for c in codes]
    return _finish(codegen, MarimoConvert, ir)


def _collect_ops(ops, original) -> tuple[dict[int, str], set[int], list[str]]:
    """Validate and bucket append/edit/delete ops into (edits, deletes, appends).

    Enforces in-range indices, a carried anchor that still matches, and at most one
    operation per cell. Raises :class:`CellPatchConflict` (stale/out-of-range) or
    ``ValueError`` (bad or duplicate op).
    """
    edits: dict[int, str] = {}
    deletes: set[int] = set()
    appends: list[str] = []
    for o in ops:
        if o.op == "append":
            code = normalize_cell_code(o.code)
            if not code.strip():
                raise ValueError("an appended cell has no code")
            _check_parses(code)
            appends.append(code)
            continue
        if o.op not in ("edit", "delete"):
            raise ValueError(f"unknown cell operation: {o.op!r}")
        idx = o.index
        if not isinstance(idx, int) or not 0 <= idx < len(original):
            raise CellPatchConflict(
                f"cell {idx} no longer exists — the notebook changed since it was read"
            )
        if idx in edits or idx in deletes:
            raise ValueError(f"cell {idx} is targeted by more than one operation")
        # An edit/delete MUST carry the anchor it was proposed against — never clobber
        # a bare index (a missing anchor would defeat the whole conflict-detection
        # guarantee, e.g. on a stale re-send after the analyst reordered cells).
        if o.anchor is None:
            raise CellPatchConflict(
                f"cell {idx} {o.op} is missing its anchor — re-open the copilot"
            )
        if original[idx].code != o.anchor:
            raise CellPatchConflict(
                f"cell {idx} changed since it was read — re-open the copilot and try again"
            )
        if o.op == "edit":
            code = normalize_cell_code(o.code)
            if not code.strip():
                raise ValueError("an edited cell has no code")
            _check_parses(code)
            edits[idx] = code
        else:
            deletes.add(idx)
    return edits, deletes, appends


def _finish(codegen, MarimoConvert, ir) -> str:
    """Generate the file source from ``ir`` and assert it round-trips (parses)."""
    result = codegen.generate_filecontents_from_ir(ir)
    try:
        MarimoConvert.from_py(result).to_ir()
    except Exception as exc:  # noqa: BLE001  # any parse failure means a bad write
        raise ValueError(f"the edited notebook would not parse: {exc}") from exc
    return result


def append_cell_source(source: str, code: str) -> str:
    """Append a cell containing ``code`` to notebook ``.py`` ``source`` (a one-op
    :func:`apply_cell_patch`). Kept as the named seam ``cellwrite.append_cell`` uses."""
    return apply_cell_patch(source, [CellOp(op="append", code=code)])


# --- static validation of a candidate notebook -----------------------------

# mooring's OWN diagnostic codes. marimo's lint engine owns the `MB` (breaking),
# `MF` (formatting), `MR` (runtime) and `MW` (wasm) prefixes; `MOOR` collides with
# none of them, and cannot start to collide when a marimo upgrade adds rules.
DIAG_VALIDATOR_UNAVAILABLE = "MOOR000"
DIAG_CELL_SYNTAX = "MOOR001"
DIAG_NESTED_CELL = "MOOR002"
DIAG_UNRESOLVED_REFERENCE = "MOOR003"
DIAG_NOT_A_NOTEBOOK = "MOOR004"
DIAG_TOO_LARGE = "MOOR005"

# Ceilings, so the cost of a check on every propose has a hard bound. Measured on this
# repo's CI-target platform: a REALISTIC notebook (four statements a cell) costs ~2.8 ms
# per cell and stays linear — 100 cells 273 ms, 150 cells 420 ms, 300 cells 922 ms. But
# marimo's multiple-definitions rule is quadratic in colliding definitions, so the
# pathological shape (every cell defining the same name) runs away: 100 cells 278 ms,
# 150 cells 655 ms, 200 cells 1.34 s, and it keeps doubling. 150 cells caps the WORST
# shape at ~0.7 s while accepting essentially every real analysis notebook — a 150-cell
# marimo notebook is already an outlier. The byte ceiling covers the other shape a cell
# count cannot see: a handful of cells with enormous bodies.
VALIDATE_MAX_CELLS = 150
VALIDATE_MAX_BYTES = 512 * 1024

# A backstop under the ceilings, not the primary bound: at :data:`VALIDATE_MAX_CELLS`
# nothing measured comes near it, so it fires only if a marimo upgrade makes some rule
# far more expensive, or a shape nobody has thought of gets through. Generous enough
# (~7x the worst measured cost at the ceiling, on a machine ~2.5x slower than the one
# measured on) that it never truncates a pass that was going to finish, and short enough
# that a caller gets an answer rather than a hang.
VALIDATE_TIMEOUT_SECONDS = 5.0

# The marimo lint rules mooring runs on a candidate, as an explicit ALLOWLIST rather
# than "everything at severity X" — a marimo upgrade that adds a chatty new rule must
# not start rejecting valid proposals behind our back. All five are `breaking`: each
# one means the notebook does not work, not that it could be prettier.
#
#   MB001 unparsable-cells   MB002 multiple-definitions   MB003 cycle-dependencies
#   MB004 setup-cell-dependencies                         MB005 invalid-syntax
#
# Deliberately EXCLUDED (verified against marimo 0.23.9, see tests):
#   * MF001-MF004, MF006, MF007 — cosmetic (stdout in a cell, an empty cell, markdown
#     dedent). A checker that reports style trains the reader to skip its output.
#   * MF005 sql-parse-error — cannot fire on this path at all: it is driven by log
#     records marimo emits while doing SQL dependency analysis, which needs duckdb
#     installed (an optional extra here) and does not run during a pure IR parse.
#     Capturing marimo's logs around the parse yields zero records, so including it
#     would advertise a check that never runs.
#   * MR001 self-import — needs a real filename to compare against; a candidate is
#     validated in memory, with no path.
#   * MR002 branch-expression — fires on `if ok: mo.md(...) else: mo.md(...)`, which
#     is ordinary correct code. Pure noise, and exactly the false positive that
#     teaches a model to ignore diagnostics.
#   * MR003 reusable-definition-order — only bites when the notebook is imported as a
#     module/script, which is not how mooring runs notebooks.
#   * MW001-MW003 — WASM-only deployment concerns; mooring runs a local kernel.
VALIDATE_LINT_RULES = ("MB001", "MB002", "MB003", "MB004", "MB005")

# The two allowlisted rules that report "this does not parse" — the one fact mooring's
# own per-cell check states more precisely (see `_validate_notebook_source`).
_PARSE_RULES = frozenset({"MB001", "MB005"})

# Python's own builtins are legitimate references that no cell defines, so they can
# never be an unresolved name. Snapshotted once at import (the set is immutable for
# the life of the interpreter).
_BUILTIN_NAMES = frozenset(dir(builtins))

# marimo's graph builder wraps EVERY cell compile in `capture_output`, which is
# `contextlib.redirect_stdout`/`redirect_stderr` — process-global, not thread-safe and
# not reentrant. Two overlapping validations interleave the save/restore and leave
# `sys.stdout`/`sys.stderr` pointing at leaked StringIO buffers for the rest of the
# process's life: the hub console and every library warning silently go dead, with
# nothing raised and nothing logged. So the whole validator is serialized on this ONE
# lock, and the work itself always happens on the single worker thread below. Both are
# load-bearing: the lock stops two validations overlapping, the dedicated thread keeps
# marimo's redirect off the caller's thread (and off any thread that might have a
# running event loop). Serializing is free — the pass is CPU-bound, so concurrent calls
# were sharing one core's worth of work anyway. Pinned by test_notebook_validate.py.
_VALIDATE_LOCK = threading.Lock()

# The last pass that overran :data:`VALIDATE_TIMEOUT_SECONDS` and was left running, if
# any. It still owns marimo's redirect, so until it finishes NOBODY may start another
# pass — the lock alone cannot express that, because the caller that gave up has to
# release it to return. Holding the THREAD rather than a flag makes `is_alive()` the one
# source of truth: there is no flag to clear, so no way to leave the validator wedged by
# losing a race with a worker that finished a moment later. Guarded by the lock above.
_VALIDATE_ORPHAN: threading.Thread | None = None


@dataclass(frozen=True)
class Diagnostic:
    """One value-free finding about a candidate notebook (see
    :func:`validate_notebook_source`).

    ``code`` is either a marimo rule code (``MB002``) or one of mooring's own
    ``MOOR`` codes; ``name`` is its short slug (``multiple-definitions``);
    ``lines`` are 1-based line numbers in the candidate source; ``fix`` is static
    advice. Nothing here is ever a runtime value — nothing is run. But ``message`` and
    ``fix`` on a marimo-owned code are marimo's own words, forwarded verbatim, so read
    the two-tier note on :func:`validate_notebook_source` before sending one anywhere.
    """

    code: str
    name: str
    message: str
    lines: tuple[int, ...] = ()
    fix: str = ""


def validate_notebook_source(source: str) -> list[Diagnostic]:
    """Statically check a CANDIDATE notebook's ``.py`` source and describe what is
    broken about it. No file IO, no subprocess, no network — but not free of process
    state: it takes a module lock and runs on a worker thread (see the last paragraph).

    Built for the gap between "the model proposed a cell" and "the analyst clicked
    Apply": :func:`apply_cell_patch` only checks that each cell PARSES, so a proposal
    that duplicates a definition, closes a dependency cycle, references a name no cell
    defines, or pastes whole ``@app.cell`` blocks into a cell BODY writes cleanly and
    then breaks the notebook. Compose the candidate first (usually with
    :func:`apply_cell_patch`), pass the result here, and act on what comes back.

    Four classes of check, in the order they are reported:

    1. **Per-cell syntax** — the :func:`_check_parses` compile, per cell, so the
       report names the cell instead of the file. Keeps ``PyCF_ALLOW_TOP_LEVEL_AWAIT``,
       so a legitimate async cell is not rejected.
    2. **Nested ``@app.cell``** — a cell whose BODY contains ``@app.cell``-decorated
       defs. Valid Python, nonsense marimo, and marimo's own linter does not flag it.
    3. **marimo's linter**, restricted to :data:`VALIDATE_LINT_RULES`.
    4. **Unresolved references** — a name some cell reads that nothing in the notebook
       could bind. Deliberately the most conservative check in here (see
       :func:`_unresolved_diagnostics`): a false positive blocks a correct proposal and
       teaches its reader to distrust every other diagnostic, so this one skips itself
       whenever it cannot see the whole picture.

    **It never executes a line of the notebook.** Every check runs on the AST: marimo's
    converter parses the source, and marimo's own graph builder ``compile()``s each cell
    to read its defs/refs — compiling is not running, and no kernel, subprocess or
    websocket is involved. Nothing runs, so there is no runtime value anywhere for a
    diagnostic to carry.

    **The remaining exposure is quoted SOURCE, and it comes in two tiers.** A notebook's
    own text can hold a literal that is a value in every sense that matters — a
    hardcoded key, a customer's name — so "nothing executed" is not on its own the whole
    story:

    * mooring's own ``MOOR`` diagnostics are **structurally** value-free. Every field is
      built here from a rule code, a cell index, a line number, an identifier, or fix
      text written in this module. The one place CPython's own words are used,
      :func:`_syntax_detail`, deliberately reads ``SyntaxError.msg`` and never ``.text``,
      so the offending line cannot come with it.
    * marimo's ``MB`` diagnostics forward its ``message`` and ``fix`` **verbatim** (one
      ships a docs URL), and :data:`DIAG_VALIDATOR_UNAVAILABLE` embeds ``str(exc)`` from
      a marimo internal. No marimo rule quotes notebook text today — checked against
      secrets planted in the failing position of every failure class — but this module
      cannot promise that a future rule will not start, the way ruff's messages do.

    So treat the forwarded text as best-effort, not as a guarantee. A caller sending
    diagnostics OUT of the workspace (to a model, say) must put them through
    ``ai/egress.py`` like any other outbound text; that scrub belongs at the propose-tool
    boundary, not here — this module is below the egress layer and cannot reach it.

    **It never raises.** A malformed candidate, a marimo too old or missing its private
    APIs, an internal that moved — all degrade to diagnostics. A checker failure comes
    back as a single :data:`DIAG_VALIDATOR_UNAVAILABLE`, which says the CHECKER failed,
    not that the notebook is wrong; an empty list means "checked, nothing to report".

    **It BLOCKS the calling thread**, for a few hundred milliseconds on a large notebook
    (see :data:`VALIDATE_MAX_CELLS` for the measurements). The work runs on a private
    worker thread, but only so that marimo's non-thread-safe stdout redirect and its
    ``asyncio.run`` stay off the caller's thread — the caller still waits for the result.
    An async caller MUST NOT call this directly; use
    ``await asyncio.to_thread(validate_notebook_source, source)`` or the loop is stalled
    for the whole pass. Calls are serialized against each other (see
    :data:`_VALIDATE_LOCK`), so concurrent callers queue; a pass that overruns
    :data:`VALIDATE_TIMEOUT_SECONDS` is abandoned and reported as unavailable rather
    than waited on, and a notebook over the ceilings is declined outright.
    """
    global _VALIDATE_ORPHAN
    with _VALIDATE_LOCK:
        # A previous pass that overran is still inside marimo's output redirect. The
        # lock is free (its caller had to return), so this costs no wait — but starting
        # a second pass now is exactly the interleaving the lock exists to prevent.
        if _VALIDATE_ORPHAN is not None and _VALIDATE_ORPHAN.is_alive():
            return [_validator_unavailable("an earlier validation is still running")]
        _VALIDATE_ORPHAN = None
        outcome: dict = {}
        # A DAEMON thread: an abandoned pass must never hold up interpreter exit.
        worker = threading.Thread(
            target=_validate_into, args=(source, outcome), name="mooring-validate", daemon=True
        )
        worker.start()
        try:
            worker.join(VALIDATE_TIMEOUT_SECONDS)
        finally:
            # In a `finally` because a KeyboardInterrupt out of `join` releases the lock
            # just the same, and would otherwise leave the worker running unguarded.
            if worker.is_alive():
                _VALIDATE_ORPHAN = worker
        if worker.is_alive():  # a running thread cannot be cancelled; let it go
            return [
                _validator_unavailable(
                    f"it did not finish within {VALIDATE_TIMEOUT_SECONDS:.0f}s"
                )
            ]
    error = outcome.get("error")
    if error is not None:
        return [_validator_unavailable(error)]
    return outcome.get("result", [])


def _validate_into(source: str, outcome: dict) -> None:
    """Run the validation on this worker thread, reporting through ``outcome``.

    Nothing escapes: a checker that breaks its caller is worse than one that says
    nothing, and an exception here would be raised on a thread with nobody to catch it.
    """
    try:
        outcome["result"] = _validate_notebook_source(source)
    except BaseException as exc:  # noqa: BLE001  # incl. anything marimo's internals raise
        outcome["error"] = exc


def _validator_unavailable(detail) -> Diagnostic:
    """The one diagnostic that says the CHECKER failed, not that the notebook is wrong."""
    return Diagnostic(
        code=DIAG_VALIDATOR_UNAVAILABLE,
        name="validator-unavailable",
        message=f"mooring could not statically validate this notebook: {detail}",
        fix=(
            "This is a failure of the checker, not a fault found in the notebook — it "
            f"usually means the installed marimo is not the expected {MARIMO_FLOOR_STR}+, "
            "or the notebook is far larger than the checker is budgeted for. Review the "
            "change by hand."
        ),
    )


def _validate_notebook_source(source: str) -> list[Diagnostic]:
    """The body of :func:`validate_notebook_source`, free to raise. Runs on the
    validator's own worker thread, which is why ``asyncio.run`` below is always safe."""
    if len(source) > VALIDATE_MAX_BYTES:
        return [_too_large(f"it is {len(source) // 1024} KB", "512 KB")]
    _require_marimo_floor()
    _, MarimoConvert = _codegen_api()
    try:
        ir = _parse_ir(MarimoConvert, source)
    except ValueError as exc:
        return [_not_a_notebook(str(exc), ())]

    # Checked here rather than before the parse because the cell count needs the IR —
    # which is the cheap, linear part of the pass (a few ms even well past the ceiling).
    if len(ir.cells) > VALIDATE_MAX_CELLS:
        return [_too_large(f"it has {len(ir.cells)} cells", f"{VALIDATE_MAX_CELLS} cells")]

    violations = list(getattr(ir, "violations", None) or ())
    if not ir.cells:
        # marimo's converter never fails outright: it swallows what it cannot read into
        # the header and records a violation, leaving ZERO cells. Whole-file rejection
        # (a plain script, prose, truncated output) surfaces here rather than as silence.
        if violations or not getattr(ir, "valid", True):
            first = violations[0] if violations else None
            detail = getattr(first, "description", "") or "no cells were found"
            lines = _positive_lines([getattr(first, "lineno", 0)]) if first else ()
            return [_not_a_notebook(detail, lines)]
        return []

    syntax = _syntax_diagnostics(ir)
    nested = _nested_cell_diagnostics(ir)
    # marimo's two "this does not parse" rules restate, per FILE, what the per-cell check
    # above already said per CELL — so drop one only when it lands on a line we have
    # already reported. Both rules stay in the allowlist, because each also fires for
    # things our compile check cannot see (a `from x import *`, say), and a line-matched
    # drop keeps those. Erring here duplicates a diagnostic; it never hides one.
    lint = _lint_diagnostics(
        ir, source, suppress_lines=frozenset(line for d in syntax for line in d.lines)
    )
    # A cell that does not parse has invisible defs, so every later cell reading one of
    # them would look unresolved. Skip the whole check rather than emit false positives.
    unresolved = [] if syntax else _unresolved_diagnostics(ir, source)
    return syntax + nested + lint + unresolved


def _too_large(detail: str, ceiling: str) -> Diagnostic:
    """Declined, not cleared. Silence here would be indistinguishable from "checked,
    nothing wrong" — the exact confusion this validator exists to remove."""
    return Diagnostic(
        code=DIAG_TOO_LARGE,
        name="notebook-too-large",
        message=(
            f"mooring did not statically validate this notebook: {detail}, over the "
            f"{ceiling} the checker is budgeted for — it has NOT been checked"
        ),
        fix=(
            "Nothing is known to be wrong; nothing has been verified either. Review the "
            "change by hand, or split the notebook into smaller ones."
        ),
    )


def _not_a_notebook(detail: str, lines: tuple[int, ...]) -> Diagnostic:
    return Diagnostic(
        code=DIAG_NOT_A_NOTEBOOK,
        name="not-a-notebook",
        message=f"this is not a notebook marimo can read: {detail}",
        lines=lines,
        fix=(
            "A marimo notebook is `import marimo`, an `app = marimo.App()`, and "
            "`@app.cell`-decorated functions. Propose cell bodies for an existing "
            "notebook rather than writing the file from scratch."
        ),
    )


def _positive_lines(values) -> tuple[int, ...]:
    """The 1-based line numbers in ``values``, de-duplicated and sorted (drops 0/None)."""
    out = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value > 0:
            out.add(value)
    return tuple(sorted(out))


def _cell_lines(cell) -> tuple[int, ...]:
    """The one line a cell starts on in the candidate source (empty if unknown).

    A cell's start is what locates a finding for a reader; mapping a position INSIDE a
    dedented cell body back to the file would need per-cell-kind offset arithmetic that
    would be wrong more often than it would be useful.
    """
    return _positive_lines([getattr(cell, "lineno", 0)])


def _parse_cell_ast(code: str):
    """``code`` as an AST, tolerating a cell's top-level ``await`` — or ``None``.

    Uses the same ``PyCF_ALLOW_TOP_LEVEL_AWAIT`` as :func:`_check_parses`, via
    ``PyCF_ONLY_AST`` so nothing is executed and no code object is produced.
    """
    try:
        return compile(
            code, "<cell>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        )
    except (SyntaxError, ValueError):  # ValueError: source with null bytes
        return None


def _syntax_detail(code: str) -> str:
    """CPython's reason for ``code`` not parsing (``invalid syntax``, ``'return'
    outside function``), WITHOUT the offending source line.

    ``SyntaxError.msg`` is the reason alone; the source fragment lives on ``.text``,
    which is deliberately never read — a diagnostic describes what is wrong with the
    notebook, it never quotes the notebook. (``str(exc)`` would also append a
    ``<cell>, line N`` counted from the dedented cell body, contradicting the file line
    numbers a :class:`Diagnostic` carries.)
    """
    try:
        compile(code, "<cell>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    except SyntaxError as exc:
        return str(exc.msg or "invalid syntax")
    except ValueError:  # e.g. source containing null bytes
        return "it contains characters Python cannot compile"
    return "invalid syntax"


def _syntax_diagnostics(ir) -> list[Diagnostic]:
    """Check 1 — every cell parses, reported per cell.

    :func:`_check_parses` stays the single gate (so the ``PyCF_ALLOW_TOP_LEVEL_AWAIT``
    that keeps a legitimate async cell valid is defined in exactly one place); the
    message is rebuilt from :func:`_syntax_detail` so it names the CELL rather than a
    line number counted inside it.
    """
    out = []
    for index, cell in enumerate(ir.cells):
        try:
            _check_parses(cell.code)
        except ValueError:
            out.append(
                Diagnostic(
                    code=DIAG_CELL_SYNTAX,
                    name="cell-syntax-error",
                    message=f"cell {index} is not valid Python: {_syntax_detail(cell.code)}",
                    lines=_cell_lines(cell),
                    fix=(
                        "A cell body is plain top-level statements — no `@app.cell` or "
                        "`def _()` wrapper, no trailing `return`, and no `return` outside "
                        "a function of its own."
                    ),
                )
            )
    return out


def _is_nested_app_decorator(dec) -> bool:
    """True for ``@app.cell`` / ``@app.function`` / ``@app.class_definition`` (with or
    without call parens) — the decorators marimo's codegen writes at FILE level.

    Stricter than :func:`_is_app_cell_decorator`, which only looks at the attribute
    name: this one also requires the receiver to be the bare name ``app``, so a user's
    own ``registry.cell`` decorator inside a cell is never mistaken for marimo's.
    """
    target = dec.func if isinstance(dec, ast.Call) else dec
    return (
        isinstance(target, ast.Attribute)
        and target.attr in ("cell", "function", "class_definition")
        and isinstance(target.value, ast.Name)
        and target.value.id == "app"
    )


def _nested_cell_diagnostics(ir) -> list[Diagnostic]:
    """Check 2 — a cell body that itself contains ``@app.cell``-decorated defs.

    The classic weak-model mistake: two ``@app.cell`` blocks copied out of the file
    source and handed back as ONE cell body. It writes cleanly (:func:`_check_parses`
    only asks that the body be valid Python) and produces a cell that wraps two more
    decorated defs — which marimo then never runs as cells. marimo's own linter says
    nothing about it, so this check is mooring's.
    """
    out = []
    for index, cell in enumerate(ir.cells):
        tree = _parse_cell_ast(cell.code)
        if tree is None:
            continue  # already reported by the per-cell syntax check
        nested = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and any(_is_nested_app_decorator(dec) for dec in node.decorator_list)
            for node in ast.walk(tree)
        )
        if nested:
            out.append(
                Diagnostic(
                    code=DIAG_NESTED_CELL,
                    name="nested-cell-definition",
                    message=(
                        f"cell {index} contains `@app.cell`-decorated definitions in its "
                        "body — marimo will never run them as cells"
                    ),
                    lines=_cell_lines(cell),
                    fix=(
                        "Supply the cell BODY only, without the `@app.cell` decorator or "
                        "the `def _()` wrapper. To add more than one cell, use one "
                        "operation per cell."
                    ),
                )
            )
    return out


def _lint_diagnostics(ir, source: str, *, suppress_lines: frozenset[int]) -> list[Diagnostic]:
    """Check 3 — marimo's own linter, restricted to :data:`VALIDATE_LINT_RULES`.

    Runs entirely in memory: marimo's ``collect_messages`` wants file patterns, but the
    engine underneath takes the parsed notebook plus its source, so a candidate is never
    written to disk — nothing for a file watcher or the sync engine to see, and no
    temp-file cleanup to get wrong.
    """
    from marimo._lint.rule_engine import RuleEngine
    from marimo._lint.rules import RULE_CODES

    rules = [RULE_CODES[code]() for code in VALIDATE_LINT_RULES if code in RULE_CODES]
    if not rules:
        return []
    engine = RuleEngine(rules)
    # `asyncio.run` is safe unconditionally: this only ever runs on the validator's own
    # worker thread (see `validate_notebook_source`), which never has a running loop.
    raw = asyncio.run(engine.check_notebook(ir, source))
    out = []
    for found in raw:
        code = str(getattr(found, "code", "") or "")
        line = getattr(found, "line", None)
        lines = _positive_lines(line if isinstance(line, list) else [line])
        if code in _PARSE_RULES and lines and set(lines) <= suppress_lines:
            continue
        out.append(
            Diagnostic(
                code=code,
                name=str(getattr(found, "name", "") or ""),
                message=str(getattr(found, "message", "") or ""),
                lines=lines,
                fix=str(getattr(found, "fix", "") or ""),
            )
        )
    # The engine runs its rules concurrently and orders by severity + completion, so the
    # order varies run to run. Sort for a stable, diffable report.
    return sorted(out, key=lambda d: (d.code, d.lines, d.message))


def _has_star_import(tree) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)
        for node in ast.walk(tree)
    )


def _bound_names(tree) -> set[str]:
    """Every name bound ANYWHERE in the file, at any depth and in any scope.

    Deliberately crude and over-generous: it is the safety net under the unresolved-name
    check, so it must never miss a binding. It counts assignment/walrus/for/with/except
    targets, ``del``, imports, function/class/argument names, ``global``/``nonlocal``
    declarations, match patterns, and PEP 695 type parameters — including bindings the
    dependency graph correctly treats as cell-local or function-local, and bindings in
    the file's own header. Anything in here is treated as resolvable, so the only names
    ever reported are those nothing in the notebook could possibly bind.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                names.add(node.name)
        elif isinstance(node, ast.MatchAs):
            if node.name:
                names.add(node.name)
        elif isinstance(node, ast.MatchStar):
            if node.name:
                names.add(node.name)
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                names.add(node.rest)
        else:
            # PEP 695 type parameters (`def f[T]()`, `type X[T] = ...`), which exist as
            # concrete node classes only on the Python versions that have them.
            type_param = getattr(ast, "type_param", None)
            if type_param is not None and isinstance(node, type_param):
                name = getattr(node, "name", None)
                if isinstance(name, str):
                    names.add(name)
    return names


# A verbatim mirror of marimo's own `valid_sql_calls` (`marimo/_ast/visitor.py`): a
# one-argument call to one of these, on a bare module name, is what makes marimo treat
# a cell as SQL and synthesise table refs for it. Mirroring the exact list rather than
# matching any `.sql(...)` keeps the fallback below precise — a SQLAlchemy `conn.sql()`
# produces no synthetic refs, so exempting it would only lose coverage.
_SQL_CALLS = frozenset({"marimo.sql", "mo.sql", "duckdb.execute", "duckdb.sql"})


def _looks_like_a_sql_cell(code: str) -> bool:
    """True if ``code`` contains a marimo-shaped SQL call — the fallback for a future
    marimo that stops reporting a cell's language."""
    tree = _parse_cell_ast(code)
    if tree is None:
        return False
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and f"{node.func.value.id}.{node.func.attr}" in _SQL_CALLS
        and len(node.args) == 1
        for node in ast.walk(tree)
    )


def _sql_names(cell, code: str) -> set[str] | None:
    """The refs a cell's SQL contributed — or ``None`` if they cannot be told apart.

    ``mo.sql("SELECT * FROM customers")`` makes marimo record ``customers`` as a cell
    REF, because a marimo SQL cell can read a dataframe defined in another cell by name.
    When the table is a real database table instead, nothing in the notebook defines it
    and it would look like an unresolved Python name — a false positive on a cell
    mooring's own SQL guide told the model to write, and one that only appears once
    duckdb and sqlglot are installed. (Qualified names are worse still:
    ``sales.public.orders`` is not even a Python identifier.)

    marimo itemises them for us on ``CellImpl.sql_refs``, which leaves a SQL cell's
    genuine Python refs (an f-string interpolation, say) still checked. ``None`` is the
    conservative answer for a cell that looks like SQL but whose table names marimo did
    not itemise — a future marimo that drops ``sql_refs`` must cost the check, not the
    caller's trust.
    """
    names = getattr(cell, "sql_refs", None)
    if names is not None:
        return set(names)
    if getattr(cell, "language", "python") != "python" or _looks_like_a_sql_cell(code):
        return None
    return set()


def _unresolved_diagnostics(ir, source: str) -> list[Diagnostic]:
    """Check 4 — a name a cell reads that nothing in the notebook could bind.

    marimo's dataflow graph gives each cell its ``defs`` and ``refs`` from static AST
    analysis, so a ``ref`` no cell ``def``ines is a ``NameError`` waiting to happen —
    the third failure mode a weak model reaches Apply with. But a wrong hit here is far
    worse than a miss: it blocks a correct proposal and teaches its reader that the
    diagnostics are noise. So the check disqualifies ITSELF whenever it cannot see the
    whole picture, and returns nothing at all rather than guess:

    * **any cell marimo could not parse** — its defs are invisible, so every reader of
      one of them would look unresolved;
    * **any ``from x import *``** — the names it binds are unknowable statically. (It is
      also why the guard below matters: marimo drops a star-importing cell from the
      graph entirely, taking its defs with it.);
    * **any cell missing from the graph** — a cell that fails marimo's own compile step
      (``return`` at cell top level, a star import) is silently absent, so the cell
      count must match exactly before any conclusion is drawn;
    * **a file that does not parse as a whole** — the binding scan needs one AST.

    What survives all that is then filtered four more ways:

    * **builtins** — empirically marimo's ``refs`` DO include ``len``, ``print``,
      ``range``, ``ValueError``; they are not pre-filtered;
    * **dunders** — ``__file__`` and ``__name__`` are real refs that no cell binds
      (mooring's own tie-out receipts key on ``__file__``);
    * **:func:`_bound_names`** — every name bound anywhere in the file, which covers
      header-level imports and function-local names the graph rightly hides;
    * **anything a cell's SQL contributed** — see :func:`_sql_names`. A ``mo.sql`` cell's
      refs include the TABLES its query reads, which are not Python names at all.

    Then, as a last backstop, anything that is not a Python identifier is dropped. A
    real ``NameError`` can only ever come from an identifier, so a ref like
    ``sales.public.orders`` is by definition something marimo synthesised rather than a
    name Python will look up. A hallucinated name is bound nowhere and IS an identifier,
    so it still surfaces. This guard is unconditional and deliberately not tied to the
    SQL path: an audit of marimo 0.23.9 found four ``_add_ref`` call sites, of which
    three pass a real ``ast.Name.id`` / ``ast.Global`` name and only the SQL branch
    synthesises — but the guard costs nothing and holds for whatever a later marimo
    invents.

    Any failure inside marimo's graph builder is swallowed: this check is an extra, and
    losing it must not cost the caller the other three.
    """
    from marimo._lint.context import LintContext
    from marimo._schemas.serialization import UnparsableCell

    if any(isinstance(cell, UnparsableCell) for cell in ir.cells):
        return []
    file_tree = _parse_cell_ast(source)
    if file_tree is None or _has_star_import(file_tree):
        return []
    try:
        graph = LintContext(ir, source).get_graph()
        cells = list(graph.cells.values())
        if len(cells) != len(ir.cells):
            return []
        defined: set[str] = set()
        for cell in cells:
            defined |= set(cell.defs)
        resolvable = defined | _BUILTIN_NAMES | _bound_names(file_tree)
        refs = []
        for cell, ir_cell in zip(cells, ir.cells):
            sql_names = _sql_names(cell, ir_cell.code)
            if sql_names is None:
                refs.append([])  # a SQL cell whose table names marimo did not itemise
            else:
                refs.append(sorted(set(cell.refs) - sql_names))
    except Exception:  # noqa: BLE001  # a private graph API that moved must not fire
        return []

    out = []
    for index, (cell_refs, ir_cell) in enumerate(zip(refs, ir.cells)):
        missing = [
            name
            for name in cell_refs
            if name not in resolvable
            and name.isidentifier()
            and not (name.startswith("__") and name.endswith("__"))
        ]
        if missing:
            out.append(
                Diagnostic(
                    code=DIAG_UNRESOLVED_REFERENCE,
                    name="unresolved-reference",
                    message=(
                        f"cell {index} uses {', '.join(missing)}, which no cell defines and "
                        "nothing in the notebook imports — running it would raise NameError"
                    ),
                    lines=_cell_lines(ir_cell),
                    fix=(
                        "Define the name in this or another cell, import it, or use a name "
                        "the notebook already defines."
                    ),
                )
            )
    return out


# --- HTTP control API: read live-kernel schemas ----------------------------

# marimo serves the skew-protection token in a dedicated element:
#   <marimo-server-token data-token="..." hidden></marimo-server-token>
# (verified against marimo 0.23.9). This is authoritative — an empty token means
# skew protection is off, so use it as-is. The JS-blob patterns are fallbacks for
# other marimo builds.
_MARIMO_TOKEN_RE = re.compile(r"<marimo-server-token[^>]*\bdata-token=\"([^\"]*)\"", re.IGNORECASE)
_SERVER_TOKEN_RES = (
    re.compile(r"serverToken[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
    re.compile(r"serverToken[\"']?\s*[:=]\s*([A-Za-z0-9_\-]+)", re.IGNORECASE),
    re.compile(r"server[_-]?token[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-]+)", re.IGNORECASE),
)


def extract_server_token(html: str) -> str:
    m = _MARIMO_TOKEN_RE.search(html)
    if m:
        return m.group(1)
    for pat in _SERVER_TOKEN_RES:
        m = pat.search(html)
        if m:
            return m.group(1)
    return ""


class KernelControl:
    """Minimal client for marimo's authenticated HTTP control API (localhost).

    Mirrors scripts/spike_marimo_http_control.py: scrape the skew (server) token
    from the served HTML, discover the notebook's session id, then run code in
    the kernel. It never opens the websocket, so it never receives an output.
    URL/transport failures surface as :class:`MarimoTransportError`.
    """

    def __init__(self, port: int, token: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        _require_marimo_floor()
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        self.timeout = timeout
        self._server_token: str | None = None
        # marimo's "/" handler 303-redirects to strip ?access_token, setting an
        # auth cookie on that redirect; we must keep the cookie across the follow
        # to land on the authenticated (token-bearing) page, exactly as a browser
        # does. A plain urlopen drops it and lands on the login page.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _get(self, path: str, params: dict | None = None) -> str:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)  # noqa: S310  # localhost only
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:  # noqa: S310
                return resp.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise MarimoTransportError(f"marimo GET {path} failed: {exc}") from exc

    def _post(self, path: str, headers: dict, json_body: dict | None = None) -> tuple[int, str]:
        if json_body is None:
            data = b""
        else:
            data = json.dumps(json_body).encode("utf-8")
            headers = {**headers, "Content-Type": "application/json"}
        req = urllib.request.Request(self.base + path, data=data, method="POST")  # noqa: S310
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise MarimoTransportError(f"marimo POST {path} failed: {exc}") from exc

    def _server_token_value(self) -> str:
        if self._server_token is None:
            self._server_token = extract_server_token(self._get("/", {"access_token": self.token}))
        return self._server_token

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Marimo-Server-Token": self._server_token_value(),
        }

    def session_for(self, notebook_rel: str) -> str | None:
        """The marimo session id serving ``notebook_rel`` (None if not open)."""
        status, body = self._post("/api/home/running_notebooks", self._auth_headers())
        if status != 200:
            return None
        files = json.loads(body).get("files", [])
        target = notebook_rel.replace("\\", "/").lstrip("./")
        target_name = Path(target).name
        for f in files:
            path = str(f.get("path", "")).replace("\\", "/")
            if path.endswith(target) or Path(path).name == target_name:
                sid = f.get("sessionId")
                return str(sid) if sid else None
        return None

    def run(self, session_id: str, code: str, *, cell_id: str = PROBE_CELL_ID) -> None:
        headers = {**self._auth_headers(), "Marimo-Session-Id": session_id}
        self._post("/api/kernel/run", headers, {"cellIds": [cell_id], "codes": [code]})
