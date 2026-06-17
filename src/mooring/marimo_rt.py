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

import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
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


# --- private-codegen: append a cell to notebook source ---------------------


def append_cell_source(source: str, code: str) -> str:
    """Append a cell containing ``code`` to marimo notebook ``.py`` ``source``,
    returning the new source. PURE — no file IO; the private marimo IR object
    never escapes this function.

    Raises :class:`MarimoTooOld` if marimo is too old, :class:`MarimoTransportError`
    if the private codegen API is unavailable (a future marimo moved it), and
    ``ValueError``/``SyntaxError`` if the source can't be parsed.
    """
    _require_marimo_floor()
    try:
        from marimo._ast import codegen
        from marimo._convert.converters import MarimoConvert
    except ImportError as exc:  # marimo present + new enough, but the private API moved
        raise MarimoTransportError(f"marimo codegen API unavailable: {exc}") from exc
    ir = MarimoConvert.from_py(source).to_ir()
    ir.cells.append(_new_cell(ir, code))
    return codegen.generate_filecontents_from_ir(ir)


def _new_cell(ir, code: str):
    """A fresh CellDef for ``code`` (reuse the notebook's cell class, or import it)."""
    if ir.cells:
        cell_cls = type(ir.cells[0])
    else:  # empty notebook — import the class directly
        from marimo._schemas.serialization import CellDef as cell_cls
    return cell_cls(code=code, name="_")


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
