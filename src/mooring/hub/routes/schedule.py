"""Schedule endpoints: the refresh board, add/remove/pause, and running one now.

The board is the point of the whole feature — *"your daily reconciliation ran at 07:30, but
segment totals no longer reconcile"* is worth more than the artifact it produces. Everything
here is value-free: booleans, counts, timestamps, and the curated reason strings
:mod:`mooring.app.refresh` records. Nothing here reaches the AI.

A refresh EXECUTES a notebook and can take minutes, so every run goes off the event loop.
"""

from __future__ import annotations

import dataclasses

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import schedule, telemetry, verify


def _background(hub) -> dict:
    """Which clock is running, and whether a better one is available on this machine.

    The tier IS the freshness guarantee, so the card states it rather than letting the user
    assume an OS-level scheduler exists. ``offer`` is what makes the Enable button appear;
    ``reason`` explains a machine that cannot go higher (blocked policy, an ephemeral uvx
    install) instead of leaving the absence unexplained."""
    from mooring import schedule_os

    alias = hub.app_cfg.active_alias or ""
    tier = schedule_os.current_tier(alias)
    capability = schedule_os.probe()
    return {
        "tier": tier,
        "tier_text": schedule_os.TIER_NAMES[tier],
        "offer": capability.tier > tier,
        "reason": "" if capability.can_background else capability.reason,
    }


def _board(hub) -> dict:
    """The whole schedules card, computed from local state only (no network)."""
    from mooring.app import refresh

    cfg = hub.cfg
    workspace = cfg.workspace()
    receipts = verify.read_results(workspace)
    rows = []
    for sched in schedule.load(workspace):
        verified = bool((receipts.get(sched.notebook) or {}).get("passed"))
        rows.append(
            {
                "notebook": sched.notebook,
                "cadence": sched.cadence,
                "cadence_text": sched.describe_cadence(),
                "at": sched.at,
                "day": sched.day,
                "deliver": sched.deliver,
                "pull": sched.pull,
                "paused": sched.paused,
                "verified": verified,
                "due": schedule.is_due(sched),
                "overdue": schedule.is_overdue(sched),
                "next_due": schedule.next_due(sched).isoformat(timespec="minutes"),
                "consecutive_failures": sched.consecutive_failures,
                "last_run": sched.last_run.to_dict(),
                # Whether the background sweep may fire this without a click. The UI shows a
                # "Run now" button exactly when this is false, so the reason a schedule is
                # sitting there is always visible rather than mysterious.
                "auto": refresh.may_auto_run(cfg, sched),
            }
        )
    return {
        "schedules": rows,
        "overdue": sum(1 for r in rows if r["overdue"]),
        "due": sum(1 for r in rows if r["due"]),
        "background": _background(hub),
    }


async def api_schedule_background(request: Request) -> JSONResponse:
    """Enable or disable background refresh (the tier 2/3 rungs of the ladder).

    Enabling tries the Windows task first and falls back to a sign-in agent when policy
    refuses — a routine outcome on a managed laptop, not an error. Any demotion is reported
    so the UI states what actually happened rather than implying the user got what they
    asked for."""
    hub = request.app.state.hub
    data = await request.json()
    return await run_in_threadpool(_set_background, hub, bool(data.get("enabled", True)))


def _set_background(hub, enabled: bool) -> JSONResponse:
    from mooring import schedule_os

    alias = hub.app_cfg.active_alias or ""
    if not enabled:
        removed = schedule_os.disable(alias)
        line = (
            "Background refresh off — schedules still run whenever the hub is open."
            if removed
            else "Background refresh was not enabled."
        )
        return JSONResponse({"ok": True, "lines": [line], **_board(hub)})
    try:
        installed = schedule_os.enable(hub.cfg.workspace(), alias)
    except schedule_os.UnstableInstall as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    lines = [installed.detail]
    if installed.reason:
        lines.append(installed.reason)
    lines.append(f"Refreshes now run {schedule_os.TIER_NAMES[installed.tier]}.")
    telemetry.log_event("schedule_background", action="enable", tier=installed.tier)
    return JSONResponse({"ok": True, "lines": lines, **_board(hub)})


async def api_schedules(request: Request) -> JSONResponse:
    """The refresh board: every schedule, when it is next due, and how the last run went."""
    hub = request.app.state.hub
    return await run_in_threadpool(lambda: JSONResponse(_board(hub)))


async def api_schedule_add(request: Request) -> JSONResponse:
    """Create or amend a schedule. Refuses a notebook that has not verified clean."""
    hub = request.app.state.hub
    data = await request.json()
    return await run_in_threadpool(_add, hub, data)


