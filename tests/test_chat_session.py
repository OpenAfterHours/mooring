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


class FakeSession:
    def __init__(self, create_kwargs):
        self.create_kwargs = create_kwargs
        self._handler = None
        self.disconnected = False

    def on(self, handler):
        self._handler = handler
        return lambda: None

    async def send(self, prompt, **kw):
        # Drive the streaming handler exactly like the real SDK would.
        self._handler(_event(ET.ASSISTANT_MESSAGE_DELTA, delta_content="Hel"))
        self._handler(_event(ET.ASSISTANT_MESSAGE_DELTA, delta_content="lo"))
        self._handler(_event(ET.ASSISTANT_MESSAGE, content="Hello"))
        self._handler(_event(ET.SESSION_IDLE, aborted=False))
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
    FakeClient.last = None
    FakeClient.authed = True


def _make(tmp_path):
    (tmp_path / "nb.py").write_text("import marimo\n", "utf-8")
    return CopilotChatSession(
        model="", system_context="CTX", workspace=tmp_path, folders=("data",), notebook_rel="nb.py"
    )


def test_streams_delta_message_idle(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    try:
        q = sess.subscribe()
        sess.send("hi")
        kinds = []
        while True:
            ev = q.get(timeout=3)
            kinds.append(ev.kind)
            if ev.kind == "idle":
                break
        assert kinds == ["delta", "delta", "message", "idle"]
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


def test_close_tears_down(fake_sdk, tmp_path):
    sess = _make(tmp_path).start()
    fake = FakeClient.last
    sess.close()
    sess._thread.join(timeout=3)
    assert not sess._thread.is_alive()
    assert fake.session.disconnected is True
    assert fake.stopped is True
