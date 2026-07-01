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
import tomllib
import webbrowser
from importlib import resources
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from mooring import (
    __version__,
    auth,
    config,
    config_store,
    notebook_template,
    pbip,
    pyproject_env,
    reveal,
    shadow,
    sync,
    telemetry,
    workspace_config,
)
from mooring.editor import EditorServer, free_port
from mooring.github import AuthFailed, GitHubClient, GitHubError, blob_url, compare_url
from mooring.hub import settings_schema
from mooring.runtime import workspace_hint


def _static_dir() -> Path:
    return Path(str(resources.files("mooring.hub").joinpath("static")))


# Sentinel returned by Hub._restore_undo when a token-scoped undo can't run because a
# newer snapshot is now on top of the (shared) per-notebook undo stack.
_UNKNOWN_CHAT_SESSION = "Unknown chat session."
_UNDO_SUPERSEDED = object()


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
        # Serializes the snapshot+write of an Apply and the restore of an Undo so two
        # near-simultaneous clicks can't race the undo stack (single-user, rare clicks
        # — a global lock is plenty and keeps the snapshot/restore atomic).
        self._apply_lock = threading.Lock()
        # Batch notebook-generation runs, keyed by a hub-minted batch_id. Each holds a
        # ChatBroadcaster for SSE progress, an abort Event, and (when finished) the
        # value-free per-job results the review tray + per-notebook Apply read. The
        # builder sessions live and die inside the planner thread, not here.
        self._batches: dict[str, dict] = {}
        self._batch_lock = threading.Lock()
        # One AI provider reused across opens, so the provider's auth (45s TTL) and
        # model-list (300s TTL) caches actually hit instead of being rebuilt — and
        # thrown away — on every chat-open / models request. Keyed on the config that
        # shapes it (provider+model); reset on a config reload. See _provider_for.
        self._provider = None
        self._provider_key: tuple | None = None
        self._provider_lock = threading.Lock()
        # Background pre-warm (editor subprocess + heavy imports) is enabled only by
        # run_hub() for a real serving hub — never under TestClient/create_app, so the
        # suite never spawns a marimo subprocess or imports the Copilot SDK. See warmup().
        self._prewarm_enabled = False
        # Serializes editor startup so the background pre-warm and a user's Open click
        # can't both spawn a marimo subprocess for the same (cold) workspace at once.
        self._editor_lock = threading.Lock()
        # Cache of the interpreter's top-level packages for the footer (bundle mode).
        # The env can't change within a running process, so enumerate site-packages
        # once instead of on every /api/state poll. See _notebook_env.
        self._top_level_pkgs: list[str] | None = None
        # Cache of the notebook-vs-module sniff (see _is_notebook), keyed by absolute
        # path → (mtime_ns, is_notebook). /api/state re-lists on every refresh, so this
        # avoids re-reading every .py off disk each time; a changed mtime invalidates it.
        self._notebook_cache: dict[str, tuple[int, bool]] = {}

    # -- helpers -------------------------------------------------------------

    @property
    def cfg(self) -> config.Config:
        from dataclasses import replace

        cfg = self.app_cfg.config_for(None)
        # Fold the repo's synced sub-folders (mooring.toml [sync] folders) into the
        # scope so a notebook created in a uv-workspace package folder lists, opens,
        # and syncs like any other. Re-read here (not cached) so a folder registered
        # by a New on this run shows up on the very next /api/state.
        folders = workspace_config.merge_extra_folders(cfg.folders, cfg.workspace())
        return cfg if folders == cfg.folders else replace(cfg, folders=folders)

    def reload(self) -> None:
        with self._lock:
            self.app_cfg = config.load_app_config()
        # Chat context (schema + notebook source) is bound to the old config;
        # drop sessions so a new chat picks up the new repo/workspace.
        self._close_all_chats()
        # In-flight batches are bound to the old workspace too — cancel them (their
        # un-reviewed proposals are lost; the UI warns not to switch repos mid-batch).
        self._abort_all_batches()
        # The provider is shaped by [ai] provider/model — a reload may change them,
        # so drop the cached one (rebuilt lazily on next use).
        with self._provider_lock:
            self._provider = None
            self._provider_key = None
        # Warm the editor for the now-active workspace off the user's first click.
        self.prewarm_editor()

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
        # Lock so a pre-warm thread and a concurrent Open don't both Popen marimo for
        # the same cold workspace; the second caller then finds it already running.
        with self._editor_lock:
            editor = self.editors.setdefault(
                str(workspace), EditorServer(workspace, theme=self.app_cfg.ui_theme)
            )
            editor.ensure_started()
            return editor

    def prewarm_editor(self) -> None:
        """Start the active workspace's marimo subprocess in the background so the
        first notebook click finds it already running (skipping the ~seconds-long
        spawn + readiness wait, and on the uv path the cold venv build). Best-effort
        and idempotent — ``ensure_started`` short-circuits when already running, and
        any failure is swallowed so a warm attempt never breaks the hub.

        Warms in local (no-repo) mode too: ``cfg.workspace()`` always resolves to a
        real directory, and the no-repo flow's whole promise is "open a notebook now",
        so it must not pay the cold start the configured flow avoids."""
        if not self._prewarm_enabled:
            return
        workspace = self.cfg.workspace()

        def _warm() -> None:
            with contextlib.suppress(Exception):
                self.ensure_editor_for(workspace)

        threading.Thread(target=_warm, name="editor-prewarm", daemon=True).start()

    def warmup(self) -> None:
        """Pre-import the heavy, one-time modules the first chat-open / live-probe
        would otherwise pay inline (marimo's import tree; the Copilot SDK), on a
        background thread at hub start. Best-effort; gated on the AI being enabled so
        a non-AI user never pays the Copilot import. Never raises."""
        self._prewarm_enabled = True
        self.prewarm_editor()
        if not self.app_cfg.ai_enabled:
            return

        def _warm() -> None:
            with contextlib.suppress(Exception):
                import marimo  # noqa: F401  # prime the import cache for the live probe
            with contextlib.suppress(Exception):
                import copilot  # noqa: F401  # prime the Copilot SDK import
            with contextlib.suppress(Exception):
                # Prime the provider's auth/model caches so the first open is warm too —
                # status() first so the hub's Copilot sign-in row can show "connected as
                # @x" without the user clicking Check (and without /api/state ever
                # spawning the CLI itself; this runs on the background warmup thread).
                provider = self._provider_for()
                provider.status()
                provider.list_models()

        threading.Thread(target=_warm, name="hub-warmup", daemon=True).start()

    def _close_all_chats(self) -> None:
        with self._chat_lock:
            sessions = list(self._chats.values())
            self._chats.clear()
            self._chat_targets.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                session.close()  # ty: ignore[unresolved-attribute]

    def _close_chat(self, sid: str) -> None:
        """Tear down one chat session (drop its target, close the provider)."""
        with self._chat_lock:
            session = self._chats.pop(sid, None)
            self._chat_targets.pop(sid, None)
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()  # ty: ignore[unresolved-attribute]

    def _close_chats_for_notebook(self, workspace: Path, notebook_rel: str) -> int:
        """Close every live chat bound to one notebook. Used when AI is disabled for
        it, so a window opened before the toggle stops streaming. Returns the count."""
        want = (str(workspace), workspace_config.normalize_notebook(notebook_rel))
        with self._chat_lock:
            sids = [
                sid
                for sid, (ws, nb) in self._chat_targets.items()
                if (ws, workspace_config.normalize_notebook(nb)) == want
            ]
        for sid in sids:
            self._close_chat(sid)
        return len(sids)

    def _disabled_block(self, sid: str) -> JSONResponse | None:
        """The per-notebook opt-out gate shared by send/apply/rollback: if the
        session's notebook is AI-disabled, tear the session down and return the 403
        the chat UI locks on, else None. Re-checked at each egress (not just open)
        because the notebook may be disabled mid-session from the hub or a sync."""
        with self._chat_lock:
            target = self._chat_targets.get(sid)
        if target and workspace_config.is_ai_disabled(Path(target[0]), target[1]):
            self._close_chat(sid)
            return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
        return None

    def shutdown(self) -> None:
        self._close_all_chats()
        self._abort_all_batches()
        for editor in self.editors.values():
            # Suppress per editor (mirrors _close_all_chats): one editor failing to
            # die must not leak the others' marimo trees or skip the lifespan's
            # telemetry.flush that runs right after this returns.
            with contextlib.suppress(Exception):
                editor.shutdown()

    def _files_artifacts(
        self, report: sync.StatusReport, workspace: Path
    ) -> tuple[list[dict], list[dict]]:
        """Build the /api/state ``files`` + ``artifacts`` rows from a status report.

        Shared by the logged-in (sync) branch and the local (no-repo) branch: the
        report carries either real three-way sync states or ``LOCAL`` rows, and the
        row shape is identical so the front-end renders both the same way. PBIP
        members are grouped into artifacts; per-notebook AI opt-outs (the synced
        ``mooring.toml``) are flagged so the row hides its AI-open button. Files present
        on the remote branch also carry a ``github_url`` (View on GitHub).
        """
        cfg = self.cfg
        artifacts, _ = pbip.group(report.files)
        artifact_of = {m.path: a.key for a in artifacts for m in a.members}
        # Notebooks the team has turned the copilot off for (synced mooring.toml).
        ai_off = workspace_config.disabled_notebooks(workspace)
        # Notebooks whose filename shadows an importable module (e.g. polars.py) —
        # surfaced as a per-row badge instead of an inscrutable kernel traceback.
        shadowed: dict[str, str] = {}
        if self.cfg.warn_shadowed_notebooks:
            extra, ignore = self._shadow_policy(workspace)
            shadowed = shadow.scan(
                [f.path for f in report.files], workspace=workspace, extra=extra, ignore=ignore
            )
        def _has_local(f: sync.FileStatus) -> bool:
            # A LOCAL row is on disk by definition (local_report doesn't hash, so it
            # carries no sha); a sync row reports presence via its local_sha.
            return f.state is sync.FileState.LOCAL or f.local_sha is not None

        # Tell a runnable marimo notebook from a plain helper module (sniffed off disk).
        # Only meaningful for a .py that exists locally; drives the Open/AI buttons and
        # the "module" badge, and keeps the editor from opening (and rewriting) a module.
        notebooks = {
            f.path
            for f in report.files
            if f.path.endswith(".py") and _has_local(f) and self._is_notebook(workspace, f.path)
        }
        files = [
            {
                "path": f.path,
                "state": f.state.value,
                "has_local": _has_local(f),
                **({"artifact": artifact_of[f.path]} if f.path in artifact_of else {}),
                **({"ai_disabled": True} if f.path.endswith(".py") and f.path in ai_off else {}),
                **({"shadows": shadowed[f.path]} if f.path in shadowed else {}),
                **({"is_notebook": True} if f.path in notebooks else {}),
                **(
                    {"is_module": True}
                    if f.path.endswith(".py") and _has_local(f) and f.path not in notebooks
                    else {}
                ),
                # A "View on GitHub" link for any file that exists on the remote branch
                # (a non-null remote sha == present at cfg.branch HEAD). It shows the
                # REMOTE version, which can differ from unpushed local edits; it is
                # omitted for local-only/never-pushed and remote-deleted files (whose
                # blob URL would 404) and in no-repo mode (no remote sha at all).
                **(
                    {"github_url": blob_url(cfg.owner, cfg.repo, cfg.branch, f.path, host=cfg.host)}
                    if f.remote_sha is not None and cfg.is_configured
                    else {}
                ),
            }
            for f in report.files
        ]
        arts = [
            {
                "key": a.key,
                "name": a.name,
                "pointer": a.pointer,
                # An all-local artifact has nothing to sync, so its aggregate badge
                # reads "local" rather than the sync default ("synced").
                "state": "local"
                if all(m.state is sync.FileState.LOCAL for m in a.members)
                else pbip.aggregate_state(a.members),
                "members": [m.path for m in a.members],
                "to_push": sum(1 for m in a.members if m.state in sync.PUSH_STATES),
                "to_pull": sum(1 for m in a.members if m.state in sync.PULL_STATES),
                "conflicts": sum(1 for m in a.members if m.state is sync.FileState.CONFLICT),
            }
            for a in artifacts
        ]
        return files, arts

    def _is_notebook(self, workspace: Path, rel: str) -> bool:
        """Whether the local ``.py`` at ``rel`` is a marimo notebook (vs a plain helper
        module). A blank/whitespace-only file counts as a notebook — it opens as a fresh
        notebook, matching the open guards (so the hub never badges a blank stub a
        'module' while /api/open would happily open it) — EXCEPT a dunder package marker
        like ``__init__.py``, which is a module even when empty (see
        :func:`notebook_template.opens_as_notebook`). Reads the whole file (the marimo
        marker can sit past a large header) but caches by mtime, so the per-row sniff on
        every /api/state doesn't re-read unchanged files. Missing/unreadable → False."""
        path = workspace / rel
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return False
        key = str(path)
        cached = self._notebook_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            source = path.read_bytes().decode("utf-8", "ignore")
        except OSError:
            return False
        result = notebook_template.opens_as_notebook(rel, source)
        self._notebook_cache[key] = (mtime, result)
        return result

    def _installed_top_level(self) -> list[str]:
        if self._top_level_pkgs is None:
            from mooring import pyproject_env

            self._top_level_pkgs = pyproject_env.installed_top_level()
        return self._top_level_pkgs

    def _shadow_policy(self, workspace: Path) -> tuple[frozenset[str], frozenset[str]]:
        """The (extra, ignore) sets parameterising the shadow guard — the hub's
        single assembly point, mirroring ``cli._shadow_policy`` (the two adapters
        can't share it: the hub must not import the cli)."""
        return (
            pyproject_env.importable_names(workspace),
            frozenset(workspace_config.shadow_ignored(workspace)),
        )

    def _notebook_env(self, workspace: Path) -> dict:
        """Where a notebook's packages come from, the actively-selected list (the
        repo's ``pyproject.toml`` deps, or the env's top-level packages when there's
        no project), and how to add one — for the hub footer. The mode + add guidance
        depend on whether notebooks run in a locked uv project, mooring's bundled
        env, or a frozen build that can't be changed at runtime.
        """
        from mooring import pyproject_env
        from mooring.editor import uses_uv

        uv_mode = uses_uv(workspace)
        declared = pyproject_env.declared_deps(workspace)
        if uv_mode or declared:
            # A workspace pyproject.toml is the source of truth either way: uv runs it,
            # and a frozen build was built from it. Show its dependency list verbatim.
            packages, source = declared, "pyproject"
        else:
            # No notebook project: approximate the deliberately-chosen packages by the
            # env's root distributions (e.g. what `uvx --with` added), since notebooks
            # share this interpreter in bundle mode.
            packages, source = self._installed_top_level(), "env"

        if uv_mode:
            summary = (
                "Notebooks run in this project's locked environment (pyproject.toml + uv.lock)."
            )
            add_hint = "Add a package with `mooring deps add <name>`, then Push to share it with your team."
        elif pyproject_env.uv_available():
            summary = "Notebooks run in mooring's bundled Python environment."
            add_hint = (
                "Add a package by relaunching as `uvx --with <name> mooring`, or set up a locked, "
                "shareable project with `mooring init` then `mooring deps add <name>`."
            )
        else:
            summary = "Notebooks run in this frozen build's bundled environment."
            add_hint = (
                "Its packages were fixed when the build was made and can't be added here — ask your "
                "admin to add the package to the repo's pyproject.toml and rebuild the bundle."
            )
        return {
            "mode": "uv" if uv_mode else "bundle",
            "source": source,
            "packages": packages,
            "summary": summary,
            "add_hint": add_hint,
        }

    # -- endpoints -------------------------------------------------------------

    def api_state(self, _request: Request) -> JSONResponse:
        cfg = self.cfg
        body: dict = {
            "version": __version__,
            "configured": cfg.is_configured,
            "repo": cfg.repo_slug if cfg.is_configured else "",
            "branch": cfg.branch,
            "host": cfg.host,
            "workspace": str(cfg.workspace()),
            "workspace_hint": workspace_hint(cfg),
            # The declared sync folders, so the hub can group files by folder and show
            # the structure (incl. an adopted/declared folder that is still empty) —
            # "here's where notebooks go" even before the first file lands.
            "folders": list(cfg.folders),
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
            # What notebooks can import + how to add packages (mode-aware: locked uv
            # project vs mooring's bundled env vs a frozen build). See _notebook_env.
            "env": self._notebook_env(cfg.workspace()),
            "ai_chat": self.app_cfg.ai_enabled,
            # Whether the workspace-level "Batch build" entry should show (AI on AND
            # the opt-in batch orchestrator enabled). The page itself re-gates.
            "ai_batch": self.app_cfg.ai_enabled and self.app_cfg.ai_batch_enabled,
            # "local" = no repo configured: the UI shows the notebook surface
            # (list/new/open/edit/AI) backed by the local workspace, with sync hidden.
            # "repo" = a team repo is configured (login then unlocks sync).
            "mode": "repo" if cfg.is_configured else "local",
            "datasets": [],
            "logged_in": False,
            "user": "",
            "files": [],
            "artifacts": [],
        }
        # Dataset paths (for the chat's @-mention autocomplete) used to be computed
        # here — a recursive data-folder walk on every hub refresh. They are only
        # consumed by the chat window, which now fetches them from the lighter
        # /api/ai/datasets, so the walk no longer rides on /api/state.
        if not cfg.is_configured:
            # Local mode: no repo, no login. List notebooks straight off disk so they
            # can be created/opened/edited (and AI'd) right now; sync (pull/push/
            # propose) needs a repo and stays unavailable until one is connected.
            report = sync.local_report(cfg.workspace(), cfg.folders, cfg.exclude)
            body["files"], body["artifacts"] = self._files_artifacts(report, cfg.workspace())
            return JSONResponse(body)
        if not auth.get_token(host=cfg.host):
            return JSONResponse(body)
        try:
            body["user"] = self.username()
            body["logged_in"] = True
            report = sync.status(self.client(), cfg)
            body["files"], body["artifacts"] = self._files_artifacts(report, cfg.workspace())
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

    # -- settings / profile ----------------------------------------------------
    # A per-machine Settings page reached from the header. The editable surface is
    # the curated registry in settings_schema.py — which IS the allowlist the rest
    # of the config write path lacks (config_store.set_value writes any key verbatim).
    # Writes mirror api_set_theme: persist via config_store, then re-read the config
    # in place (NOT the destructive reload()). Team/synced decisions (the per-notebook
    # AI opt-out, sync folders) and the structural value-blindness guarantees are
    # intentionally NOT here. See docs/admins/configuration.md.

    def settings_page(self, _request: Request) -> HTMLResponse:
        """The Settings page, served like chat/batch so it pre-paints the theme."""
        return self._themed_page("settings.html")

    @staticmethod
    def _enum_options(spec) -> list[dict] | None:
        """[{value, label}] for an enum control (friendly labels where the spec gives
        them, else the raw token), or None for a non-enum control."""
        if not spec.enum_values:
            return None
        labels = spec.enum_labels or spec.enum_values
        return [{"value": v, "label": label} for v, label in zip(spec.enum_values, labels)]

    def _needs_confirm(self, spec, value) -> bool:
        """Whether writing ``value`` is a privacy-weakening flip that needs an explicit
        confirm. Wraps the registry rule with one runtime refinement: the warn-only
        downgrade of ``ai.pii.block_prompt`` only weakens anything when the scan itself
        (``ai.pii.enabled``) is on, so we don't pop a scary dialog for a no-op toggle."""
        if not settings_schema.needs_confirm(spec, value):
            return False
        if spec.key == "ai.pii.block_prompt" and not self.app_cfg.ai_pii:
            return False
        return True

    def _settings_payload(self) -> dict:
        """Value-free snapshot of every editable setting for the page: the EFFECTIVE
        value (read off the live app_cfg, so it reflects MOORING_* overrides — what the
        app actually runs with) plus whether an env var is masking the file, the
        read-only admin block, and the live PII guard status."""
        import os

        cfg = self.app_cfg
        editable = []
        for spec in settings_schema.EDITABLE:
            value = getattr(cfg, spec.accessor)
            if isinstance(value, tuple):
                value = list(value)
            editable.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "group": spec.group,
                    "type": spec.type,
                    "control": spec.control,
                    "value": value,
                    "default": spec.default,
                    "sensitivity": spec.sensitivity,
                    "weakens": spec.weaken_value is not None,
                    "enum_options": self._enum_options(spec),
                    "min": spec.minimum,
                    "max": spec.maximum,
                    "help": spec.help,
                    "env_overridden": bool(
                        spec.env_var and os.environ.get(spec.env_var) is not None
                    ),
                }
            )
        return {
            "groups": list(settings_schema.GROUPS),
            "editable": editable,
            "admin": self._admin_rows(),
            "pii": self._pii_status(),
            "ai_enabled": cfg.ai_enabled,
        }

    def _admin_rows(self) -> list[dict]:
        """Read-only 'managed by your admin' rows: identity, telemetry, the NER model
        supply-chain pins, and the team-consistent sync scope. Value-free — the logging
        endpoint URL and the OAuth client id are shown only as on/off / present-absent,
        never their literal value."""
        cfg = self.app_cfg
        single = cfg.config_for(None)
        return [
            {"label": "GitHub OAuth client id", "value": "set" if cfg.client_id else "not set"},
            {"label": "Repo owner", "value": single.owner or "—"},
            {"label": "Repo", "value": single.repo or "—"},
            {"label": "GitHub host", "value": cfg.host},
            {"label": "AI provider", "value": cfg.ai_provider},
            {"label": "Central logging", "value": f"on ({cfg.log_level})" if cfg.log_endpoint else "off"},
            {"label": "PII name model", "value": cfg.ai_pii_name_model},
            {"label": "PII name model revision", "value": cfg.ai_pii_name_revision or "latest"},
            {"label": "PII name model variant", "value": cfg.ai_pii_name_variant or "default"},
            {"label": "Synced folders", "value": ", ".join(cfg.folders) or "—"},
            {"label": "Sync excludes", "value": ", ".join(cfg.exclude) or "—"},
        ]

    def api_get_settings(self, _request: Request) -> JSONResponse:
        return JSONResponse(self._settings_payload())

    def _apply_setting_change(self) -> None:
        """Make a just-written config.toml change live WITHOUT the destructive reload():
        re-read the whole config under the lock (so the loader applies every
        normalization and the TOML-key -> field mapping in one place), re-theme open
        editors if the theme changed, and tear down chats if the copilot was turned off.
        Open chats/batches otherwise survive — a model/PII change applies to the NEXT
        chat (its guard/model is captured at open), mirroring the theme endpoint. The
        provider auto-rebuilds for a new model because _provider_for keys on it."""
        was_ai = self.app_cfg.ai_enabled
        old_theme = self.app_cfg.ui_theme
        with self._lock:
            self.app_cfg = config.load_app_config()
        if self.app_cfg.ui_theme != old_theme:
            for editor in list(self.editors.values()):
                editor.apply_theme(self.app_cfg.ui_theme)
        if was_ai and not self.app_cfg.ai_enabled:
            self._close_all_chats()

    async def api_set_settings(self, request: Request) -> JSONResponse:
        """Persist one per-machine setting and make it live. The allowlist is the
        registry: a key with no SettingSpec is a 400, so this can never write the
        dead/unread keys a raw `mooring config set` could. A privacy-weakening flip
        needs an explicit confirm (409 needs_confirm otherwise)."""
        data = await request.json()
        key = str(data.get("key", ""))
        spec = settings_schema.by_key(key)
        if spec is None:
            return JSONResponse({"error": f"Unknown or read-only setting {key!r}."}, status_code=400)
        if "value" not in data:
            return JSONResponse({"error": "A value is required."}, status_code=400)
        try:
            value = settings_schema.coerce(spec, data["value"])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if self._needs_confirm(spec, value) and not bool(data.get("confirm")):
            return JSONResponse(
                {"needs_confirm": True, "key": key, "message": spec.confirm}, status_code=409
            )
        try:
            config_store.set_value(key, value)
        except tomllib.TOMLDecodeError:
            return JSONResponse(
                {"error": "Your config.toml is malformed — fix it before changing settings."},
                status_code=409,
            )
        except OSError as exc:
            return JSONResponse({"error": f"Could not save the setting: {exc}"}, status_code=502)
        self._apply_setting_change()
        # Value-free telemetry: the key plus, for non-text settings, the new
        # boolean/number/enum — never a model id, label, or path.
        extra = {"value": value} if spec.type in ("bool", "int", "float", "enum") else {}
        telemetry.log_event("settings_change", key=key, **extra)
        return JSONResponse({"ok": True, **self._settings_payload()})

    async def api_reset_settings(self, request: Request) -> JSONResponse:
        """Revert one setting to the packaged default (delete it from config.toml)."""
        data = await request.json()
        key = str(data.get("key", ""))
        spec = settings_schema.by_key(key)
        if spec is None:
            return JSONResponse({"error": f"Unknown or read-only setting {key!r}."}, status_code=400)
        # Resetting can itself be the weakening direction (e.g. ai.pii.enabled reverts
        # to its off default), so gate it the same as a set rather than letting Reset
        # silently slip past the confirm the toggle requires.
        if self._needs_confirm(spec, spec.default) and not bool(data.get("confirm")):
            return JSONResponse(
                {"needs_confirm": True, "key": key, "message": spec.confirm}, status_code=409
            )
        try:
            config_store.unset_value(key)
        except tomllib.TOMLDecodeError:
            return JSONResponse(
                {"error": "Your config.toml is malformed — fix it before changing settings."},
                status_code=409,
            )
        except OSError as exc:
            return JSONResponse({"error": f"Could not save the setting: {exc}"}, status_code=502)
        self._apply_setting_change()
        telemetry.log_event("settings_reset", key=key)
        return JSONResponse({"ok": True, **self._settings_payload()})

    def api_login_start(self, _request: Request) -> JSONResponse:
        try:
            device = auth.start_device_flow(self.cfg.client_id, host=self.cfg.host)
        except Exception as exc:  # noqa: BLE001  # shown in the UI
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

    def api_login_poll(self, _request: Request) -> JSONResponse:
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

    def api_logout(self, _request: Request) -> JSONResponse:
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

    def api_discover(self, _request: Request) -> JSONResponse:
        """Top-level repo folders that hold files outside the synced folders — the
        adopt candidates. Read-only; called on demand by the hub (not on every
        /api/state) so the extra full-tree read stays off the refresh hot path."""
        cfg = self.cfg
        if not cfg.is_configured or not auth.get_token(host=cfg.host):
            return JSONResponse({"candidates": []})
        try:
            candidates = sync.discover_unsynced_folders(self.client(), cfg)
        except (GitHubError, OSError) as exc:
            telemetry.log_error(exc=exc, op="discover")
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse(
            {
                "candidates": [
                    {"folder": c.folder, "files": c.files, "py_files": c.py_files}
                    for c in candidates
                ]
            }
        )

    async def api_adopt(self, request: Request) -> JSONResponse:
        """Register the chosen folders in the synced ``mooring.toml`` and pull them.

        The request's folders are validated against what discovery actually found, so
        adopt never registers a non-existent folder, then re-derives the scope and runs
        a normal pull through :meth:`_sync_op` (so the response shape matches push/pull)."""
        from dataclasses import replace

        data = await request.json() if await request.body() else {}
        requested = [str(f) for f in (data.get("folders") or [])]
        if not requested:
            return JSONResponse({"error": "No folders given."}, status_code=400)
        cfg = self.cfg
        try:
            known = {c.folder for c in sync.discover_unsynced_folders(self.client(), cfg)}
        except (GitHubError, OSError) as exc:
            telemetry.log_error(exc=exc, op="adopt")
            return JSONResponse({"error": str(exc)}, status_code=502)
        chosen = [
            folder
            for folder in (workspace_config.normalize_notebook(r) for r in requested)
            if folder in known
        ]
        if not chosen:
            return JSONResponse({"error": "None of those folders are adoptable."}, status_code=400)
        workspace = cfg.workspace()
        try:
            workspace_config.add_extra_folders(workspace, chosen)
        except tomllib.TOMLDecodeError as exc:
            return JSONResponse(
                {"error": f"{workspace_config.WORKSPACE_CONFIG_NAME} is not valid TOML: {exc}"},
                status_code=400,
            )
        new_cfg = replace(cfg, folders=workspace_config.merge_extra_folders(cfg.folders, workspace))
        return self._sync_op("adopt", lambda: sync.pull(self.client(), new_cfg))

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
        cfg = self.cfg
        try:
            rel_path = notebook_template.create_from_input(
                cfg.workspace(), data.get("name", ""), folders=cfg.folders, exclude=cfg.exclude
            )
        except (ValueError, FileExistsError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        telemetry.log_event("new")
        return self._open(rel_path)

    async def api_open(self, request: Request) -> JSONResponse:
        data = await request.json()
        # _open may spawn the marimo subprocess and block on its readiness poll;
        # run it off the event loop so the first open doesn't freeze the whole hub.
        return await run_in_threadpool(self._open, data.get("path", ""))

    async def api_reveal(self, request: Request) -> JSONResponse:
        """Reveal a file in the OS file manager so the user can open a non-marimo .py
        (a plain helper module) in their own editor. Deliberately SEPARATE from
        /api/open — that stays the marimo-notebook path and still refuses modules
        (opening one in marimo would rewrite it into notebook form). Revealing the
        folder also sidesteps the Windows trap where the default verb for a .py runs
        the script. Reuses _ws_file's containment + dot-part guards, so .mooring/ and
        any workspace escape are unreachable."""
        data = await request.json()
        rel_path = str(data.get("path", ""))
        try:
            target = self._ws_file(self.cfg.workspace(), rel_path)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"error": f"No such file: {rel_path}"}, status_code=404)
        try:
            reveal.reveal(target)
        except reveal.RevealError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        telemetry.log_event("open", kind="reveal")
        name = rel_path.rsplit("/", 1)[-1]
        return JSONResponse({"path": rel_path, "lines": [f"Revealed {name} in the file manager"]})

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

    async def api_rollback(self, request: Request) -> JSONResponse:
        """Restore one notebook to its last-synced version (the manifest base),
        discarding local edits. The pre-revert bytes of a ``.py`` are snapshotted onto
        the local undo stack first and the snapshot token returned (``undo_token``), so
        :meth:`api_undo` can put them back — and refuse if a later write has since
        landed on top. Held under ``_apply_lock`` so the snapshot+write can't race an
        in-flight AI Apply on the same notebook (the same guard Apply/Undo take)."""
        from mooring import notebook_undo

        data = await request.json()
        rel_path = str(data.get("path", ""))
        include_conflict = bool(data.get("conflicts"))
        workspace = self.cfg.workspace()
        captured: dict[str, str] = {}

        def snapshot_fn(rel: str, content: bytes) -> None:
            if rel.endswith(".py"):
                captured["token"] = notebook_undo.snapshot(workspace, rel, content)

        try:
            with self._apply_lock:
                result = sync.revert(
                    self.client(),
                    self.cfg,
                    rel_path,
                    include_conflict=include_conflict,
                    snapshot_fn=snapshot_fn,
                )
        except (GitHubError, OSError) as exc:
            telemetry.log_error(exc=exc, op="rollback")
            return JSONResponse({"error": str(exc)}, status_code=502)
        telemetry.log_event("rollback", reverted=result.reverted, lines=len(result.lines))
        body = {"lines": result.lines, "summary": result.summary()}
        if "token" in captured:
            body["undo_token"] = captured["token"]
        return JSONResponse(body)

    async def api_undo(self, request: Request) -> JSONResponse:
        """Restore a notebook's most recent local snapshot — the pre-revert (or
        pre-AI-edit) bytes. AI-independent: unlike :meth:`api_chat_rollback` this is
        not bound to a chat session or gated on the AI being enabled, so a Revert done
        from the file list is itself undoable. The snapshot stack is shared LIFO, so a
        ``token`` (from /api/rollback) must still be the newest entry — otherwise a
        later write (e.g. an AI Apply) is on top and we refuse (409) rather than
        restore the wrong layer."""
        data = await request.json()
        rel_path = str(data.get("path", ""))
        token = str(data.get("token", "")) or None
        workspace = self.cfg.workspace()
        try:
            nb_path = self._ws_file(workspace, rel_path, suffix=".py")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"error": f"No such notebook: {rel_path}"}, status_code=404)
        try:
            outcome = await asyncio.to_thread(
                self._restore_undo, nb_path, workspace, rel_path, expect_token=token
            )
        except OSError as exc:  # momentarily locked — the snapshot is kept to retry
            return JSONResponse(
                {"error": f"Could not restore the notebook: {exc}"}, status_code=502
            )
        if outcome is _UNDO_SUPERSEDED:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "A later change is on top of your revert, so Undo would "
                    "restore the wrong version.",
                },
                status_code=409,
            )
        if outcome is None:
            return JSONResponse({"ok": False, "error": "Nothing to undo."}, status_code=400)
        telemetry.log_event("undo")
        return JSONResponse({"ok": True, "can_undo": outcome > 0, "undo_depth": outcome})

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
        # Refuse to open a plain Python module as a notebook: the marimo editor would
        # rewrite it into notebook form on save, corrupting a helper module. A blank
        # stub is allowed (it becomes a new notebook); a non-empty module without the
        # marimo.App marker — and a dunder package marker like __init__.py even when
        # empty — is not (see notebook_template.opens_as_notebook). The hub already hides
        # Open on such rows (is_module) and offers Reveal instead; this backstops a direct
        # call / stale client. Reads the whole file — the marker can sit past a large
        # leading header.
        source = target.read_bytes().decode("utf-8", "ignore")
        if not notebook_template.opens_as_notebook(rel_path, source):
            return JSONResponse(
                {
                    "error": (
                        f"{rel_path.rsplit('/', 1)[-1]} is a Python module, not a marimo "
                        "notebook — opening it in the editor could overwrite it. Import it "
                        "from a notebook instead."
                    )
                },
                status_code=400,
            )
        try:
            editor = self.ensure_editor()
        except Exception as exc:  # noqa: BLE001  # shown in the UI
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
        # The shadow trap is backend-independent (plain sys.path[0] resolution), so it
        # is checked outside the use_uv() gate. Folder-scoped: opening an innocent
        # notebook still warns when a sibling poisons the directory. Merged into the
        # single `warning` string the front-end shows (never clobbering missing-deps).
        if self.cfg.warn_shadowed_notebooks:
            extra, ignore = self._shadow_policy(workspace)
            # The notebook's folder AND the workspace root are both on the kernel's
            # sys.path (the latter via runtime.pythonpath — see editor.py), so scan both.
            findings = {
                **shadow.root_shadows(workspace, extra=extra, ignore=ignore),
                **shadow.folder_shadows(rel_path, workspace=workspace, extra=extra, ignore=ignore),
            }
            if findings:
                names = ", ".join(sorted(set(findings.values())))
                offenders = ", ".join(sorted(findings))
                note = (
                    f"Notebook name(s) shadow an importable module ({offenders} → {names}). "
                    "Rename the file(s); otherwise notebooks in this folder can fail to import."
                )
                existing = payload.get("warning")
                payload["warning"] = f"{existing}\n{note}" if existing else note
        return JSONResponse(payload)

    # -- AI copilot (chat) -----------------------------------------------------

    def _ws_file(self, workspace: Path, rel: str, *, suffix: str | None = None) -> Path:
        """Resolve a workspace-relative path, rejecting escapes/missing files."""
        # Reject any dot-prefixed path component (mirrors sync.is_synced_path) so the
        # internal state dir — .mooring/ (manifest + undo snapshots) — is structurally
        # unreachable through this resolver regardless of caller. Defence in depth.
        if any(part.startswith(".") for part in Path(str(rel).replace("\\", "/")).parts):
            raise ValueError("Path is not allowed.")
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
            # Resolve "auto" -> concrete and shape the name model only HERE, under the
            # ai_pii gate — so a default (guard-off) install never imports spaCy just
            # to pick a backend it won't use. Consistent with the chat session.
            pii_backend = ner.resolve_backend(self.app_cfg.ai_pii_name_backend)
            pii_name_model = ner.model_for(
                pii_backend,
                self.app_cfg.ai_pii_name_model,
                self.app_cfg.ai_pii_name_revision,
                self.app_cfg.ai_pii_name_variant,
            )
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

        # Schemas of dataframes LIVE in the running kernel are DEFERRED off the open
        # path (a freshly opened notebook's kernel is often still loading frames, so
        # the probe's worst case is a multi-second poll). The very first turn picks
        # them up via the per-turn refresh (api_chat_send -> _live_schema_for_sid),
        # over the SAME value-free probe -> scrub -> format pipeline, so nothing about
        # the privacy contract changes — only WHEN the probe runs. The system context
        # opens on the file-based schema; the live schema joins on turn 1.
        live_text = ""

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
        except Exception:  # noqa: BLE001  # never block chat on introspection
            return "", []

    def _live_schema_for_sid(self, sid: str) -> tuple[str, list[dict]]:
        """The current live-kernel schema for an open chat session (best-effort)."""
        with self._chat_lock:
            target = self._chat_targets.get(sid)
        if target is None:
            return "", []
        workspace_str, notebook_rel = target
        return self._live_schema_text(Path(workspace_str), notebook_rel)

    def _provider_for(self):
        """The shared AI provider, built once and reused so its auth (45s) and
        model-list (300s) TTL caches survive across opens instead of being rebuilt
        and discarded per request. Rebuilt when the provider/model config changes.

        Imports ``get_provider`` late (not at module load) so a test that
        monkeypatches ``mooring.ai.get_provider`` still takes effect."""
        from mooring.ai import get_provider

        key = (self.app_cfg.ai_provider, self.app_cfg.ai_model)
        with self._provider_lock:
            if self._provider is None or self._provider_key != key:
                self._provider = get_provider(self.app_cfg)
                self._provider_key = key
            return self._provider

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
        Raises AIError (-> 502) if Copilot isn't available/installed; a sign-in or
        handshake failure surfaces over the SSE stream instead (the session starts
        in the background — ``background=True`` — so the open response is immediate).
        """
        provider = self._provider_for()
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
            # Don't block the open request on the (CLI-spawning, networked) Copilot
            # handshake — stream readiness/failure over the SSE channel instead.
            background=True,
        )

    def _reap_idle_chats(self) -> None:
        timeout = self.app_cfg.ai_chat_idle_timeout
        with self._chat_lock:
            dead = [sid for sid, s in self._chats.items() if s.idle_seconds() > timeout]  # ty: ignore[unresolved-attribute]
            sessions = [self._chats.pop(sid) for sid in dead]
            for sid in dead:
                self._chat_targets.pop(sid, None)
        for session in sessions:
            with contextlib.suppress(Exception):
                session.close()  # ty: ignore[unresolved-attribute]

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

    def index_page(self, _request: Request) -> HTMLResponse:
        return self._themed_page("index.html")

    def chat_page(self, _request: Request) -> HTMLResponse | JSONResponse:
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
        # Per-notebook opt-out (synced mooring.toml). 403 + reason distinguishes
        # this from the global-off 404 above, so the chat UI shows the right message.
        if workspace_config.is_ai_disabled(workspace, notebook):
            return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
        try:
            # File IO (notebook source, dataset schema, team context) — off the event
            # loop so a slow read can't stall the hub's other requests.
            context, index, pii_banner, live_text = await run_in_threadpool(
                self._build_chat_context, workspace, notebook, dataset
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
        except Exception as exc:  # noqa: BLE001  # AIError surfaces to the UI in Phase 1
            return JSONResponse({"error": str(exc)}, status_code=502)
        # The live-kernel schema is deferred off the open path (see _build_chat_context),
        # so live_text is ""; the first turn picks it up. This seeds the (empty) snapshot.
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
            {
                "sid": sid,
                "notebook": notebook,
                "pii": pii_banner,
                "guard": self._pii_status(),
                # Whether the chat is usable NOW. A backgrounded provider session is
                # still starting (Copilot handshake) — the UI shows "connecting…" and
                # waits for the "ready"/"fail" event on the stream. The stub/already-
                # ready sessions report True and the UI enables the input immediately.
                "ready": session.is_ready(),
            }
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

    def api_chat_stream(self, request: Request) -> StreamingResponse | JSONResponse:
        # Sync: this handler only builds the StreamingResponse; the awaiting happens
        # inside _sse_gen (the async generator it wraps), so there's nothing to await here.
        sid = request.path_params["sid"]
        session = self._chats.get(sid)
        if session is None:
            return JSONResponse({"error": _UNKNOWN_CHAT_SESSION}, status_code=404)
        return StreamingResponse(
            self._sse_gen(session),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _sse_gen(self, session):
        q = session.subscribe()
        try:
            yield ": connected\n\n"
            # Replay startup readiness so a subscriber that connects after the (async,
            # backgrounded) provider handshake finished — or failed — still learns the
            # outcome and unblocks the input, even though the live "ready"/"fail" event
            # fired before this subscribe.
            start_status = getattr(session, "start_status", None)
            if isinstance(start_status, dict):
                if start_status.get("state") == "ready":
                    yield "event: ready\ndata: {}\n\n"
                elif start_status.get("state") == "error":
                    fail_data = {"text": start_status.get("text", "")}
                    if start_status.get("reason"):  # e.g. "not_connected" -> sign-in button
                        fail_data["reason"] = start_status["reason"]
                    yield f"event: fail\ndata: {json.dumps(fail_data)}\n\n"
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
            return JSONResponse({"error": _UNKNOWN_CHAT_SESSION}, status_code=404)
        # Refresh the live-kernel schema so dataframes added since chat-open (or the
        # last turn) are visible without reopening. Value-free + best-effort; the
        # session re-injects it only when it changed. Off-thread — it does kernel I/O.
        live_text, live_banner = await asyncio.to_thread(self._live_schema_for_sid, sid)
        if live_banner:  # a refreshed column NAME was itself PII (withheld) — count only
            telemetry.log_event("ai_pii", findings=len(live_banner))
        # The notebook may have been disabled (from the hub, or a teammate's sync)
        # since this window opened — re-check at the LATEST point before egress. The
        # live-schema probe above can take real time (a kernel poll), a wide window;
        # this _chat_targets re-check, not the hidden button, is the real guarantee.
        if (blocked := self._disabled_block(sid)) is not None:
            return blocked
        # "Send anyway" path: forward a prompt the PII guard held, verbatim, once.
        confirm = str(data.get("confirm_token", "")).strip()
        if confirm:
            try:
                await asyncio.to_thread(session.send_confirmed, confirm, live_text)  # ty: ignore[unresolved-attribute]
            except Exception as exc:  # noqa: BLE001  # AIError surfaces to the UI
                return JSONResponse({"error": str(exc)}, status_code=502)
            telemetry.log_event("ai_chat_send", confirmed=1)
            return JSONResponse({"ok": True, "pii": live_banner})
        text = str(data.get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "Type a message."}, status_code=400)
        try:
            await asyncio.to_thread(session.send, text, live_text)  # ty: ignore[unresolved-attribute]
        except Exception as exc:  # noqa: BLE001  # AIError surfaces to the UI in Phase 1
            return JSONResponse({"error": str(exc)}, status_code=502)
        telemetry.log_event("ai_chat_send")
        return JSONResponse({"ok": True, "pii": live_banner})

    async def api_chat_apply(self, request: Request) -> JSONResponse:
        data = await request.json()
        sid = str(data.get("sid", ""))
        with self._chat_lock:
            target = self._chat_targets.get(sid)
        if target is None:
            return JSONResponse({"error": _UNKNOWN_CHAT_SESSION}, status_code=404)
        # Apply WRITES the notebook, so it is the highest-value gate. This early
        # refusal covers the common case; _apply_with_undo re-checks under
        # _apply_lock right before the write to close the toggle/write race.
        if (blocked := self._disabled_block(sid)) is not None:
            return blocked
        # The UI echoes the proposal's normalized ops; a bare ``code`` (the append
        # proposal, and the legacy contract) is normalized to a one-op append. The
        # write re-validates each edit/delete anchor against the file, so a stale
        # proposal becomes a loud 409 rather than a silent clobber.
        ops = data.get("ops")
        if isinstance(ops, list) and ops:
            op_dicts = ops
        else:
            code = str(data.get("code", ""))
            if not code.strip():
                return JSONResponse({"error": "Nothing to apply."}, status_code=400)
            op_dicts = [{"op": "append", "code": code}]
        workspace_str, notebook_rel = target
        workspace = Path(workspace_str)
        from mooring.ai.cellwrite import CellApplyConflict, CellWriteError

        try:
            nb_path = self._ws_file(workspace, notebook_rel, suffix=".py")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"error": f"No such notebook: {notebook_rel}"}, status_code=404)
        # Snapshot the pre-edit bytes (for Undo), then rewrite the .py; the editor's
        # --watch picks it up and (with watcher_on_save=autorun) re-runs the changed
        # cells, so the change appears in the open notebook tab.
        try:
            undo_depth = await asyncio.to_thread(
                self._apply_with_undo, nb_path, workspace, notebook_rel, op_dicts
            )
        except PermissionError:  # disabled between the gate above and the write
            self._close_chat(sid)
            return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
        except CellApplyConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except CellWriteError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        telemetry.log_event("ai_chat_apply")
        return JSONResponse({"ok": True, "can_undo": undo_depth > 0, "undo_depth": undo_depth})

    def _apply_with_undo(self, nb_path: Path, workspace: Path, notebook_rel: str, op_dicts) -> int:
        """Snapshot the notebook, apply the patch, and return the new undo depth.

        Runs in a thread (file IO), serialized with Undo by ``_apply_lock``. If the
        patch fails the just-taken snapshot is discarded, so a failed Apply never
        leaves a phantom Undo step.
        """
        from mooring import notebook_undo
        from mooring.ai import cellwrite

        with self._apply_lock:
            # Final TOCTOU guard: a concurrent disable writes mooring.toml before it
            # tears sessions down, so an in-flight Apply re-reads it here, under the
            # same lock, and refuses to land on the now-protected notebook.
            if workspace_config.is_ai_disabled(workspace, notebook_rel):
                raise PermissionError("notebook_disabled")
            token = notebook_undo.snapshot(workspace, notebook_rel, nb_path.read_bytes())
            try:
                cellwrite.apply_wire_patch(nb_path, op_dicts)
            except BaseException:
                notebook_undo.discard(workspace, notebook_rel, token)
                raise
            return notebook_undo.depth(workspace, notebook_rel)

    async def api_chat_rollback(self, request: Request) -> JSONResponse:
        data = await request.json()
        sid = str(data.get("sid", ""))
        with self._chat_lock:
            target = self._chat_targets.get(sid)
        if target is None:
            return JSONResponse({"error": _UNKNOWN_CHAT_SESSION}, status_code=404)
        # Rollback WRITES the notebook (restores a snapshot), so it is gated by the
        # per-notebook opt-out exactly like apply — otherwise a disabled notebook
        # could still be rewritten through the undo path.
        if (blocked := self._disabled_block(sid)) is not None:
            return blocked
        workspace_str, notebook_rel = target
        workspace = Path(workspace_str)
        try:
            nb_path = self._ws_file(workspace, notebook_rel, suffix=".py")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"error": f"No such notebook: {notebook_rel}"}, status_code=404)
        try:
            remaining = await asyncio.to_thread(
                self._restore_undo, nb_path, workspace, notebook_rel
            )
        except OSError as exc:  # e.g. the file is momentarily locked — the snapshot is kept
            return JSONResponse(
                {"error": f"Could not restore the notebook: {exc}"}, status_code=502
            )
        if remaining is None:
            return JSONResponse({"ok": False, "error": "Nothing to undo."}, status_code=400)
        telemetry.log_event("ai_chat_rollback")
        return JSONResponse({"ok": True, "can_undo": remaining > 0, "undo_depth": remaining})

    def _restore_undo(
        self, nb_path: Path, workspace: Path, notebook_rel: str, *, expect_token: str | None = None
    ):
        """Restore the most recent snapshot (the editor's --watch reloads it). Returns
        the remaining undo depth, ``None`` when there is nothing to undo, or
        :data:`_UNDO_SUPERSEDED` when ``expect_token`` is given but no longer the newest
        snapshot (a later write is on top — restoring it would revert the wrong layer).

        Write-then-discard: the snapshot is only consumed AFTER it is safely written
        back, so a failed restore leaves the undo step intact to retry (symmetric with
        the discard-on-failure in :meth:`_apply_with_undo`)."""
        from mooring import notebook_undo
        from mooring.paths import safe_write_bytes

        with self._apply_lock:
            peeked = notebook_undo.peek_latest(workspace, notebook_rel)
            if peeked is None:
                return None
            token, prior = peeked
            if expect_token is not None and token != expect_token:
                return _UNDO_SUPERSEDED
            safe_write_bytes(nb_path, prior)  # raises before the snapshot is consumed
            notebook_undo.discard(workspace, notebook_rel, token)
            return notebook_undo.depth(workspace, notebook_rel)

    def api_chat_datasets(self, _request: Request) -> JSONResponse:
        """The value-free dataset PATHS for the chat's @-mention autocomplete, plus
        the current theme. A LIGHT alternative to /api/state, which (when logged in)
        makes GitHub sync round-trips this window doesn't need. Sync def -> Starlette
        runs it in a threadpool, so the directory walk never blocks the event loop."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        from mooring import schema

        cfg = self.cfg
        datasets = schema.list_datasets(cfg.workspace(), cfg.folders)
        return JSONResponse({"datasets": datasets, "ui_theme": self.app_cfg.ui_theme})

    async def api_chat_models(self, _request: Request) -> JSONResponse:
        """The models the user can pick, plus the configured defaults (value-free)."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        provider = self._provider_for()
        models = await asyncio.to_thread(provider.list_models)
        payload = {
            "models": models,
            "default_model": self.app_cfg.ai_model or "",
            "default_effort": self.app_cfg.ai_reasoning_effort or "",
        }
        # When the list is empty because the provider REJECTED the request (e.g. a
        # 403 "not authorized to use this Copilot feature" — a signed-in but
        # unlicensed account), pass the reason through so the page can show it
        # instead of a silently empty picker. Value-free (a provider error string).
        error = getattr(provider, "models_error", lambda: "")()
        if error and not models:
            payload["error"] = error
        return JSONResponse(payload)

    # -- AI copilot (Copilot sign-in) ------------------------------------------
    # GitHub Copilot signs in SEPARATELY from mooring's GitHub login (auth.py): a
    # different OAuth flow, a different credential store (~/.copilot), and possibly
    # a different GitHub account. These endpoints expose that sign-in in the UI so a
    # user never has to drop to `mooring ai login` in a terminal.

    def _ai_status_dict(self, st) -> dict:
        """Shape a ProviderStatus (or None = not probed yet) for the UI. Value-free:
        only the connection booleans, the resolved provider name, and the signed-in
        account login (so the user can see WHICH Copilot identity is connected)."""
        if st is None:
            return {
                "enabled": True,
                "checked": False,  # no probe has run yet — the UI offers a Check button
                "available": True,
                "connected": False,
                "account": "",
                "detail": "",
                "provider": self.app_cfg.ai_provider,
            }
        return {
            "enabled": True,
            "checked": True,
            "available": bool(st.available),
            "connected": bool(st.connected),
            "account": st.account or "",
            "detail": st.detail or "",
            "provider": self.app_cfg.ai_provider,
        }

    def api_ai_status(self, request: Request) -> JSONResponse:
        """Copilot sign-in status for the hub/chat. Default returns the CACHED status
        (never spawns the 150 MB CLI on a hub poll); ``?probe=1`` forces a real check.

        Sync def => Starlette runs it in a threadpool, so the forced probe's CLI spawn
        never blocks the event loop."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        provider = self._provider_for()
        probe = request.query_params.get("probe", "").lower() in ("1", "true", "yes")
        if probe and hasattr(provider, "status"):
            st = provider.status(force=True)
            # A forced check also re-lists models, so an AUTHORIZATION failure
            # (signed in, but the account can't actually USE Copilot) is current —
            # status()'s auth probe alone reports "connected" for such an account.
            if hasattr(provider, "list_models"):
                provider.list_models(force=True)
        else:
            st = provider.cached_status() if hasattr(provider, "cached_status") else None
        data = self._ai_status_dict(st)
        # Surface "signed in but not authorized for Copilot" so the menu (which has
        # the Switch account button) can tell the user how to fix access.
        authz = getattr(provider, "models_error", lambda: "")()
        if authz:
            data["authz_error"] = authz
        return JSONResponse(data)

    async def api_ai_login_start(self, request: Request) -> JSONResponse:
        """Kick off the Copilot browser sign-in (device flow) in the background.

        Returns immediately; the client polls ``/api/ai/login/poll`` until the user
        has authorised in the browser. ``host`` (optional) targets a GHE Copilot."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        data = await request.json() if await request.body() else {}
        host = str(data.get("host", "")).strip() or None
        provider = self._provider_for()
        if not hasattr(provider, "connect"):
            return JSONResponse(
                {"error": "This AI provider has no interactive sign-in."}, status_code=400
            )
        try:
            st = await run_in_threadpool(provider.connect, host)
        except Exception as exc:  # noqa: BLE001  # AIError/OSError surface to the UI
            return JSONResponse({"error": str(exc)}, status_code=502)
        telemetry.log_event("ai_login_start")
        return JSONResponse({"ok": True, "detail": st.detail})

    def api_ai_login_poll(self, _request: Request) -> JSONResponse:
        """Poll the in-progress Copilot sign-in. ``pending`` while the CLI is still
        running (browser open), then a real status probe confirms the outcome.

        Sync def => threadpool, so the final probe's CLI spawn is off the loop."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        provider = self._provider_for()
        state = (
            provider.login_state()
            if hasattr(provider, "login_state")
            else {"running": False, "output": []}
        )
        if state.get("running"):
            return JSONResponse({"status": "pending", "output": state.get("output", [])})
        # The login process has exited — confirm with a real (forced) probe.
        st = provider.status(force=True) if hasattr(provider, "status") else None
        if st is not None and st.connected:
            telemetry.log_event("ai_login")
            return JSONResponse({"status": "ok", "account": st.account or ""})
        return JSONResponse(
            {
                "status": "error",
                "detail": (st.detail if st is not None else "")
                or "Copilot sign-in didn't complete.",
                "output": state.get("output", []),
            }
        )

    async def api_notebook_ai_toggle(self, request: Request) -> JSONResponse:
        """Turn the copilot off (or back on) for ONE notebook. Writes the synced
        mooring.toml opt-out so the decision travels to teammates, and tears down any
        open chat window for that notebook when disabling. Backs both the hub-row
        toggle and the chat window's off-switch."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        data = await request.json()
        notebook = str(data.get("notebook", "")).strip()
        disabled = bool(data.get("disabled", True))
        if not notebook:
            return JSONResponse({"error": "A notebook is required."}, status_code=400)
        workspace = self.cfg.workspace()
        # Validate the path is safe and a notebook, but do NOT require it to exist:
        # disabling should work for a notebook not pulled yet, and re-enabling must
        # stay possible after the file was renamed/deleted (to clear a stale opt-out).
        # _ws_file runs its traversal/.py checks before the is_file check, so a
        # FileNotFoundError here means "safe path, just absent" — which is fine.
        try:
            self._ws_file(workspace, notebook, suffix=".py")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileNotFoundError:
            pass
        try:
            await run_in_threadpool(workspace_config.set_ai_disabled, workspace, notebook, disabled)
        except tomllib.TOMLDecodeError:
            return JSONResponse(
                {"error": "mooring.toml is malformed — fix it before changing AI settings."},
                status_code=409,
            )
        closed = (
            await run_in_threadpool(self._close_chats_for_notebook, workspace, notebook)
            if disabled
            else 0
        )
        telemetry.log_event("ai_notebook_toggle", disabled=int(disabled))
        return JSONResponse(
            {"ok": True, "notebook": notebook, "ai_disabled": disabled, "closed_sessions": closed}
        )

    # -- AI batch (the orchestrator) ------------------------------------------

    def _abort_all_batches(self) -> None:
        with self._batch_lock:
            runs = list(self._batches.values())
            self._batches.clear()
        for run in runs:
            run["abort"].set()
            with contextlib.suppress(Exception):
                if run.get("planner") is not None:
                    run["planner"].close(cancel=True)
            with contextlib.suppress(Exception):
                run["broadcaster"].close()

    def _reap_idle_batches(self) -> None:
        """Drop batch runs that are caught up (no build in flight) and have had no
        activity for the idle timeout, freeing their worker pool. A still-building run
        is never reaped (its job events keep the broadcaster fresh)."""
        timeout = self.app_cfg.ai_chat_idle_timeout
        with self._batch_lock:
            dead = [
                bid
                for bid, run in self._batches.items()
                if run["status"] != "closed"
                and run["planner"].is_idle()
                and run["broadcaster"].idle_seconds() > timeout
            ]
            runs = [self._batches.pop(bid) for bid in dead]
        for run in runs:
            run["status"] = "closed"
            with contextlib.suppress(Exception):
                run["planner"].close()
            with contextlib.suppress(Exception):
                run["broadcaster"].close()

    def _discard_batch_notebook(self, workspace: Path, notebook_rel: str) -> None:
        """Best-effort remove the empty skeleton a non-built batch job left behind
        (pii-blocked / failed / empty), so a batch doesn't litter the workspace. Path-
        guarded via _ws_file; only ever a .py the batch itself just created and the
        builder only PROPOSED into (never wrote), so no analyst work is lost."""
        try:
            target = self._ws_file(workspace, notebook_rel, suffix=".py")
        except (ValueError, FileNotFoundError):
            return
        with contextlib.suppress(OSError):
            target.unlink()

    def _make_batch_session(
        self, system_context, notebook_rel, model="", reasoning_effort=None, dictionary=None
    ):
        """A builder session for one batch notebook: the SAME value-free, background
        copilot as the interactive chat (allowlist + deny-all + empty workdir + the
        single egress assembler), with the outbound PII guard forced to BLOCK mode, so a
        flagged brief stops the job by default rather than slipping through in warn mode.
        The analyst can still override a block per job from the review tray ("Build
        anyway" -> api_batch_force), which re-runs it auto-confirming the held brief. NOT
        registered in self._chats; the planner owns its lifecycle and closes it the moment
        the build finishes."""
        from dataclasses import replace

        provider = self._provider_for()
        return provider.open_chat(
            system_context=system_context,
            workspace=self.cfg.workspace(),
            folders=self.cfg.folders,
            notebook_rel=notebook_rel,
            model=model,
            reasoning_effort=reasoning_effort,
            dictionary=dictionary,
            pii=replace(self.app_cfg.ai.pii, block_prompt=True),
            background=True,
        )

    def _new_batch_planner(self, workspace: Path, broadcaster, abort):
        """Build + start an appendable batch planner bound to this workspace, streaming
        each value-free per-job lifecycle event over the run's broadcaster. The planner
        owns one bounded worker pool for the run's whole life; ``add`` may be called
        repeatedly while earlier jobs build, so the user can keep writing the next."""
        from mooring import notebook_template
        from mooring.ai.batch import BatchPlanner
        from mooring.ai.chat import ChatEvent

        def progress(ev):
            broadcaster.touch()  # job activity keeps the run from being idle-reaped
            broadcaster._broadcast(ChatEvent("job", ev))

        planner = BatchPlanner(
            config=self.app_cfg.ai.batch,
            pii=self.app_cfg.ai.pii,
            make_notebook=lambda name: notebook_template.create_unique(workspace, name),
            build_context=lambda nb, ds: self._build_chat_context(workspace, nb, ds)[:2],
            open_session=lambda ctx, nb, model, effort, dic: self._make_batch_session(
                ctx, nb, model=model, reasoning_effort=(effort or None), dictionary=dic
            ),
            is_disabled=lambda nb: workspace_config.is_ai_disabled(workspace, nb),
            discard_notebook=lambda nb: self._discard_batch_notebook(workspace, nb),
            on_progress=progress,
            abort=abort,
        )
        return planner.start()

    def _parse_batch_jobs(self, raw_jobs):
        """Validate a jobs payload into ``list[BatchJob]`` (value-free: name + brief +
        dataset PATH). Returns ``(jobs, None)`` or ``(None, error_response)``. Shared by
        open and add."""
        from mooring.ai.batch import BatchJob

        if not isinstance(raw_jobs, list) or not raw_jobs:
            return None, JSONResponse({"error": "Provide at least one job."}, status_code=400)
        jobs = []
        for j in raw_jobs:
            if not isinstance(j, dict):
                return None, JSONResponse({"error": "Each job must be an object."}, status_code=400)
            brief = str(j.get("brief", "")).strip()
            if not brief:
                return None, JSONResponse({"error": "Each job needs a brief."}, status_code=400)
            jobs.append(
                BatchJob(
                    name=str(j.get("name", "")).strip(),
                    brief=brief,
                    dataset_rel=str(j.get("dataset", "")).strip(),
                    model=str(j.get("model", "")).strip(),
                    reasoning_effort=str(j.get("reasoning_effort", "")).strip(),
                )
            )
        return jobs, None

    async def api_batch_state(self, _request: Request) -> JSONResponse:
        """What the batch page needs to render: whether batch is enabled, its caps,
        the value-free dataset paths for per-job dataset selection, and the theme."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        from mooring import schema

        cfg = self.cfg
        datasets = await run_in_threadpool(schema.list_datasets, cfg.workspace(), cfg.folders)
        return JSONResponse(
            {
                "enabled": self.app_cfg.ai_batch_enabled,
                "max_jobs": self.app_cfg.ai_batch_max_jobs,
                "max_concurrency": self.app_cfg.ai_batch_max_concurrency,
                "pii_policy": self.app_cfg.ai_batch_pii_policy,
                "datasets": datasets,
                "ui_theme": self.app_cfg.ui_theme,
            }
        )

    async def api_batch_open(self, request: Request) -> JSONResponse:
        """Open a NEW batch queue and submit the first job(s). The run stays open so the
        analyst can keep adding more (api_batch_add) while these build."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        if not self.app_cfg.ai_batch_enabled:
            return JSONResponse({"enabled": False, "reason": "batch_disabled"}, status_code=403)
        data = await request.json()
        jobs, err = self._parse_batch_jobs(data.get("jobs"))
        if err is not None:
            return err
        max_jobs = self.app_cfg.ai_batch_max_jobs
        if max_jobs and len(jobs) > max_jobs:
            return JSONResponse(
                {"error": f"This batch has {len(jobs)} jobs but the limit is {max_jobs}."},
                status_code=400,
            )
        from mooring.ai.batch import BatchError
        from mooring.ai.chat import ChatBroadcaster

        self._reap_idle_batches()
        workspace = self.cfg.workspace()
        broadcaster = ChatBroadcaster()
        abort = threading.Event()
        planner = self._new_batch_planner(workspace, broadcaster, abort)
        batch_id = secrets.token_urlsafe(9)
        run = {
            "broadcaster": broadcaster,
            "abort": abort,
            "planner": planner,
            "status": "open",
            "applied": set(),
            "workspace": str(workspace),
        }
        with self._batch_lock:
            self._batches[batch_id] = run
        broadcaster.touch()
        try:
            # add() runs the PII pre-flight + mints the notebooks then submits builders;
            # it returns quickly (the builds run in the pool), off the event loop.
            await asyncio.to_thread(planner.add, jobs)
        except BatchError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        telemetry.log_event("ai_batch_open", jobs=len(jobs))
        return JSONResponse({"batch_id": batch_id, "jobs": len(jobs)})

    async def api_batch_add(self, request: Request) -> JSONResponse:
        """Queue MORE jobs onto an already-open run — so a job can be kicked off while
        the next is still being written. Respects the cumulative max_jobs cap."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        if not self.app_cfg.ai_batch_enabled:
            return JSONResponse({"enabled": False, "reason": "batch_disabled"}, status_code=403)
        data = await request.json()
        batch_id = str(data.get("batch_id", ""))
        with self._batch_lock:
            run = self._batches.get(batch_id)
        if run is None:
            return JSONResponse({"error": "Unknown batch."}, status_code=404)
        if run["status"] == "closed":
            return JSONResponse({"error": "This batch is finished."}, status_code=409)
        jobs, err = self._parse_batch_jobs(data.get("jobs"))
        if err is not None:
            return err
        from mooring.ai.batch import BatchError

        run["broadcaster"].touch()
        try:
            indices = await asyncio.to_thread(run["planner"].add, jobs)
        except BatchError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        telemetry.log_event("ai_batch_add", jobs=len(jobs))
        return JSONResponse({"ok": True, "added": len(indices)})

    async def api_batch_refine(self, request: Request) -> JSONResponse:
        """Re-build ONE built notebook's proposal with the analyst's revision note, so a
        proposal can be tweaked in the tray before it's Applied. The note runs the
        non-interactive PII gate; the notebook file is never written; a poor revision
        keeps the previous proposal."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        if not self.app_cfg.ai_batch_enabled:
            return JSONResponse({"enabled": False, "reason": "batch_disabled"}, status_code=403)
        data = await request.json()
        batch_id = str(data.get("batch_id", ""))
        with self._batch_lock:
            run = self._batches.get(batch_id)
        if run is None:
            return JSONResponse({"error": "Unknown batch."}, status_code=404)
        if run["status"] == "closed":
            return JSONResponse({"error": "This batch is finished."}, status_code=409)
        try:
            job_idx = int(data.get("job"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "A job index is required."}, status_code=400)
        feedback = str(data.get("feedback", ""))
        run["broadcaster"].touch()
        from mooring.ai.batch import BatchError

        try:
            await asyncio.to_thread(run["planner"].refine, job_idx, feedback)
        except BatchError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        telemetry.log_event("ai_batch_refine")
        return JSONResponse({"ok": True})

    async def api_batch_force(self, request: Request) -> JSONResponse:
        """Re-build ONE pii-blocked job, overriding the outbound-PII guard — the tray's
        "Build anyway". The human reviewing the tray authorizes forwarding the flagged
        brief verbatim (the batch analogue of the chat's "Send anyway"); the notebook is
        still only PROPOSED into, never written, so the existing per-notebook Apply gate
        remains the only write path."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        if not self.app_cfg.ai_batch_enabled:
            return JSONResponse({"enabled": False, "reason": "batch_disabled"}, status_code=403)
        data = await request.json()
        batch_id = str(data.get("batch_id", ""))
        with self._batch_lock:
            run = self._batches.get(batch_id)
        if run is None:
            return JSONResponse({"error": "Unknown batch."}, status_code=404)
        if run["status"] == "closed":
            return JSONResponse({"error": "This batch is finished."}, status_code=409)
        try:
            job_idx = int(data.get("job"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "A job index is required."}, status_code=400)
        run["broadcaster"].touch()
        from mooring.ai.batch import BatchError

        try:
            await asyncio.to_thread(run["planner"].force, job_idx)
        except BatchError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        telemetry.log_event("ai_batch_force")
        return JSONResponse({"ok": True})

    async def api_batch_stream(self, request: Request) -> StreamingResponse | JSONResponse:
        batch_id = request.path_params["batch_id"]
        with self._batch_lock:
            run = self._batches.get(batch_id)
        if run is None:
            return JSONResponse({"error": "Unknown batch."}, status_code=404)
        return StreamingResponse(
            self._batch_sse_gen(run),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _batch_sse_gen(self, run):
        broadcaster = run["broadcaster"]
        q = broadcaster.subscribe()
        try:
            yield ": connected\n\n"
            # An appendable run streams 'job' events for its whole life (no single
            # terminal 'done' — the user keeps adding). A late subscriber catches up via
            # GET /tray; here we just stream live events. If the run was already closed
            # (reaped / repo switch), say so instead of pinging forever.
            if run["status"] == "closed":
                yield "event: closed\ndata: {}\n\n"
                return
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
            broadcaster.unsubscribe(q)

    async def api_batch_tray(self, request: Request) -> JSONResponse:
        batch_id = request.path_params["batch_id"]
        with self._batch_lock:
            run = self._batches.get(batch_id)
        if run is None:
            return JSONResponse({"error": "Unknown batch."}, status_code=404)
        snapshot = run["planner"].snapshot()
        return JSONResponse(
            {
                "status": run["status"],
                "pending": run["planner"].pending,
                "jobs": self._batch_tray_jobs(run, snapshot),
            }
        )

    def _batch_tray_jobs(self, run, results) -> list[dict]:
        """Value-free per-job view for the live review tray: status, the user's own
        brief, and each proposal's source/diff (never a data value). In-flight jobs show
        as queued/building with no proposals yet; built jobs carry their proposals."""
        applied = run["applied"]  # stable proposal ids (pid), not (job, position) tuples
        refining = run["planner"].refining_indices()
        forcing = run["planner"].forcing_indices()
        out = []
        for idx, res in enumerate(results):
            proposals = [
                {
                    "proposal": j,
                    "kind": str(p.get("kind", "append")),
                    "rationale": str(p.get("rationale", "")),
                    "code": str(p.get("code", "")),
                    "diffs": p.get("diffs", []),
                    "applied": p.get("pid") in applied,
                }
                for j, p in enumerate(res.proposals)
            ]
            out.append(
                {
                    "index": idx,
                    "name": res.job.name,
                    "brief": res.job.brief,
                    "notebook": res.notebook_rel,
                    "status": res.status,
                    "error": res.error,
                    "pii": res.pii,
                    "proposals": proposals,
                    "refining": idx in refining,
                    "forcing": idx in forcing,
                }
            )
        return out

    async def api_batch_apply(self, request: Request) -> JSONResponse:
        """Apply ONE proposal from a finished batch into its notebook — the human's
        per-notebook authorization. Reuses the SAME single-notebook write path as the
        chat Apply (_apply_with_undo: snapshot + _apply_lock + per-notebook opt-out
        re-check), so there is no autonomous-write path; only the review is batched."""
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        data = await request.json()
        batch_id = str(data.get("batch_id", ""))
        with self._batch_lock:
            run = self._batches.get(batch_id)
        if run is None:
            return JSONResponse({"error": "Unknown batch."}, status_code=404)
        results = run["planner"].snapshot()
        try:
            job_idx = int(data.get("job"))
            prop_idx = int(data.get("proposal", 0))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "A job and proposal index are required."}, status_code=400
            )
        if not 0 <= job_idx < len(results):
            return JSONResponse({"error": "No such job."}, status_code=404)
        res = results[job_idx]
        if res.notebook_rel is None or not 0 <= prop_idx < len(res.proposals):
            return JSONResponse({"error": "No such proposal."}, status_code=404)
        proposal = res.proposals[prop_idx]
        pid = proposal.get("pid")
        # Idempotent by the proposal's STABLE id (not its position): a re-submit of an
        # already-applied proposal (a tray re-render re-armed the button) is a no-op, so
        # the same cell can never be appended twice. Keying by position would wrongly
        # treat a refined proposal at the same slot as already applied — the Bug this fixes.
        with self._batch_lock:
            if pid is not None and pid in run["applied"]:
                return JSONResponse({"ok": True, "noop": True})
        ops = proposal.get("ops")
        if isinstance(ops, list) and ops:
            op_dicts = ops
        elif str(proposal.get("code", "")).strip():
            op_dicts = [{"op": "append", "code": proposal["code"]}]
        else:
            return JSONResponse({"error": "Nothing to apply."}, status_code=400)
        workspace = Path(run["workspace"])
        notebook_rel = res.notebook_rel
        from mooring.ai.cellwrite import CellApplyConflict, CellWriteError

        try:
            nb_path = self._ws_file(workspace, notebook_rel, suffix=".py")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"error": f"No such notebook: {notebook_rel}"}, status_code=404)
        try:
            undo_depth = await asyncio.to_thread(
                self._apply_with_undo, nb_path, workspace, notebook_rel, op_dicts
            )
        except PermissionError:
            return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
        except CellApplyConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except CellWriteError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        with self._batch_lock:
            if pid is not None:
                run["applied"].add(pid)
        telemetry.log_event("ai_batch_apply")
        return JSONResponse({"ok": True, "can_undo": undo_depth > 0, "undo_depth": undo_depth})

    def batch_page(self, _request: Request) -> HTMLResponse | JSONResponse:
        if not self.app_cfg.ai_enabled:
            return JSONResponse({"error": "The AI copilot is disabled."}, status_code=404)
        return self._themed_page("batch.html")


