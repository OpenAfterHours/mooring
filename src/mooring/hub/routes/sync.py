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

from mooring import auth, celldiff, manifest, policy, pushguard, sync, telemetry, whatsnew
from mooring import workspace_config
from mooring.app import conflict_merge
from mooring.app import notebooks as nb_ops
from mooring.app import sweep_run as nb_sweep
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


def _findings_payload(collected: dict) -> list[dict]:
    return [
        {
            "path": path,
            "token": info["token"],
            "findings": [{"line": f.line, "kind": f.kind} for f in info["findings"]],
        }
        for path, info in sorted(collected.items())
    ]


def _guarded_sync_op(hub, name: str, data: dict, run, *, direct: bool = True) -> JSONResponse:
    """Run push/propose behind the warn-and-confirm flow for all THREE guards on
    the sync seam: the content scanners, the dependency-change gate, and the team
    policy's propose-only gate.

    Every outgoing candidate is scanned (mooring.pushguard) via sync's injected
    ``guard_fn``; flagged files are WITHHELD (clean ones still go), and the
    response upgrades to a 409 carrying value-free findings + per-file confirm
    tokens. "Push anyway" re-POSTs with those tokens: each token binds the exact
    findings to the exact bytes, so a changed file or a new finding is never
    covered by an old confirm. In block mode ([guard] push = "block", or the
    policy's own floor) tokens are refused — the pragma/fix is the only way.

    The three guards' OVERRIDE RULES differ deliberately, and the response has to
    carry all three faithfully:

    * ``guard_findings`` — content. Acknowledgeable in warn mode only.
    * ``sweep_findings`` — the dependency-change gate, asking the last verify
      sweep whether an outgoing ``uv.lock`` still runs the team's notebooks
      (mooring.sweep). ALWAYS acknowledgeable: it warns about broken notebooks,
      not about bytes that must not leave, so the content policy's block mode
      must not swallow it.
    * ``policy_blocked`` — the propose-only gate, with NO token at all. A
      propose-only path has no override; the road is Propose. Composed only when
      ``direct`` (a write to the SHARED branch — push, and resolve's PUSH_COPY),
      and at the same sync seam as the scanners, so no second code path can push
      those bytes without passing it.

    ``needs_confirm`` therefore means "something here CAN be acknowledged": content
    in warn mode, or deps in any mode — and never because of a policy block.
    """
    workspace = hub.cfg.workspace()
    pol = policy.load(workspace)
    mode = pol.guard_mode(workspace_config.guard_mode(workspace))
    confirmed = frozenset(str(t) for t in (data.get("confirm_tokens") or []))
    # Block mode zeroes the CONTENT tokens only. The deps gate keeps its own set:
    # a policy about sensitive content must not become a wall around lock files.
    content_ok = frozenset() if mode == "block" else confirmed
    content_fn, collected = pushguard.make_guard(content_ok)
    lock_fn, lock_collected = pushguard.make_lock_guard(
        workspace, confirmed, notebooks_fn=lambda: nb_sweep.plan(hub.cfg)
    )
    gate_fn, blocked = policy.make_propose_gate(pol) if direct else (None, {})
    combined = policy.compose_guards(content_fn, lock_fn, gate_fn)
    body, status = hub._sync_op_body(name, lambda: run(combined))
    if status == 200 and (collected or lock_collected or blocked):
        telemetry.log_event(
            "push_guard",
            findings=sum(
                len(info["findings"])
                for info in (*collected.values(), *lock_collected.values())
            ),
            policy_blocked=len(blocked),
        )
        content_ack = bool(collected) and mode != "block"
        body["needs_confirm"] = content_ack or bool(lock_collected)
        # The mode that APPLIES to this response: with no content finding, block mode
        # is not what is being reported, and saying "block" would hide the deps
        # gate's override behind a policy that has nothing to say about it.
        body["guard_mode"] = mode if collected else "warn"
        body["guard_findings"] = _findings_payload(collected)
        body["sweep_findings"] = _findings_payload(lock_collected)
        body["policy_blocked"] = [
            {"path": path, "reason": reason} for path, reason in sorted(blocked.items())
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
    # direct=False: propose is exactly the road a propose-only policy points at,
    # so that gate must never fire here (it would strand those files entirely).
    return _guarded_sync_op(
        hub, "propose", data,
        lambda guard_fn: sync.propose(
            hub.client(), hub.cfg, paths=paths_arg, message=_note(data), guard_fn=guard_fn
        ),
        direct=False,
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


def _merge_target(hub, data: dict) -> str:
    """The conflicted file a cell-merge request names, validated against the
    workspace (the /api/diff posture). Raises ``ValueError`` for a bad path."""
    rel, _ = _resolve_within(hub.cfg.workspace(), str(data.get("path", "")))
    return rel


async def api_resolve_cells(request: Request) -> JSONResponse:
    """Plan a per-cell resolution of one conflicted notebook (see
    :mod:`mooring.app.conflict_merge`). Strictly read-only — it fetches the base and
    remote blobs and reads the local file, and writes nothing.

    A conflict that cannot be merged per cell answers 409 with ``unavailable``, which
    the hub renders as a notice beside the three unchanged whole-file resolutions —
    this endpoint only ever ADDS an option."""
    hub = request.app.state.hub
    data = await request.json()
    try:
        rel = _merge_target(hub, data)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        # Two blob fetches plus three marimo parses: network + CPU, so off the
        # event loop exactly like /api/diff.
        merge_plan = await asyncio.to_thread(conflict_merge.plan, hub.client(), hub.cfg, rel)
    except conflict_merge.MergeUnavailable as exc:
        return JSONResponse({"error": str(exc), "unavailable": True}, status_code=409)
    except (GitHubError, OSError) as exc:
        # Type only: a NotFound message embeds the contents/<path> URL (the history
        # endpoints' telemetry posture — paths never reach central telemetry).
        telemetry.log_error(exc=type(exc)("cell merge plan failed"), op="resolve_cells")
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(conflict_merge.plan_payload(merge_plan))


async def api_resolve_cells_apply(request: Request) -> JSONResponse:
    """Write the merged notebook for one conflicted file.

    LOCAL ONLY: it never pushes. Afterwards the file is a normal modified file the
    analyst publishes themselves, and its pre-merge bytes are in the local trash, so
    the response's ``trashed`` drives the hub's existing Undo toast. The request
    carries only per-cell decisions — the server recomputes the plan and refuses
    (409) if any of the three sides moved since it was rendered, or 400s if the
    request did not say which versions it was rendered against."""
    hub = request.app.state.hub
    data = await request.json()
    try:
        rel = _merge_target(hub, data)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    choices = {str(k): str(v) for k, v in (data.get("choices") or {}).items()}
    # A missing sha is passed through as "" rather than defaulted or dropped: the
    # engine treats a blank as a malformed request (400), so ONE place decides
    # whether a merge may proceed and neither adapter can waive the check.
    expect = {key: str(data.get(key) or "") for key in ("base_sha", "local_sha", "remote_sha")}
    try:
        outcome = await asyncio.to_thread(
            conflict_merge.apply, hub.client(), hub.cfg, rel, choices, expect=expect
        )
    except conflict_merge.MergeStale as exc:
        return JSONResponse({"error": str(exc), "stale": True}, status_code=409)
    except conflict_merge.MergeUnavailable as exc:
        return JSONResponse({"error": str(exc), "unavailable": True}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (GitHubError, OSError) as exc:
        telemetry.log_error(exc=type(exc)("cell merge failed"), op="resolve_cells_apply")
        return JSONResponse({"error": str(exc)}, status_code=502)
    # Counts only — the ledger and central telemetry never carry cell source.
    telemetry.log_event(
        "resolve_cells",
        auto=outcome.auto_merged,
        chosen=outcome.chosen_local + outcome.chosen_remote,
    )
    trashed = [{"path": p, "token": t} for p, t in outcome.trashed]
    hub._activity("resolve-cells", path=rel, summary=outcome.summary(), trashed=trashed)
    return JSONResponse(
        {
            "path": rel,
            "lines": list(outcome.lines),
            "summary": outcome.summary(),
            "auto_merged": outcome.auto_merged,
            "chosen_local": outcome.chosen_local,
            "chosen_remote": outcome.chosen_remote,
            "trashed": trashed,
        }
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
