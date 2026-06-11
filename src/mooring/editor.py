"""Manage the marimo editor as a subprocess.

marimo has no programmatic edit-mode API (only run mode), so we spawn
`python -m marimo edit <workspace>` as a single directory-mode server and
open individual notebooks via its `?file=` URL parameter. cli.main() puts the
bundled site-packages on PYTHONPATH before anything runs, so this subprocess
— and the kernel processes marimo itself spawns — can import everything even
when mooring runs from a moonlit-extracted zipapp.
"""

from __future__ import annotations

import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STARTUP_TIMEOUT = 30.0


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

    def ensure_started(self) -> None:
        if self.running:
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        cmd = [
            sys.executable,
            "-m",
            "marimo",
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
        self._proc = subprocess.Popen(cmd, cwd=str(self.workspace))
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
