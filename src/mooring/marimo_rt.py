"""The marimo transport seam — the one module allowed to touch marimo's internals.

mooring couples to two volatile, undocumented marimo surfaces:

* the **private codegen** API (``marimo._ast.codegen``,
  ``marimo._convert.converters.MarimoConvert``, ``marimo._schemas.serialization``)
  used to append a cell to a notebook's ``.py`` source, and
* the **HTTP control API** (the ``<marimo-server-token>`` scrape, the
  ``/?access_token`` 303+cookie dance, ``/api/home/running_notebooks``,
  ``/api/kernel/run``) used to read live-kernel schemas.

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
import http.cookiejar
import json
import re
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
    except Exception as exc:  # noqa: BLE001 - marimo parse failures surface as ValueError
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
    return "\n".join(segments)


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
        cls = _cell_class(ir)
        codes = [normalize_cell_code(str(c)) for c in rewrites[0].cells]
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
        ir.cells[:] = [
            _with_code(by_code[c], c) if c in by_code else _new_cell(cls, c) for c in codes
        ]
        return _finish(codegen, MarimoConvert, ir)

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
            raise CellPatchConflict(f"cell {idx} {o.op} is missing its anchor — re-open the copilot")
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


def _finish(codegen, MarimoConvert, ir) -> str:
    """Generate the file source from ``ir`` and assert it round-trips (parses)."""
    result = codegen.generate_filecontents_from_ir(ir)
    try:
        MarimoConvert.from_py(result).to_ir()
    except Exception as exc:  # noqa: BLE001 - any parse failure means a bad write
        raise ValueError(f"the edited notebook would not parse: {exc}") from exc
    return result


def append_cell_source(source: str, code: str) -> str:
    """Append a cell containing ``code`` to notebook ``.py`` ``source`` (a one-op
    :func:`apply_cell_patch`). Kept as the named seam ``cellwrite.append_cell`` uses."""
    return apply_cell_patch(source, [CellOp(op="append", code=code)])


# --- HTTP control API: read live-kernel schemas ----------------------------

# marimo serves the skew-protection token in a dedicated element:
#   <marimo-server-token data-token="..." hidden></marimo-server-token>
# (verified against marimo 0.23.9). This is authoritative — an empty token means
# skew protection is off, so use it as-is. The JS-blob patterns are fallbacks for
# other marimo builds.
_MARIMO_TOKEN_RE = re.compile(
    r"<marimo-server-token[^>]*\bdata-token=\"([^\"]*)\"", re.IGNORECASE
)
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
        req = urllib.request.Request(url)  # noqa: S310 - localhost only
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
