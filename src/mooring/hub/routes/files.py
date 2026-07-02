"""File endpoints: new, open, reveal, delete, the rollback/undo pair, and the
local safety net (trash listing/restore + the activity ledger)."""

from __future__ import annotations

import asyncio

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import notebook_template, reveal, sync, telemetry, trash
from mooring.github import GitHubError


async def api_new(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    cfg = hub.cfg
    try:
        rel_path = notebook_template.create_from_input(
            cfg.workspace(), data.get("name", ""), folders=cfg.folders, exclude=cfg.exclude
        )
    except (ValueError, FileExistsError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    telemetry.log_event("new")
    return hub._open(rel_path)


async def api_open(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    # _open may spawn the marimo subprocess and block on its readiness poll;
    # run it off the event loop so the first open doesn't freeze the whole hub.
    return await run_in_threadpool(hub._open, data.get("path", ""))


async def api_reveal(request: Request) -> JSONResponse:
    """Reveal a file in the OS file manager so the user can open a non-marimo .py
    (a plain helper module) in their own editor. Deliberately SEPARATE from
    /api/open — that stays the marimo-notebook path and still refuses modules
    (opening one in marimo would rewrite it into notebook form). Revealing the
    folder also sidesteps the Windows trap where the default verb for a .py runs
    the script. Reuses _ws_file's containment + dot-part guards, so .mooring/ and
    any workspace escape are unreachable."""
    hub = request.app.state.hub
    data = await request.json()
    rel_path = str(data.get("path", ""))
    try:
        target = hub._ws_file(hub.cfg.workspace(), rel_path)
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


async def api_delete(request: Request) -> JSONResponse:
    from mooring import deletion

    hub = request.app.state.hub
    data = await request.json()
    rel_path = str(data.get("path", ""))
    cfg = hub.cfg
    trashed: list[dict] = []
    try:
        removed = deletion.delete(
            cfg.workspace(),
            rel_path,
            cfg.exclude,
            cfg.folders,
            trash_cap_mb=cfg.trash_max_file_mb,
            on_trash=lambda rel, token: trashed.append({"path": rel, "token": token}),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": f"No such notebook: {rel_path}"}, status_code=404)
    telemetry.log_event("delete", count=len(removed))
    hub._activity("delete", path=rel_path, paths=removed, trashed=trashed)
    name = rel_path.rsplit("/", 1)[-1]
    return JSONResponse(
        {
            "lines": [f"deleted {r}" for r in removed],
            "summary": f"Deleted {name}. If it was shared, push or propose to "
            "remove it for the team.",
            **({"trashed": trashed} if trashed else {}),
        }
    )


async def api_rollback(request: Request) -> JSONResponse:
    """Restore one notebook to its last-synced version (the manifest base),
    discarding local edits. The pre-revert bytes of a ``.py`` are snapshotted onto
    the local undo stack first and the snapshot token returned (``undo_token``), so
    :func:`api_undo` can put them back — and refuse if a later write has since
    landed on top. Held under the shared apply guard's lock so the snapshot+write
    can't race an in-flight AI Apply on the same notebook (the same guard
    Apply/Undo take — see app/apply.py)."""
    from mooring import notebook_undo

    hub = request.app.state.hub
    data = await request.json()
    rel_path = str(data.get("path", ""))
    include_conflict = bool(data.get("conflicts"))
    workspace = hub.cfg.workspace()
    captured: dict[str, str] = {}

    def snapshot_fn(rel: str, content: bytes) -> None:
        if rel.endswith(".py"):
            captured["token"] = notebook_undo.snapshot(workspace, rel, content)

    try:
        with hub.apply.lock:
            result = sync.revert(
                hub.client(),
                hub.cfg,
                rel_path,
                include_conflict=include_conflict,
                snapshot_fn=snapshot_fn,
            )
    except (GitHubError, OSError) as exc:
        telemetry.log_error(exc=exc, op="rollback")
        return JSONResponse({"error": str(exc)}, status_code=502)
    telemetry.log_event("rollback", reverted=result.reverted, lines=len(result.lines))
    hub._activity(
        "rollback",
        path=rel_path,
        summary=result.summary(),
        trashed=[{"path": p, "token": t} for p, t in result.trashed],
    )
    body = {"lines": result.lines, "summary": result.summary()}
    if "token" in captured:
        body["undo_token"] = captured["token"]
    if result.trashed:
        # A non-.py revert banks its pre-image in the trash (no notebook-undo
        # stack for data files) — surface the token so the toast can offer Undo.
        body["trashed"] = [{"path": p, "token": t} for p, t in result.trashed]
    return JSONResponse(body)


async def api_undo(request: Request) -> JSONResponse:
    """Restore a notebook's most recent local snapshot — the pre-revert (or
    pre-AI-edit) bytes. AI-independent: unlike the chat rollback this is not
    bound to a chat session or gated on the AI being enabled, so a Revert done
    from the file list is itself undoable. The snapshot stack is shared LIFO, so a
    ``token`` (from /api/rollback) must still be the newest entry — otherwise a
    later write (e.g. an AI Apply) is on top and we refuse (409) rather than
    restore the wrong layer."""
    from mooring.app.apply import UNDO_SUPERSEDED

    hub = request.app.state.hub
    data = await request.json()
    rel_path = str(data.get("path", ""))
    token = str(data.get("token", "")) or None
    workspace = hub.cfg.workspace()
    try:
        nb_path = hub._ws_file(workspace, rel_path, suffix=".py")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": f"No such notebook: {rel_path}"}, status_code=404)
    try:
        outcome = await asyncio.to_thread(
            hub.apply.restore_undo, nb_path, workspace, rel_path, expect_token=token
        )
    except OSError as exc:  # momentarily locked — the snapshot is kept to retry
        return JSONResponse({"error": f"Could not restore the notebook: {exc}"}, status_code=502)
    if outcome is UNDO_SUPERSEDED:
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
    hub._activity("undo", path=rel_path)
    return JSONResponse({"ok": True, "can_undo": outcome > 0, "undo_depth": outcome})


def _resolve_within(workspace, rel_path: str):
    """Resolve a workspace-relative path, rejecting anything that escapes it.
    Unlike _ws_file this does NOT require the file to exist locally — history
    and restore legitimately target files that are deleted or never local."""
    rel = str(rel_path).replace("\\", "/").strip("/")
    if not rel:
        raise ValueError("No path given.")
    target = (workspace / rel).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ValueError("Path escapes the workspace.") from exc
    return rel, target


async def api_history(request: Request) -> JSONResponse:
    """One page of a file's version history on the team branch (see sync.history)."""
    hub = request.app.state.hub
    path = request.query_params.get("path", "")
    try:
        rel, _ = _resolve_within(hub.cfg.workspace(), path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    try:
        versions = sync.history(hub.client(), hub.cfg, rel, page=page)
    except (GitHubError, OSError) as exc:
        # Central telemetry never carries file paths — and a NotFound message
        # embeds the request URL including contents/<path>, so log the TYPE only.
        telemetry.log_error(exc=type(exc)("history read failed"), op="history")
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"path": rel, "page": page, "versions": versions})


async def api_history_file(request: Request) -> JSONResponse:
    """A file's source at one commit, plus a unified diff against the current
    local copy. STRICTLY read-only: never opens an editor, never writes the
    workspace — old code may not run under current dependencies, so it is
    only ever displayed."""
    import difflib

    hub = request.app.state.hub
    path = request.query_params.get("path", "")
    at = request.query_params.get("at", "")
    workspace = hub.cfg.workspace()
    try:
        rel, target = _resolve_within(workspace, path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not at:
        return JSONResponse({"error": "No version given."}, status_code=400)
    try:
        _, data = hub.client().get_file_at(rel, at)
    except (GitHubError, OSError) as exc:
        # Type only: a NotFound message embeds the contents/<path> URL.
        telemetry.log_error(exc=type(exc)("history file read failed"), op="history_file")
        return JSONResponse({"error": str(exc)}, status_code=502)
    old = data.decode("utf-8", "replace")
    current = target.read_text("utf-8", errors="replace") if target.is_file() else ""
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            current.splitlines(),
            fromfile=f"{rel} @ {at[:7]}",
            tofile=f"{rel} (current)",
            lineterm="",
        )
    )
    return JSONResponse({"path": rel, "at": at, "source": old, "diff": diff})


async def api_restore(request: Request) -> JSONResponse:
    """Restore a historic version — as a sibling copy (safe default) or over the
    current file. An overwrite banks the current bytes first (the .py undo
    stack / the trash, exactly like Revert) and is held under the shared apply
    lock so it can't race an in-flight AI Apply. Purely local: the restored
    file rides normal three-way sync and is pushed explicitly, never silently."""
    from mooring import notebook_undo

    hub = request.app.state.hub
    data = await request.json()
    at = str(data.get("at", ""))
    as_copy = bool(data.get("copy"))
    workspace = hub.cfg.workspace()
    try:
        rel, _ = _resolve_within(workspace, str(data.get("path", "")))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not at:
        return JSONResponse({"error": "No version given."}, status_code=400)
    captured: dict[str, str] = {}

    def snapshot_fn(rel_path: str, content: bytes) -> None:
        if rel_path.endswith(".py"):
            captured["token"] = notebook_undo.snapshot(workspace, rel_path, content)

    try:
        with hub.apply.lock:
            result = sync.restore_version(
                hub.client(), hub.cfg, rel, at, as_copy=as_copy, snapshot_fn=snapshot_fn
            )
    except (GitHubError, OSError) as exc:
        # Type only: a NotFound message embeds the contents/<path> URL.
        telemetry.log_error(exc=type(exc)("restore failed"), op="restore")
        return JSONResponse({"error": str(exc)}, status_code=502)
    telemetry.log_event("restore", copy=int(as_copy), reverted=result.reverted)
    hub._activity(
        "restore",
        path=rel,
        summary=result.summary(),
        trashed=[{"path": p, "token": t} for p, t in result.trashed],
    )
    body = {"lines": result.lines, "summary": result.summary()}
    if "token" in captured:
        body["undo_token"] = captured["token"]
    if result.trashed:
        body["trashed"] = [{"path": p, "token": t} for p, t in result.trashed]
    return JSONResponse(body)


async def api_trash(request: Request) -> JSONResponse:
    """List the local trash — the pre-images banked before mooring overwrote or
    removed local files (see mooring.trash). Value-local: nothing here touches
    GitHub or the AI; the Activity page renders it as the Trash panel."""
    hub = request.app.state.hub
    return JSONResponse({"entries": trash.entries(hub.cfg.workspace())})


async def api_trash_restore(request: Request) -> JSONResponse:
    """Put one trash deposit's bytes back at its original path.

    Token-exact, refusing with a 409 when a later write is on top (the file's
    current blob no longer matches what the destructive action left) — the
    same supersession posture as /api/undo. Held under the shared apply
    guard's lock so a restore can't race an in-flight AI Apply on the same
    notebook. The manifest is never touched: the three-way engine simply
    reclassifies the file on the next status."""
    hub = request.app.state.hub
    data = await request.json()
    token = str(data.get("token", ""))
    workspace = hub.cfg.workspace()

    def _restore() -> str:
        with hub.apply.lock:
            return trash.restore(workspace, token)

    try:
        rel = await asyncio.to_thread(_restore)
    except KeyError:
        return JSONResponse(
            {"ok": False, "error": "Unknown or expired trash entry."}, status_code=404
        )
    except trash.RestoreSuperseded:
        return JSONResponse(
            {
                "ok": False,
                "error": "The file has changed since this copy was saved, so restoring "
                "it would overwrite newer work.",
            },
            status_code=409,
        )
    except OSError as exc:
        return JSONResponse({"error": f"Could not restore the file: {exc}"}, status_code=502)
    telemetry.log_event("trash_restore")
    hub._activity("trash_restore", path=rel)
    return JSONResponse({"ok": True, "path": rel, "lines": [f"restored {rel} from the trash"]})


async def api_activity(request: Request) -> JSONResponse:
    """The workspace's local activity ledger, newest first (see mooring.activity).
    Strictly local: this is the analyst's own journal, not telemetry."""
    hub = request.app.state.hub
    path = request.query_params.get("path") or None
    try:
        limit = min(int(request.query_params.get("limit", "200")), 1000)
    except ValueError:
        limit = 200
    from mooring import activity

    return JSONResponse({"entries": activity.read(hub.cfg.workspace(), limit=limit, path=path)})
