"""Sync endpoints: pull, push, propose, resolve, and the discover/adopt pair."""

from __future__ import annotations

import tomllib

from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import auth, sync, telemetry, workspace_config
from mooring.app import notebooks as nb_ops
from mooring.github import GitHubError


async def api_pull(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json() if await request.body() else {}
    strategy = sync.ConflictStrategy(data.get("strategy", "skip"))
    return hub._sync_op("pull", lambda: sync.pull(hub.client(), hub.cfg, strategy=strategy))


async def api_push(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json() if await request.body() else {}
    paths_arg = data.get("paths") or None
    return hub._sync_op("push", lambda: sync.push(hub.client(), hub.cfg, paths=paths_arg))


async def api_propose(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json() if await request.body() else {}
    paths_arg = data.get("paths") or None
    return hub._sync_op("propose", lambda: sync.propose(hub.client(), hub.cfg, paths=paths_arg))


async def api_resolve(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    strategy = sync.ConflictStrategy(data["strategy"])
    username = hub.username() if strategy is sync.ConflictStrategy.PUSH_COPY else ""
    return hub._sync_op(
        "resolve",
        lambda: sync.resolve(hub.client(), hub.cfg, data["path"], strategy, username),
    )


def api_freshness(request: Request) -> JSONResponse:
    """Whether the branch head still matches the head the last /api/state render
    was computed from — the staleness dialog's near-open check. One fast ref
    lookup, no tree walk. Advisory by design: the client timeboxes the call and
    opens anyway on error/timeout, so this must never gate anything server-side."""
    hub = request.app.state.hub
    cfg = hub.cfg
    if not cfg.is_configured or not auth.get_token(host=cfg.host):
        return JSONResponse({"fresh": True, "head": ""})
    last = hub._state_heads.get(str(cfg.workspace()))
    try:
        head = hub.client().get_branch_head(cfg.branch)
    except (GitHubError, OSError) as exc:
        telemetry.log_error(exc=exc, op="freshness")
        return JSONResponse({"error": str(exc)}, status_code=502)
    # No state rendered yet this session → nothing cached to be stale against.
    return JSONResponse({"fresh": last is None or head == last, "head": head})


def api_discover(request: Request) -> JSONResponse:
    """Top-level repo folders that hold files outside the synced folders — the
    adopt candidates. Read-only; called on demand by the hub (not on every
    /api/state) so the extra full-tree read stays off the refresh hot path."""
    hub = request.app.state.hub
    cfg = hub.cfg
    if not cfg.is_configured or not auth.get_token(host=cfg.host):
        return JSONResponse({"candidates": []})
    try:
        candidates = sync.discover_unsynced_folders(hub.client(), cfg)
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


async def api_adopt(request: Request) -> JSONResponse:
    """Register the chosen folders in the synced ``mooring.toml`` and pull them.

    The request's folders are validated against what discovery actually found, so
    adopt never registers a non-existent folder, then re-derives the scope and runs
    a normal pull through ``Hub._sync_op`` (so the response shape matches push/pull)."""
    hub = request.app.state.hub
    data = await request.json() if await request.body() else {}
    requested = [str(f) for f in (data.get("folders") or [])]
    if not requested:
        return JSONResponse({"error": "No folders given."}, status_code=400)
    cfg = hub.cfg
    try:
        candidates = sync.discover_unsynced_folders(hub.client(), cfg)
    except (GitHubError, OSError) as exc:
        telemetry.log_error(exc=exc, op="adopt")
        return JSONResponse({"error": str(exc)}, status_code=502)
    # Silently drop unknowns and adopt the valid subset (the CLI, by contrast,
    # refuses the whole command when any requested folder isn't adoptable).
    chosen, _unknown = nb_ops.resolve_adoptable(candidates, requested)
    if not chosen:
        return JSONResponse({"error": "None of those folders are adoptable."}, status_code=400)
    try:
        return hub._sync_op("adopt", lambda: nb_ops.adopt_folders(hub.client(), cfg, chosen))
    except tomllib.TOMLDecodeError as exc:
        return JSONResponse(
            {"error": f"{workspace_config.WORKSPACE_CONFIG_NAME} is not valid TOML: {exc}"},
            status_code=400,
        )
