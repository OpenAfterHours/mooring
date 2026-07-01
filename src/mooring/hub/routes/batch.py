"""AI batch-orchestrator endpoints: open/add a queue, stream progress, the
review tray, and per-proposal apply/refine/force."""

from __future__ import annotations

import asyncio
import secrets
import threading
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from mooring import telemetry
from mooring.hub.sse import batch_replay, event_stream, sse_response


def _gates(hub) -> JSONResponse | None:
    """The two feature gates every batch endpoint re-checks."""
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    if not hub.app_cfg.ai_batch_enabled:
        return JSONResponse({"enabled": False, "reason": "batch_disabled"}, status_code=403)
    return None


async def api_batch_state(request: Request) -> JSONResponse:
    """What the batch page needs to render: whether batch is enabled, its caps,
    the value-free dataset paths for per-job dataset selection, and the theme."""
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    from mooring import schema

    cfg = hub.cfg
    datasets = await run_in_threadpool(schema.list_datasets, cfg.workspace(), cfg.folders)
    return JSONResponse(
        {
            "enabled": hub.app_cfg.ai_batch_enabled,
            "max_jobs": hub.app_cfg.ai_batch_max_jobs,
            "max_concurrency": hub.app_cfg.ai_batch_max_concurrency,
            "pii_policy": hub.app_cfg.ai_batch_pii_policy,
            "datasets": datasets,
            "ui_theme": hub.app_cfg.ui_theme,
        }
    )


async def api_batch_open(request: Request) -> JSONResponse:
    """Open a NEW batch queue and submit the first job(s). The run stays open so the
    analyst can keep adding more (api_batch_add) while these build."""
    hub = request.app.state.hub
    if (gate := _gates(hub)) is not None:
        return gate
    data = await request.json()
    jobs, err = hub._parse_batch_jobs(data.get("jobs"))
    if err is not None:
        return err
    max_jobs = hub.app_cfg.ai_batch_max_jobs
    if max_jobs and len(jobs) > max_jobs:
        return JSONResponse(
            {"error": f"This batch has {len(jobs)} jobs but the limit is {max_jobs}."},
            status_code=400,
        )
    from mooring.ai.batch import BatchError
    from mooring.ai.chat import ChatBroadcaster

    hub._reap_idle_batches()
    workspace = hub.cfg.workspace()
    broadcaster = ChatBroadcaster()
    abort = threading.Event()
    planner = hub._new_batch_planner(workspace, broadcaster, abort)
    batch_id = secrets.token_urlsafe(9)
    run = {
        "broadcaster": broadcaster,
        "abort": abort,
        "planner": planner,
        "status": "open",
        "applied": set(),
        "workspace": str(workspace),
    }
    with hub._batch_lock:
        hub._batches[batch_id] = run
    broadcaster.touch()
    try:
        # add() runs the PII pre-flight + mints the notebooks then submits builders;
        # it returns quickly (the builds run in the pool), off the event loop.
        await asyncio.to_thread(planner.add, jobs)
    except BatchError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    telemetry.log_event("ai_batch_open", jobs=len(jobs))
    return JSONResponse({"batch_id": batch_id, "jobs": len(jobs)})


async def api_batch_add(request: Request) -> JSONResponse:
    """Queue MORE jobs onto an already-open run — so a job can be kicked off while
    the next is still being written. Respects the cumulative max_jobs cap."""
    hub = request.app.state.hub
    if (gate := _gates(hub)) is not None:
        return gate
    data = await request.json()
    batch_id = str(data.get("batch_id", ""))
    with hub._batch_lock:
        run = hub._batches.get(batch_id)
    if run is None:
        return JSONResponse({"error": "Unknown batch."}, status_code=404)
    if run["status"] == "closed":
        return JSONResponse({"error": "This batch is finished."}, status_code=409)
    jobs, err = hub._parse_batch_jobs(data.get("jobs"))
    if err is not None:
        return err
    from mooring.ai.batch import BatchError

    run["broadcaster"].touch()
    try:
        indices = await asyncio.to_thread(run["planner"].add, jobs)
    except BatchError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    telemetry.log_event("ai_batch_add", jobs=len(jobs))
    return JSONResponse({"ok": True, "added": len(indices)})


async def api_batch_refine(request: Request) -> JSONResponse:
    """Re-build ONE built notebook's proposal with the analyst's revision note, so a
    proposal can be tweaked in the tray before it's Applied. The note runs the
    non-interactive PII gate; the notebook file is never written; a poor revision
    keeps the previous proposal."""
    hub = request.app.state.hub
    if (gate := _gates(hub)) is not None:
        return gate
    data = await request.json()
    batch_id = str(data.get("batch_id", ""))
    with hub._batch_lock:
        run = hub._batches.get(batch_id)
    if run is None:
        return JSONResponse({"error": "Unknown batch."}, status_code=404)
    if run["status"] == "closed":
        return JSONResponse({"error": "This batch is finished."}, status_code=409)
    try:
        job_idx = int(data.get("job"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "A job index is required."}, status_code=400)
    feedback = str(data.get("feedback", ""))
    run["broadcaster"].touch()
    from mooring.ai.batch import BatchError

    try:
        await asyncio.to_thread(run["planner"].refine, job_idx, feedback)
    except BatchError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    telemetry.log_event("ai_batch_refine")
    return JSONResponse({"ok": True})


