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
    return out


def batch_replay(run) -> list[str]:
    """Catch-up frames for a batch subscriber: an appendable run streams ``job``
    events for its whole life (no single terminal ``done`` — the user keeps
    adding; a late subscriber catches up via GET /tray). If the run was already
    closed (reaped / repo switch), say so instead of pinging forever."""
    return [sse_event("closed", {})] if run.status == "closed" else []


async def event_stream(broadcaster, replay: list[str]):
    """The shared SSE generator over a ChatBroadcaster-shaped object
    (``subscribe()`` returning a queue, ``unsubscribe(q)``)."""
    q = broadcaster.subscribe()
    try:
        yield ": connected\n\n"
        for chunk in replay:
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
