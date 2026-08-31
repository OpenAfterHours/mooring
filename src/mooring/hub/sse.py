"""The hub's ONE Server-Sent-Events transport (chat and batch share it).

Both streams had grown their own near-identical generator (subscribe →
``: connected`` → catch-up → 15s-timeout poll loop → ``closed`` ends it →
unsubscribe). The transport now lives here once; what differs per stream is only
the REPLAY — the catch-up chunks for events that fired before this subscriber
attached (chat: startup readiness + NER-model prepare; batch: a run that already
closed). A replayed ``closed`` ends the stream exactly like a live one, so a
finished batch never pings forever.
"""

from __future__ import annotations

import asyncio
import json
import queue

from starlette.responses import StreamingResponse


def sse_event(kind: str, data) -> str:
    """One SSE frame."""
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"


def sse_response(gen) -> StreamingResponse:
    """The standard headers both streams used (no proxy buffering, no cache)."""
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def chat_replay(session) -> list[str]:
    """Catch-up frames for a chat subscriber.

    Replays startup readiness so a subscriber that connects after the (async,
    backgrounded) provider handshake finished — or failed — still learns the
    outcome and unblocks the input; and the current NER-model prepare status so
    a subscriber joining mid-download immediately sees progress.

    …and, when the session keeps them, the RECEIPTS for writes the model already made.
    A dropped stream used to cost only a suggestion; with ``[ai] auto_apply`` on it costs
    the only record of a change that is ON DISK — no receipt, no Revert, and a notebook
    that has silently moved. Every replay here is duck-typed and optional (the batch
    stub, the test sessions and an older session object simply expose nothing), so a
    session that retains nothing degrades to today's behaviour rather than failing; the
    browser separately warns when a drop happened mid-turn.
    """
    out: list[str] = []
    start_status = getattr(session, "start_status", None)
    if isinstance(start_status, dict):
        if start_status.get("state") == "ready":
            out.append(sse_event("ready", {}))
        elif start_status.get("state") == "error":
            fail_data = {"text": start_status.get("text", "")}
            if start_status.get("reason"):  # e.g. "not_connected" -> sign-in button
                fail_data["reason"] = start_status["reason"]
            out.append(sse_event("fail", fail_data))
    ner_status = getattr(session, "ner_status", None)
    if ner_status:
        out.append(sse_event("ner", ner_status))
    route_replay = getattr(session, "route_replay", None)
    if isinstance(route_replay, dict):
        out.append(sse_event("routing", route_replay))
    out.extend(applied_replay(session))
    return out


# A ceiling on the receipts one reconnect replays. A turn with no iteration cap can write
# a great many times, and a reconnect must not stall on a wall of history; the newest are
# the ones that still carry a live way back.
MAX_APPLIED_REPLAY = 25


def applied_replay(session) -> list[str]:
    """``applied`` frames for writes this subscriber may have missed, newest last.

    The session exposes ``applied_replay`` — the value-free receipt payloads it has
    broadcast this session, oldest first (a list, or a callable returning one). Anything
    else, including the common case of a session that keeps none, replays nothing.

    A receipt is replayed ONLY if it carries a non-empty string ``id``. An EventSource
    reconnects by itself, so a replay lands in a transcript that may already hold most of
    these rows; the id is what lets the browser drop the ones it has already drawn.
    Without one, a reconnect would double every receipt — which is a worse account of
    what happened to the notebook than the missing rows this exists to restore. The rule
    is enforced here rather than asked for in a comment, so a payload that forgets the id
    degrades to today's behaviour instead of to duplicates.
    """
    receipts = getattr(session, "applied_replay", None)
    if callable(receipts):
        try:
            receipts = receipts()
        except Exception:  # noqa: BLE001 - a broken hook must never break the stream
            return []
    if not isinstance(receipts, (list, tuple)):
        return []
    kept = [
        item
        for item in receipts
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    ]
    return [sse_event("applied", item) for item in kept[-MAX_APPLIED_REPLAY:]]


def batch_replay(run) -> list[str]:
    """Catch-up frames for a batch subscriber: an appendable run streams ``job``
    events for its whole life (no single terminal ``done`` — the user keeps
    adding; a late subscriber catches up via GET /tray). If the run was already
    closed (reaped / repo switch), say so instead of pinging forever."""
    return [sse_event("closed", {})] if run.status == "closed" else []


async def event_stream(broadcaster, replay):
    """The shared SSE generator over a ChatBroadcaster-shaped object
    (``subscribe()`` returning a queue, ``unsubscribe(q)``).

    ``replay`` is a CALLABLE returning the catch-up frames, and it runs strictly
    AFTER ``subscribe()`` — the ordering the original per-stream generators had.
    That order is load-bearing: a state transition (ready/fail/closed) can then
    never fall through the gap, because it lands either in the replay snapshot
    (fired before subscribe) or in the queue (fired after). Computing the replay
    in the route handler, before this generator first runs, would silently drop
    a transition landing in between — a chat stuck on "connecting…", or a
    cancelled batch stream pinging forever.
    """
    q = broadcaster.subscribe()
    try:
        yield ": connected\n\n"
        for chunk in replay():
            yield chunk
            if chunk.startswith("event: closed\n"):
                return  # the replayed close ends the stream like a live one
        while True:
            try:
                event = await asyncio.to_thread(q.get, True, 15.0)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            yield sse_event(event.kind, event.data)
            if event.kind == "closed":
                break
    finally:
        broadcaster.unsubscribe(q)
