"""Setup, state, repo management, theme, and GitHub login endpoints."""

from __future__ import annotations

import time
import tomllib

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import __version__, auth, config, config_store, sync, telemetry, workspace_config
from mooring.github import AuthFailed, GitHubError, TlsFailure, Unreachable, compare_url
from mooring.runtime import workspace_hint


def _read_context_dirs(hub, cfg) -> tuple[str, ...]:
    """The context folders this machine's copilot would read (subscription ∩ offer)."""
    from mooring.app import context_folders as ctxdirs

    return ctxdirs.read_dirs(hub.app_cfg, cfg.workspace())


def api_state(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    cfg = hub.cfg
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
        # Repo-curated hub display order: the folders a teammate STARRED (synced
        # mooring.toml [hub] featured_folders) get pinned to the top; the rest fold
        # under a "More folders" disclosure. Additive — absent/empty = ordinary render.
        "featured_folders": list(workspace_config.featured_folders(cfg.workspace())),
        # The team's OFFERED AI context folders (synced mooring.toml [ai] context_folders):
        # the value-free menu a curator publishes so the copilot can read them. Reading
        # still needs each machine's own [ai] context consent — this list is only the
        # offer. Drives the per-folder "AI context" toggle (repo mode + AI on).
        "context_folders": list(workspace_config.context_folders(cfg.workspace())),
        # Which offered folders THIS machine's copilot actually reads (subscription ∩
        # offer, or the whole offer when unsubscribed). Drives the per-user subscription
        # checklist; the offer stays the ceiling.
        "selected_context_folders": list(_read_context_dirs(hub, cfg)),
        "repos": [
            {
                "alias": s.alias,
                "slug": s.slug,
                "branch": s.branch,
                "workspace": str(hub.app_cfg.config_for(s.alias).workspace()),
                "active": s.alias == hub.app_cfg.active_alias,
            }
            for s in hub.app_cfg.repos
        ],
        "active_repo": hub.app_cfg.active_alias,
        "ui_theme": hub.app_cfg.ui_theme,
        # What notebooks can import + how to add packages (mode-aware: locked uv
        # project vs mooring's bundled env vs a frozen build). See _notebook_env.
        "env": hub._notebook_env(cfg.workspace()),
        "ai_chat": hub.app_cfg.ai_enabled,
        # This machine's [ai] context consent bool — gates whether the copilot reads ANY
        # team context. Drives showing the per-user subscription checklist.
        "ai_context": hub.app_cfg.ai_context,
        # Whether the workspace-level "Batch build" entry should show (AI on AND
        # the opt-in batch orchestrator enabled). The page itself re-gates.
        "ai_batch": hub.app_cfg.ai_enabled and hub.app_cfg.ai_batch_enabled,
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
        body["files"], body["artifacts"] = hub._files_artifacts(report, cfg.workspace())
        return JSONResponse(body)
    if not auth.get_token(host=cfg.host):
        return JSONResponse(body)
    try:
        body["user"] = hub.username()
        body["logged_in"] = True
        report = sync.status(hub.client(), cfg)
        body["files"], body["artifacts"] = hub._files_artifacts(report, cfg.workspace())
        body["summary"] = report.summary()
        # Remember which branch head this render was computed from, so a later
        # /api/freshness can tell the client whether its cached rows are stale.
        hub._state_heads[str(cfg.workspace())] = report.head_commit
        # Whether "Recall last push" has anything to recall (a local manifest
        # read — no extra API call), and WHICH files it would touch — the
        # confirm dialog names them so the user can catch a stale record.
        from mooring import manifest as manifest_mod

        last_push = manifest_mod.load(cfg.workspace()).last_push
        body["can_recall"] = bool(last_push)
        body["recall_paths"] = sorted(last_push)
        if report.review_branch:
            body["review"] = {
                "branch": report.review_branch,
                "compare_url": compare_url(
                    cfg.owner, cfg.repo, cfg.branch, report.review_branch, host=cfg.host
                ),
            }
    except Unreachable as exc:
        # An outage, not an auth failure (ordered BEFORE AuthFailed/GitHubError;
        # Unreachable subclasses the latter): the token is NOT deleted and the
        # user stays logged in. Fall back to the last observed remote view so
        # the files card degrades to stale-with-a-banner instead of vanishing.
        # hub._state_heads is deliberately not touched — /api/freshness has
        # nothing new to compare against, and it too stays silent offline.
        telemetry.log_error(exc=exc, op="state")
        body["user"] = hub._user_login  # may be "" on a cold start; don't retry here
        body["logged_in"] = True
        as_of = ""
        cached = sync.cached_status(cfg)
        if cached is not None:
            report, as_of = cached
            body["files"], body["artifacts"] = hub._files_artifacts(report, cfg.workspace())
            body["summary"] = report.summary()
        body["offline"] = {
            "reason": "tls" if isinstance(exc, TlsFailure) else "network",
            "as_of": as_of,
        }
    except AuthFailed:
        auth.delete_token(host=cfg.host)
        hub._user_login = ""
        body["logged_in"] = False
        body["error"] = "Your GitHub login expired. Please log in again."
    except GitHubError as exc:
        telemetry.log_error(exc=exc, op="state")
        body["error"] = str(exc)
    return JSONResponse(body)


async def api_doctor(request: Request) -> JSONResponse:
    """Run the diagnosis engine (mooring.doctor) — the hub's Health check.

    On demand only, off the event loop; never part of startup or /api/state.
    The Copilot probe is appended HERE (the engine sits below ai/ and cannot
    import it): a slow force-check is fine for an explicit health click."""
    import asyncio
    from dataclasses import asdict

    from mooring import doctor

    hub = request.app.state.hub
    cfg = hub.cfg

    def copilot_probe() -> doctor.ProbeResult:
        try:
            st = hub._provider_for().status(force=True)
        except Exception:  # noqa: BLE001  # a probe never raises; unknown is honest
            return doctor.ProbeResult(
                "copilot", "AI copilot", doctor.UNKNOWN,
                "Copilot could not be checked.",
                "Use the Copilot menu in the hub header to sign in / check status.",
            )
        if not st.available:
            return doctor.ProbeResult(
                "copilot", "AI copilot", doctor.WARN,
                "Copilot isn't available in this build.",
                "Install the mooring[copilot] extra, or ask your admin to include it.",
            )
        if not st.connected:
            return doctor.ProbeResult(
                "copilot", "AI copilot", doctor.WARN,
                "Copilot is installed but not signed in.",
                "Sign in from the Copilot menu in the hub header.",
            )
        detail = f"Connected as @{st.account}." if st.account else "Connected."
        return doctor.ProbeResult("copilot", "AI copilot", doctor.PASS, detail)

    extra = [copilot_probe] if hub.app_cfg.ai_enabled else []
    results = await asyncio.to_thread(doctor.run_probes, cfg, extra)
    telemetry.log_event(
        "doctor",
        **{s: sum(1 for r in results if r.status == s) for s in ("pass", "warn", "fail")},
    )
    return JSONResponse(
        {
            "results": [asdict(r) for r in results],
            "report": doctor.build_report(results, cfg),
        }
    )


async def api_setup(request: Request) -> JSONResponse:
    """Register a repo (and on first run, the OAuth client id); makes it active."""
    hub = request.app.state.hub
    data = await request.json()
    fields = {
        k: str(data.get(k, "")).strip()
        for k in ("client_id", "owner", "repo", "branch", "alias", "host")
    }
    if not (fields["owner"] and fields["repo"]):
        return JSONResponse({"error": "owner and repo are required"}, status_code=400)
    if not (fields["client_id"] or hub.app_cfg.client_id):
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
    hub.reload()
    telemetry.log_event("repo_add", alias=fields["alias"] or fields["repo"])
    return JSONResponse({"ok": True, "active_repo": hub.app_cfg.active_alias})


async def api_repo_switch(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    alias = str(data.get("alias", ""))
    try:
        config_store.set_active(alias)
    except KeyError:
        return JSONResponse({"error": f"Unknown repo alias {alias!r}."}, status_code=400)
    hub.reload()
    telemetry.log_event("repo_switch", alias=alias)
    return JSONResponse({"ok": True, "active_repo": alias})


async def api_repo_remove(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    alias = str(data.get("alias", ""))
    try:
        workspace = hub.app_cfg.config_for(alias).workspace()
        config_store.remove_repo(alias)
    except KeyError:
        return JSONResponse({"error": f"Unknown repo alias {alias!r}."}, status_code=400)
    hub.reload()
    telemetry.log_event("repo_remove", alias=alias)
    return JSONResponse(
        {"ok": True, "lines": [f"Removed {alias!r}; workspace folder kept at {workspace}"]}
    )


async def api_set_theme(request: Request) -> JSONResponse:
    """Set the shared appearance (light/dark/system) from the hub toggle.

    Persists it to the user config, updates the live config, and re-themes
    every running editor's workspace ``.marimo.toml`` so open notebooks pick
    up the new theme on reopen/reload. The chat UI re-themes itself via the
    ``/api/state`` value plus a same-origin storage event. Does NOT reload
    the whole config (that would drop open chat sessions for an appearance
    change)."""
    from dataclasses import replace

    hub = request.app.state.hub
    data = await request.json()
    theme = config.normalize_theme(data.get("theme", ""))
    config_store.set_value("ui.theme", theme)
    with hub._lock:
        hub.app_cfg = replace(hub.app_cfg, ui_theme=theme)
    for editor in list(hub.editors.values()):
        editor.apply_theme(theme)
    telemetry.log_event("ui_theme", theme=theme)
    return JSONResponse({"ok": True, "theme": theme})


async def api_set_featured(request: Request) -> JSONResponse:
    """Star (or un-star) one folder in the synced ``mooring.toml`` ``[hub]
    featured_folders`` so the hub shows it pinned at the top for the whole team, with
    the rest folded under "More folders". Display-only and additive — it NEVER touches
    ``[sync] folders``, so what actually syncs is unchanged. The path is validated to
    resolve under the workspace (no traversal), but need not exist (star before the
    first pull; un-star after a rename to clear a stale entry). Order is preserved
    (display priority). The write runs off the event loop like the model toggle."""
    hub = request.app.state.hub
    data = await request.json()
    folder = str(data.get("folder", "")).strip()
    featured = bool(data.get("featured", True))
    if not folder:
        return JSONResponse({"error": "A folder is required."}, status_code=400)
    workspace = hub.cfg.workspace()
    key = workspace_config.normalize_notebook(folder)
    if not key:  # e.g. "/" or "///" — normalizes to "", which can never be stored
        return JSONResponse({"error": "A folder is required."}, status_code=400)
    try:
        target = (workspace / key).resolve()
        target.relative_to(workspace.resolve())
    except (ValueError, OSError):
        return JSONResponse({"error": "Path escapes the workspace."}, status_code=400)
    try:
        await run_in_threadpool(workspace_config.set_featured_folder, workspace, key, featured)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        # A non-UTF-8 mooring.toml (UTF-16/BOM — a Windows hazard) decodes to a
        # UnicodeDecodeError, not a TOMLDecodeError; both mean "fix the file first".
        return JSONResponse(
            {"error": "mooring.toml is malformed — fix it before changing featured folders."},
            status_code=409,
        )
    telemetry.log_event("hub_feature", featured=int(featured))
    return JSONResponse({"ok": True, "folder": key, "featured": featured})


async def api_set_context_folder(request: Request) -> JSONResponse:
    """Offer (or withdraw) one folder as team AI context in the synced ``mooring.toml``
    ``[ai] context_folders`` — the value-free menu a curator publishes so the whole team's
    copilot can read it (reading still needs each machine's own ``[ai] context`` consent).
    Unlike featured folders this is AI GOVERNANCE, not display order, so the offer is stored
    SORTED. The path is validated to resolve under the workspace (no traversal) but need not
    exist yet (offer before the first pull; withdraw after a rename to clear a stale entry).
    The write runs off the event loop like the featured/model toggles."""
    hub = request.app.state.hub
    data = await request.json()
    folder = str(data.get("folder", "")).strip()
    offered = bool(data.get("offered", True))
    if not folder:
        return JSONResponse({"error": "A folder is required."}, status_code=400)
    workspace = hub.cfg.workspace()
    key = workspace_config.normalize_notebook(folder)
    if not key:  # e.g. "/" or "///" — normalizes to "", which can never be stored
        return JSONResponse({"error": "A folder is required."}, status_code=400)
    try:
        target = (workspace / key).resolve()
        target.relative_to(workspace.resolve())
    except (ValueError, OSError):
        return JSONResponse({"error": "Path escapes the workspace."}, status_code=400)
    try:
        await run_in_threadpool(workspace_config.set_context_folder, workspace, key, offered)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        # A non-UTF-8 mooring.toml (UTF-16/BOM — a Windows hazard) decodes to a
        # UnicodeDecodeError, not a TOMLDecodeError; both mean "fix the file first".
        return JSONResponse(
            {"error": "mooring.toml is malformed — fix it before changing context folders."},
            status_code=409,
        )
    telemetry.log_event("hub_context_folder", offered=int(offered))
    return JSONResponse({"ok": True, "folder": key, "offered": offered})


async def api_context_subscribe(request: Request) -> JSONResponse:
    """Subscribe/unsubscribe THIS machine's copilot to one of the repo's offered AI
    context folders — a per-user, per-repo choice (the synced offer stays the ceiling).

    Writes the user config.toml ``[repos.<alias>].ai_context_folders`` and updates the
    live config WITHOUT a full ``hub.reload()``: a subscription changes only what the
    copilot READS, so open chat sessions and in-flight batches must not be torn down
    (the theme endpoint's light-refresh idiom). Selecting every offered folder clears the
    subscription (follow the whole offer, including later additions); an explicit empty
    selection reads nothing. Rejects a folder the repo doesn't offer."""
    from dataclasses import replace

    from mooring.app import context_folders as ctxdirs

    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"error": "AI is disabled."}, status_code=400)
    alias = hub.app_cfg.active_alias
    if not alias:
        return JSONResponse({"error": "No active repo to subscribe for."}, status_code=400)
    data = await request.json()
    folder = workspace_config.normalize_notebook(str(data.get("folder", "")))
    on = bool(data.get("on", True))
    workspace = hub.cfg.workspace()
    offer = workspace_config.context_folders(workspace)
    if folder not in offer:
        return JSONResponse(
            {"error": "That folder isn't offered as team AI context."}, status_code=400
        )
    # Derive the new explicit subscription from the current effective read set.
    selected = set(ctxdirs.read_dirs(hub.app_cfg, workspace))
    selected.add(folder) if on else selected.discard(folder)
    new_sub = None if selected >= set(offer) else sorted(selected)
    try:
        await run_in_threadpool(config_store.set_repo_context_folders, alias, new_sub)
    except (KeyError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"error": "config.toml is malformed — fix it before changing your subscription."},
            status_code=409,
        )
    # Light refresh: rebuild only the active repo's subscription in the live config.
    with hub._lock:
        specs = tuple(
            replace(s, context_folders=(None if new_sub is None else tuple(new_sub)))
            if s.alias == alias
            else s
            for s in hub.app_cfg.repos
        )
        hub.app_cfg = replace(hub.app_cfg, repos=specs)
    telemetry.log_event("ai_context_subscribe", on=int(on))
    return JSONResponse(
        {"ok": True, "folder": folder, "on": on,
         "selected_context_folders": list(ctxdirs.read_dirs(hub.app_cfg, workspace))}
    )


