"""CopilotChatSession on its loop thread, driven by a fake SDK (no real auth).

We monkeypatch ``copilot.CopilotClient`` so the session builds against a fake
client/session whose ``send`` drives the registered ``on`` handler with scripted
events — exercising the loop-thread + queue→event mapping + the value-blind
create_session config, without the Copilot CLI or a GitHub login.
"""

from __future__ import annotations

import queue
import types
from collections.abc import Callable

import copilot
import pytest
from copilot import SessionEventType as ET

from mooring.ai.session import CopilotChatSession
from mooring.ai.tools import TOOL_NAMES


def _event(etype, **data):
    return types.SimpleNamespace(type=etype, data=types.SimpleNamespace(**data))


# A scripted turn: (SessionEventType, data kwargs). Tests can override.
BASIC_TURN: list[tuple[ET, dict[str, object]]] = [
    (ET.ASSISTANT_MESSAGE_DELTA, {"delta_content": "Hel"}),
    (ET.ASSISTANT_MESSAGE_DELTA, {"delta_content": "lo"}),
    (ET.ASSISTANT_MESSAGE, {"content": "Hello"}),
    (ET.SESSION_IDLE, {"aborted": False}),
]


class FakeSession:
    SCRIPT: list[tuple[ET, dict[str, object]]] = BASIC_TURN

    def __init__(self, create_kwargs):
        self.create_kwargs = create_kwargs
        self._handler: Callable[..., object] | None = None
        self.disconnected = False
        self.aborted = 0  # times the SDK's own turn abort was asked for
        self.sent = []  # prompts actually forwarded to the SDK

    def on(self, handler):
        self._handler = handler
        return lambda: None

    async def send(self, prompt, **kw):
        self.sent.append(prompt)
        # Drive the streaming handler exactly like the real SDK would.
        assert self._handler is not None
        for etype, data in type(self).SCRIPT:
            self._handler(_event(etype, **data))
        return "turn-1"

    async def abort(self):
        # The real SDK's CopilotSession.abort(): "Abort the currently processing message
        # in this session. The session remains valid and can continue to be used."
        self.aborted += 1

    async def disconnect(self):
        self.disconnected = True


class FakeClient:
    last: FakeClient | None = None
    authed = True

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = self.stopped = False
        self.session: FakeSession | None = None
        FakeClient.last = self

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def get_auth_status(self):
        return types.SimpleNamespace(isAuthenticated=type(self).authed, login="phil")

    async def create_session(self, **kwargs):
        self.session = FakeSession(kwargs)
        return self.session


@pytest.fixture
def fake_sdk(monkeypatch):
    monkeypatch.setattr(copilot, "CopilotClient", FakeClient)
    FakeSession.SCRIPT = BASIC_TURN  # reset any per-test override
    FakeClient.last = None
    FakeClient.authed = True


def _make(tmp_path, **kw):
    (tmp_path / "nb.py").write_text("import marimo\n", "utf-8")
    return CopilotChatSession(
        model="",
        system_context="CTX",
        workspace=tmp_path,
        folders=("data",),
        notebook_rel="nb.py",
        **kw,
    )


def _drain(q, until="idle", timeout=3):
    kinds = []
    while True:
        ev = q.get(timeout=timeout)
        kinds.append((ev.kind, ev.data))
        if ev.kind == until:
            return kinds


