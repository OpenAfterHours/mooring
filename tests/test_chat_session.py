"""CopilotChatSession on its loop thread, driven by a fake SDK (no real auth).

We monkeypatch ``copilot.CopilotClient`` so the session builds against a fake
client/session whose ``send`` drives the registered ``on`` handler with scripted
events — exercising the loop-thread + queue→event mapping + the value-blind
create_session config, without the Copilot CLI or a GitHub login.
"""

from __future__ import annotations

import types

import copilot
import pytest
from copilot import SessionEventType as ET

from mooring.ai.base import AIError
from mooring.ai.session import CopilotChatSession
from mooring.ai.tools import TOOL_NAMES


def _event(etype, **data):
    return types.SimpleNamespace(type=etype, data=types.SimpleNamespace(**data))


# A scripted turn: (SessionEventType, data kwargs). Tests can override.
BASIC_TURN = [
    (ET.ASSISTANT_MESSAGE_DELTA, {"delta_content": "Hel"}),
    (ET.ASSISTANT_MESSAGE_DELTA, {"delta_content": "lo"}),
    (ET.ASSISTANT_MESSAGE, {"content": "Hello"}),
    (ET.SESSION_IDLE, {"aborted": False}),
]


class FakeSession:
    SCRIPT = BASIC_TURN

    def __init__(self, create_kwargs):
        self.create_kwargs = create_kwargs
        self._handler = None
        self.disconnected = False
        self.sent = []  # prompts actually forwarded to the SDK

    def on(self, handler):
        self._handler = handler
        return lambda: None

    async def send(self, prompt, **kw):
        self.sent.append(prompt)
        # Drive the streaming handler exactly like the real SDK would.
        for etype, data in type(self).SCRIPT:
            self._handler(_event(etype, **data))
        return "turn-1"

    async def disconnect(self):
        self.disconnected = True


class FakeClient:
    last = None
    authed = True

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = self.stopped = False
        self.session = None
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
        sent = FakeClient.last.session.sent

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
        assert FakeClient.last.session.create_kwargs["reasoning_effort"] == "high"
    finally:
        sess.close()


def test_no_reasoning_effort_by_default(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    try:
        assert "reasoning_effort" not in FakeClient.last.session.create_kwargs
    finally:
        sess.close()


def test_create_session_is_value_blind(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    try:
        kw = FakeClient.last.session.create_kwargs
        assert kw["available_tools"] == TOOL_NAMES  # only mooring's safe tools
        assert kw["streaming"] is True
        assert kw["enable_session_store"] is False
        assert kw["enable_config_discovery"] is False
        assert kw["skip_embedding_retrieval"] is True
        assert kw["enable_file_hooks"] is False
        assert callable(kw["on_permission_request"])  # deny-all backstop
        assert kw["working_directory"]  # isolated dir, no data files
        assert FakeClient.last.kwargs["use_logged_in_user"] is True
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
    FakeClient.authed = False
    with pytest.raises(AIError):
        _make(tmp_path).start()


def test_pii_prompt_is_held_until_confirmed(fake_sdk, tmp_path):
    # With the guard armed (block mode), a PII-shaped prompt must NOT reach the SDK
    # until the analyst confirms — proving the hold is strictly upstream of dispatch.
    sess = _make(tmp_path, pii_enabled=True, pii_block=True).start()
    try:
        q = sess.subscribe()
        sess.send("why does 4012888888881881 fail validation?")
        held = q.get(timeout=2)
        assert held.kind == "pii" and held.data["token"]
        assert FakeClient.last.session.sent == []  # the SDK was sent nothing

        sess.send_confirmed(held.data["token"])
        kinds = []
        while True:
            ev = q.get(timeout=2)
            kinds.append(ev.kind)
            if ev.kind == "idle":
                break
        assert "message" in kinds and "idle" in kinds
        assert FakeClient.last.session.sent  # forwarded verbatim, exactly now
    finally:
        sess.close()


def test_close_tears_down(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    fake = FakeClient.last
    sess.close()
    sess._thread.join(timeout=3)
    assert not sess._thread.is_alive()
    assert fake.session.disconnected is True
    assert fake.stopped is True
