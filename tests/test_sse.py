"""hub/sse — the shared SSE transport's load-bearing ordering.

The replay must be computed AFTER subscribe: a state transition (ready / fail /
closed) then lands either in the replay snapshot or in the queue — never in a
gap between them. Deterministic (recorded fakes, no timing).
"""

from __future__ import annotations

import asyncio
import queue

from mooring.hub.sse import event_stream, sse_event


class _Broadcaster:
    def __init__(self, order: list[str]):
        self._order = order
        self.q: queue.Queue = queue.Queue()

    def subscribe(self):
        self._order.append("subscribe")
        return self.q

    def unsubscribe(self, q):
        self._order.append("unsubscribe")


def test_replay_is_computed_after_subscribe():
    order: list[str] = []
    b = _Broadcaster(order)

    def replay():
        order.append("replay")
        return [sse_event("closed", {})]  # ends the stream deterministically

    async def drain():
        chunks = [chunk async for chunk in event_stream(b, replay)]
        return chunks

    chunks = asyncio.run(drain())
    assert order == ["subscribe", "replay", "unsubscribe"]
    assert chunks[0].startswith(": connected")
    assert chunks[1].startswith("event: closed")


def test_replayed_close_ends_the_stream_and_unsubscribes():
    order: list[str] = []
    b = _Broadcaster(order)
    b.q.put_nowait(object())  # a queued event that must never be reached

    async def drain():
        return [chunk async for chunk in event_stream(b, lambda: [sse_event("closed", {})])]

    chunks = asyncio.run(drain())
    assert len(chunks) == 2  # connected + closed, then the generator returned
    assert "unsubscribe" in order