def create_app(hub: Hub) -> Starlette:
    static = _static_dir()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            # Teardown is fast: with marimo in its own process group (see
            # editor.ensure_started) the first Ctrl+C reaches only mooring, and
            # shutdown() force-kills the marimo tree (taskkill /F), so the blocking
            # proc.wait returns near-instantly. (Running this off the loop wouldn't
            # help a second Ctrl+C anyway — uvicorn checks force_exit once, before
            # awaiting lifespan shutdown, and never re-checks it mid-teardown.)
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
            Route("/settings", hub.settings_page),
            Route("/api/settings", hub.api_get_settings),
            Route("/api/settings", hub.api_set_settings, methods=["POST"]),
            Route("/api/settings/reset", hub.api_reset_settings, methods=["POST"]),
            Route("/api/login/start", hub.api_login_start, methods=["POST"]),
            Route("/api/login/poll", hub.api_login_poll),
            Route("/api/logout", hub.api_logout, methods=["POST"]),
            Route("/api/discover", hub.api_discover),
            Route("/api/adopt", hub.api_adopt, methods=["POST"]),
            Route("/api/pull", hub.api_pull, methods=["POST"]),
            Route("/api/push", hub.api_push, methods=["POST"]),
            Route("/api/propose", hub.api_propose, methods=["POST"]),
            Route("/api/resolve", hub.api_resolve, methods=["POST"]),
            Route("/api/new", hub.api_new, methods=["POST"]),
            Route("/api/open", hub.api_open, methods=["POST"]),
            Route("/api/reveal", hub.api_reveal, methods=["POST"]),
            Route("/api/delete", hub.api_delete, methods=["POST"]),
            Route("/api/rollback", hub.api_rollback, methods=["POST"]),
            Route("/api/undo", hub.api_undo, methods=["POST"]),
            Route("/ai/chat", hub.chat_page),
            Route("/api/ai/datasets", hub.api_chat_datasets),
            Route("/api/ai/models", hub.api_chat_models),
            Route("/api/ai/status", hub.api_ai_status),
            Route("/api/ai/login/start", hub.api_ai_login_start, methods=["POST"]),
            Route("/api/ai/login/poll", hub.api_ai_login_poll),
            Route("/api/ai/chat/open", hub.api_chat_open, methods=["POST"]),
            Route("/api/ai/chat/stream/{sid}", hub.api_chat_stream),
            Route("/api/ai/chat/send", hub.api_chat_send, methods=["POST"]),
            Route("/api/ai/chat/apply", hub.api_chat_apply, methods=["POST"]),
            Route("/api/ai/chat/rollback", hub.api_chat_rollback, methods=["POST"]),
            Route("/api/ai/notebook/toggle", hub.api_notebook_ai_toggle, methods=["POST"]),
            Route("/ai/batch", hub.batch_page),
            Route("/api/ai/batch/state", hub.api_batch_state),
            Route("/api/ai/batch/open", hub.api_batch_open, methods=["POST"]),
            Route("/api/ai/batch/add", hub.api_batch_add, methods=["POST"]),
            Route("/api/ai/batch/stream/{batch_id}", hub.api_batch_stream),
            Route("/api/ai/batch/tray/{batch_id}", hub.api_batch_tray),
            Route("/api/ai/batch/apply", hub.api_batch_apply, methods=["POST"]),
            Route("/api/ai/batch/refine", hub.api_batch_refine, methods=["POST"]),
            Route("/api/ai/batch/force", hub.api_batch_force, methods=["POST"]),
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
    # Pre-warm the editor subprocess and prime heavy imports in the background so the
    # first notebook open / chat open isn't paying that cold start on the user's click.
    hub.warmup()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
