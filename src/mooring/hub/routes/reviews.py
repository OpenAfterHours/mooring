"""Reviewer-inbox endpoints: list teammates' proposals, diff one, submit a review.

Four-eyes without leaving the hub — see :mod:`mooring.app.reviews`. All three reach
GitHub, so they run off the event loop; a not-logged-in / no-repo hub simply returns an
empty inbox. Nothing here goes near the AI.
"""

from __future__ import annotations

import asyncio
import dataclasses

from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import auth, telemetry
from mooring.app import reviews
from mooring.github import GitHubError


def _configured(hub) -> bool:
    cfg = hub.cfg
    return cfg.is_configured and bool(auth.get_token(host=cfg.host))


async def api_reviews(request: Request) -> JSONResponse:
    """Open proposals awaiting review — mooring review branches, never your own."""
    hub = request.app.state.hub
    if not _configured(hub):
        return JSONResponse({"reviews": []})

    def _run() -> dict:
        try:
            me = hub.username()
        except (GitHubError, OSError):
            me = ""  # can't identify you -> can't exclude your own, but still list
        items = reviews.list_reviews(hub.client(), me)
        return {"reviews": [dataclasses.asdict(r) for r in items]}

    try:
        body = await asyncio.to_thread(_run)
    except (GitHubError, OSError) as exc:
        # Type only: a NotFound message embeds the request URL (the history-endpoint
        # posture — paths/URLs never reach central telemetry).
        telemetry.log_error(exc=type(exc)("reviews list failed"), op="reviews")
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(body)


async def api_review_detail(request: Request) -> JSONResponse:
    """The cell-aware diff of one proposal (PR ``number``)."""
    hub = request.app.state.hub
    data = await request.json()
    try:
        number = int(data.get("number", 0))
    except (TypeError, ValueError):
        number = 0
    if number <= 0:
        return JSONResponse({"error": "No pull request given."}, status_code=400)
    if not _configured(hub):
        return JSONResponse({"error": "Log in to review changes."}, status_code=400)
    try:
        body = await asyncio.to_thread(reviews.review_detail, hub.client(), number)
    except (GitHubError, OSError) as exc:
        telemetry.log_error(exc=type(exc)("review detail failed"), op="reviews")
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(body)


async def api_review_submit(request: Request) -> JSONResponse:
    """Approve or request changes on PR ``number`` with a whole-change note."""
    hub = request.app.state.hub
    data = await request.json()
    try:
        number = int(data.get("number", 0))
    except (TypeError, ValueError):
        number = 0
    event = str(data.get("event", ""))
    note = str(data.get("body", ""))
    if number <= 0:
        return JSONResponse({"error": "No pull request given."}, status_code=400)
    try:
        await asyncio.to_thread(reviews.submit, hub.client(), number, event, note)
    except ValueError as exc:  # bad event / missing required note
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (GitHubError, OSError) as exc:
        telemetry.log_error(exc=type(exc)("review submit failed"), op="reviews")
        return JSONResponse({"error": str(exc)}, status_code=502)
    telemetry.log_event("review", action=event.lower())  # value-free: the action only
    verb = "approved" if event.upper() == "APPROVE" else "requested changes on"
    return JSONResponse({"ok": True, "lines": [f"You {verb} #{number}."]})
