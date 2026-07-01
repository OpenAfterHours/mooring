"""File endpoints: new, open, reveal, delete, and the rollback/undo pair."""

from __future__ import annotations

import asyncio

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import notebook_template, reveal, sync, telemetry
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
    try:
        removed = deletion.delete(cfg.workspace(), rel_path, cfg.exclude, cfg.folders)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": f"No such notebook: {rel_path}"}, status_code=404)
    telemetry.log_event("delete", count=len(removed))
    name = rel_path.rsplit("/", 1)[-1]
    return JSONResponse(
        {
            "lines": [f"deleted {r}" for r in removed],
            "summary": f"Deleted {name}. If it was shared, push or propose to "
            "remove it for the team.",
        }
    )


async def api_rollback(request: Request) -> JSONResponse:
    """Restore one notebook to its last-synced version (the manifest base),
    discarding local edits. The pre-revert bytes of a ``.py`` are snapshotted onto
    the local undo stack first and the snapshot token returned (``undo_token``), so
    :func:`api_undo` can put them back — and refuse if a later write has since
    landed on top. Held under ``_apply_lock`` so the snapshot+write can't race an
    in-flight AI Apply on the same notebook (the same guard Apply/Undo take)."""
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
        with hub._apply_lock:
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
    body = {"lines": result.lines, "summary": result.summary()}
    if "token" in captured:
        body["undo_token"] = captured["token"]
    return JSONResponse(body)


async def api_undo(request: Request) -> JSONResponse:
    """Restore a notebook's most recent local snapshot — the pre-revert (or
    pre-AI-edit) bytes. AI-independent: unlike the chat rollback this is not
    bound to a chat session or gated on the AI being enabled, so a Revert done
    from the file list is itself undoable. The snapshot stack is shared LIFO, so a
    ``token`` (from /api/rollback) must still be the newest entry — otherwise a
    later write (e.g. an AI Apply) is on top and we refuse (409) rather than
    restore the wrong layer."""
    from mooring.hub.server import _UNDO_SUPERSEDED

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
            hub._restore_undo, nb_path, workspace, rel_path, expect_token=token
        )
    except OSError as exc:  # momentarily locked — the snapshot is kept to retry
        return JSONResponse({"error": f"Could not restore the notebook: {exc}"}, status_code=502)
    if outcome is _UNDO_SUPERSEDED:
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
    return JSONResponse({"ok": True, "can_undo": outcome > 0, "undo_depth": outcome})
