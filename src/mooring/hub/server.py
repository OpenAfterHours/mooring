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

from mooring import __version__, auth, config, config_store, pbip, sync
from mooring.cli import SELFTEST_PACKAGES, legacy_workspace_hint
from mooring.editor import EditorServer, _free_port
from mooring.github import AuthFailed, GitHubClient, GitHubError, compare_url


def _static_dir() -> Path:
    return Path(str(resources.files("mooring.hub").joinpath("static")))


class Hub:
    def __init__(self, app_cfg: config.AppConfig) -> None:
        self.app_cfg = app_cfg
        # One editor per workspace, created lazily: switching repos must not
        # kill marimo tabs open against the previous workspace.
        self.editors: dict[str, EditorServer] = {}
        self._device: auth.DeviceCode | None = None
        self._poll_interval = 5
        self._next_poll = 0.0
        self._user_login = ""
        self._lock = threading.Lock()

    # -- helpers -------------------------------------------------------------

    @property
    def cfg(self) -> config.Config:
        return self.app_cfg.config_for(None)

    def reload(self) -> None:
        with self._lock:
            self.app_cfg = config.load_app_config()

    def client(self) -> GitHubClient:
        cfg = self.cfg
        token = auth.get_token(host=cfg.host)
        if not token:
            raise AuthFailed("Not logged in.")
        return GitHubClient(token, cfg.owner, cfg.repo, host=cfg.host)

    def username(self) -> str:
        if not self._user_login:
            self._user_login = self.client().get_user()["login"]
        return self._user_login

    def ensure_editor(self) -> EditorServer:
        workspace = self.cfg.workspace()
        editor = self.editors.setdefault(str(workspace), EditorServer(workspace))
        editor.ensure_started()
        return editor

    def shutdown(self) -> None:
        for editor in self.editors.values():
            editor.shutdown()

    # -- endpoints -------------------------------------------------------------

    def api_state(self, request: Request) -> JSONResponse:
        cfg = self.cfg
        body: dict = {
            "version": __version__,
            "configured": cfg.is_configured,
            "repo": cfg.repo_slug if cfg.is_configured else "",
            "branch": cfg.branch,
            "host": cfg.host,
            "workspace": str(cfg.workspace()),
            "workspace_hint": legacy_workspace_hint(cfg),
            "repos": [
                {
                    "alias": s.alias,
                    "slug": s.slug,
                    "branch": s.branch,
                    "workspace": str(self.app_cfg.config_for(s.alias).workspace()),
                    "active": s.alias == self.app_cfg.active_alias,
                }
                for s in self.app_cfg.repos
            ],
            "active_repo": self.app_cfg.active_alias,
            "packages": sorted(SELFTEST_PACKAGES),
            "logged_in": False,
            "user": "",
            "files": [],
            "artifacts": [],
        }
        if not cfg.is_configured:
            return JSONResponse(body)
        if not auth.get_token(host=cfg.host):
            return JSONResponse(body)
        try:
            body["user"] = self.username()
            body["logged_in"] = True
            report = sync.status(self.client(), cfg)
            artifacts, _ = pbip.group(report.files)
            artifact_of = {m.path: a.key for a in artifacts for m in a.members}
            body["files"] = [
                {
                    "path": f.path,
                    "state": f.state.value,
                    **({"artifact": artifact_of[f.path]} if f.path in artifact_of else {}),
                }
                for f in report.files
            ]
            body["artifacts"] = [
                {
                    "key": a.key,
                    "name": a.name,
                    "pointer": a.pointer,
                    "state": pbip.aggregate_state(a.members),
                    "members": [m.path for m in a.members],
                    "to_push": sum(1 for m in a.members if m.state in sync.PUSH_STATES),
                    "to_pull": sum(1 for m in a.members if m.state in sync.PULL_STATES),
                    "conflicts": sum(1 for m in a.members if m.state is sync.FileState.CONFLICT),
                }
                for a in artifacts
            ]
            body["summary"] = report.summary()
            if report.review_branch:
                body["review"] = {
                    "branch": report.review_branch,
                    "compare_url": compare_url(
                        cfg.owner, cfg.repo, cfg.branch, report.review_branch, host=cfg.host
                    ),
                }
        except AuthFailed:
            auth.delete_token(host=cfg.host)
            self._user_login = ""
            body["logged_in"] = False
            body["error"] = "Your GitHub login expired. Please log in again."
        except GitHubError as exc:
            body["error"] = str(exc)
        return JSONResponse(body)

    async def api_setup(self, request: Request) -> JSONResponse:
        """Register a repo (and on first run, the OAuth client id); makes it active."""
        data = await request.json()
        fields = {
            k: str(data.get(k, "")).strip()
            for k in ("client_id", "owner", "repo", "branch", "alias", "host")
        }
        if not (fields["owner"] and fields["repo"]):
            return JSONResponse({"error": "owner and repo are required"}, status_code=400)
        if not (fields["client_id"] or self.app_cfg.client_id):
            return JSONResponse({"error": "client_id is required on first setup"}, status_code=400)
        try:
            config_store.add_repo(
                fields["alias"] or fields["repo"],
                fields["owner"],
                fields["repo"],
                branch=fields["branch"] or "main",
                make_active=True,
                client_id=fields["client_id"] or None,
                host=fields["host"] or None,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        self.reload()
        return JSONResponse({"ok": True, "active_repo": self.app_cfg.active_alias})

    async def api_repo_switch(self, request: Request) -> JSONResponse:
        data = await request.json()
        alias = str(data.get("alias", ""))
        try:
            config_store.set_active(alias)
        except KeyError:
            return JSONResponse({"error": f"Unknown repo alias {alias!r}."}, status_code=400)
        self.reload()
        return JSONResponse({"ok": True, "active_repo": alias})

    async def api_repo_remove(self, request: Request) -> JSONResponse:
        data = await request.json()
        alias = str(data.get("alias", ""))
        try:
            workspace = self.app_cfg.config_for(alias).workspace()
            config_store.remove_repo(alias)
        except KeyError:
            return JSONResponse({"error": f"Unknown repo alias {alias!r}."}, status_code=400)
        self.reload()
        return JSONResponse(
            {"ok": True, "lines": [f"Removed {alias!r}; workspace folder kept at {workspace}"]}
        )

    def api_login_start(self, request: Request) -> JSONResponse:
        try:
            device = auth.start_device_flow(self.cfg.client_id, host=self.cfg.host)
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
            # device.host, not self.cfg.host: the token belongs to the host the
            # flow was started against, even if the config changed mid-login.
            auth.save_token(result.token, host=device.host)
            with self._lock:
                self._device = None
            self._user_login = ""
            return JSONResponse({"status": "ok"})
        with self._lock:
            self._poll_interval = result.interval
            self._next_poll = time.monotonic() + result.interval
        return JSONResponse({"status": "pending"})

    def api_logout(self, request: Request) -> JSONResponse:
        auth.delete_token(host=self.cfg.host)
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

    async def api_propose(self, request: Request) -> JSONResponse:
        data = await request.json() if await request.body() else {}
        paths_arg = data.get("paths") or None
        return self._sync_op(lambda: sync.propose(self.client(), self.cfg, paths=paths_arg))

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
        body = {"lines": result.lines, "summary": result.summary()}
        if result.review_branch:
            body["review_branch"] = result.review_branch
            body["compare_url"] = result.compare_url
        return JSONResponse(body)

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
        workspace = self.cfg.workspace()
        target = (workspace / rel_path).resolve()
        try:
            target.relative_to(workspace.resolve())
        except ValueError:
            return JSONResponse({"error": "Path escapes the workspace."}, status_code=400)
        if not target.is_file():
            return JSONResponse({"error": f"No such file: {rel_path}"}, status_code=404)
        if rel_path.endswith(".pbip"):
            try:
                pbip.launch(target)
            except pbip.PbipLaunchError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            name = rel_path.rsplit("/", 1)[-1]
            return JSONResponse(
                {"path": rel_path, "lines": [f"Opened {name} in Power BI Desktop"]}
            )
        if not rel_path.endswith(".py"):
            return JSONResponse(
                {"error": "Only .py notebooks and .pbip projects can be opened."},
                status_code=400,
            )
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
            Route("/api/repo/switch", hub.api_repo_switch, methods=["POST"]),
            Route("/api/repo/remove", hub.api_repo_remove, methods=["POST"]),
            Route("/api/login/start", hub.api_login_start, methods=["POST"]),
            Route("/api/login/poll", hub.api_login_poll),
            Route("/api/logout", hub.api_logout, methods=["POST"]),
            Route("/api/pull", hub.api_pull, methods=["POST"]),
            Route("/api/push", hub.api_push, methods=["POST"]),
            Route("/api/propose", hub.api_propose, methods=["POST"]),
            Route("/api/resolve", hub.api_resolve, methods=["POST"]),
            Route("/api/new", hub.api_new, methods=["POST"]),
            Route("/api/open", hub.api_open, methods=["POST"]),
            Mount("/static", StaticFiles(directory=static)),
        ],
        lifespan=lifespan,
    )


def run_hub(
    app_cfg: config.AppConfig, open_browser: bool = True, port: int | None = None
) -> int:
    hub = Hub(app_cfg)
    app = create_app(hub)
    port = port or _free_port()
    url = f"http://127.0.0.1:{port}/"
    print(f"mooring hub running at {url} (Ctrl+C to quit)")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
