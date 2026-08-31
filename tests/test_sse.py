"""hub/sse — the shared SSE transport's load-bearing ordering.

The replay must be computed AFTER subscribe: a state transition (ready / fail /
closed) then lands either in the replay snapshot or in the queue — never in a
gap between them. Deterministic (recorded fakes, no timing).
"""

from __future__ import annotations

import asyncio
import queue
import types

from mooring.hub.sse import chat_replay, event_stream, sse_event


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


def _session(**kw) -> types.SimpleNamespace:
    base = {"start_status": None, "ner_status": None, "route_replay": None}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_chat_replay_restores_receipts_for_writes_that_are_already_on_disk():
    """A dropped stream used to cost a suggestion. With ``[ai] auto_apply`` on it costs
    the only record of a change that HAS ALREADY HAPPENED — no receipt, no Revert, and a
    notebook that silently moved. The receipts come back with the readiness frames."""
    session = _session(
        applied_replay=[
            {"id": "r1", "summary": {"edited": [0]}, "undo_depth": 1},
            {"id": "r2", "summary": {"appended": [3]}, "undo_depth": 1},
        ]
    )

    assert chat_replay(session) == [
        sse_event("applied", {"id": "r1", "summary": {"edited": [0]}, "undo_depth": 1}),
        sse_event("applied", {"id": "r2", "summary": {"appended": [3]}, "undo_depth": 1}),
    ]


def test_chat_replay_takes_the_receipts_from_a_callable_too():
    session = _session(applied_replay=lambda: [{"id": "r1"}])
    assert chat_replay(session) == [sse_event("applied", {"id": "r1"})]


def test_chat_replay_never_replays_a_receipt_with_no_id():
    """An EventSource reconnects on its own, so a replay lands in a transcript that may
    already hold these rows. The id is what lets the browser drop the duplicates — and a
    doubled receipt is a worse account of the notebook than a missing one, so a payload
    without an id is not replayed at all rather than replayed unsafely."""
    session = _session(
        applied_replay=[{"summary": {"edited": [0]}}, {"id": ""}, {"id": 7}, {"id": "keep"}]
    )
    assert chat_replay(session) == [sse_event("applied", {"id": "keep"})]


def test_chat_replay_bounds_how_much_history_one_reconnect_carries():
    from mooring.hub.sse import MAX_APPLIED_REPLAY

    session = _session(applied_replay=[{"id": f"r{i}"} for i in range(MAX_APPLIED_REPLAY + 10)])
    out = chat_replay(session)
    assert len(out) == MAX_APPLIED_REPLAY
    # …and it keeps the NEWEST, which are the ones that still carry a live way back.
    assert out[-1] == sse_event("applied", {"id": f"r{MAX_APPLIED_REPLAY + 9}"})


def test_chat_replay_ignores_a_session_that_keeps_no_receipts():
    """Every replay here is duck-typed and optional: the stub sessions, the batch stream
    and an older session object expose nothing, and must degrade rather than fail."""
    for value in (None, "receipts", 7, {"id": "r1"}, [None, "x", 7]):
        assert chat_replay(_session(applied_replay=value)) == []
    assert chat_replay(_session()) == []


def test_chat_replay_survives_a_broken_receipts_hook():
    def boom():
        raise RuntimeError("no")

    assert chat_replay(_session(applied_replay=boom)) == []


def test_chat_replay_restores_a_mid_conversation_route_switch():
    session = types.SimpleNamespace(
        start_status=None,
        ner_status=None,
        route_replay={
            "zone": "trusted",
            "reason_codes": ["customer_context"],
            "conversation_carried": True,
        },
    )

    assert chat_replay(session) == [
        sse_event(
            "routing",
            {
                "zone": "trusted",
                "reason_codes": ["customer_context"],
                "conversation_carried": True,
            },
        )
    ]
