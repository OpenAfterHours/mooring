"""Manage the marimo editor as a subprocess.

marimo has no programmatic edit-mode API (only run mode), so we spawn a single
directory-mode server and open individual notebooks via its `?file=` URL
parameter. The launch backend is chosen by capability:

- uv available + a repo `pyproject.toml` → `uv run --frozen --project <ws> marimo
  edit <ws>`, so notebooks run in the team's locked dependency env (see
  pyproject_env). The bundled-site-packages PYTHONPATH bridge is stripped for this
  subprocess so it can't shadow the project env.
- otherwise → `python -m marimo edit <ws>` against the frozen bundle. cli.main()
  puts the bundled site-packages on PYTHONPATH first, so this subprocess — and the
  kernels marimo spawns — can import everything even from a moonlit zipapp.

`MOORING_FORCE_FROZEN=1` forces the frozen path regardless of uv.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import tomli_w

from mooring import checks, inputs, connections, paths, pyproject_env

STARTUP_TIMEOUT = 30.0


def _force_frozen() -> bool:
    return os.environ.get("MOORING_FORCE_FROZEN", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


class EditorError(Exception):
    pass


def uses_uv(workspace: Path) -> bool:
    """Whether notebooks in ``workspace`` launch via the team's locked uv project
    rather than the frozen bundle: uv on PATH, a workspace ``pyproject.toml``, and
    not force-frozen. The single source of truth for the launch-backend decision —
    shared by :class:`EditorServer` and the hub's notebook-packages footer."""
    return (
        not _force_frozen()
        and pyproject_env.uv_available()
        and pyproject_env.has_pyproject(workspace)
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def bind_or_free(preferred: int) -> int:
    """Return ``preferred`` if it's free to bind on 127.0.0.1, else a random free
    port. Lets a caller prefer a *stable* port (so the browser origin — and thus
    its per-origin localStorage — is the same each launch) while still yielding
    gracefully when something already holds it."""
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            return free_port()
        return sock.getsockname()[1]


def _launch_prefix(workspace: Path) -> tuple[list[str], dict[str, str] | None]:
    """The command prefix that runs ``marimo`` in the right environment for
    ``workspace``, plus an optional env override (None = inherit).

    Shared by the edit server (:meth:`EditorServer._invocation`) and the one-shot
    HTML export (:func:`export_html_command`), so both pick the same backend: the
    team's locked uv project when available, else the frozen bundle. On the uv path
    the bundled-site-packages ``PYTHONPATH`` bridge is stripped so it can't shadow
    the project env uv builds (uv's venv is self-contained).
    """
    if not uses_uv(workspace):
        return [sys.executable, "-m", "marimo"], None
    run = ["uv", "run"]
    if pyproject_env.lock_path(workspace).is_file():
        run.append("--frozen")
    run += ["--project", str(workspace)]
    if not pyproject_env.declares(workspace, "marimo"):
        run += ["--with", "marimo"]  # safety net: always startable
    run.append("marimo")
    # Drop the bundled-site-packages PYTHONPATH so it can't shadow the project env
    # uv builds; uv's venv is self-contained.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return run, env


def export_html_command(
    workspace: Path,
    notebook_rel: str,
    out_path: Path,
    *,
    include_code: bool = False,
) -> tuple[list[str], dict[str, str] | None]:
    """Command (and optional env override) to render ``notebook_rel`` to a
    self-contained HTML file at ``out_path`` via ``marimo export html``.

    Runs in the SAME backend as the editor (uv project or frozen bundle), so the
    notebook executes against the team's locked deps. ``--no-include-code`` hides
    the source for a stakeholder-facing snapshot; ``-f`` overwrites an existing
    file. The notebook executes LOCALLY to capture its outputs — the data values it
    reads never leave the machine (the rendered HTML lands in the sync-excluded
    ``.mooring`` outbox, see :mod:`mooring.app.deliver`).
    """
    prefix, env = _launch_prefix(workspace)
    args = [
        "export",
        "html",
        notebook_rel.replace("\\", "/"),
        "-o",
        str(out_path),
        "-f",
        "--include-code" if include_code else "--no-include-code",
    ]
    return [*prefix, *args], env


def ensure_runtime_config(workspace: Path, *, theme: str | None = None) -> None:
    """Write the workspace ``.marimo.toml`` mooring relies on, and install the
    value-free checks runtime. Idempotent, atomic, and best-effort (never raises).

    Five things:

    1. Turn marimo's OWN AI off (``ai.enabled``/``completion.copilot`` = false).
       marimo's built-in AI would send real column *sample values* to whatever
       model is configured — a data-confidentiality leak outside mooring's control.
       mooring never uses marimo's AI (its copilot is schema-only and value-blind).
    2. ``runtime.watcher_on_save = "autorun"`` so that when the copilot applies a
       cell (by writing the .py source), ``--watch`` reloads AND runs it — matching
       the "Apply = add + run" behaviour.
    3. ``runtime.pythonpath`` = the workspace root **and** the ``.mooring/pylib``
       dir, so a notebook in any sub-folder can import the repo's shared helper
       modules AND ``import mooring_checks`` (the injected value-free tie-out
       helper — see :mod:`mooring.checks`). marimo only auto-adds the notebook's own
       directory to ``sys.path``; ``runtime.pythonpath`` is its sanctioned fix
       (inserted at the head of ``sys.path`` at kernel init). ABSOLUTE paths —
       marimo does NOT resolve a ``.marimo.toml`` pythonpath entry — and any
       existing entries are preserved.
    4. ``display.theme`` = ``theme`` (the hub's appearance) so notebooks open in the
       theme the user picked. mooring owns this key: the hub is the single control
       point. When ``theme`` is None the existing value is PRESERVED (used by the
       one-shot HTML export, which must not disturb an open editor's theme).

    marimo resolves its user config from the first ``.marimo.toml`` found searching
    the cwd (the workspace) upward, so a file written here wins over any personal
    ``~/.marimo.toml``. It is a dotfile, so sync never uploads it.
    """
    checks.install_runtime(workspace)  # best-effort; keeps mooring_checks importable
    inputs.install_runtime(workspace)  # and mooring_inputs (input fingerprints)
    connections.install_runtime(workspace)  # and mooring_connections (shape + local secret)
    path = workspace / ".marimo.toml"
    try:
        data: dict = {}
        if path.is_file():
            data = tomllib.loads(path.read_text("utf-8"))
        ai = data.get("ai")
        completion = data.get("completion")
        runtime = data.get("runtime")
        display = data.get("display")
        if not isinstance(ai, dict):
            ai = data["ai"] = {}
        if not isinstance(completion, dict):
            completion = data["completion"] = {}
        if not isinstance(runtime, dict):
            runtime = data["runtime"] = {}
        if not isinstance(display, dict):
            display = data["display"] = {}
        ws_root = str(workspace.resolve())
        pylib = str(checks.pylib_dir(workspace).resolve())
        raw_pp = runtime.get("pythonpath")
        existing_pp = [p for p in raw_pp if isinstance(p, str)] if isinstance(raw_pp, list) else []
        heads = [ws_root, pylib]
        desired_pp = [*heads, *(p for p in existing_pp if p not in heads)]
        # Theme: mooring owns display.theme when a theme is GIVEN (the hub is the
        # single control point). theme=None means "leave display.theme untouched" —
        # used by the one-shot HTML export, which must never disturb an open editor's
        # appearance (nor introduce a theme key on a workspace that had none).
        theme_ok = theme is None or display.get("theme") == theme
        already = (
            ai.get("enabled") is False
            and completion.get("copilot") is False
            and runtime.get("watcher_on_save") == "autorun"
            and runtime.get("pythonpath") == desired_pp
            and theme_ok
        )
        if already:
            return  # nothing to change — don't rewrite the file
        ai["enabled"] = False
        completion["copilot"] = False
        runtime["watcher_on_save"] = "autorun"
        runtime["pythonpath"] = desired_pp
        if theme is not None:
            display["theme"] = theme
        # Write atomically through a UNIQUE temp file (safe_write_text uses mkstemp):
        # apply_theme() runs on the hub's event loop while a Deliver export runs on a
        # threadpool worker, so both can reach this concurrently — a FIXED tmp name
        # could interleave into a corrupt config, which would make marimo fall back to
        # its AI-on default (re-enabling the value-leaking built-in AI). Distinct tmp
        # files keep each os.replace atomic (last writer wins, never a partial file).
        paths.safe_write_text(path, tomli_w.dumps(data))
    except (OSError, tomllib.TOMLDecodeError):
        pass


class EditorServer:
    def __init__(self, workspace: Path, theme: str = "system") -> None:
        self.workspace = workspace
        # The appearance mooring writes into this workspace's .marimo.toml so
        # notebooks open in the same theme as the hub. Updated live by the hub
        # via apply_theme() when the user switches the toggle.
        self.theme = theme
        self.port: int | None = None
        self.token = secrets.token_urlsafe(16)
        self._proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def use_uv(self) -> bool:
        """Whether to launch via the team's locked uv project rather than the
        frozen bundle."""
        return uses_uv(self.workspace)

    def _invocation(self) -> tuple[list[str], dict[str, str] | None]:
        """The launch command and an optional env override (None = inherit)."""
        prefix, env = _launch_prefix(self.workspace)
        marimo_args = [
            "edit",
            str(self.workspace),
            "--headless",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--token-password",
            self.token,
            "--skip-update-check",
            # Watch the .py files: when the AI copilot applies a cell by writing
            # the notebook source, marimo reloads it and the cell appears in the
            # open tab. (See ai/cellwrite.py and docs/admins/ai-privacy.md.)
            "--watch",
        ]
        return [*prefix, *marimo_args], env

    def ensure_started(self) -> None:
        if self.running:
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._ensure_marimo_config()
        self.port = free_port()
        cmd, env = self._invocation()
        kwargs: dict = {}
        if sys.platform == "win32":
            # A console Ctrl+C is received by EVERY process attached to that console,
            # so without this flag it hits mooring, marimo, AND marimo's kernel
            # children at once — they fight over the console and the terminal is left
            # wedged (the prompt never returns; you have to close the window).
            # CREATE_NEW_PROCESS_GROUP puts marimo's whole subtree in a new group, for
            # which Windows DISABLES Ctrl+C: marimo and its descendants ignore the
            # keystroke, so only mooring handles it. mooring then tears the marimo tree
            # down deliberately via taskkill /T in shutdown() (which walks the PID
            # tree, so the new group membership doesn't affect the kill).
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self._proc = subprocess.Popen(cmd, cwd=str(self.workspace), env=env, **kwargs)
        self._wait_ready()

    def _ensure_marimo_config(self) -> None:
        """Write this workspace's ``.marimo.toml`` for the editor's current theme
        and install the value-free checks runtime — see
        :func:`ensure_runtime_config`."""
        ensure_runtime_config(self.workspace, theme=self.theme)

    def apply_theme(self, theme: str) -> None:
        """Re-theme this workspace's notebooks: update ``self.theme`` and rewrite
        ``.marimo.toml``. marimo re-reads its config on each page render, so a
        notebook opened (or reloaded) after this picks up the new theme without
        restarting the editor subprocess. Best-effort — never raises."""
        self.theme = theme
        self._ensure_marimo_config()

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        url = f"http://127.0.0.1:{self.port}/"
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise EditorError(f"marimo exited during startup (code {self._proc.returncode}).")
            try:
                urllib.request.urlopen(url, timeout=1)  # noqa: S310  # localhost only
                return
            except urllib.error.HTTPError:
                return  # any HTTP response (401 included) means the server is up
            except (urllib.error.URLError, OSError, TimeoutError):
                time.sleep(0.25)
        raise EditorError("marimo did not become ready in time.")

    def url_for(self, rel_path: str) -> str:
        if not self.running:
            raise EditorError("Editor is not running.")
        query = urllib.parse.urlencode(
            {"file": rel_path.replace("\\", "/"), "access_token": self.token}
        )
        return f"http://127.0.0.1:{self.port}/?{query}"

    def wait(self) -> None:
        if self._proc is not None:
            self._proc.wait()

    def shutdown(self) -> None:
        if not self.running:
            return
        proc = self._proc
        assert proc is not None  # self.running guarantees a live process
        if sys.platform == "win32":
            # TerminateProcess would orphan marimo's kernel children; kill the tree.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
