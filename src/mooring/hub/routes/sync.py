"""Sync endpoints: pull, push, propose, resolve, recall, and discover/adopt."""

from __future__ import annotations

import tomllib

from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import auth, pushguard, sync, telemetry, workspace_config
from mooring.app import notebooks as nb_ops
from mooring.github import GitHubError


async def api_pull(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json() if await request.body() else {}
    strategy = sync.ConflictStrategy(data.get("strategy", "skip"))
    return hub._sync_op("pull", lambda: sync.pull(hub.client(), hub.cfg, strategy=strategy))


def _guarded_sync_op(hub, name: str, data: dict, run) -> JSONResponse:
    """Run push/propose behind the push guard's warn-and-confirm flow.

    Every outgoing candidate is scanned (mooring.pushguard) via sync's injected
    ``guard_fn``; flagged files are WITHHELD (clean ones still go), and the
    response upgrades to a 409 carrying value-free findings + per-file confirm
    tokens. "Push anyway" re-POSTs with those tokens: each token binds the exact
    findings to the exact bytes, so a changed file or a new finding is never
    covered by an old confirm. In block mode ([guard] push = "block" in the
    synced mooring.toml) tokens are refused — the pragma/fix is the only way.
    """
    mode = workspace_config.guard_mode(hub.cfg.workspace())
    confirmed = frozenset(str(t) for t in (data.get("confirm_tokens") or []))
    if mode == "block":
        confirmed = frozenset()
    guard_fn, collected = pushguard.make_guard(confirmed)
    body, status = hub._sync_op_body(name, lambda: run(guard_fn))
    if status == 200 and collected:
        telemetry.log_event("push_guard", findings=sum(
            len(info["findings"]) for info in collected.values()
        ))
        body["needs_confirm"] = mode != "block"
        body["guard_mode"] = mode
        body["guard_findings"] = [
            {
                "path": path,
                "token": info["token"],
                "findings": [{"line": f.line, "kind": f.kind} for f in info["findings"]],
            }
            for path, info in sorted(collected.items())
        ]
        status = 409
    return JSONResponse(body, status_code=status)


def _note(data: dict) -> str | None:
    """The optional "What changed?" note from the review panel — the commit
    message for this push/propose (sync already threads ``message`` through to
    the Contents API; absent means the machine default "Update {path} via
    mooring"). Read from the request body so the push guard's confirm re-POST,
    which re-sends the whole body, carries the note through a 409 round trip."""
    return str(data.get("message") or "").strip() or None


async def api_push(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json() if await request.body() else {}
    paths_arg = data.get("paths") or None
    return _guarded_sync_op(
        hub, "push", data,
        lambda guard_fn: sync.push(
            hub.client(), hub.cfg, paths=paths_arg, message=_note(data), guard_fn=guard_fn
        ),
    )


async def api_propose(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json() if await request.body() else {}
    paths_arg = data.get("paths") or None
    return _guarded_sync_op(
        hub, "propose", data,
        lambda guard_fn: sync.propose(
            hub.client(), hub.cfg, paths=paths_arg, message=_note(data), guard_fn=guard_fn
        ),
    )


async def api_recall(request: Request) -> JSONResponse:
    """Undo the LAST push on the team branch (see sync.recall). The response is
    honest about limits: history retains the commit; conflicts are loud."""
    hub = request.app.state.hub
    return hub._sync_op("recall", lambda: sync.recall(hub.client(), hub.cfg))


async def api_resolve(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    strategy = sync.ConflictStrategy(data["strategy"])
    username = hub.username() if strategy is sync.ConflictStrategy.PUSH_COPY else ""
    # PUSH_COPY uploads local bytes to the shared branch — the one resolve
    # strategy the push guard must cover, with the same warn-and-confirm flow.
    return _guarded_sync_op(
        hub, "resolve", data,
        lambda guard_fn: sync.resolve(
            hub.client(), hub.cfg, data["path"], strategy, username, guard_fn=guard_fn
        ),
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