def _add(hub, data: dict) -> JSONResponse:
    from mooring.app import refresh, verify_run

    cfg = hub.cfg
    workspace = cfg.workspace()
    rel = str(data.get("path", "")).replace("\\", "/")
    if not rel:
        return JSONResponse({"error": "No notebook given."}, status_code=400)
    try:
        rel = verify_run.ensure_runnable(workspace, rel, refresh.RefreshRefused)
    except (ValueError, FileNotFoundError, refresh.RefreshRefused) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # The preflight gate: only a notebook that has RUN CLEAN may be scheduled. Verify receipts
    # are SHA-keyed, so this also means editing a scheduled notebook lapses its verification
    # for free (see app/refresh.preflight).
    receipt = verify.read_results(workspace).get(rel)
    if not (receipt and receipt.get("passed")):
        return JSONResponse(
            {
                "error": (
                    "Verify this notebook first — only a notebook that has run clean can be "
                    "scheduled. Use Actions ▾ → Verify runs, then schedule it."
                )
            },
            status_code=409,
        )
    try:
        cadence = schedule.normalize_cadence(str(data.get("cadence", "daily")))
        at = schedule.normalize_at(str(data.get("at") or schedule.DEFAULT_AT))
        day = schedule.normalize_day(str(data.get("day", "mon")))
    except schedule.ScheduleError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    existing = schedule.get(workspace, rel)
    sched = schedule.Schedule(
        notebook=rel,
        cadence=cadence,
        at=at,
        day=day,
        deliver=bool(data.get("deliver", True)),
        pull=bool(data.get("pull", True)),
        # Amending clears any auto-pause: the user has just said what they want.
        last_run=existing.last_run if existing else schedule.LastRun(),
    )
    try:
        schedule.put(workspace, sched)
    except OSError as exc:
        return JSONResponse({"error": f"Could not save the schedule: {exc}"}, status_code=500)
    telemetry.log_event("schedule", action="add", cadence=cadence)  # value-free: no path
    verb = "Updated" if existing else "Scheduled"
    return JSONResponse(
        {"ok": True, "lines": [f"{verb} {rel} — {sched.describe_cadence()}."], **_board(hub)}
    )


async def api_schedule_remove(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()

    def _run() -> JSONResponse:
        rel = str(data.get("path", "")).replace("\\", "/")
        if not schedule.remove(hub.cfg.workspace(), rel):
            return JSONResponse({"error": "No schedule for that notebook."}, status_code=404)
        return JSONResponse({"ok": True, "lines": [f"Removed the schedule for {rel}."], **_board(hub)})

    return await run_in_threadpool(_run)


async def api_schedule_pause(request: Request) -> JSONResponse:
    """Pause or resume. Resuming also clears the failure counter, so an auto-paused schedule
    gets a full budget rather than re-pausing on its next single failure."""
    hub = request.app.state.hub
    data = await request.json()

    def _run() -> JSONResponse:
        rel = str(data.get("path", "")).replace("\\", "/")
        paused = bool(data.get("paused", True))
        if schedule.set_paused(hub.cfg.workspace(), rel, paused) is None:
            return JSONResponse({"error": "No schedule for that notebook."}, status_code=404)
        verb = "Paused" if paused else "Resumed"
        return JSONResponse({"ok": True, "lines": [f"{verb} {rel}."], **_board(hub)})

    return await run_in_threadpool(_run)


async def api_refresh(request: Request) -> JSONResponse:
    """Run one schedule now, or every due one when no path is given.

    This EXECUTES the notebook (pull → run → receipts), so it runs off the event loop and can
    take minutes. It never pushes."""
    hub = request.app.state.hub
    data = await request.json()
    return await run_in_threadpool(_refresh, hub, data)


def _refresh(hub, data: dict) -> JSONResponse:
    from mooring.app import refresh

    cfg = hub.cfg
    rel = str(data.get("path", "")).replace("\\", "/")
    if not rel:
        results = refresh.run_due(cfg)
        lines = [refresh.describe_result(r) for r in results] or ["Nothing due."]
        return JSONResponse({"ok": True, "lines": lines, **_board(hub)})
    sched = schedule.get(cfg.workspace(), rel)
    try:
        result = refresh.refresh_notebook(cfg, rel, sched=sched)
    except (ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (refresh.RefreshRefused, refresh.RefreshBusy) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(
        {
            "ok": result.ok,
            "outcome": result.outcome,
            "lines": [refresh.describe_result(result)],
            "result": dataclasses.asdict(result),
            **_board(hub),
        }
    )
