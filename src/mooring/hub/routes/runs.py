"""Parameterised-run endpoints: start a fan-out, watch it value by value, stop it.

A fan-out executes a whole notebook once per value, so it can run for many minutes and it
must never block the event loop or the rest of the hub. The work therefore lives on its own
thread inside :class:`mooring.app.param_runs.RunHandle`, and these three handlers only
start it, read its snapshot, and set its cancel event — each of which is instant.

Progress is POLLED rather than streamed. The hub does own an SSE transport, but a fan-out
changes state at most once per notebook run (tens of seconds to minutes); a stream would
carry one frame a minute in exchange for a broadcaster, a run registry and a replay path to
keep correct. On loopback, a one-second poll is the honest transport.

ONE fan-out per hub at a time, which is not a limitation this module invents: the workspace
run lock (:func:`mooring.app.refresh.workspace_guard`) already permits exactly one
whole-notebook run per workspace, shared with the scheduled refresh.

Everything returned is either a count, a boolean, a timestamp, a curated reason, or the
parameter value the user typed (which is already in the artifact's filename they are about
to read). Never marimo's stderr, and nothing here reaches the AI.
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import params


async def api_run_start(request: Request) -> JSONResponse:
    """Validate a ``--for`` spec and start the fan-out on a background thread.

    Every refusal is decided synchronously (:func:`mooring.app.param_runs.start` does the
    path, notebook and reads-the-parameter checks before spawning), so the user gets a real
    error instead of a run that appears and instantly dies."""
    hub = request.app.state.hub
    data = await request.json()
    return await run_in_threadpool(_start, hub, data)


def _start(hub, data: dict) -> JSONResponse:
    from mooring.app import param_runs, refresh

    rel = str(data.get("path", "")).replace("\\", "/")
    if not rel:
        return JSONResponse({"error": "No notebook given."}, status_code=400)
    try:
        spec = params.parse_spec(str(data.get("for", "")))
    except params.ParamError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Check-then-claim under the hub's lock, so two concurrent starts cannot both spawn —
    # the loser would otherwise replace the live handle with one about to die on the
    # workspace lock, and the UI would lose sight of the run that is actually going.
    with hub.param_run_lock:
        active = hub.param_run
        if active is not None and not active.snapshot()["done"]:
            return JSONResponse(
                {"error": "A parameterised run is already going — wait for it or cancel it."},
                status_code=409,
            )
        try:
            handle, _thread = param_runs.start(
                hub.cfg, rel, spec, do_deliver=bool(data.get("deliver", True))
            )
        except (ValueError, FileNotFoundError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except param_runs.FanOutRefused as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except refresh.RefreshBusy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        hub.param_run = handle
    return JSONResponse({"ok": True, "run": handle.snapshot()})


async def api_run_state(request: Request) -> JSONResponse:
    """The current fan-out's snapshot, or ``{"run": null}`` when nothing is running."""
    hub = request.app.state.hub
    handle = hub.param_run
    return JSONResponse({"run": handle.snapshot() if handle is not None else None})


async def api_run_cancel(request: Request) -> JSONResponse:
    """Stop the fan-out. The in-flight marimo process TREE is killed (the runner's cancel
    rides the same ``taskkill /T`` the timeout uses), and every value still queued is
    reported as not run — never quietly dropped."""
    hub = request.app.state.hub
    handle = hub.param_run
    if handle is None:
        return JSONResponse({"error": "Nothing is running."}, status_code=404)
    handle.cancel.set()
    return JSONResponse({"ok": True, "run": handle.snapshot()})
