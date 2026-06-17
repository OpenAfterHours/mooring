"""The mooring hub: a local web page for login, sync, and opening notebooks.

A small Starlette app bound to 127.0.0.1. Endpoints are plain sync functions
(Starlette runs them in a threadpool), the frontend is one static page with
vanilla JS. The marimo editor runs as a separate subprocess (see editor.py)
that the hub starts lazily and tears down on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import secrets
import threading
import time
import webbrowser
from importlib import resources
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from mooring import __version__, auth, config, config_store, pbip, pyproject_env, sync, telemetry
from mooring.editor import EditorServer, free_port
from mooring.github import AuthFailed, GitHubClient, GitHubError, compare_url
from mooring.runtime import SELFTEST_PACKAGES, workspace_hint


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
        # AI copilot chat sessions, keyed by a hub-minted sid. Each is bound to
        # one open notebook; the value is a chat.StubChatSession (Phase 0) or a
        # CopilotChatSession (Phase 1) — both ChatBroadcasters.
        self._chats: dict[str, object] = {}
        self._chat_targets: dict[str, tuple[str, str]] = {}  # sid -> (workspace, notebook rel)
        self._chat_lock = threading.Lock()

    # -- helpers -------------------------------------------------------------

    @property
    def cfg(self) -> config.Config:
        return self.app_cfg.config_for(None)

    def reload(self) -> None:
        with self._lock:
            self.app_cfg = config.load_app_config()
        # Chat context (schema + notebook source) is bound to the old config;
        # drop sessions so a new chat picks up the new repo/workspace.
        self._close_all_chats()

    def client(self) -> GitHubClient:
        cfg = self.cfg
        token = auth.get_token(host=cfg.host)
        if not token:
            raise AuthFailed("Not logged in.")
        return GitHubClient(token, cfg.owner, cfg.repo, host=cfg.host)

    def username(self) -> str:
        if not self._user_login:
            self._user_login = self.client().get_user()["login"]
            telemetry.set_user(self._user_login)
        return self._user_login

    def ensure_editor(self) -> EditorServer:
        return self.ensure_editor_for(self.cfg.workspace())

    def ensure_editor_for(self, workspace: Path) -> EditorServer:
        editor = self.editors.setdefault(
            str(workspace), EditorServer(workspace, theme=self.app_cfg.ui_theme)
        )
        editor.ensure_started()
        return editor

    def _close_all_chats(self) -> None:
        with self._chat_lock:
            sessions = list(self._chats.values())
            self._chats.clear()
            self._chat_targets.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                session.close()  # type: ignore[attr-defined]

    def shutdown(self) -> None:
        self._close_all_chats()
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
            "workspace_hint": workspace_hint(cfg),
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
            "ui_theme": self.app_cfg.ui_theme,
            "packages": sorted(SELFTEST_PACKAGES),
            "ai_chat": self.app_cfg.ai_enabled,
            "datasets": [],
            "logged_in": False,
            "user": "",
            "files": [],
            "artifacts": [],
        }
        if self.app_cfg.ai_enabled:
            from mooring import schema

            body["datasets"] = schema.list_datasets(cfg.workspace(), cfg.folders)
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
                    "has_local": f.local_sha is not None,
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
            telemetry.log_error(exc=exc, op="state")
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
        telemetry.log_event("repo_add", alias=fields["alias"] or fields["repo"])
        return JSONResponse({"ok": True, "active_repo": self.app_cfg.active_alias})

    async def api_repo_switch(self, request: Request) -> JSONResponse:
        data = await request.json()
        alias = str(data.get("alias", ""))
        try:
            config_store.set_active(alias)
        except KeyError:
            return JSONResponse({"error": f"Unknown repo alias {alias!r}."}, status_code=400)
        self.reload()
        telemetry.log_event("repo_switch", alias=alias)
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
        telemetry.log_event("repo_remove", alias=alias)
        return JSONResponse(
            {"ok": True, "lines": [f"Removed {alias!r}; workspace folder kept at {workspace}"]}
        )

    async def api_set_theme(self, request: Request) -> JSONResponse:
        """Set the shared appearance (light/dark/system) from the hub toggle.

        Persists it to the user config, updates the live config, and re-themes
        every running editor's workspace ``.marimo.toml`` so open notebooks pick
        up the new theme on reopen/reload. The chat UI re-themes itself via the
        ``/api/state`` value plus a same-origin storage event. Does NOT reload
        the whole config (that would drop open chat sessions for an appearance
        change)."""
        from dataclasses import replace

        data = await request.json()
        theme = config.normalize_theme(data.get("theme", ""))
        config_store.set_value("ui.theme", theme)
        with self._lock:
            self.app_cfg = replace(self.app_cfg, ui_theme=theme)
        for editor in list(self.editors.values()):
            editor.apply_theme(theme)
        telemetry.log_event("ui_theme", theme=theme)
        return JSONResponse({"ok": True, "theme": theme})

    def api_login_start(self, request: Request) -> JSONResponse:
        try:
            device = auth.start_device_flow(self.cfg.client_id, host=self.cfg.host)
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            return JSONResponse(
                {"error": auth.device_flow_hint(self.cfg.host, exc)}, status_code=502
            )
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
            telemetry.log_event("login")
            return JSONResponse({"status": "ok"})
        with self._lock:
            self._poll_interval = result.interval
            self._next_poll = time.monotonic() + result.interval
        return JSONResponse({"status": "pending"})

    def api_logout(self, request: Request) -> JSONResponse:
        auth.delete_token(host=self.cfg.host)
        self._user_login = ""
        telemetry.log_event("logout")
        return JSONResponse({"ok": True})

    async def api_pull(self, request: Request) -> JSONResponse:
        data = await request.json() if await request.body() else {}
        strategy = sync.ConflictStrategy(data.get("strategy", "skip"))
        return self._sync_op("pull", lambda: sync.pull(self.client(), self.cfg, strategy=strategy))

    async def api_push(self, request: Request) -> JSONResponse:
        data = await request.json() if await request.body() else {}
        paths_arg = data.get("paths") or None
        return self._sync_op("push", lambda: sync.push(self.client(), self.cfg, paths=paths_arg))

    async def api_propose(self, request: Request) -> JSONResponse:
        data = await request.json() if await request.body() else {}
        paths_arg = data.get("paths") or None
        return self._sync_op(
            "propose", lambda: sync.propose(self.client(), self.cfg, paths=paths_arg)
        )

    async def api_resolve(self, request: Request) -> JSONResponse:
        data = await request.json()
        strategy = sync.ConflictStrategy(data["strategy"])
        username = self.username() if strategy is sync.ConflictStrategy.PUSH_COPY else ""
        return self._sync_op(
            "resolve",
            lambda: sync.resolve(self.client(), self.cfg, data["path"], strategy, username),
        )

    def _sync_op(self, name: str, op) -> JSONResponse:
        try:
            result = op()
        except (GitHubError, OSError) as exc:
            telemetry.log_error(exc=exc, op=name)
            return JSONResponse({"error": str(exc)}, status_code=502)
        telemetry.log_event(
            name,
            pulled=result.pulled,
            pushed=result.pushed,
            proposed=result.proposed,
            conflicts=len(result.skipped_conflicts) + len(result.blocked_conflicts),
            lines=len(result.lines),
        )
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
        telemetry.log_event("new")
        return self._open(rel_path)

    async def api_open(self, request: Request) -> JSONResponse:
        data = await request.json()
        return self._open(data.get("path", ""))

    async def api_delete(self, request: Request) -> JSONResponse:
        from mooring import deletion

        data = await request.json()
        rel_path = str(data.get("path", ""))
        cfg = self.cfg
        try:
            removed = deletion.delete(cfg.workspace(), rel_path, cfg.exclude, cfg.folders)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"error": f"No such notebook: {rel_path}"}, status_code=404)
        telemetry.log_event("delete", count=len(removed))
        name = rel_path.rsplit("/", 1)[-1]
        return JSONResponse(
            {
                "lines": [f"deleted {r}" for r in removed],
                "summary": f"Deleted {name}. If it was shared, push or propose to "
                "remove it for the team.",
            }
        )

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
            telemetry.log_event("open", kind="pbip")
            return JSONResponse({"path": rel_path, "lines": [f"Opened {name} in Power BI Desktop"]})
        if not rel_path.endswith(".py"):
            return JSONResponse(
                {"error": "Only .py notebooks and .pbip projects can be opened."},
                status_code=400,
            )
        try:
            editor = self.ensure_editor()
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            return JSONResponse({"error": f"Could not start the editor: {exc}"}, status_code=502)
        telemetry.log_event("open", kind="notebook", uv=editor.use_uv())
        payload = {"path": rel_path, "url": editor.url_for(rel_path)}
        if not editor.use_uv():
            missing = pyproject_env.missing_deps(workspace)
            if missing:
                payload["warning"] = (
                    f"This build can't provide: {', '.join(missing)}. They're declared in "
                    f"{pyproject_env.PYPROJECT_NAME} but not bundled, so importing them will fail. "
                    "Ask your admin to include them in the build, or run mooring via uv."
                )
        return JSONResponse(payload)

    # -- AI copilot (chat) -----------------------------------------------------

    def _ws_file(self, workspace: Path, rel: str, *, suffix: str | None = None) -> Path:
        """Resolve a workspace-relative path, rejecting escapes/missing files."""
        target = (workspace / rel).resolve()
        try:
            target.relative_to(workspace.resolve())
        except ValueError as exc:
            raise ValueError("Path escapes the workspace.") from exc
        if suffix and not rel.endswith(suffix):
            raise ValueError(f"Expected a {suffix} file.")
        if not target.is_file():
            raise FileNotFoundError(rel)
        return target

    def _build_chat_context(self, workspace: Path, notebook_rel: str, dataset_rel: str):
        """Return ``(system_context, dictionary_index, pii_banner)`` for a chat.

        The value-free core is the dataset SCHEMA + notebook SOURCE. When the
        opt-in context feature is on, it also folds in the team instructions and
        a locality-selected, value-minimised data-dictionary slice (with the
        selected dataset's schema enriched by matching dictionary entries), and
        returns the parsed index so the session can offer the pull tools.

        When the opt-in PII guard is on, it additionally withholds any schema
        column whose NAME is itself a PII value (a pivot/transpose on a PII key)
        and, with ``scan_notebook_source``, collects value-free findings for the
        notebook source into ``pii_banner`` (a one-time, warn-only UI banner —
        the source is never mutated).
        """
        from dataclasses import replace

        from mooring import schema
        from mooring.ai import context as ctxmod
        from mooring.ai import egress, locality, ner, pii
        from mooring.ai.datadictionary import DictionaryIndex

        pii_banner: list[dict] = []
        # Resolve "auto" -> concrete and shape the name model for it once, shared by
        # the notebook-source warn scan below (and consistent with the chat session).
        pii_backend = ner.resolve_backend(self.app_cfg.ai_pii_name_backend)
        pii_name_model = ner.model_for(
            pii_backend,
            self.app_cfg.ai_pii_name_model,
            self.app_cfg.ai_pii_name_revision,
            self.app_cfg.ai_pii_name_variant,
        )

        repo_ctx = ctxmod.discover_context(
            workspace,
            context_dir=self.app_cfg.ai_context_dir,
            enabled=self.app_cfg.ai_context,
            max_kb=self.app_cfg.ai_context_max_kb,
        )
        index = repo_ctx.index
        has_dict = not index.is_empty()

        schema_text = ""
        dataset_schema = None
        if dataset_rel:
            ds = self._ws_file(workspace, dataset_rel)
            try:
                dataset_schema = schema.extract_schema(ds)
            except (ValueError, OSError) as exc:
                raise ValueError(f"Could not read the schema for {dataset_rel}: {exc}") from exc
            if self.app_cfg.ai_pii:
                kept, col_findings = egress.scrub_columns(dataset_schema.columns)
                if col_findings:  # a column NAME is itself a PII value — withhold it
                    dataset_schema = replace(dataset_schema, columns=kept)
                    pii_banner += [
                        {"where": f"{dataset_rel} column", "kind": f.kind} for f in col_findings
                    ]
            schema_text = (
                locality.enrich_dataset_schema(dataset_schema, index, dataset_rel)
                if has_dict
                else schema.format_for_ai(dataset_schema, source=dataset_rel)
            )

        source = self._ws_file(workspace, notebook_rel, suffix=".py").read_text("utf-8")
        if self.app_cfg.ai_pii and self.app_cfg.ai_pii_scan_source:
            # Warn-only: the notebook source is the analyst's own working file, so we
            # never mutate it — we surface a value-free banner and let them decide.
            pii_banner += [
                {"where": f"{notebook_rel}:{f.line}", "kind": f.kind}
                for f in pii.scan_prose(
                    source,
                    names=self.app_cfg.ai_pii_names,
                    labels=self.app_cfg.ai_pii_name_labels,
                    threshold=self.app_cfg.ai_pii_name_threshold,
                    model=pii_name_model,
                    backend=pii_backend,
                )
            ]

        # Schemas of dataframes LIVE in the running kernel — covers data loaded
        # from OUTSIDE the workspace (network/warehouse/DB) and derived frames no
        # file holds. Best-effort + value-free; the single pipeline (_live_schema_text)
        # shared with the per-turn refresh. On any failure live_text is "" and we
        # fall back to the file-based schema above.
        live_text, live_banner = self._live_schema_text(workspace, notebook_rel)
        pii_banner += live_banner

        dictionary_text = ""
        if has_dict:
            dataset_cols = (
                {n for n, _ in dataset_schema.columns} if dataset_schema is not None else set()
            )
            stem = Path(dataset_rel).stem if dataset_rel else ""
            tables, reasons, n_more = locality.working_set(
                index,
                dataset_columns=dataset_cols,
                dataset_stem=stem,
                notebook_source=source,
                notebook_rel=notebook_rel,
            )
            dictionary_text = locality.seed_text(tables, reasons, n_more)

        context = egress.build_system_context(
            schema_text=schema_text,
            notebook_source=source,
            notebook_rel=notebook_rel,
            live_schemas_text=live_text,
            instructions_text=repo_ctx.instructions,
            dictionary_text=dictionary_text,
        )
        return context, (index if has_dict else DictionaryIndex()), pii_banner, live_text

    def _live_schema_text(self, workspace: Path, notebook_rel: str) -> tuple[str, list[dict]]:
        """Value-free schema of the dataframes LIVE in ``notebook_rel``'s kernel.

        Returns ``(rendered_text, pii_banner)``. Best-effort: any failure (live
        schema off, no running editor/session, frames not loaded, probe error)
        yields ``("", [])`` and the caller falls back to the file-based schema.
        The ONE value-free pipeline (introspect probe -> ``scrub_columns`` ->
        ``format_live_schemas``) shared by chat-open and the per-turn refresh.
        """
        if not self.app_cfg.ai_live_schema:
            return "", []
        from dataclasses import replace

        from mooring.ai import egress, introspect

        banner: list[dict] = []
        try:
            frames = introspect.live_dataset_schemas(self.editors.get(str(workspace)), notebook_rel)
            if self.app_cfg.ai_pii:
                scrubbed = []
                for fr in frames:
                    kept, ff = egress.scrub_columns(fr.columns)
                    if ff:  # a pivot/transpose put a PII value in a column NAME
                        banner += [
                            {"where": f"live `{fr.name}` column", "kind": f.kind} for f in ff
                        ]
                        fr = replace(fr, columns=kept)
                    scrubbed.append(fr)
                frames = scrubbed
            return introspect.format_live_schemas(frames), banner
        except Exception:  # noqa: BLE001 - never block chat on introspection
            return "", []

    def _live_schema_for_sid(self, sid: str) -> tuple[str, list[dict]]:
        """The current live-kernel schema for an open chat session (best-effort)."""
        with self._chat_lock:
            target = self._chat_targets.get(sid)
        if target is None:
            return "", []
        workspace_str, notebook_rel = target
        return self._live_schema_text(Path(workspace_str), notebook_rel)

    def _make_chat_session(
        self,
        system_context: str,
        workspace: Path,
        notebook_rel: str,
        model: str = "",
        reasoning_effort: str | None = None,
        dictionary=None,
    ):
        """Open a streaming Copilot chat session bound to this notebook.

        ``model``/``reasoning_effort`` override the configured defaults;
        ``dictionary`` (a parsed index) enables the value-free dictionary tools.
        Raises AIError (-> 502 with an install/connect hint) if Copilot isn't
        available or signed in.
        """
        from mooring.ai import get_provider

        provider = get_provider(self.app_cfg)
        return provider.open_chat(
            system_context=system_context,
            workspace=workspace,
            folders=self.cfg.folders,
            notebook_rel=notebook_rel,
            model=model,
            reasoning_effort=reasoning_effort,
            dictionary=dictionary,
            # The whole guard config travels as ONE object, so a field can't be
            # silently dropped on the way to the session (the session downloads any
            # NER model in the background and the prompt path skips it until ready).
            pii=self.app_cfg.ai.pii,
        )

    def _reap_idle_chats(self) -> None:
        timeout = self.app_cfg.ai_chat_idle_timeout
        with self._chat_lock:
            dead = [sid for sid, s in self._chats.items() if s.idle_seconds() > timeout]  # type: ignore[attr-defined]
            sessions = [self._chats.pop(sid) for sid in dead]
            for sid in dead:
                self._chat_targets.pop(sid, None)
        for session in sessions:
            with contextlib.suppress(Exception):
                session.close()  # type: ignore[attr-defined]

    def _themed_page(self, filename: str) -> HTMLResponse:
        """Serve a hub HTML page with the configured default theme inlined.

        The page's pre-paint script applies ``localStorage`` first, falling back
        to this server default — so a brand-new browser (empty localStorage)
        paints in the admin's configured theme immediately, with no flash, and
        consistent with what ``/api/state`` later reports. The value comes from
        :func:`config.normalize_theme`, so it is always one of light/dark/system
        (no injection risk)."""
        html = (_static_dir() / filename).read_text("utf-8")
        return HTMLResponse(html.replace("__MOORING_DEFAULT_THEME__", self.app_cfg.ui_theme))

    def index_page(self, request: Request) -> HTMLResponse:
        return self._themed_page("index.html")

    def chat_page(self, request: Request) -> HTMLResponse | JSONResponse:
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"error": "The AI copilot is disabled."}, status_code=404)
        return self._themed_page("chat.html")

    async def api_chat_open(self, request: Request) -> JSONResponse:
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        data = await request.json()
        notebook = str(data.get("notebook", "")).strip()
        dataset = str(data.get("dataset", "")).strip()
        model = str(data.get("model", "")).strip()
        reasoning_effort = (
            str(data.get("reasoning_effort", "")).strip() or self.app_cfg.ai_reasoning_effort
        )
        if not notebook:
            return JSONResponse({"error": "A notebook is required."}, status_code=400)
        workspace = self.cfg.workspace()
        try:
            context, index, pii_banner, live_text = self._build_chat_context(
                workspace, notebook, dataset
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileNotFoundError as exc:
            return JSONResponse({"error": f"No such file: {exc}"}, status_code=404)
        self._reap_idle_chats()
        try:
            session = self._make_chat_session(
                context,
                workspace,
                notebook,
                model=model,
                reasoning_effort=reasoning_effort,
                dictionary=index,
            )
        except Exception as exc:  # noqa: BLE001 - AIError surfaces to the UI in Phase 1
            return JSONResponse({"error": str(exc)}, status_code=502)
        # Seed the snapshot already in the system context so turn 1 doesn't redundantly
        # re-inject it; later turns refresh from the kernel (see api_chat_send).
        session.set_initial_live_schema(live_text)
        # Kick off the (one-time) NER model download in the background with progress,
        # so name detection doesn't hang the first chat turn silently.
        session.prepare_pii_model()
        sid = secrets.token_urlsafe(9)
        with self._chat_lock:
            self._chats[sid] = session
            self._chat_targets[sid] = (str(workspace), notebook)
        telemetry.log_event("ai_chat_open")
        if pii_banner:  # count only — never a kind/value reaches the central sink
            telemetry.log_event("ai_pii", findings=len(pii_banner))
        return JSONResponse(
            {"sid": sid, "notebook": notebook, "pii": pii_banner, "guard": self._pii_status()}
        )

    def _pii_status(self) -> dict:
        """Value-free snapshot of the outbound-PII guard for the chat UI badge: is
        the pre-flight scan on, does a hit block, and can the optional name pass
        actually run right now. Carries no finding, value, or path — only config
        booleans plus the resolved backend name."""
        cfg = self.app_cfg
        enabled = bool(cfg.ai_pii)
        names = bool(cfg.ai_pii_names)
        backend = ""
        names_active = False
        if enabled and names:
            from mooring.ai import ner

            backend = ner.resolve_backend(cfg.ai_pii_name_backend)
            model = ner.model_for(
                backend, cfg.ai_pii_name_model, cfg.ai_pii_name_revision, cfg.ai_pii_name_variant
            )
            names_active = bool(ner.available(backend) and ner.is_ready(model, backend))
        return {
            "enabled": enabled,
            "block": bool(cfg.ai_pii_block_prompt),
            "names": names,
            "names_active": names_active,
            "backend": backend,
        }

    async def api_chat_stream(self, request: Request) -> StreamingResponse | JSONResponse:
        sid = request.path_params["sid"]
        session = self._chats.get(sid)
        if session is None:
            return JSONResponse({"error": "Unknown chat session."}, status_code=404)
        return StreamingResponse(
            self._sse_gen(session),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _sse_gen(self, session):
        q = session.subscribe()
        try:
            yield ": connected\n\n"
            # Replay the current NER-model prepare status so a subscriber that connects
            # mid-download immediately sees progress (events emitted before this
            # subscribe would otherwise be missed).
            ner_status = getattr(session, "ner_status", None)
            if ner_status:
                yield f"event: ner\ndata: {json.dumps(ner_status)}\n\n"
            while True:
                try:
                    event = await asyncio.to_thread(q.get, True, 15.0)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield f"event: {event.kind}\ndata: {json.dumps(event.data)}\n\n"
                if event.kind == "closed":
                    break
        finally:
            session.unsubscribe(q)

    async def api_chat_send(self, request: Request) -> JSONResponse:
        data = await request.json()
        sid = str(data.get("sid", ""))
        session = self._chats.get(sid)
        if session is None:
            return JSONResponse({"error": "Unknown chat session."}, status_code=404)
        # Refresh the live-kernel schema so dataframes added since chat-open (or the
        # last turn) are visible without reopening. Value-free + best-effort; the
        # session re-injects it only when it changed. Off-thread — it does kernel I/O.
        live_text, live_banner = await asyncio.to_thread(self._live_schema_for_sid, sid)
        if live_banner:  # a refreshed column NAME was itself PII (withheld) — count only
            telemetry.log_event("ai_pii", findings=len(live_banner))
        # "Send anyway" path: forward a prompt the PII guard held, verbatim, once.
        confirm = str(data.get("confirm_token", "")).strip()
        if confirm:
            try:
                await asyncio.to_thread(session.send_confirmed, confirm, live_text)
            except Exception as exc:  # noqa: BLE001 - AIError surfaces to the UI
                return JSONResponse({"error": str(exc)}, status_code=502)
            telemetry.log_event("ai_chat_send", confirmed=1)
            return JSONResponse({"ok": True, "pii": live_banner})
        text = str(data.get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "Type a message."}, status_code=400)
        try:
            await asyncio.to_thread(session.send, text, live_text)
        except Exception as exc:  # noqa: BLE001 - AIError surfaces to the UI in Phase 1
            return JSONResponse({"error": str(exc)}, status_code=502)
        telemetry.log_event("ai_chat_send")
        return JSONResponse({"ok": True, "pii": live_banner})

    async def api_chat_apply(self, request: Request) -> JSONResponse:
        data = await request.json()
        sid = str(data.get("sid", ""))
        code = str(data.get("code", ""))
        with self._chat_lock:
            target = self._chat_targets.get(sid)
        if target is None:
            return JSONResponse({"error": "Unknown chat session."}, status_code=404)
        if not code.strip():
            return JSONResponse({"error": "Nothing to apply."}, status_code=400)
        workspace_str, notebook_rel = target
        from mooring.ai.cellwrite import CellWriteError, append_cell

        try:
            nb_path = self._ws_file(Path(workspace_str), notebook_rel, suffix=".py")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"error": f"No such notebook: {notebook_rel}"}, status_code=404)
        # Write the cell into the .py; the editor's --watch picks it up and (with
        # watcher_on_save=autorun) runs it, so it appears in the open notebook tab.
        try:
            await asyncio.to_thread(append_cell, nb_path, code)
        except CellWriteError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        telemetry.log_event("ai_chat_apply")
        return JSONResponse({"ok": True})

    async def api_chat_models(self, request: Request) -> JSONResponse:
        """The models the user can pick, plus the configured defaults (value-free)."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        from mooring.ai import get_provider

        provider = get_provider(self.app_cfg)
        models = await asyncio.to_thread(provider.list_models)
        return JSONResponse(
            {
                "models": models,
                "default_model": self.app_cfg.ai_model or "",
                "default_effort": self.app_cfg.ai_reasoning_effort or "",
            }
        )


def create_app(hub: Hub) -> Starlette:
    static = _static_dir()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            hub.shutdown()
            telemetry.flush(timeout=3.0)

    return Starlette(
        routes=[
            Route("/", hub.index_page),
            Route("/api/state", hub.api_state),
            Route("/api/setup", hub.api_setup, methods=["POST"]),
            Route("/api/repo/switch", hub.api_repo_switch, methods=["POST"]),
            Route("/api/repo/remove", hub.api_repo_remove, methods=["POST"]),
            Route("/api/ui/theme", hub.api_set_theme, methods=["POST"]),
            Route("/api/login/start", hub.api_login_start, methods=["POST"]),
            Route("/api/login/poll", hub.api_login_poll),
            Route("/api/logout", hub.api_logout, methods=["POST"]),
            Route("/api/pull", hub.api_pull, methods=["POST"]),
            Route("/api/push", hub.api_push, methods=["POST"]),
            Route("/api/propose", hub.api_propose, methods=["POST"]),
            Route("/api/resolve", hub.api_resolve, methods=["POST"]),
            Route("/api/new", hub.api_new, methods=["POST"]),
            Route("/api/open", hub.api_open, methods=["POST"]),
            Route("/api/delete", hub.api_delete, methods=["POST"]),
            Route("/ai/chat", hub.chat_page),
            Route("/api/ai/models", hub.api_chat_models),
            Route("/api/ai/chat/open", hub.api_chat_open, methods=["POST"]),
            Route("/api/ai/chat/stream/{sid}", hub.api_chat_stream),
            Route("/api/ai/chat/send", hub.api_chat_send, methods=["POST"]),
            Route("/api/ai/chat/apply", hub.api_chat_apply, methods=["POST"]),
            Mount("/static", StaticFiles(directory=static)),
        ],
        lifespan=lifespan,
    )


def run_hub(app_cfg: config.AppConfig, open_browser: bool = True, port: int | None = None) -> int:
    hub = Hub(app_cfg)
    app = create_app(hub)
    port = port or free_port()
    url = f"http://127.0.0.1:{port}/"
    telemetry.log_event("hub_start")
    print(f"mooring hub running at {url} (Ctrl+C to quit)")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
