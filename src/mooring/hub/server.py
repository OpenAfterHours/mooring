"""The mooring hub: a local web app for login, sync, and opening notebooks.

A small Starlette app bound to 127.0.0.1. This module holds the Hub — the one
shared state-holder (editors, chat sessions, batch runs, provider cache, login
flow) with its lifecycle and service helpers — plus create_app/run_hub. The
route handlers live in hub/routes/* (one module per concern: setup, settings,
sync, files, chat, batch), the HTML pages in hub/pages.py, and the shared SSE
transport in hub/sse.py; handlers reach the Hub via ``request.app.state.hub``.
The frontend is static vanilla JS; the marimo editor runs as a separate
subprocess (see editor.py) that the hub starts lazily and tears down on
shutdown.
"""

from __future__ import annotations

import contextlib
import threading
import webbrowser
from importlib import resources
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from mooring import (
    auth,
    config,
    notebook_template,
    pbip,
    pyproject_env,
    shadow,
    sync,
    telemetry,
    workspace_config,
)
from mooring.app import notebooks as nb_ops
from mooring.app.apply import ApplyGuard
from mooring.app.chat_service import ChatService
from mooring.editor import EditorServer, free_port
from mooring.github import GitHubClient, GitHubError, blob_url
from mooring.hub import settings_schema


def _static_dir() -> Path:
    return Path(str(resources.files("mooring.hub").joinpath("static")))


