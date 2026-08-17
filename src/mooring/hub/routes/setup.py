"""Setup, state, repo management, theme, and GitHub login endpoints."""

from __future__ import annotations

import time
import tomllib

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import (
    __version__,
    auth,
    config,
    config_store,
    credhelper,
    githost,
    sync,
    telemetry,
    workspace_config,
)
from mooring.app import accounts
from mooring.github import AuthFailed, GitHubError, TlsFailure, Unreachable, compare_url
from mooring.runtime import workspace_hint


def _read_context_dirs(hub, cfg) -> tuple[str, ...]:
    """The context folders this machine's copilot would read (subscription ∩ offer)."""
    from mooring.app import context_folders as ctxdirs

    return ctxdirs.read_dirs(hub.app_cfg, cfg.workspace())


def _account_label(app_cfg, alias: str) -> str:
    if not alias:
        return ""
    try:
        return app_cfg.account(alias).label
    except KeyError:
        return alias  # dangling binding; api_state also reports account_error


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
        # Identity is per-repo, so each row carries its own account and host rather
        # than the page showing one global host badge.
        "repos": [
            {
                "alias": s.alias,
                "slug": s.slug,
                "branch": s.branch,
                "workspace": str(hub.app_cfg.config_for(s.alias).workspace()),
                "active": s.alias == hub.app_cfg.active_alias,
                "account": s.account,
                "account_label": _account_label(hub.app_cfg, s.account),
                "host": hub.app_cfg.config_for(s.alias).host,
            }
            for s in hub.app_cfg.repos
        ],
        "active_repo": hub.app_cfg.active_alias,
        "accounts": [dict(row, repos=list(row["repos"])) for row in accounts.status(hub.app_cfg)],
        "active_account": hub.app_cfg.active_account,
        # Set when the active repo names an account that is missing or unusable. The
        # repo is BROKEN, not unconfigured, and the UI must say which — otherwise a
        # falsy client_id silently drops the page into local mode and the repo
        # appears to have vanished.
        "account_error": cfg.account_error,
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
    if not auth.token_for(cfg.token_slot, method=cfg.auth_method):
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
        # May be "" on a cold start with no account record; don't retry here.
        body["user"] = cfg.account_login or hub._user_login.get(cfg.account, "")
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
        if cfg.auth_method == config.AUTH_GIT:
            # A borrowed credential has no stored copy to delete. Report it REFUSED to
            # git's helper instead, which is what makes the next read re-authenticate
            # through whatever flow this organisation has already approved — the same
            # move git makes when a fetch gets a 401. The account stays signed in
            # because it still names a valid identity; only the credential was stale.
            auth.reject_borrowed(cfg.host)
            body["error"] = (
                "The credential git holds for this host was refused. Mooring has asked "
                "git to renew it — retry in a moment, or run a `git fetch` in your clone "
                "to re-authenticate."
            )
        else:
            auth.delete_token(host=cfg.host, login=cfg.account_login)
            hub._user_login.pop(cfg.account, None)
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
        for k in ("client_id", "owner", "repo", "branch", "alias", "host", "account")
    }
    if not (fields["owner"] and fields["repo"]):
        return JSONResponse({"error": "owner and repo are required"}, status_code=400)
    account = fields["account"]
    if account and not any(a.alias == account for a in hub.app_cfg.accounts):
        return JSONResponse({"error": f"Unknown account {account!r}."}, status_code=400)
    if not account and hub.app_cfg.accounts:
        account = hub.app_cfg.active_account
    # The client id only has to be asked for on the pre-accounts path — with an
    # account chosen it comes from that account's record.
    if not (account or fields["client_id"] or hub.app_cfg.client_id):
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
            account=account or None,
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


def _login_target(hub, alias: str) -> tuple[str, str, str]:
    """Resolve (alias, host, client_id) for a sign-in the UI asked to start.

    With no alias the request is about the active repo: its account when it has
    one, else the pre-accounts global host/client_id.
    """
    if alias:
        account = hub.app_cfg.account(alias)  # KeyError -> 404 at the caller
        return alias, account.host, account.client_id
    cfg = hub.cfg
    if cfg.account:
        return cfg.account, cfg.host, cfg.client_id
    return "", cfg.host, cfg.client_id