def api_login_start(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    try:
        device = auth.start_device_flow(hub.cfg.client_id, host=hub.cfg.host)
    except Exception as exc:  # noqa: BLE001  # shown in the UI
        return JSONResponse({"error": auth.device_flow_hint(hub.cfg.host, exc)}, status_code=502)
    with hub._lock:
        hub._device = device
        hub._poll_interval = device.interval
        hub._next_poll = time.monotonic() + device.interval
    return JSONResponse(
        {"user_code": device.user_code, "verification_uri": device.verification_uri}
    )


def api_login_poll(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    with hub._lock:
        device = hub._device
        if device is None:
            return JSONResponse({"status": "error", "message": "No login in progress."})
        if time.monotonic() < hub._next_poll:
            return JSONResponse({"status": "pending"})
    try:
        result = auth.poll_once(hub.cfg.client_id, device, interval=hub._poll_interval)
    except auth.AuthError as exc:
        with hub._lock:
            hub._device = None
        return JSONResponse({"status": "error", "message": str(exc)})
    if result.token:
        # device.host, not hub.cfg.host: the token belongs to the host the
        # flow was started against, even if the config changed mid-login.
        auth.save_token(result.token, host=device.host)
        with hub._lock:
            hub._device = None
        hub._user_login = ""
        telemetry.log_event("login")
        return JSONResponse({"status": "ok"})
    with hub._lock:
        hub._poll_interval = result.interval
        hub._next_poll = time.monotonic() + result.interval
    return JSONResponse({"status": "pending"})


def api_logout(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    auth.delete_token(host=hub.cfg.host)
    hub._user_login = ""
    telemetry.log_event("logout")
    return JSONResponse({"ok": True})
