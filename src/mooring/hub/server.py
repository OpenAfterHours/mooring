"""The mooring hub: a local web page for login, sync, and opening notebooks.

A small Starlette app bound to 127.0.0.1. Endpoints are plain sync functions
(Starlette runs them in a threadpool), the frontend is one static page with
vanilla JS. The marimo editor runs as a separate subprocess (see editor.py)
that the hub starts lazily and tears down on shutdown.
"""

from __future__ import annotations

import contextlib
import threading
import time
import webbrowser
from importlib import resources
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from mooring import __version__, auth, config, paths, sync
from mooring.cli import SELFTEST_PACKAGES
from mooring.editor import EditorServer, _free_port
from mooring.github import AuthFailed, GitHubClient, GitHubError


def _static_dir() -> Path:
    return Path(str(resources.files("mooring.hub").joinpath("static")))


class Hub:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.editor: EditorServer | None = None
        self._device: auth.DeviceCode | None = None
        self._poll_interval = 5
        self._next_poll = 0.0
        self._user_login = ""
        self._lock = threading.Lock()

    # -- helpers -------------------------------------------------------------

    def client(self) -> GitHubClient:
        token = auth.get_token()
        if not token:
            raise AuthFailed("Not logged in.")
        return GitHubClient(token, self.cfg.owner, self.cfg.repo)

    def username(self) -> str:
        if not self._user_login:
            self._user_login = self.client().get_user()["login"]
        return self._user_login

    def ensure_editor(self) -> EditorServer:
        if self.editor is None:
            self.editor = EditorServer(self.cfg.workspace())
        self.editor.ensure_started()
        return self.editor

    def shutdown(self) -> None:
        if self.editor is not None:
            self.editor.shutdown()

    # -- endpoints -------------------------------------------------------------

    def api_state(self, request: Request) -> JSONResponse:
        body: dict = {
            "version": __version__,
            "configured": self.cfg.is_configured,
            "repo": self.cfg.repo_slug if self.cfg.is_configured else "",
            "branch": self.cfg.branch,
            "workspace": str(self.cfg.workspace()),
            "packages": sorted(SELFTEST_PACKAGES),
            "logged_in": False,
            "user": "",
            "files": [],
        }
        if not self.cfg.is_configured:
            return JSONResponse(body)
        if not auth.get_token():
            return JSONResponse(body)
        try:
            body["user"] = self.username()
            body["logged_in"] = True
            report = sync.status(self.client(), self.cfg)
            body["files"] = [
                {"path": f.path, "state": f.state.value} for f in report.files
            ]
            body["summary"] = report.summary()
        except AuthFailed:
            auth.delete_token()
            self._user_login = ""
            body["logged_in"] = False
            body["error"] = "Your GitHub login expired. Please log in again."
        except GitHubError as exc:
            body["error"] = str(exc)
        return JSONResponse(body)

    async def api_setup(self, request: Request) -> JSONResponse:
        data = await request.json()
        fields = {k: str(data.get(k, "")).strip() for k in ("client_id", "owner", "repo", "branch")}
        if not (fields["client_id"] and fields["owner"] and fields["repo"]):
            return JSONResponse({"error": "client_id, owner and repo are required"}, status_code=400)
        path = paths.user_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[github]\n"
            f'client_id = "{fields["client_id"]}"\n'
            f'owner = "{fields["owner"]}"\n'
            f'repo = "{fields["repo"]}"\n'
            f'branch = "{fields["branch"] or "main"}"\n',
            "utf-8",
        )
        self.cfg = config.load_config()
        return JSONResponse({"ok": True})

    def api_login_start(self, request: Request) -> JSONResponse:
        try:
            device = auth.start_device_flow(self.cfg.client_id)
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            return JSONResponse({"error": str(exc)}, status_code=502)
        with self._lock:
            self._device = device
            self._poll_interval = device.interval
            self._next_poll = time.monotonic() + device.interval
        return JSONResponse(
            {"user_code": device.user_code, "verification_uri": device.verification_uri}
        )

    def api_login_poll(self, request: Request) -> JSONResponse:
        with self._lock:
            device = self._device
            if device is None:
                return JSONResponse({"status": "error", "message": "No login in progress."})
            if time.monotonic() < self._next_poll:
                return JSONResponse({"status": "pending"})
        try:
            result = auth.poll_once(self.cfg.client_id, device, interval=self._poll_interval)
        except auth.AuthError as exc:
            with self._lock:
                self._device = None
            return JSONResponse({"status": "error", "message": str(exc)})
        if result.token:
            auth.save_token(result.token)
            with self._lock:
                self._device = None
            self._user_login = ""
            return JSONResponse({"status": "ok"})
        with self._lock:
            self._poll_interval = result.interval
            self._next_poll = time.monotonic() + result.interval
        return JSONResponse({"status": "pending"})

    def api_logout(self, request: Request) -> JSONResponse:
        auth.delete_token()
        self._user_login = ""
        return JSONResponse({"ok": True})

    async def api_pull(self, request: Request) -> JSONResponse:
        data = await request.json() if await request.body() else {}
        strategy = sync.ConflictStrategy(data.get("strategy", "skip"))
        return self._sync_op(lambda: sync.pull(self.client(), self.cfg, strategy=strategy))

    async def api_push(self, request: Request) -> JSONResponse:
        data = await request.json() if await request.body() else {}
        paths_arg = data.get("paths") or None
        return self._sync_op(lambda: sync.push(self.client(), self.cfg, paths=paths_arg))

    async def api_resolve(self, request: Request) -> JSONResponse:
        data = await request.json()
        strategy = sync.ConflictStrategy(data["strategy"])
        username = self.username() if strategy is sync.ConflictStrategy.PUSH_COPY else ""
        return self._sync_op(
            lambda: sync.resolve(self.client(), self.cfg, data["path"], strategy, username)
        )

    def _sync_op(self, op) -> JSONResponse:
        try:
            result = op()
        except (GitHubError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"lines": result.lines, "summary": result.summary()})

    async def api_new(self, request: Request) -> JSONResponse:
        from mooring import notebook_template

        data = await request.json()
        try:
            rel_path = notebook_template.create(self.cfg.workspace(), data.get("name", ""))
        except (ValueError, FileExistsError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return self._open(rel_path)

    async def api_open(self, request: Request) -> JSONResponse:
        data = await request.json()
        return self._open(data.get("path", ""))

    def _open(self, rel_path: str) -> JSONResponse:
        target = self.cfg.workspace() / rel_path
        if not target.is_file():
            return JSONResponse({"error": f"No such file: {rel_path}"}, status_code=404)
        if not rel_path.endswith(".py"):
            return JSONResponse({"error": "Only .py notebooks can be opened."}, status_code=400)
        try:
            editor = self.ensure_editor()
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            return JSONResponse({"error": f"Could not start the editor: {exc}"}, status_code=502)
        return JSONResponse({"path": rel_path, "url": editor.url_for(rel_path)})


def create_app(hub: Hub) -> Starlette:
    static = _static_dir()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            hub.shutdown()

    return Starlette(
        routes=[
            Route("/", lambda r: FileResponse(static / "index.html")),
            Route("/api/state", hub.api_state),
            Route("/api/setup", hub.api_setup, methods=["POST"]),
            Route("/api/login/start", hub.api_login_start, methods=["POST"]),
            Route("/api/login/poll", hub.api_login_poll),
            Route("/api/logout", hub.api_logout, methods=["POST"]),
            Route("/api/pull", hub.api_pull, methods=["POST"]),
            Route("/api/push", hub.api_push, methods=["POST"]),
            Route("/api/resolve", hub.api_resolve, methods=["POST"]),
            Route("/api/new", hub.api_new, methods=["POST"]),
            Route("/api/open", hub.api_open, methods=["POST"]),
            Mount("/static", StaticFiles(directory=static)),
        ],
        lifespan=lifespan,
    )


def run_hub(cfg: config.Config, open_browser: bool = True, port: int | None = None) -> int:
    hub = Hub(cfg)
    app = create_app(hub)
    port = port or _free_port()
    url = f"http://127.0.0.1:{port}/"
    print(f"mooring hub running at {url} (Ctrl+C to quit)")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
