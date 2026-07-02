"""Setup, state, repo management, theme, and GitHub login endpoints."""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import __version__, auth, config, config_store, sync, telemetry
from mooring.github import AuthFailed, GitHubError, compare_url
from mooring.runtime import workspace_hint


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
        # read — no extra API call). Drives the toolbar button's visibility.
        from mooring import manifest as manifest_mod

        body["can_recall"] = bool(manifest_mod.load(cfg.workspace()).last_push)
        if report.review_branch:
            body["review"] = {
                "branch": report.review_branch,
                "compare_url": compare_url(
                    cfg.owner, cfg.repo, cfg.branch, report.review_branch, host=cfg.host
                ),
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
