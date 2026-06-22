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

from mooring import pyproject_env

STARTUP_TIMEOUT = 30.0


def _force_frozen() -> bool:
    return os.environ.get("MOORING_FORCE_FROZEN", "").strip().lower() in (
        "1", "true", "yes", "on",
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
        if not self.use_uv():
            return [sys.executable, "-m", "marimo", *marimo_args], None
        run = ["uv", "run"]
        if pyproject_env.lock_path(self.workspace).is_file():
            run.append("--frozen")
        run += ["--project", str(self.workspace)]
        if not pyproject_env.declares(self.workspace, "marimo"):
            run += ["--with", "marimo"]  # safety net: always startable
        run.append("marimo")
        # Drop the bundled-site-packages PYTHONPATH so it can't shadow the
        # project env uv builds; uv's venv is self-contained.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return [*run, *marimo_args], env

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
        """Write the workspace ``.marimo.toml`` mooring relies on, for every editor.

        Three things:
        1. Turn marimo's OWN AI off (``ai.enabled``/``completion.copilot`` =
           false). marimo's built-in AI would send real column *sample values* to
           whatever model is configured — a data-confidentiality leak outside
           mooring's control. mooring never uses marimo's AI (its copilot is
           schema-only and value-blind).
        2. ``runtime.watcher_on_save = "autorun"`` so that when the copilot applies
           a cell (by writing the .py source), ``--watch`` reloads AND runs it —
           matching the "Apply = add + run" behaviour.
        3. ``display.theme`` = the hub's appearance (``self.theme``) so notebooks
           open in the same light/dark/system theme the user picked on the hub.
           mooring owns this key: the hub is the single control point, so a value
           set here intentionally overrides marimo's own appearance toggle.

        marimo resolves its user config from the first ``.marimo.toml`` found
        searching the cwd (the workspace) upward, so a file written here wins over
        any personal ``~/.marimo.toml``. It is a dotfile, so sync never uploads
        it. Residual: a ``[tool.marimo.ai]`` section committed to the repo's
        ``pyproject.toml`` is a higher-precedence project override — see
        docs/admins/ai-privacy.md. Best-effort: never block the editor on it.
        """
        path = self.workspace / ".marimo.toml"
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
            already = (
                ai.get("enabled") is False
                and completion.get("copilot") is False
                and runtime.get("watcher_on_save") == "autorun"
                and display.get("theme") == self.theme
            )
            if already:
                return  # nothing to change — don't rewrite the file
            ai["enabled"] = False
            completion["copilot"] = False
            runtime["watcher_on_save"] = "autorun"
            display["theme"] = self.theme
            # Write atomically: apply_theme() can rewrite this WHILE marimo is
            # running, and marimo re-reads the file on every page render. A
            # truncated read would make marimo fall back to its AI-on default —
            # momentarily re-enabling the value-leaking built-in AI this very
            # file disables. os.replace swaps it in one step, so a render sees
            # either the old or the new file, never a partial one.
            tmp = path.parent / (path.name + ".tmp")
            tmp.write_text(tomli_w.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, tomllib.TOMLDecodeError):
            pass

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
                raise EditorError(
                    f"marimo exited during startup (code {self._proc.returncode})."
                )
            try:
                urllib.request.urlopen(url, timeout=1)  # noqa: S310 - localhost only
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
