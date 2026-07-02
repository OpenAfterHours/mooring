"""Sync endpoints: pull, push, propose, resolve, recall, discover/adopt, and
the what's-new pull digest."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import tomllib
from collections import Counter

from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import auth, celldiff, manifest, pushguard, sync, telemetry, whatsnew
from mooring import workspace_config
from mooring.app import notebooks as nb_ops
from mooring.github import GitHubError, Unreachable
from mooring.hub.routes.files import _resolve_within


async def api_pull(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json() if await request.body() else {}
    strategy = sync.ConflictStrategy(data.get("strategy", "skip"))

    def _run() -> tuple[dict, int]:
        # The digest of what this pull is about to land, computed BEFORE the pull
        # runs — pull rewrites Manifest.head_commit, the digest's horizon. Strictly
        # best-effort: a digest failure must never fail (or even color) the pull the
        # user actually asked for; they simply get no "what's new" section.
        digest = None
        with contextlib.suppress(Exception):
            report = sync.status(hub.client(), hub.cfg)
            digest = whatsnew.pending_digest(hub.client(), hub.cfg, report)
        body, status = hub._sync_op_body(
            "pull", lambda: sync.pull(hub.client(), hub.cfg, strategy=strategy)
        )
        if status == 200 and digest is not None and digest.entries:
            body["whatsnew"] = dataclasses.asdict(digest)
        return body, status

    # The pre-pull digest is a second full status walk (plus, with a blank
    # anchor, up to FALLBACK_MAX_LOOKUPS commits-API calls) and the pull itself
    # is a network drain — keep the whole thing off the event loop so /api/state
    # polls and open SSE streams stay alive during a slow (or offline) pull.
    body, status = await asyncio.to_thread(_run)
    return JSONResponse(body, status_code=status)


def api_whatsnew(request: Request) -> JSONResponse:
    """The pull digest on demand: every synced file changed on the team branch
    since this analyst's last sync (the manifest horizon), with best-effort
    who/when/why (see mooring.whatsnew). Read-only, and kept off the /api/state
    hot path — the hub calls it from the toolbar button (the /api/discover
    posture), never on every refresh."""
    hub = request.app.state.hub
    cfg = hub.cfg
    if not cfg.is_configured or not auth.get_token(host=cfg.host):
        return JSONResponse({"entries": []})
    try:
        report = sync.status(hub.client(), cfg)
        digest = whatsnew.pending_digest(hub.client(), cfg, report)
    except (GitHubError, OSError) as exc:
        telemetry.log_error(exc=exc, op="whatsnew")
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(dataclasses.asdict(digest))


def _detail_summary(base: bytes | None, remote: bytes | None, rel: str) -> dict:
    """Cell counts for a marimo notebook ("2 cells changed, 1 added"), line
    counts for everything else. The cell differ's line/binary results are kept
    (not recomputed): its "binary" answer includes the 4 MB size cap, and
    re-running the same blobs through whatsnew.summarize_diff would silently
    UN-cap exactly the work celldiff refused. The response stays a compact
    summary (counts/sizes), never a full diff body — /api/diff is the full view."""
    if rel.endswith(".py"):
        result = celldiff.diff(base, remote, rel)
        if result.kind == "cells":
            counts = Counter(c.status for c in result.cells)
            return {
                "kind": "cells",
                "changed": counts.get("changed", 0),
                "added": counts.get("added", 0),
                "removed": counts.get("removed", 0),
                "unmatched": counts.get("unmatched", 0),
                "note": result.note,
            }
        if result.kind == "binary":
            return {
                "kind": "binary",
                "added": 0,
                "removed": 0,
                "base_size": len(base or b""),
                "head_size": len(remote or b""),
            }
        added = removed = 0
        for line in result.line_diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return {"kind": "lines", "added": added, "removed": removed}
    return whatsnew.summarize_diff(base, remote, rel)


async def api_whatsnew_detail(request: Request) -> JSONResponse:
    """A compact "what actually changed" summary for ONE digest entry: the
    last-synced base blob diffed against the digest's remote blob. Read-only.
    ``remote_sha`` and ``base_sha`` come from the digest entry itself (blank =
    deleted remotely / new remote), so re-expanding the same digest is exact
    even if the branch — or the manifest, after the pull that rendered the
    digest — has moved since; results are cached on the Hub keyed (path,
    base_sha, remote_sha) — blob content is immutable per sha."""
    hub = request.app.state.hub
    data = await request.json()
    workspace = hub.cfg.workspace()
    try:
        rel, _ = _resolve_within(workspace, str(data.get("path", "")))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    remote_sha = str(data.get("remote_sha") or "")
    # The entry's own PRE-pull base rides with the request when the client has
    # it. It must win over a manifest read: the pull handler renders the digest
    # AFTER sync.pull rewrote the manifest entry to the remote sha, so deriving
    # the base here would diff the pulled blob against itself — "no cell
    # changes" for a file a teammate just rewrote. Absent the key (an older
    # client), fall back to the manifest, which is exact for the pre-pull panel.
    has_base = "base_sha" in data
    base_override = str(data.get("base_sha") or "")

    def _compute() -> dict:
        base_sha = base_override if has_base else (manifest.load(workspace).files.get(rel) or "")
        key = (rel, base_sha, remote_sha)
        cached = hub._whatsnew_detail.get(key)
        if cached is not None:
            return cached
        base = hub.client().get_blob(base_sha) if base_sha else None
        remote = hub.client().get_blob(remote_sha) if remote_sha else None
        summary = _detail_summary(base, remote, rel)
        hub._whatsnew_detail[key] = summary
        return summary

    try:
        # The blob fetches are synchronous network calls and the cell parse is
        # CPU-bound — keep both off the event loop (the /api/diff idiom).
        body = await asyncio.to_thread(_compute)
    except ValueError:
        return JSONResponse(
            {"error": f"Nothing to summarize for {rel}: no synced base and no remote blob."},
            status_code=404,
        )
    except (GitHubError, OSError) as exc:
        # Type only: a NotFound message embeds the request URL (the history
        # endpoints' telemetry posture — paths never reach central telemetry).
        telemetry.log_error(exc=type(exc)("whatsnew detail read failed"), op="whatsnew_detail")
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"path": rel, **body})


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
    except Unreachable:
        # Offline the staleness guard stays SILENT: it cannot check the head,
        # and the offline banner already owns the "your view is stale" story.
        # (The client fails open on errors anyway — this just avoids a 502 +
        # telemetry error on every Open while the network is down.)
        return JSONResponse({"fresh": True, "head": ""})
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
