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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mooring import pyproject_env

STARTUP_TIMEOUT = 30.0


def _force_frozen() -> bool:
    return os.environ.get("MOORING_FORCE_FROZEN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


class EditorError(Exception):
    pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class EditorServer:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.port: int | None = None
        self.token = secrets.token_urlsafe(16)
        self._proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def use_uv(self) -> bool:
        """Whether to launch via the team's locked uv project rather than the
        frozen bundle."""
        return (
            not _force_frozen()
            and pyproject_env.uv_available()
            and pyproject_env.has_pyproject(self.workspace)
        )

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
        self.port = _free_port()
        cmd, env = self._invocation()
        self._proc = subprocess.Popen(cmd, cwd=str(self.workspace), env=env)
        self._wait_ready()

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