async def api_batch_force(request: Request) -> JSONResponse:
    """Re-build ONE pii-blocked job, overriding the outbound-PII guard — the tray's
    "Build anyway". The human reviewing the tray authorizes forwarding the flagged
    brief verbatim (the batch analogue of the chat's "Send anyway"); the notebook is
    still only PROPOSED into, never written, so the existing per-notebook Apply gate
    remains the only write path."""
    hub = request.app.state.hub
    if (gate := _gates(hub)) is not None:
        return gate
    data = await request.json()
    batch_id = str(data.get("batch_id", ""))
    with hub._batch_lock:
        run = hub._batches.get(batch_id)
    if run is None:
        return JSONResponse({"error": "Unknown batch."}, status_code=404)
    if run["status"] == "closed":
        return JSONResponse({"error": "This batch is finished."}, status_code=409)
    try:
        job_idx = int(data.get("job"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "A job index is required."}, status_code=400)
    run["broadcaster"].touch()
    from mooring.ai.batch import BatchError

    try:
        await asyncio.to_thread(run["planner"].force, job_idx)
    except BatchError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    telemetry.log_event("ai_batch_force")
    return JSONResponse({"ok": True})


async def api_batch_stream(request: Request) -> StreamingResponse | JSONResponse:
    hub = request.app.state.hub
    batch_id = request.path_params["batch_id"]
    with hub._batch_lock:
        run = hub._batches.get(batch_id)
    if run is None:
        return JSONResponse({"error": "Unknown batch."}, status_code=404)
    return sse_response(event_stream(run["broadcaster"], batch_replay(run)))


async def api_batch_tray(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    batch_id = request.path_params["batch_id"]
    with hub._batch_lock:
        run = hub._batches.get(batch_id)
    if run is None:
        return JSONResponse({"error": "Unknown batch."}, status_code=404)
    snapshot = run["planner"].snapshot()
    return JSONResponse(
        {
            "status": run["status"],
            "pending": run["planner"].pending,
            "jobs": hub._batch_tray_jobs(run, snapshot),
        }
    )


async def api_batch_apply(request: Request) -> JSONResponse:
    """Apply ONE proposal from a finished batch into its notebook — the human's
    per-notebook authorization. Reuses the SAME single-notebook write path as the
    chat Apply (_apply_with_undo: snapshot + _apply_lock + per-notebook opt-out
    re-check), so there is no autonomous-write path; only the review is batched."""
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    data = await request.json()
    batch_id = str(data.get("batch_id", ""))
    with hub._batch_lock:
        run = hub._batches.get(batch_id)
    if run is None:
        return JSONResponse({"error": "Unknown batch."}, status_code=404)
    results = run["planner"].snapshot()
    try:
        job_idx = int(data.get("job"))
        prop_idx = int(data.get("proposal", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "A job and proposal index are required."}, status_code=400)
    if not 0 <= job_idx < len(results):
        return JSONResponse({"error": "No such job."}, status_code=404)
    res = results[job_idx]
    if res.notebook_rel is None or not 0 <= prop_idx < len(res.proposals):
        return JSONResponse({"error": "No such proposal."}, status_code=404)
    proposal = res.proposals[prop_idx]
    pid = proposal.get("pid")
    # Idempotent by the proposal's STABLE id (not its position): a re-submit of an
    # already-applied proposal (a tray re-render re-armed the button) is a no-op, so
    # the same cell can never be appended twice. Keying by position would wrongly
    # treat a refined proposal at the same slot as already applied — the Bug this fixes.
    with hub._batch_lock:
        if pid is not None and pid in run["applied"]:
            return JSONResponse({"ok": True, "noop": True})
    ops = proposal.get("ops")
    if isinstance(ops, list) and ops:
        op_dicts = ops
    elif str(proposal.get("code", "")).strip():
        op_dicts = [{"op": "append", "code": proposal["code"]}]
    else:
        return JSONResponse({"error": "Nothing to apply."}, status_code=400)
    workspace = Path(run["workspace"])
    notebook_rel = res.notebook_rel
    from mooring.ai.cellwrite import CellApplyConflict, CellWriteError

    try:
        nb_path = hub._ws_file(workspace, notebook_rel, suffix=".py")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": f"No such notebook: {notebook_rel}"}, status_code=404)
    try:
        undo_depth = await asyncio.to_thread(
            hub.apply.apply_with_undo, nb_path, workspace, notebook_rel, op_dicts
        )
    except PermissionError:
        return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
    except CellApplyConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except CellWriteError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    with hub._batch_lock:
        if pid is not None:
            run["applied"].add(pid)
    telemetry.log_event("ai_batch_apply")
    return JSONResponse({"ok": True, "can_undo": undo_depth > 0, "undo_depth": undo_depth})
