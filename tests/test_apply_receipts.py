"""The chat's local echo of a model-driven write: receipts, failures, and replay.

Three things are pinned here, all of them about what the ANALYST is told after the
copilot writes to their notebook without asking first:

* a write that landed leaves a receipt the SSE layer can replay, because the change is
  sitting in their notebook and a dropped stream must not lose the only offer of Revert;
* a write that did NOT land says so, rather than leaving a tool row that ends in a cross
  and no account of whether the notebook was touched;
* neither channel carries a data value.

The receipt buffer is deliberately bounded: it is a replay buffer for a reconnect, not a
history — the transcript is the history.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mooring.ai.chat import _APPLIED_REPLAY_MAX, ChatBroadcaster
from mooring.hub import sse

SECRET_VALUE_DO_NOT_LEAK = "acct-4012888888881881-Jane-Q-Public"


@dataclass(frozen=True)
class _Outcome:
    """The duck-typed shape `app.auto_apply.ApplyOutcome` presents to `ai/`."""

    status: str
    text: str = ""
    is_error: bool = False
    payload: dict = field(default_factory=dict)


def _session(*outcomes):
    """A broadcaster whose applier returns `outcomes` in order, with a drained queue."""
    b = ChatBroadcaster()
    pending = list(outcomes)
    b._applier = lambda ops, rationale: pending.pop(0)
    return b, b.subscribe()


def _kinds(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_a_landed_write_leaves_a_receipt_that_can_be_replayed():
    b, q = _session(_Outcome("applied", "ran", payload={"id": "w1", "summary": {}}))

    b._apply_edit([{"op": "append", "code": "x = 1"}], "add x")

    assert [e.kind for e in _kinds(q)] == ["applied"]
    assert b.applied_replay == [{"id": "w1", "summary": {}}]
    # The SSE layer turns it into a frame a reconnecting browser can de-duplicate.
    assert sse.applied_replay(b) == [sse.sse_event("applied", {"id": "w1", "summary": {}})]


def test_a_receipt_without_an_id_is_kept_but_never_replayed():
    """A doubled receipt is a worse account of the notebook than a missing one, so the
    id — not the payload — is what earns a replay. Enforced in `hub.sse`, pinned here so
    a payload that stops carrying one degrades to silence rather than to duplicates."""
    b, _q = _session(_Outcome("applied", "ran", payload={"summary": {}}))

    b._apply_edit([{"op": "append", "code": "x = 1"}], "")

    assert b.applied_replay == [{"summary": {}}]
    assert sse.applied_replay(b) == []


def test_a_write_that_did_not_land_tells_the_analyst_so():
    """`conflict` / `disabled` / `cancelled` / `error` used to broadcast NOTHING, which
    left "the write failed" and "the write worked and something else broke" looking
    identical from the transcript — the two readings an analyst most needs told apart."""
    b, q = _session(_Outcome("conflict", "The notebook moved under that change.", True))

    b._apply_edit([{"op": "edit", "index": 2, "code": "x = 1"}], "")

    events = _kinds(q)
    assert [e.kind for e in events] == ["apply_failed"]
    assert events[0].data == {
        "status": "conflict",
        "text": "The notebook moved under that change.",
    }
    # Nothing landed, so there is nothing to replay and nothing to Revert.
    assert b.applied_replay == []


def test_a_held_write_still_renders_as_the_ordinary_hold_card():
    """The one remaining human gate must not sprout a second UI: a held write goes out
    on the SAME `proposal` event a proposed cell always did."""
    b, q = _session(_Outcome("held", "waiting on the analyst", payload={"kind": "patch"}))

    b._apply_edit([{"op": "append", "code": "import os; os.remove('x')"}], "")

    assert [e.kind for e in _kinds(q)] == ["proposal"]
    assert b.applied_replay == []


def test_the_replay_buffer_is_bounded_and_keeps_the_newest():
    b, _q = _session(
        *(_Outcome("applied", "ran", payload={"id": f"w{i}"}) for i in range(_APPLIED_REPLAY_MAX + 5))
    )

    for _ in range(_APPLIED_REPLAY_MAX + 5):
        b._apply_edit([{"op": "append", "code": "x = 1"}], "")

    kept = b.applied_replay
    assert len(kept) == _APPLIED_REPLAY_MAX
    assert kept[-1] == {"id": f"w{_APPLIED_REPLAY_MAX + 4}"}  # newest survives
    assert kept[0] == {"id": "w5"}  # oldest five pruned


def test_the_replay_buffer_is_a_copy_callers_cannot_mutate():
    b, _q = _session(_Outcome("applied", "ran", payload={"id": "w1"}))
    b._apply_edit([{"op": "append", "code": "x = 1"}], "")

    b.applied_replay.append({"id": "forged"})

    assert b.applied_replay == [{"id": "w1"}]


def test_neither_channel_carries_a_value():
    """The receipt is the applier's own value-free payload and the failure text is the
    applier's fixed wording. Nothing read out of the notebook reaches either."""
    b, q = _session(
        _Outcome("applied", "ran", payload={"id": "w1", "observation": "sales: 3 columns"}),
        _Outcome("error", "The change could not be applied. Nothing was written.", True),
    )

    b._apply_edit([{"op": "append", "code": f"secret = {SECRET_VALUE_DO_NOT_LEAK!r}"}], "")
    b._apply_edit([{"op": "append", "code": f"secret = {SECRET_VALUE_DO_NOT_LEAK!r}"}], "")

    seen = repr([e.data for e in _kinds(q)]) + repr(b.applied_replay)
    assert SECRET_VALUE_DO_NOT_LEAK not in seen