def test_streams_delta_message_idle(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    try:
        q = sess.subscribe()
        sess.send("hi")
        kinds = [k for k, _ in _drain(q)]
        assert kinds == ["delta", "delta", "message", "idle"]
    finally:
        sess.close()


def test_live_schema_refresh_prepended_only_on_change(fake_sdk, tmp_path):
    # The per-turn live-schema refresh reaches the SDK as a turn PREFIX, but only
    # when the kernel's dataframes changed since the model last saw them.
    sess = _make(tmp_path).start()
    try:
        snapshot = "`orders` (10 rows):\n- id: Int64"
        sess.set_initial_live_schema(snapshot)  # already folded into the system context
        client = FakeClient.last
        assert client is not None and client.session is not None
        sent = client.session.sent

        # Same as the open-time snapshot -> no prefix, just the analyst's turn.
        sess.send("hi", live_schema_text=snapshot)
        assert sent[-1] == "hi"

        # A new dataframe appears -> the refreshed schema is prepended to the turn.
        grown = snapshot + "\n`flags`:\n- ok: Boolean"
        sess.send("now?", live_schema_text=grown)
        assert sent[-1].startswith("UPDATED LIVE NOTEBOOK DATAFRAMES")
        assert "flags" in sent[-1] and sent[-1].endswith("now?")

        # Unchanged kernel -> no re-injection.
        sess.send("again", live_schema_text=grown)
        assert sent[-1] == "again"
    finally:
        sess.close()


def test_tool_and_intent_events(fake_sdk, tmp_path):
    FakeSession.SCRIPT = [
        (ET.ASSISTANT_INTENT, {"intent": "Aggregate sales by region"}),
        (ET.TOOL_EXECUTION_START, {"tool_name": "mooring_get_schema", "arguments": {}}),
        (ET.TOOL_EXECUTION_PROGRESS, {"progress_message": "reading footer", "tool_call_id": "c1"}),
        (ET.TOOL_EXECUTION_COMPLETE, {"success": True, "tool_call_id": "c1"}),
        (ET.ASSISTANT_MESSAGE, {"content": "done"}),
        (ET.SESSION_IDLE, {"aborted": False}),
    ]
    sess = _make(tmp_path).start()
    try:
        q = sess.subscribe()
        sess.send("group sales by region")
        events = _drain(q)
        kinds = [k for k, _ in events]
        assert kinds[0] == "intent"
        assert ("tool", {"name": "mooring_get_schema"}) in events
        assert ("tool", {"progress": "reading footer"}) in events
        assert ("tool_done", {"success": True}) in events
    finally:
        sess.close()


def test_reasoning_effort_passed_through(fake_sdk, tmp_path):
    sess = _make(tmp_path, reasoning_effort="high").start()
    try:
        client = FakeClient.last
        assert client is not None and client.session is not None
        assert client.session.create_kwargs["reasoning_effort"] == "high"
    finally:
        sess.close()


def test_no_reasoning_effort_by_default(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    try:
        client = FakeClient.last
        assert client is not None and client.session is not None
        assert "reasoning_effort" not in client.session.create_kwargs
    finally:
        sess.close()


@pytest.mark.parametrize("sentinel", ["default", "Default", "auto", " AUTO ", "  "])
def test_the_default_effort_sentinel_is_not_forwarded_to_the_sdk(fake_sdk, tmp_path, sentinel):
    # The effort picker offers "default" to mean "leave it to the model", and the hub's
    # ai.reasoning_effort is a FREE-TEXT settings field, so the word reaches this ctor as
    # an ordinary string. Un-normalised it rides on as a LITERAL reasoning_effort into
    # create_session — a value the SDK never defined. Normalising in __init__ covers
    # every route into the session at once, so no caller can forget it.
    sess = _make(tmp_path, reasoning_effort=sentinel).start()
    try:
        client = FakeClient.last
        assert client is not None and client.session is not None
        assert "reasoning_effort" not in client.session.create_kwargs
    finally:
        sess.close()


def test_create_session_is_value_blind(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    try:
        client = FakeClient.last
        assert client is not None and client.session is not None
        kw = client.session.create_kwargs
        # only mooring's safe tools: the three value-free reads plus the ONE propose tool
        assert kw["available_tools"] == TOOL_NAMES
        assert kw["streaming"] is True
        assert kw["enable_session_store"] is False
        assert kw["enable_config_discovery"] is False
        assert kw["skip_embedding_retrieval"] is True
        assert kw["enable_file_hooks"] is False
        assert callable(kw["on_permission_request"])  # deny-all backstop
        assert kw["working_directory"]  # isolated dir, no data files
        assert client.kwargs["use_logged_in_user"] is True
    finally:
        sess.close()


def test_proposal_event_is_broadcast(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    try:
        q = sess.subscribe()
        sess._emit_proposal("x = 1", "why")  # what the propose_cell tool calls
        ev = q.get(timeout=2)
        assert ev.kind == "proposal"
        assert ev.data == {"code": "x = 1", "rationale": "why"}
    finally:
        sess.close()


def test_start_raises_on_not_authed(fake_sdk, tmp_path):
    from mooring.ai.base import AINotConnectedError

    FakeClient.authed = False
    # Typed (AINotConnectedError, an AIError) so the hub can offer an in-app sign-in.
    with pytest.raises(AINotConnectedError):
        _make(tmp_path).start()


def test_background_start_not_authed_fails_with_reason(fake_sdk, tmp_path):
    # The non-blocking open path can't raise, so the not-signed-in case must arrive
    # on the stream as a "fail" event carrying reason="not_connected" — the signal the
    # chat UI branches on to show a "Sign in to Copilot" button instead of dead text.
    FakeClient.authed = False
    sess = _make(tmp_path)
    q = sess.subscribe()  # subscribe before start so the live "fail" event is caught
    sess.start(block=False)
    try:
        fail = None
        while True:
            ev = q.get(timeout=3)
            if ev.kind == "fail":
                fail = ev
                break
        assert fail.data.get("reason") == "not_connected"
        assert fail.data.get("text")  # a human-readable message rides along too
        # A late subscriber catches up via the replayed start_status.
        assert sess.start_status["state"] == "error"
        assert sess.start_status["reason"] == "not_connected"
    finally:
        sess.close()


def test_background_start_returns_immediately_then_announces_ready(fake_sdk, tmp_path):
    # block=False returns a still-starting session; readiness arrives over the stream
    # (so the hub need not hold the open request on the Copilot handshake).
    sess = _make(tmp_path)
    assert sess.is_ready() is False  # marked "starting" at construction
    q = sess.subscribe()  # subscribe before start so the live "ready" event is caught
    sess.start(block=False)  # does not block, does not raise
    try:
        kinds = []
        while True:
            ev = q.get(timeout=3)
            kinds.append(ev.kind)
            if ev.kind == "ready":
                break
        assert "ready" in kinds
        assert sess.is_ready() is True
        assert sess.start_status == {"state": "ready"}
    finally:
        sess.close()


def test_background_start_times_out_on_a_hung_handshake(fake_sdk, tmp_path, monkeypatch):
    # A HUNG (not failed) handshake must not leave the session stuck "starting"
    # forever: the loop-thread deadline turns it into a "fail" so the UI recovers.
    import asyncio

    from mooring.ai import session as session_mod

    monkeypatch.setattr(session_mod, "_START_TIMEOUT", 0.3)

    async def _hang(self):  # client.start() never returns within the deadline
        await asyncio.sleep(30)

    monkeypatch.setattr(FakeClient, "start", _hang)
    sess = _make(tmp_path)
    q = sess.subscribe()
    sess.start(block=False)  # returns immediately
    try:
        ev = q.get(timeout=3)
        while ev.kind != "fail":
            ev = q.get(timeout=3)
        assert "timed out" in ev.data["text"].lower()
        assert sess.is_ready() is False
        assert sess.start_status["state"] == "error"
    finally:
        sess.close()


def test_background_start_emits_fail_event_on_not_authed(fake_sdk, tmp_path):
    # A sign-in failure on the background path surfaces as a "fail" event (the open
    # request already returned), NOT a raised exception — and start_status records it.
    FakeClient.authed = False
    sess = _make(tmp_path)
    q = sess.subscribe()
    sess.start(block=False)  # must not raise
    try:
        ev = q.get(timeout=3)
        while ev.kind != "fail":
            ev = q.get(timeout=3)
        assert ev.data.get("text")
        assert sess.is_ready() is False
        assert sess.start_status["state"] == "error"
    finally:
        sess.close()


def test_pii_prompt_is_held_until_confirmed(fake_sdk, tmp_path):
    # With the guard armed (block mode), a PII-shaped prompt must NOT reach the SDK
    # until the analyst confirms — proving the hold is strictly upstream of dispatch.
    sess = _make(tmp_path, pii_enabled=True, pii_block=True).start()
    try:
        q = sess.subscribe()
        client = FakeClient.last
        assert client is not None and client.session is not None
        session = client.session
        sess.send("why does 4012888888881881 fail validation?")
        held = q.get(timeout=2)
        assert held.kind == "pii" and held.data["token"]
        assert session.sent == []  # the SDK was sent nothing

        sess.send_confirmed(held.data["token"])
        kinds = []
        while True:
            ev = q.get(timeout=2)
            kinds.append(ev.kind)
            if ev.kind == "idle":
                break
        assert "message" in kinds and "idle" in kinds
        assert session.sent  # forwarded verbatim, exactly now
    finally:
        sess.close()


def test_traceback_prompt_raw_never_reaches_the_sdk(fake_sdk, tmp_path):
    # With the traceback guard armed, a traceback-bearing prompt is sanitised and
    # HELD — the SDK sees nothing until confirm, and then ONLY the sanitised
    # rewrite: no call sequence can forward the raw paste, because the raw paste
    # is never stored. The workspace frame's source line is re-read from disk.
    secret = "SECRET_VALUE_DO_NOT_LEAK"
    sess = _make(tmp_path, traceback_guard=True).start()
    try:
        q = sess.subscribe()
        client = FakeClient.last
        assert client is not None and client.session is not None
        session = client.session
        sess.send(
            "Traceback (most recent call last):\n"
            f'  File "{tmp_path / "nb.py"}", line 1, in _\n'
            f"    x = load({secret!r})\n"
            f"KeyError: '{secret}'"
        )
        held = q.get(timeout=2)
        assert held.kind == "traceback" and held.data["token"]
        assert session.sent == []  # the SDK was sent nothing while held

        sess.send_confirmed(held.data["token"])
        kinds = []
        while True:
            ev = q.get(timeout=2)
            kinds.append(ev.kind)
            if ev.kind == "idle":
                break
        assert "message" in kinds
        assert len(session.sent) == 1
        forwarded = session.sent[0]
        assert secret not in forwarded  # raw paste never crossed
        assert 'File "nb.py", line 1, in _' in forwarded
        assert "import marimo" in forwarded  # the disk re-read, not the doctored paste
        assert "KeyError: <redacted:" in forwarded
    finally:
        sess.close()


# -- cancellation (the analyst's stop button) -----------------------------------


def test_cancel_raises_the_tool_boundary_flag_and_asks_the_sdk_to_abort(fake_sdk, tmp_path):
    # The SDK drives its own tool loop, so the FLAG is the mechanism that always works:
    # every later tool call comes back as a terminal refusal. session.abort() is a real
    # bonus on top (it ends the completion in flight), so use it when the SDK has it.
    sess = _make(tmp_path).start()
    try:
        q = sess.subscribe()
        session = FakeClient.last.session
        assert sess.cancel_requested() is False
        sess.request_cancel()
        assert sess.cancel_requested() is True
        assert session.aborted == 1
        ev = q.get(timeout=2)
        assert ev.kind == "cancelled" and ev.data["text"] == "(Stopped at your request.)"

        # Idempotent: a second press neither re-announces nor re-aborts.
        sess.request_cancel()
        assert session.aborted == 1
    finally:
        sess.close()


def test_a_missing_sdk_abort_is_not_an_error(fake_sdk, tmp_path, monkeypatch):
    # The SDK is an optional extra whose surface can move; abort is duck-typed and the
    # flag alone still stops the turn.
    monkeypatch.delattr(FakeSession, "abort")
    sess = _make(tmp_path).start()
    try:
        sess.request_cancel()  # must not raise
        assert sess.cancel_requested() is True
    finally:
        sess.close()


def test_a_cancelled_turn_leaves_the_session_usable_and_the_flag_cleared(fake_sdk, tmp_path):
    # The single most important behaviour: one cancel must not poison every later turn
    # (each tool call would refuse to run, on a chat the analyst believes is live).
    sess = _make(tmp_path).start()
    try:
        q = sess.subscribe()
        sess.request_cancel()
        assert sess.cancel_requested() is True

        sess.send("carry on then")
        assert sess.cancel_requested() is False  # re-armed at the start of the turn
        kinds = [k for k, _ in _drain(q)]
        assert kinds[-1] == "idle" and "message" in kinds  # a full, normal turn ran
        assert FakeClient.last.session.sent == ["carry on then"]
    finally:
        sess.close()


def test_closing_the_session_raises_the_stop_flag(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    assert sess.cancel_requested() is False
    sess.close()
    assert sess.cancel_requested() is True


# -- the write capability (edit mode) ------------------------------------------


def _captured_tool_kwargs(tmp_path, monkeypatch, **kw):
    """Build a session, capturing what it hands :func:`mooring.ai.tools.build_tools`.

    Returns ``(captured, session)``; the caller closes it (closing raises the stop
    flag, so the cancel predicate has to be read first)."""
    captured: dict = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("mooring.ai.tools.build_tools", fake_build)
    return captured, _make(tmp_path, **kw).start()


def test_no_applier_leaves_the_write_tool_in_propose_mode(fake_sdk, tmp_path, monkeypatch):
    # `[ai] auto_apply = false` and the policy escape hatch both ride on this path.
    captured, sess = _captured_tool_kwargs(tmp_path, monkeypatch)
    try:
        assert captured["apply_edit"] is None
        assert captured["emit_proposal"] is not None
        assert captured["emit_proposal_patch"] is not None
        assert captured["cancelled"] == sess.cancel_requested
    finally:
        sess.close()


def test_an_applier_is_wired_into_the_write_tool(fake_sdk, tmp_path, monkeypatch):
    captured, sess = _captured_tool_kwargs(
        tmp_path, monkeypatch, applier=lambda ops, why: None
    )
    try:
        assert captured["apply_edit"] == sess._apply_edit
    finally:
        sess.close()


def test_a_read_only_session_gets_no_write_capability_at_all(fake_sdk, tmp_path, monkeypatch):
    # The load-bearing privacy gate, extended to the applier: a read-only investigate
    # sub-agent gets no proposal callbacks AND no way to write, even mis-wired as here.
    captured, sess = _captured_tool_kwargs(
        tmp_path, monkeypatch, read_only=True, applier=lambda ops, why: "never"
    )
    try:
        assert captured["apply_edit"] is None
        assert captured["emit_proposal"] is None
        assert captured["emit_proposal_patch"] is None
        assert sess._applier is None  # dropped at the ctor, not only at the builder
    finally:
        sess.close()


def _outcome(status, **extra):
    return types.SimpleNamespace(status=status, text=f"{status} text", is_error=False, **extra)


def test_an_applied_edit_broadcasts_a_receipt_and_returns_the_outcome(fake_sdk, tmp_path):
    receipt = {"summary": "changed cell 3", "undo": "u1"}
    outcome = _outcome("applied", payload=receipt)
    sess = _make(tmp_path, applier=lambda ops, why: outcome).start()
    try:
        q = sess.subscribe()
        got = sess._apply_edit([{"op": "edit", "index": 3}], "why")
        assert got is outcome  # the model reads the applier's own observation, unchanged
        ev = q.get(timeout=2)
        assert ev.kind == "applied" and ev.data == receipt
        assert ev.data is not receipt  # copied, so a later mutation can't rewrite it
    finally:
        sess.close()


def test_a_held_edit_reuses_the_existing_proposal_card(fake_sdk, tmp_path):
    # A held change must show TODAY's hold card — there is deliberately no second UI.
    patch = {"kind": "patch", "ops": [{"op": "edit"}], "diffs": [], "gate": {"band": "ask"}}
    sess = _make(tmp_path, applier=lambda ops, why: _outcome("held", payload=patch)).start()
    try:
        q = sess.subscribe()
        sess._apply_edit([{"op": "edit"}], "why")
        ev = q.get(timeout=2)
        assert ev.kind == "proposal" and ev.data == patch
    finally:
        sess.close()


@pytest.mark.parametrize("status", ["conflict", "disabled", "cancelled", "error"])
def test_an_unapplied_edit_tells_the_model_and_leaves_the_ui_alone(fake_sdk, tmp_path, status):
    sess = _make(tmp_path, applier=lambda ops, why: _outcome(status, payload={"x": 1})).start()
    try:
        q = sess.subscribe()
        got = sess._apply_edit([{"op": "edit"}], "why")
        assert got.status == status
        with pytest.raises(queue.Empty):  # no card for a change that did not happen
            q.get(timeout=0.2)
    finally:
        sess.close()


def test_a_broken_applier_becomes_a_value_free_error_not_a_crashed_turn(fake_sdk, tmp_path):
    secret = "SECRET_VALUE_DO_NOT_LEAK"

    def boom(ops, why):
        raise RuntimeError(f"exploded on {secret}")

    sess = _make(tmp_path, applier=boom).start()
    try:
        out = sess._apply_edit([{"op": "edit"}], "why")
        assert out.status == "error" and out.is_error is True
        assert secret not in out.text  # the failure is never repeated back to the model
        assert "Nothing was written" in out.text
    finally:
        sess.close()


def test_close_tears_down(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    fake = FakeClient.last
    assert fake is not None and fake.session is not None
    session = fake.session
    sess.close()
    sess._thread.join(timeout=3)
    assert not sess._thread.is_alive()
    assert session.disconnected is True
    assert fake.stopped is True