_UNKNOWN_CHAT_SESSION = "Unknown chat session."


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
        # The chat application service: the session registry + lifecycle, the
        # context assembly (the sole egress.build_system_context caller), and the
        # live-schema pipeline (app/chat_service.py — the landed P7).
        self.chat = ChatService()
        # THE per-notebook apply/undo write guard: chat Apply, batch Apply, Undo,
        # and the sync rollback all serialize on apply.lock (app/apply.py).
        self.apply = ApplyGuard()
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
        # Shared construction (app/notebooks): RAISES AuthFailed/NotConfigured —
        # never exits — so the hub process stays up and answers with an error.
        return nb_ops.client_for(self.cfg)

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

    # -- chat service delegates -------------------------------------------------
    # Thin views over app/chat_service + app/apply, kept on the Hub so the routes
    # and the test suite keep one stable surface while the service owns the logic.

    @property
    def _chats(self) -> dict:
        """The live session dict (a VIEW onto the service's registry — the suite
        reads and seeds sessions through it)."""
        return self.chat._chats

    @property
    def _chat_targets(self) -> dict:
        """The sid -> (workspace, notebook) dict (the same view, for the suite)."""
        return self.chat._targets

    def _close_all_chats(self) -> None:
        self.chat.close_all()

    def _close_chat(self, sid: str) -> None:
        self.chat.close(sid)

    def _close_chats_for_notebook(self, workspace: Path, notebook_rel: str) -> int:
        return self.chat.close_for_notebook(workspace, notebook_rel)

    def _disabled_block(self, sid: str) -> JSONResponse | None:
        """The per-notebook opt-out gate shared by send/apply/rollback: the service
        decides (and tears the session down); the 403 the chat UI locks on is
        transport, so it stays here."""
        if self.chat.close_if_disabled(sid):
            return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
        return None

    def _ws_file(self, workspace: Path, rel: str, *, suffix: str | None = None) -> Path:
        return nb_ops.ws_file(workspace, rel, suffix=suffix)

    def _build_chat_context(self, workspace: Path, notebook_rel: str, dataset_rel: str):
        return self.chat.build_context(self.app_cfg, workspace, notebook_rel, dataset_rel)

    def _live_schema_for_sid(self, sid: str) -> tuple[str, list[dict]]:
        return self.chat.live_schema_for_sid(self.app_cfg, self.editors, sid)

    def _reap_idle_chats(self) -> None:
        self.chat.reap_idle(self.app_cfg.ai_chat_idle_timeout)

    def _pii_status(self) -> dict:
        return self.chat.pii_status(self.app_cfg)


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
            extra, ignore = nb_ops.shadow_policy(workspace)
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

    # -- settings / profile ----------------------------------------------------
    # A per-machine Settings page reached from the header. The editable surface is
    # the curated registry in settings_schema.py — which IS the allowlist the rest
    # of the config write path lacks (config_store.set_value writes any key verbatim).
    # Writes mirror api_set_theme: persist via config_store, then re-read the config
    # in place (NOT the destructive reload()). Team/synced decisions (the per-notebook
    # AI opt-out, sync folders) and the structural value-blindness guarantees are
    # intentionally NOT here. See docs/admins/configuration.md.


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







    def _open(self, rel_path: str) -> JSONResponse:
        workspace = self.cfg.workspace()
        target = (workspace / rel_path).resolve()
        try:
            target.relative_to(workspace.resolve())
        except ValueError:
            return JSONResponse({"error": "Path escapes the workspace."}, status_code=400)
        if not target.is_file():
            return JSONResponse({"error": f"No such file: {rel_path}"}, status_code=404)
        # The gate (pbip / .py-only / module-refusal) is shared policy in
        # app/notebooks — the hub hides Open on module rows (is_module) and offers
        # Reveal instead; the gate backstops a direct call / stale client.
        try:
            kind = nb_ops.openable_kind(target, rel_path)
        except nb_ops.OpenRefused as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if kind == "pbip":
            try:
                pbip.launch(target)
            except pbip.PbipLaunchError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            name = rel_path.rsplit("/", 1)[-1]
            telemetry.log_event("open", kind="pbip")
            return JSONResponse({"path": rel_path, "lines": [f"Opened {name} in Power BI Desktop"]})
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
            findings = nb_ops.open_shadow_findings(workspace, rel_path)
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

    # Function-local so the handler modules (which import server for the shared
    # constants) never form an import cycle: by the time create_app runs, this
    # module is fully initialized.
    from mooring.hub import pages
    from mooring.hub.routes import batch, chat, files, settings, setup
    from mooring.hub.routes import sync as sync_routes

    app = Starlette(
        routes=[
            Route("/", pages.index_page),
            Route("/api/state", setup.api_state),
            Route("/api/setup", setup.api_setup, methods=["POST"]),
            Route("/api/repo/switch", setup.api_repo_switch, methods=["POST"]),
            Route("/api/repo/remove", setup.api_repo_remove, methods=["POST"]),
            Route("/api/ui/theme", setup.api_set_theme, methods=["POST"]),
            Route("/settings", pages.settings_page),
            Route("/api/settings", settings.api_get_settings),
            Route("/api/settings", settings.api_set_settings, methods=["POST"]),
            Route("/api/settings/reset", settings.api_reset_settings, methods=["POST"]),
            Route("/api/login/start", setup.api_login_start, methods=["POST"]),
            Route("/api/login/poll", setup.api_login_poll),
            Route("/api/logout", setup.api_logout, methods=["POST"]),
            Route("/api/discover", sync_routes.api_discover),
            Route("/api/adopt", sync_routes.api_adopt, methods=["POST"]),
            Route("/api/pull", sync_routes.api_pull, methods=["POST"]),
            Route("/api/push", sync_routes.api_push, methods=["POST"]),
            Route("/api/propose", sync_routes.api_propose, methods=["POST"]),
            Route("/api/resolve", sync_routes.api_resolve, methods=["POST"]),
            Route("/api/new", files.api_new, methods=["POST"]),
            Route("/api/open", files.api_open, methods=["POST"]),
            Route("/api/reveal", files.api_reveal, methods=["POST"]),
            Route("/api/delete", files.api_delete, methods=["POST"]),
            Route("/api/rollback", files.api_rollback, methods=["POST"]),
            Route("/api/undo", files.api_undo, methods=["POST"]),
            Route("/ai/chat", pages.chat_page),
            Route("/api/ai/datasets", chat.api_chat_datasets),
            Route("/api/ai/models", chat.api_chat_models),
            Route("/api/ai/status", chat.api_ai_status),
            Route("/api/ai/login/start", chat.api_ai_login_start, methods=["POST"]),
            Route("/api/ai/login/poll", chat.api_ai_login_poll),
            Route("/api/ai/chat/open", chat.api_chat_open, methods=["POST"]),
            Route("/api/ai/chat/stream/{sid}", chat.api_chat_stream),
            Route("/api/ai/chat/send", chat.api_chat_send, methods=["POST"]),
            Route("/api/ai/chat/apply", chat.api_chat_apply, methods=["POST"]),
            Route("/api/ai/chat/rollback", chat.api_chat_rollback, methods=["POST"]),
            Route("/api/ai/notebook/toggle", chat.api_notebook_ai_toggle, methods=["POST"]),
            Route("/ai/batch", pages.batch_page),
            Route("/api/ai/batch/state", batch.api_batch_state),
            Route("/api/ai/batch/open", batch.api_batch_open, methods=["POST"]),
            Route("/api/ai/batch/add", batch.api_batch_add, methods=["POST"]),
            Route("/api/ai/batch/stream/{batch_id}", batch.api_batch_stream),
            Route("/api/ai/batch/tray/{batch_id}", batch.api_batch_tray),
            Route("/api/ai/batch/apply", batch.api_batch_apply, methods=["POST"]),
            Route("/api/ai/batch/refine", batch.api_batch_refine, methods=["POST"]),
            Route("/api/ai/batch/force", batch.api_batch_force, methods=["POST"]),
            Mount("/static", StaticFiles(directory=static)),
        ],
        lifespan=lifespan,
    )
    # The one shared state-holder every handler reaches via request.app.state.hub.
    app.state.hub = hub
    return app


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
