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
import time
import webbrowser
from importlib import resources
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from mooring import (
    activity,
    auth,
    checks,
    config,
    inputs,
    lineage,
    notebook_template,
    pbip,
    pbip_model,
    policy,
    pyproject_env,
    shadow,
    sync,
    telemetry,
    trash,
    verify,
    workspace_config,
)
from mooring.app import notebooks as nb_ops
from mooring.app.apply import ApplyGuard
from mooring.app.batch_service import BatchService
from mooring.app.chat_service import ChatService
from mooring.editor import EditorServer, bind_or_free
from mooring.github import GitHubClient, GitHubError, Unreachable, blob_url
from mooring.hub import settings_schema


class _RevalidatingStatic(StaticFiles):
    """Serve the hub's own JS/CSS with ``Cache-Control: no-cache``.

    Starlette's ``StaticFiles`` sets no ``Cache-Control``, so a browser applies HEURISTIC
    freshness and may reuse a script for a long time WITHOUT revalidating. Because the
    hub's frontend is several cooperating files, that goes wrong in a way no reload fixes:
    a fresh ``chat.js`` can offer a command whose handler lives in a stale, cached
    ``chat_core.js`` (the ``/investigate`` menu entry did exactly this).

    ``no-cache`` means "you may store it, but revalidate before reuse" — NOT "don't
    store". The hub is a LOCAL app on 127.0.0.1, so revalidation costs a round trip to
    ourselves and normally answers ``304 Not Modified`` from the existing ETag. Correctness
    over a saving that is worth nothing on loopback.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _static_dir() -> Path:
    return Path(str(resources.files("mooring.hub").joinpath("static")))


_UNKNOWN_CHAT_SESSION = "Unknown chat session."


class Hub:
    def __init__(self, app_cfg: config.AppConfig) -> None:
        # The RAW per-machine config. Everything reads it through the app_cfg
        # PROPERTY below, which folds the team policy over it on the way out.
        self._app_cfg = app_cfg
        # (workspace, stat-signature) -> Policy, so the property costs one stat()
        # rather than a TOML parse (the _model_summary_cache idiom).
        self._policy_cache: tuple[tuple, policy.Policy] | None = None
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
        # live-schema pipeline (app/chat_service.py — plan phase P3).
        self.chat = ChatService()
        # THE per-notebook apply/undo write guard: chat Apply, batch Apply, Undo,
        # and the sync rollback all serialize on apply.lock (app/apply.py).
        self.apply = ApplyGuard()
        # The batch application service: the run registry (+ reap/abort/cancel)
        # around the pure ai.batch.BatchPlanner (app/batch_service.py).
        self.batch = BatchService()
        # The one in-flight parameterised run (app/param_runs.RunHandle), or None. A
        # single slot rather than a registry on purpose: the workspace run lock already
        # permits exactly one whole-notebook run at a time, so a second slot could only
        # ever hold something that is blocked. Kept after it finishes so the page can
        # still read the final per-value report on a reload. The lock makes check-then-claim
        # atomic: two concurrent POSTs to /api/run/start both run on the threadpool, and
        # without it the loser would overwrite the winner's handle with a run about to die
        # on the workspace lock — losing the live one from the UI.
        self.param_run = None
        self.param_run_lock = threading.Lock()
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
        # Cache of the notebook sniff (see _sniff_notebook), keyed by absolute path →
        # (mtime_ns, is_notebook, title, catalog terms). /api/state re-lists on every
        # refresh, so this avoids re-reading AND re-parsing every .py each time; a changed
        # mtime invalidates it.
        self._notebook_cache: dict[str, tuple[int, bool, str, tuple[str, ...]]] = {}
        # Cache of each PBIP semantic model's tables/measures summary (the artifact
        # row's `model` field), keyed by model dir → (definition signature, summary).
        # The same idiom as _notebook_cache: _files_artifacts runs on EVERY /api/state
        # poll, and re-parsing a 200-file TMDL tree per poll is unacceptable — the
        # stat-only signature (see pbip_model.definition_signature) invalidates it.
        self._model_summary_cache: dict[str, tuple[tuple, dict | None]] = {}
        # The branch head each workspace's last /api/state render was computed from,
        # so /api/freshness can answer "has the remote moved since what you're looking
        # at?" with one fast ref lookup (routes/sync.api_freshness). Keyed like editors.
        self._state_heads: dict[str, str] = {}
        # Cache of the What's-new per-entry detail summaries, keyed (path, base_sha,
        # remote_sha) — blob content is immutable per sha, so re-expanding an entry
        # never re-fetches its blobs (routes/sync.api_whatsnew_detail). Tiny values
        # (count dicts), so no eviction beyond process life.
        self._whatsnew_detail: dict[tuple[str, str, str], dict] = {}

    # -- helpers -------------------------------------------------------------

    @property
    def app_cfg(self) -> config.AppConfig:
        """The per-machine config with the repo's admin policy folded over it.

        A PROPERTY, not an attribute set at three well-chosen moments: a policy
        arrives by PULL, mid-session, and every "fold it at each assignment"
        scheme missed that — the pull route touches none of them, so every
        ``[policy.settings]`` knob kept its permissive value for the rest of the
        session while the Settings page rendered the row as locked. Folding on
        READ makes the seam impossible to bypass; it mirrors ``cfg`` below, which
        already re-reads the synced file on every access for the same reason.
        Tighten-only, so with no ``[policy]`` block this returns the raw config
        unchanged (``policy.tighten`` short-circuits on an empty rule set).
        """
        return policy.tighten(self._app_cfg, self.team_policy())

    @app_cfg.setter
    def app_cfg(self, value: config.AppConfig) -> None:
        self._app_cfg = value

    def team_policy(self) -> policy.Policy:
        """The active repo's policy, re-read whenever the synced file changes.

        Cached on a stat signature (mtime_ns + size + path) so the hot read path
        pays one ``stat()``; a pull that rewrites ``mooring.toml`` changes the
        signature and the next read re-parses. The single source the Settings
        page, the sync routes and the ``app_cfg`` fold all ask.
        """
        workspace = self._app_cfg.config_for(None).workspace()
        path = workspace_config.config_path(workspace)
        try:
            st = path.stat()
            sig = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            sig = (str(path), None, None)
        cached = self._policy_cache
        if cached is not None and cached[0] == sig:
            return cached[1]
        pol = policy.load(workspace)
        self._policy_cache = (sig, pol)
        return pol

    @property
    def cfg(self) -> config.Config:
        from dataclasses import replace

        cfg = self.app_cfg.config_for(None)
        # Fold the repo's synced sub-folders (mooring.toml [sync] folders) AND the team
        # AI context OFFER ([ai] context_folders) into the scope so a notebook created in
        # a uv-workspace package folder — and every offered context folder — lists, opens,
        # and syncs like any other. Re-read here (not cached) so a folder registered by a
        # New (or a Use-as-context toggle) on this run shows up on the very next /api/state.
        from mooring.app import context_folders as ctxdirs

        folders = ctxdirs.sync_dirs(self.app_cfg, cfg.folders, cfg.workspace())
        return cfg if folders == cfg.folders else replace(cfg, folders=folders)

    def reload(self) -> None:
        with self._lock:
            self.app_cfg = config.load_app_config()
        # Chat context (schema + notebook source) is bound to the old config;
        # drop sessions so a new chat picks up the new repo/workspace.
        self._close_all_chats()
        # In-flight batches are bound to the old workspace too — cancel them (their
        # un-reviewed proposals are lost; the UI warns not to switch repos mid-batch).
        self.batch.abort_all()
        # ...and so is a parameterised run: its remaining values would execute against the
        # OLD workspace while the page shows the new one. Cancel it and drop the handle.
        self._cancel_param_run()
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
        # cfg.folders (not app_cfg's raw list) so the synced mooring.toml extras are
        # in scope — a semantic model in an adopted sub-folder is discovered too.
        return self.chat.build_context(
            self.app_cfg, workspace, notebook_rel, dataset_rel, folders=self.cfg.folders
        )

    def _live_schema_for_sid(self, sid: str) -> tuple[str, list[dict]]:
        return self.chat.live_schema_for_sid(self.app_cfg, self.editors, sid)

    def _reap_idle_chats(self) -> None:
        self.chat.reap_idle(self.app_cfg.ai_chat_idle_timeout)

    def _pii_status(self) -> dict:
        return self.chat.pii_status(self.app_cfg)


    def _cancel_param_run(self) -> None:
        """Stop any in-flight fan-out and forget it. Best-effort — the runner's own
        process-tree kill is what actually stops marimo; this only fires the event."""
        handle, self.param_run = self.param_run, None
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.cancel.set()

    def shutdown(self) -> None:
        self.chat.close_all()
        self.batch.abort_all()
        self._cancel_param_run()
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
        # Is the copilot off for this path? The per-notebook opt-out list UNIONED
        # with the policy's [policy] ai_off globs (mooring.policy.ai_gate) — one
        # read of the shared file, then a cheap predicate per row.
        ai_off = policy.ai_gate(workspace)
        # Value-free tie-out check results per notebook (.mooring/checks/*.json),
        # written by mooring_checks calls in the kernel — surfaced as a green/red
        # row badge. Counts + names only; local-only, never synced, never seen by AI.
        check_results = checks.read_results(workspace)
        # Value-free run-verification receipts per notebook (.mooring/verify/*.json):
        # did a local smoke re-run go clean, keyed to the file's content SHA. Only
        # SHA-current receipts are returned, so an edited notebook drops its badge —
        # the trust badge auto-clears the instant the code moves on. Local-only. The
        # report already carries each file's local_sha, so pass it in to spare
        # read_results from re-hashing every verified notebook on each poll.
        local_shas = {f.path: f.local_sha for f in report.files if f.local_sha is not None}
        verify_results = verify.read_results(workspace, local_shas)
        # Value-free input/output fingerprints per notebook (.mooring/inputs/*.json),
        # written by mooring_inputs calls in the kernel — content hash + shape + schema,
        # never a value. Surfaced as a row badge: N inputs pinned, M changed since last run.
        # The same receipts carry the lineage graph, so read them ONCE and derive both.
        receipts = inputs.read_receipts(workspace)
        input_results = inputs.summarize(receipts)
        # "3 notebooks read this" for a data file others depend on — the warning that makes
        # overwriting one a decision rather than an accident. Only paths with a recorded
        # reader or writer get an entry: lineage knows only opted-in notebooks, so the row
        # may claim what IS recorded and must never imply a bare row is safe to change.
        lineage_counts = lineage.counts(
            lineage.from_receipts(receipts), [f.path for f in report.files]
        )
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

        # Tell a runnable marimo notebook from a plain helper module (sniffed off disk),
        # and harvest each notebook's value-free title + catalog terms in the same read.
        # Only meaningful for a .py that exists locally; drives the Open/AI buttons, the
        # "module" badge, the catalog title/search, and keeps the editor opening a module.
        notebooks: set[str] = set()
        titles: dict[str, str] = {}
        terms: dict[str, list[str]] = {}
        for f in report.files:
            if f.path.endswith(".py") and _has_local(f):
                is_notebook, title, row_terms = self._sniff_notebook(workspace, f.path)
                if is_notebook:
                    notebooks.add(f.path)
                    if title:
                        titles[f.path] = title
                    if row_terms:
                        terms[f.path] = list(row_terms)
        files = [
            {
                "path": f.path,
                "state": f.state.value,
                "has_local": _has_local(f),
                **({"artifact": artifact_of[f.path]} if f.path in artifact_of else {}),
                **({"ai_disabled": True} if f.path.endswith(".py") and ai_off(f.path) else {}),
                **({"shadows": shadowed[f.path]} if f.path in shadowed else {}),
                **({"checks": check_results[f.path]} if f.path in check_results else {}),
                **({"verified": verify_results[f.path]} if f.path in verify_results else {}),
                **({"inputs": input_results[f.path]} if f.path in input_results else {}),
                **({"lineage": lineage_counts[f.path]} if f.path in lineage_counts else {}),
                **({"is_notebook": True} if f.path in notebooks else {}),
                **({"title": titles[f.path]} if f.path in titles else {}),
                # The notebook's value-free catalog terms (mooring.ai.notebookindex) —
                # what it says it does, what it imports, the inputs/checks/tables its
                # source declares — so the hub's filter box searches CONTENT, not just a
                # filename. Client-side only; the same allowlist the copilot's catalog
                # tools serve, so local search and the model's view never diverge.
                **({"terms": terms[f.path]} if f.path in terms else {}),
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
                # The remote blob sha keys the staleness dialog's session dismissals
                # ("Open my copy anyway" re-arms only when the remote moves AGAIN).
                **({"remote_sha": f.remote_sha} if f.remote_sha is not None else {}),
            }
            for f in report.files
        ]
        # Semantic models the team turned the copilot off for (synced mooring.toml),
        # read once per render like the notebook opt-outs above.
        models_off = workspace_config.disabled_semantic_models(workspace)
        model_summaries = {
            a.key: summary
            for a in artifacts
            if (summary := self._model_summary(workspace, a.key)) is not None
        }
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
                # The semantic-model summary (present only when a readable local
                # definition exists) + the synced per-model AI opt-out flag.
                **({"model": model_summaries[a.key]} if a.key in model_summaries else {}),
                **(
                    {"ai_model_disabled": True}
                    if workspace_config.normalize_notebook(a.key) in models_off
                    else {}
                ),
            }
            for a in artifacts
        ]
        return files, arts

    def _model_summary(self, workspace: Path, key: str) -> dict | None:
        """The ``{tables, measures}`` counts for a PBIP artifact's semantic model,
        or None when it has no readable local definition (a report-only PBIP, or
        members not pulled yet). Cached by the definition's stat-only signature —
        the _notebook_cache idiom — because /api/state calls this per poll and a
        real model is a couple-hundred-file TMDL tree."""
        model_dir = workspace / f"{key}{pbip_model.MODEL_DIR_SUFFIX}"
        sig = pbip_model.definition_signature(model_dir)
        if not sig:
            return None
        cache_key = str(model_dir)
        cached = self._model_summary_cache.get(cache_key)
        if cached is not None and cached[0] == sig:
            return cached[1]
        model = pbip_model.extract_model(model_dir, key=key)
        summary = {"tables": len(model.tables), "measures": model.n_measures}
        self._model_summary_cache[cache_key] = (sig, summary)
        return summary

    def _sniff_notebook(self, workspace: Path, rel: str) -> tuple[bool, str, tuple[str, ...]]:
        """``(is_notebook, title, terms)`` for the local ``.py`` at ``rel``, from ONE
        mtime-cached file read + parse (this runs per row on every /api/state).

        ``is_notebook``: whether it is a marimo notebook (vs a plain helper module). A
        blank/whitespace-only file counts as a notebook — it opens as a fresh notebook,
        matching the open guards — EXCEPT a dunder package marker like ``__init__.py``
        (see :func:`notebook_template.opens_as_notebook`). ``title``: the notebook's own
        first-markdown-cell heading, harvested value-free (authored text, never a data
        value; ``""`` for a module or a title-less notebook). ``terms``: that notebook's
        value-free catalog terms, so the hub's filter box can search CONTENT — the same
        :mod:`mooring.ai.notebookindex` allowlist the copilot's catalog tools serve, so
        the two can never drift apart. The whole file is read (the marimo marker can sit
        past a large header). Missing/unreadable → ``(False, "", ())``."""
        path = workspace / rel
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return (False, "", ())
        key = str(path)
        cached = self._notebook_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return (cached[1], cached[2], cached[3])
        try:
            source = path.read_bytes().decode("utf-8", "ignore")
        except OSError:
            return (False, "", ())
        is_notebook = notebook_template.opens_as_notebook(rel, source)
        title = ""
        terms: tuple[str, ...] = ()
        if is_notebook:
            from mooring.ai import notebookindex

            title = notebook_template.notebook_title(source)
            # extract_notebook never raises: a half-written notebook reports a parse error
            # and yields no terms, rather than blanking the whole file listing. The path
            # is dropped from the row's copy — /api/state polls, and `matches` already
            # searches `file.path`, so repeating it would be payload for nothing.
            entry, report = notebookindex.extract_notebook(source, rel)
            terms = () if report.error else tuple(t for t in entry.terms() if t != rel)
        self._notebook_cache[key] = (mtime, is_notebook, title, terms)
        return (is_notebook, title, terms)

    def _is_notebook(self, workspace: Path, rel: str) -> bool:
        return self._sniff_notebook(workspace, rel)[0]

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

    # -- settings / profile helpers ---------------------------------------------
    # The payload/confirm helpers behind the Settings page; the read/write/reset
    # endpoints (and the full write-path story) live in routes/settings.py.


    @staticmethod
    def _enum_options(spec) -> list[dict] | None:
        """[{value, label}] for an enum control (friendly labels where the spec gives
        them, else the raw token), or None for a non-enum control."""
        if not spec.enum_values:
            return None
        labels = spec.enum_labels or spec.enum_values
        return [{"value": v, "label": label} for v, label in zip(spec.enum_values, labels)]

    def _policy_lock(self, key: str) -> bool | None:
        """The value the team's policy pins ``key`` to, or ``None`` when free."""
        return self.team_policy().locked_value(key)

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
        pol = self.team_policy()
        editable = []
        for spec in settings_schema.EDITABLE:
            value = getattr(cfg, spec.accessor)
            if isinstance(value, tuple):
                value = list(value)
            # HONESTY: a policy-locked row says so, and says where the lock came
            # from. The value shown is already the tightened one (app_cfg is folded
            # at every assignment), so the page can never display a setting the app
            # is not actually running with.
            locked = pol.locked_value(spec.key)
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
                    "locked": locked is not None,
                    "locked_note": (
                        settings_schema.POLICY_LOCK_NOTE if locked is not None else ""
                    ),
                }
            )
        return {
            "groups": list(settings_schema.GROUPS),
            "editable": editable,
            "admin": self._admin_rows(),
            "pii": self._pii_status(),
            "policy": self._policy_rows(pol),
            "ai_enabled": cfg.ai_enabled,
        }

    def _policy_rows(self, pol: policy.Policy) -> dict:
        """The value-free "what your team enforces" block for the Settings page:
        counts, rule names, and the ignored-rule warning — never a data value."""
        from mooring import __version__

        return {
            "in_force": pol.in_force,
            "unreadable": pol.unreadable,
            "lines": policy.describe(
                pol,
                current_version=__version__,
                local_guard=workspace_config.guard_mode(self.cfg.workspace()),
            ),
            "locked_keys": sorted(pol.settings),
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
            {"label": "Team policy", "value": self._policy_admin_value()},
        ]

    def _policy_admin_value(self) -> str:
        """A one-line, value-free summary of the synced team policy for the admin block."""
        pol = self.team_policy()
        if pol.unreadable:
            return "unreadable mooring.toml — no policy in force"
        if not pol.in_force:
            return "none"
        parts = []
        if pol.min_version:
            parts.append(f"min version {pol.min_version}")
        if pol.push_guard:
            parts.append(f"push guard {pol.push_guard}")
        if pol.propose_only:
            parts.append(f"{len(pol.propose_only.globs)} propose-only")
        if pol.ai_off:
            parts.append(f"{len(pol.ai_off.globs)} AI-off")
        if pol.settings:
            parts.append(f"{len(pol.settings)} locked setting(s)")
        if pol.ignored:
            parts.append(f"{len(pol.ignored)} rule(s) ignored")
        return ", ".join(parts)


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












    def _activity(self, op: str, **fields) -> None:
        """Append to the workspace's LOCAL activity ledger (activity.py) — the
        "what just happened?" journal, distinct from the opt-in central telemetry
        (which never carries file paths). Best-effort by construction."""
        activity.record(self.cfg.workspace(), op, **fields)

    def _sync_op_body(self, name: str, op) -> tuple[dict, int]:
        """Run one sync operation and shape its JSON body + status. Split out of
        :meth:`_sync_op` so the guarded push/propose endpoints can append their
        warn-and-confirm fields before the response is sealed."""
        try:
            result = op()
        except Unreachable as exc:
            # Ordered BEFORE the generic pair (Unreachable subclasses GitHubError):
            # an outage is not a sync failure — nothing was lost, nothing to fix.
            telemetry.log_error(exc=exc, op=name)
            return {
                "error": "GitHub is unreachable — your changes are safe on disk; "
                "push or pull again when you're back online."
            }, 503
        except (GitHubError, OSError) as exc:
            telemetry.log_error(exc=exc, op=name)
            return {"error": str(exc)}, 502
        telemetry.log_event(
            name,
            pulled=result.pulled,
            pushed=result.pushed,
            proposed=result.proposed,
            conflicts=len(result.skipped_conflicts) + len(result.blocked_conflicts),
            lines=len(result.lines),
        )
        self._activity(
            name,
            summary=result.summary(),
            lines=result.lines[:20],
            trashed=[{"path": p, "token": t} for p, t in result.trashed],
        )
        body = {"lines": result.lines, "summary": result.summary()}
        if result.trashed:
            # The Undo affordance: local pre-images banked before this operation
            # overwrote/removed files (the frontend shows a toast per entry).
            body["trashed"] = [{"path": p, "token": t} for p, t in result.trashed]
        if result.review_branch:
            body["review_branch"] = result.review_branch
            body["compare_url"] = result.compare_url
            if result.pull_url:  # Slice 2: the PR mooring opened for this proposal
                body["pull_url"] = result.pull_url
                body["pull_number"] = result.pull_number
        return body, 200

    def _sync_op(self, name: str, op) -> JSONResponse:
        body, status = self._sync_op_body(name, op)
        return JSONResponse(body, status_code=status)







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

        # Keyed on everything that shapes the provider — provider + model, and the
        # OpenAI-compatible endpoint (base_url/api_version) — so changing the endpoint
        # in Settings rebuilds the provider live instead of reusing a stale client.
        key = (
            self.app_cfg.ai_provider,
            self.app_cfg.ai_model,
            self.app_cfg.ai_openai_base_url,
            self.app_cfg.ai_openai_api_version,
        )
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
        semantic_models=None,
        helpers=None,
        catalog=None,
    ):
        """Open a streaming Copilot chat session bound to this notebook.

        ``model``/``reasoning_effort`` override the configured defaults;
        ``dictionary`` (a parsed index) enables the value-free dictionary tools;
        ``semantic_models`` (pre-parsed, already-gated Power BI models from
        build_context) enables the semantic-model tools; ``catalog`` (the parsed
        repo-wide notebook index) enables the notebook-catalog tools.
        Raises AIError (-> 502) if Copilot isn't available/installed; a sign-in or
        handshake failure surfaces over the SSE stream instead (the session starts
        in the background — ``background=True`` — so the open response is immediate).
        """
        provider = self._provider_for()
        # Wire the fan-out "investigate" tool: a value-free coordinator closure that opens
        # READ-ONLY sub-agents per branch and returns their merged findings. None when the
        # feature is off, so open_chat doesn't build mooring_investigate. The sub-agents
        # never get this tool (open_readonly forces read_only=True), so depth is bounded to 1.
        import threading

        from mooring.app.investigate_service import make_run_investigation

        # The parent session's cancel signal for work that OUTLIVES a turn. A fan-out runs
        # read-only sub-agents on its own pool while the parent's tool call blocks, so
        # closing / idle-reaping / repo-switching the chat must stop them — otherwise each
        # branch runs to its full branch_timeout, burning spend the analyst cancelled. It
        # is armed by a close hook once the session exists (below).
        investigate_abort = threading.Event()
        run_investigation = make_run_investigation(
            app_cfg=self.app_cfg,
            notebook_rel=notebook_rel,
            build_context=lambda nb, ds: self.chat.build_context(
                self.app_cfg, workspace, nb, ds, folders=self.cfg.folders
            ),
            open_readonly_session=lambda ctx, nb, m, e: self._make_investigator_session(
                ctx, workspace, nb, model=m, reasoning_effort=(e or None)
            ),
            abort=investigate_abort,
        )
        session = provider.open_chat(
            system_context=system_context,
            workspace=workspace,
            folders=self.cfg.folders,
            notebook_rel=notebook_rel,
            model=model,
            reasoning_effort=reasoning_effort,
            dictionary=dictionary,
            semantic_models=semantic_models,
            helpers=helpers,
            catalog=catalog,
            run_investigation=run_investigation,
            # The whole guard config travels as ONE object, so a field can't be
            # silently dropped on the way to the session (the session downloads any
            # NER model in the background and the prompt path skips it until ready).
            pii=self.app_cfg.ai.pii,
            # Pasted-traceback sanitise-and-hold (default ON) — armed at the same
            # seam as the PII config; the session already holds the workspace and
            # notebook the sanitiser needs, so no route ever arms it separately.
            traceback_guard=self.app_cfg.ai.traceback_guard,
            # Don't block the open request on the (CLI-spawning, networked) Copilot
            # handshake — stream readiness/failure over the SSE channel instead.
            background=True,
        )
        # Arm the cancel signal: closing the session (explicit close, idle-reap, repo
        # switch, shutdown) now aborts any in-flight investigation within one poll.
        session.add_close_hook(investigate_abort.set)
        return session

    def _make_investigator_session(
        self, ctx, workspace: Path, notebook_rel: str, model: str = "", reasoning_effort=None
    ):
        """A READ-ONLY value-blind sub-agent for ONE investigate branch: the same session
        as the interactive chat, but built with NO propose/edit tool and NO
        ``mooring_investigate`` (so it can neither write nor recurse), and the PII guard
        forced to BLOCK mode because there is no human at a sub-agent to confirm. ``ctx`` is
        the 7-tuple ``build_context`` returns; a branch's finding is trusted because this
        session is structurally value-blind, which the read_only tool subset guarantees."""
        from dataclasses import replace

        system_context, index, _pii_banner, _live_text, models, code_index, catalog = ctx
        provider = self._provider_for()
        return provider.open_chat(
            system_context=system_context,
            workspace=workspace,
            folders=self.cfg.folders,
            notebook_rel=notebook_rel,
            model=model,
            reasoning_effort=reasoning_effort,
            dictionary=index,
            semantic_models=models,
            helpers=code_index,
            catalog=catalog,
            read_only=True,
            pii=replace(self.app_cfg.ai.pii, block_prompt=True),
            traceback_guard=self.app_cfg.ai.traceback_guard,
            background=True,
        )
















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
            # The traceback guard holds only SANITISED text; the batch worker
            # auto-confirms it unattended ONLY when the PII scan of that sanitised
            # text (prose around the traceback is untouched by design) did not
            # itself hold — otherwise the forced block above applies — see ai/batch.py.
            traceback_guard=self.app_cfg.ai.traceback_guard,
            background=True,
        )

    def _new_batch_planner(self, workspace: Path, broadcaster, abort):
        """Build + start an appendable batch planner bound to this workspace, streaming
        each value-free per-job lifecycle event over the run's broadcaster. The planner
        owns one bounded worker pool for the run's whole life; ``add`` may be called
        repeatedly while earlier jobs build, so the user can keep writing the next."""
        from mooring import notebook_template
        from mooring.ai.batch import BatchPlanner
        from mooring.app import batch_service

        planner = BatchPlanner(
            config=self.app_cfg.ai.batch,
            pii=self.app_cfg.ai.pii,
            make_notebook=lambda name: notebook_template.create_unique(workspace, name),
            # Deliberately NOT _build_chat_context (which passes cfg.folders): batch
            # builder sessions get no semantic-model tools (_make_batch_session passes
            # no models), so their context must not carry the "use the model tools"
            # hint either — and skipping folders also skips the per-job TMDL
            # extraction whose result the [:2] slice would throw away anyway.
            build_context=lambda nb, ds: self.chat.build_context(
                self.app_cfg, workspace, nb, ds
            )[:2],
            open_session=lambda ctx, nb, model, effort, dic: self._make_batch_session(
                ctx, nb, model=model, reasoning_effort=(effort or None), dictionary=dic
            ),
            is_disabled=policy.ai_gate(workspace),
            discard_notebook=lambda nb: batch_service.discard_batch_notebook(workspace, nb),
            # emit_job is the broadcaster's PUBLIC progress channel: it touches the
            # activity clock (so a building run is never idle-reaped) and fans out.
            on_progress=broadcaster.emit_job,
            abort=abort,
        )
        return planner.start()














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
    from mooring.hub.routes import batch, chat, files, reviews, runs, settings, setup
    from mooring.hub.routes import schedule as schedule_routes
    from mooring.hub.routes import sync as sync_routes

    app = Starlette(
        routes=[
            Route("/", pages.index_page),
            Route("/api/state", setup.api_state),
            Route("/api/setup", setup.api_setup, methods=["POST"]),
            Route("/api/repo/switch", setup.api_repo_switch, methods=["POST"]),
            Route("/api/repo/remove", setup.api_repo_remove, methods=["POST"]),
            Route("/api/ui/theme", setup.api_set_theme, methods=["POST"]),
            Route("/api/hub/feature", setup.api_set_featured, methods=["POST"]),
            Route("/api/hub/context-folder", setup.api_set_context_folder, methods=["POST"]),
            Route("/api/ai/context/subscribe", setup.api_context_subscribe, methods=["POST"]),
            Route("/api/doctor", setup.api_doctor, methods=["POST"]),
            Route("/settings", pages.settings_page),
            Route("/api/settings", settings.api_get_settings),
            Route("/api/settings", settings.api_set_settings, methods=["POST"]),
            Route("/api/settings/reset", settings.api_reset_settings, methods=["POST"]),
            Route("/api/login/start", setup.api_login_start, methods=["POST"]),
            Route("/api/login/poll", setup.api_login_poll),
            Route("/api/logout", setup.api_logout, methods=["POST"]),
            Route("/api/discover", sync_routes.api_discover),
            Route("/api/whatsnew", sync_routes.api_whatsnew),
            Route("/api/whatsnew/detail", sync_routes.api_whatsnew_detail, methods=["POST"]),
            Route("/api/freshness", sync_routes.api_freshness),
            Route("/api/adopt", sync_routes.api_adopt, methods=["POST"]),
            Route("/api/pull", sync_routes.api_pull, methods=["POST"]),
            Route("/api/push", sync_routes.api_push, methods=["POST"]),
            Route("/api/propose", sync_routes.api_propose, methods=["POST"]),
            Route("/api/resolve", sync_routes.api_resolve, methods=["POST"]),
            Route("/api/resolve/cells", sync_routes.api_resolve_cells, methods=["POST"]),
            Route(
                "/api/resolve/cells/apply",
                sync_routes.api_resolve_cells_apply,
                methods=["POST"],
            ),
            Route("/api/recall", sync_routes.api_recall, methods=["POST"]),
            Route("/api/new", files.api_new, methods=["POST"]),
            Route("/api/duplicate", files.api_duplicate, methods=["POST"]),
            Route("/api/open", files.api_open, methods=["POST"]),
            Route("/api/reveal", files.api_reveal, methods=["POST"]),
            Route("/api/deliver", files.api_deliver, methods=["POST"]),
            Route("/api/deliver/excel", files.api_deliver_excel, methods=["POST"]),
            Route("/api/verify", files.api_verify, methods=["POST"]),
            Route("/api/delete", files.api_delete, methods=["POST"]),
            Route("/api/rollback", files.api_rollback, methods=["POST"]),
            Route("/api/undo", files.api_undo, methods=["POST"]),
            Route("/api/history", files.api_history),
            Route("/api/history/file", files.api_history_file),
            Route("/api/restore", files.api_restore, methods=["POST"]),
            Route("/api/diff", files.api_diff, methods=["POST"]),
            Route("/activity", pages.activity_page),
            Route("/reviews", pages.reviews_page),
            Route("/api/reviews", reviews.api_reviews),
            Route("/api/reviews/detail", reviews.api_review_detail, methods=["POST"]),
            Route("/api/reviews/submit", reviews.api_review_submit, methods=["POST"]),
            Route("/api/schedules", schedule_routes.api_schedules),
            Route("/api/schedule/add", schedule_routes.api_schedule_add, methods=["POST"]),
            Route("/api/schedule/remove", schedule_routes.api_schedule_remove, methods=["POST"]),
            Route("/api/schedule/pause", schedule_routes.api_schedule_pause, methods=["POST"]),
            Route(
                "/api/schedule/background",
                schedule_routes.api_schedule_background,
                methods=["POST"],
            ),
            Route("/api/refresh", schedule_routes.api_refresh, methods=["POST"]),
            # Attended parameterised runs: one notebook, once per value (roadmap:
            # parameterised-runs). /api/run/start EXECUTES a notebook; it never pushes.
            Route("/api/run/start", runs.api_run_start, methods=["POST"]),
            Route("/api/run/state", runs.api_run_state),
            Route("/api/run/cancel", runs.api_run_cancel, methods=["POST"]),
            Route("/api/trash", files.api_trash),
            Route("/api/trash/restore", files.api_trash_restore, methods=["POST"]),
            Route("/api/activity", files.api_activity),
            Route("/ai/chat", pages.chat_page),
            Route("/workbench", pages.workbench_page),
            Route("/api/ai/datasets", chat.api_chat_datasets),
            Route("/api/ai/models", chat.api_chat_models),
            Route("/api/ai/status", chat.api_ai_status),
            Route("/api/ai/login/start", chat.api_ai_login_start, methods=["POST"]),
            Route("/api/ai/login/poll", chat.api_ai_login_poll),
            Route("/api/ai/key", chat.api_ai_key_set, methods=["POST"]),
            Route("/api/ai/chat/open", chat.api_chat_open, methods=["POST"]),
            Route("/api/ai/chat/stream/{sid}", chat.api_chat_stream),
            Route("/api/ai/chat/send", chat.api_chat_send, methods=["POST"]),
            Route("/api/ai/chat/apply", chat.api_chat_apply, methods=["POST"]),
            Route("/api/ai/chat/rollback", chat.api_chat_rollback, methods=["POST"]),
            Route("/api/ai/notebook/toggle", chat.api_notebook_ai_toggle, methods=["POST"]),
            Route("/api/ai/model/toggle", chat.api_model_ai_toggle, methods=["POST"]),
            Route("/ai/batch", pages.batch_page),
            Route("/api/ai/batch/state", batch.api_batch_state),
            Route("/api/ai/batch/open", batch.api_batch_open, methods=["POST"]),
            Route("/api/ai/batch/add", batch.api_batch_add, methods=["POST"]),
            Route("/api/ai/batch/stream/{batch_id}", batch.api_batch_stream),
            Route("/api/ai/batch/tray/{batch_id}", batch.api_batch_tray),
            Route("/api/ai/batch/apply", batch.api_batch_apply, methods=["POST"]),
            Route("/api/ai/batch/refine", batch.api_batch_refine, methods=["POST"]),
            Route("/api/ai/batch/force", batch.api_batch_force, methods=["POST"]),
            Route("/api/ai/batch/cancel", batch.api_batch_cancel, methods=["POST"]),
            # Always revalidate: a stale cached chat_core.js beside a fresh chat.js is a
            # silent, reload-proof frontend break. See _RevalidatingStatic.
            Mount("/static", _RevalidatingStatic(directory=static)),
        ],
        lifespan=lifespan,
    )
    # The one shared state-holder every handler reaches via request.app.state.hub.
    app.state.hub = hub
    return app