def api_login_start(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    requested = str(request.query_params.get("account", "")).strip()
    try:
        alias, host, client_id = _login_target(hub, requested)
    except KeyError:
        return JSONResponse({"error": f"Unknown account {requested!r}."}, status_code=404)
    try:
        device = auth.start_device_flow(client_id, host=host, account=alias)
    except Exception as exc:  # noqa: BLE001  # shown in the UI
        return JSONResponse({"error": auth.device_flow_hint(host, exc)}, status_code=502)
    with hub._lock:
        hub._device[alias] = device
        hub._poll_interval[alias] = device.interval
        hub._next_poll[alias] = time.monotonic() + device.interval
    return JSONResponse(
        {
            "user_code": device.user_code,
            "verification_uri": device.verification_uri,
            "account": alias,
        }
    )


def api_login_poll(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    alias = str(request.query_params.get("account", "")).strip()
    with hub._lock:
        device = hub._device.get(alias)
        if device is None:
            return JSONResponse({"status": "error", "message": "No login in progress."})
        now = time.monotonic()
        if now < hub._next_poll.get(alias, 0.0):
            return JSONResponse({"status": "pending"})
        interval = hub._poll_interval.get(alias, device.interval)
        # Claim the next slot BEFORE releasing the lock and hitting the network:
        # two concurrent polls that both passed the gate would earn a `slow_down`.
        hub._next_poll[alias] = now + interval
    try:
        # device carries its own client_id, so a repo switch mid-login cannot make
        # this poll present a different account's OAuth app.
        result = auth.poll_once(device.client_id, device, interval=interval)
    except auth.AuthError as exc:
        with hub._lock:
            hub._device.pop(alias, None)
        return JSONResponse({"status": "error", "message": str(exc)})
    if result.token:
        with hub._lock:
            hub._device.pop(alias, None)
            hub._user_login.pop(alias, None)
        if alias:
            # host/client_id come from the device, not live config, for the same
            # reason as above. finish_login parks the token before naming its owner.
            try:
                account = accounts.finish_login(device, result.token)
            except accounts.AccountError as exc:
                return JSONResponse({"status": "error", "message": str(exc), "resumable": True})
            hub.reload()
            telemetry.log_event("login")
            return JSONResponse({"status": "ok", "account": account.alias, "user": account.login})
        # Pre-accounts repo: keep the old host-keyed save.
        auth.save_token(result.token, host=device.host)
        telemetry.log_event("login")
        return JSONResponse({"status": "ok"})
    with hub._lock:
        hub._poll_interval[alias] = result.interval
        hub._next_poll[alias] = time.monotonic() + result.interval
    return JSONResponse({"status": "pending"})


async def api_login_git(request: Request) -> JSONResponse:
    """Sign in by BORROWING the credential git already holds for the host.

    The one sign-in that needs no OAuth app and stores no token — for organisations
    that restrict third-party apps and cap personal access token lifetimes, which
    blocks the device flow and a pasted token respectively. See
    ``app.accounts.sign_in_with_git``.

    Runs off the event loop: it shells out to git, and the probe (though
    non-interactive) is still a subprocess.
    """
    hub = request.app.state.hub
    data = await request.json() if await request.body() else {}
    requested = str(data.get("account", "")).strip()
    try:
        alias, host, _client_id = _login_target(hub, requested)
    except KeyError:
        return JSONResponse({"error": f"Unknown account {requested!r}."}, status_code=404)
    if not alias:
        # A pre-accounts repo has no account record to attach this to; make one named
        # after the host, the way `mooring login` does for the same case.
        alias = accounts.fresh_alias(hub.app_cfg, host)
    try:
        account = await run_in_threadpool(accounts.sign_in_with_git, alias, host)
    except accounts.AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Bind the active repo if it was unbound, so the repo stops depending on the
    # shared host-keyed slot (mirrors cmd_login).
    cfg = hub.cfg
    if not cfg.account and cfg.owner and cfg.repo:
        await run_in_threadpool(
            _add_repo_bound,
            hub.app_cfg.active_alias or cfg.repo,
            cfg.owner,
            cfg.repo,
            cfg.branch,
            account.alias,
        )
    hub.reload()
    kind = await run_in_threadpool(lambda: credhelper.probe(host).kind)
    telemetry.log_event("login", method="git", kind=kind)
    return JSONResponse(
        {"ok": True, "account": account.alias, "user": account.login, "kind": kind}
    )


async def api_login_git_probe(request: Request) -> JSONResponse:
    """Whether there is a git credential to borrow for a host, and of what TYPE.

    Value-free: reports the token's type PREFIX (``gho_``/``ghp_``/…), never the
    token. Lets the sign-in dialog offer "use my git credential" only when it would
    actually work, and warn when the credential is a personal access token that an
    enterprise lifetime cap would expire."""
    hub = request.app.state.hub
    requested = str(request.query_params.get("account", "")).strip()
    try:
        _alias, host, _client_id = _login_target(hub, requested)
    except KeyError:
        return JSONResponse({"error": f"Unknown account {requested!r}."}, status_code=404)
    probe = await run_in_threadpool(credhelper.probe, host)
    return JSONResponse(
        {
            "host": probe.host,
            "git_present": probe.git_present,
            "found": probe.found,
            "kind": probe.kind,
            "refreshable": probe.refreshable,
            "expires_in": probe.expires_in,
            "summary": probe.summary,
        }
    )


def api_logout(request: Request) -> JSONResponse:
    """Sign the active repo's account out, by whichever route its method allows.

    A borrowed ("git") account has no stored token to delete, and mooring must not
    touch git's own credential — that belongs to the user's git setup, not to us. So
    signing out is recorded on mooring's side by clearing the account's login, which
    ``token_slot`` already reads as "cannot produce a credential". Without this, a
    borrowed account would silently re-borrow on the next poll and the Sign out button
    would do nothing at all.
    """
    hub = request.app.state.hub
    cfg = hub.cfg
    if cfg.auth_method == config.AUTH_GIT:
        auth.forget_borrowed(cfg.host)
        if cfg.account:
            try:
                config_store.clear_account_login(cfg.account)
            except KeyError:
                pass
    else:
        auth.delete_token(host=cfg.host, login=cfg.account_login)
    hub._user_login.pop(cfg.account, None)
    hub.reload()
    telemetry.log_event("logout")
    return JSONResponse({"ok": True})


# -- accounts: the identities the hub can sign in and act as --------------------


def api_accounts(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    return JSONResponse(
        {
            "accounts": [dict(r, repos=list(r["repos"])) for r in accounts.status(hub.app_cfg)],
            "active_account": hub.app_cfg.active_account,
        }
    )


async def api_account_add(request: Request) -> JSONResponse:
    """Register an account (host + client id). Signing in is a separate step —
    the caller then starts a device flow against this alias."""
    hub = request.app.state.hub
    data = await request.json()
    alias = str(data.get("alias", "")).strip()
    host = str(data.get("host", "")).strip() or githost.DEFAULT_HOST
    client_id = str(data.get("client_id", "")).strip()
    if not alias:
        return JSONResponse({"error": "alias is required"}, status_code=400)
    if not client_id:
        existing = next((a for a in hub.app_cfg.accounts if a.alias == alias), None)
        client_id = existing.client_id if existing else hub.app_cfg.client_id
    if not client_id:
        return JSONResponse(
            {
                "error": "An OAuth client id is required. Register an OAuth app on that "
                "host with Device Flow enabled, then paste its client id."
            },
            status_code=400,
        )
    try:
        await run_in_threadpool(config_store.add_account, alias, host, "", client_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    hub.reload()
    telemetry.log_event("account_add", alias=alias)
    return JSONResponse({"ok": True, "alias": alias})


async def api_account_remove(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    alias = str(data.get("alias", "")).strip()
    try:
        orphaned = await run_in_threadpool(accounts.forget, alias)
    except accounts.AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    hub.reload()
    telemetry.log_event("account_remove", alias=alias)
    return JSONResponse({"ok": True, "orphaned": list(orphaned)})


async def api_account_use(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    alias = str(data.get("alias", "")).strip()
    try:
        await run_in_threadpool(config_store.set_active_account, alias)
    except KeyError:
        return JSONResponse({"error": f"Unknown account {alias!r}."}, status_code=404)
    hub.reload()
    return JSONResponse({"ok": True, "active_account": alias})


def _owner_rows(client, login: str) -> dict:
    owners, truncated = client.list_owners()
    if login and login not in owners:
        owners = [login, *owners]
    return {"owners": owners, "truncated": truncated}


async def api_account_owners(request: Request) -> JSONResponse:
    """The owners (you + your orgs) a repo could be created under or picked from."""
    hub = request.app.state.hub
    alias = request.path_params["alias"]
    try:
        client = accounts.client_for_account(hub.app_cfg, alias)
        login = hub.app_cfg.account(alias).login
        # Blocking `requests` work inside an async handler would stall the event
        # loop and every open SSE stream, so it goes to the threadpool.
        return JSONResponse(await run_in_threadpool(_owner_rows, client, login))
    except (accounts.AccountError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (AuthFailed, GitHubError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


def _repo_rows(client, owner: str) -> dict:
    repos, truncated = client.list_repos(owner=owner)
    return {
        "repos": [
            {
                "name": str(r.get("name", "")),
                "full_name": str(r.get("full_name", "")),
                "owner": str(r.get("owner", {}).get("login", "")),
                "private": bool(r.get("private")),
                "default_branch": str(r.get("default_branch") or "main"),
            }
            for r in repos
        ],
        "truncated": truncated,
    }


async def api_account_repos(request: Request) -> JSONResponse:
    """Repositories this account can reach, for the picker's second dropdown."""
    hub = request.app.state.hub
    alias = request.path_params["alias"]
    owner = str(request.query_params.get("owner", "")).strip()
    try:
        client = accounts.client_for_account(hub.app_cfg, alias)
        return JSONResponse(await run_in_threadpool(_repo_rows, client, owner))
    except (accounts.AccountError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (AuthFailed, GitHubError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


def _create_and_seed(client, hub, alias, owner, repo, private, seed) -> dict:
    login = hub.app_cfg.account(alias).login
    # An owner equal to the signed-in user is a personal repo; anything else is an
    # organisation, which is a different creation endpoint.
    created = client.create_repo(repo, owner="" if owner == login else owner, private=private)
    branch = str(created.get("default_branch") or "main")
    if seed:
        seeder = accounts.repo_client_for_account(hub.app_cfg, alias, owner, repo)
        for folder in ("notebooks", "data"):
            # GitHub cannot hold an empty folder, so each one needs a file.
            seeder.put_file(f"{folder}/.gitkeep", b"", f"mooring: add {folder}/", branch)
    return {"branch": branch, "html_url": str(created.get("html_url", ""))}


async def api_account_create_repo(request: Request) -> JSONResponse:
    """Create a repo under this account and register it, bound to the account."""
    hub = request.app.state.hub
    alias = request.path_params["alias"]
    data = await request.json()
    owner = str(data.get("owner", "")).strip()
    repo = str(data.get("repo", "")).strip()
    if not owner or not repo:
        return JSONResponse({"error": "owner and repo are required"}, status_code=400)
    private = bool(data.get("private", True))
    seed = bool(data.get("seed", True))
    try:
        client = accounts.client_for_account(hub.app_cfg, alias)
        result = await run_in_threadpool(
            _create_and_seed, client, hub, alias, owner, repo, private, seed
        )
    except (accounts.AccountError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (AuthFailed, GitHubError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    try:
        await run_in_threadpool(
            _add_repo_bound, str(data.get("alias", "")).strip() or repo, owner, repo,
            result["branch"], alias,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    hub.reload()
    telemetry.log_event("repo_create", alias=str(data.get("alias", "")).strip() or repo)
    return JSONResponse({"ok": True, "html_url": result["html_url"]})


def _add_repo_bound(alias: str, owner: str, repo: str, branch: str, account: str) -> None:
    config_store.add_repo(alias, owner, repo, branch=branch, make_active=True, account=account)