# A fixed default so the hub serves from a STABLE origin (http://127.0.0.1:8724)
# every launch. The browser scopes localStorage per origin *including the port*,
# so a fresh random port each launch would orphan everything we persist
# client-side — the first-run checklist (incl. its dismissed flag), the what's-new
# watch set, the AI model/effort override. A stable port keeps them across
# relaunches. Chosen outside the OS ephemeral range and clear of common dev ports
# (3000/5000/8000/8080/8888…). `--port` still overrides, and if 8724 is already
# taken bind_or_free() falls back to a random free port for that session.
DEFAULT_HUB_PORT = 8724


# The refresh sweep's tick. A tick with nothing due costs a small JSON read and a few
# datetime comparisons, so this can be frequent; what it must not do is let a schedule sit
# visibly overdue for long once the hub IS open.
_SWEEP_INTERVAL_S = 300
# Delay before the first (catch-up) sweep, so a notebook run never competes with hub
# startup, the editor pre-warm, and the user's first click.
_SWEEP_FIRST_DELAY_S = 30


def _start_refresh_sweep(hub: Hub) -> None:
    """Start the background clock behind scheduled refreshes (roadmap tiers 0 and 1).

    Tier 0 ("catch up when the hub opens") and tier 1 ("fire on cadence while the hub is
    open") are the SAME loop — tier 0 is simply its first tick. Neither needs any OS
    permission, which is the point: on a managed laptop where Task Scheduler is blocked by
    policy, this is still the whole feature.

    Three deliberate restraints:

    * Only ``run_hub`` starts this — never ``create_app`` — so the test suite and any
      embedded use never execute a notebook (the same posture as the editor pre-warm).
    * ``auto_only`` means only a verified, unpaused, didn't-fail-last-time schedule fires by
      itself. Anything doubtful waits for a human to click Run now, so an unattended run
      never surprises someone whose notebook is in a questionable state.
    * It sweeps the ACTIVE repo's workspace only. Refreshing notebooks in a repo the user
      isn't looking at would be a surprise, and ``hub.cfg`` follows a repo switch.

    Best-effort throughout: a sweep that raises must never take the hub down with it."""

    def _sweep() -> None:
        from mooring.app import refresh

        time.sleep(_SWEEP_FIRST_DELAY_S)
        while True:
            with contextlib.suppress(Exception):
                results = refresh.run_due(hub.cfg, auto_only=True)
                for result in results:
                    print(f"mooring refresh: {refresh.describe_result(result)}")
            time.sleep(_SWEEP_INTERVAL_S)

    threading.Thread(target=_sweep, name="refresh-sweep", daemon=True).start()


def run_hub(app_cfg: config.AppConfig, open_browser: bool = True, port: int | None = None) -> int:
    hub = Hub(app_cfg)
    app = create_app(hub)
    port = port or bind_or_free(DEFAULT_HUB_PORT)
    url = f"http://127.0.0.1:{port}/"
    telemetry.log_event("hub_start")

    # Trash retention runs at start, in the background and best-effort — a full
    # or locked store must never delay or break the hub coming up.
    def _prune_trash() -> None:
        with contextlib.suppress(Exception):
            cfg = app_cfg.config_for(None)
            trash.prune(
                cfg.workspace(),
                keep_days=cfg.trash_keep_days,
                keep_per_file=cfg.trash_keep_per_file,
                max_total_mb=cfg.trash_max_total_mb,
            )

    threading.Thread(target=_prune_trash, name="trash-prune", daemon=True).start()
    _start_refresh_sweep(hub)
    print(f"mooring hub running at {url} (Ctrl+C to quit)")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    # Pre-warm the editor subprocess and prime heavy imports in the background so the
    # first notebook open / chat open isn't paying that cold start on the user's click.
    hub.warmup()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
