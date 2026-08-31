"""The hop that turns the edit loop on, and the control that ends it.

``ai/`` cannot build the applier — it sits BELOW ``app/`` — so a chat opens in propose
mode unless the hub injects one. That makes this wiring load-bearing rather than
cosmetic: with it missing, everything in ``test_auto_apply.py`` still passes and the
feature is entirely off. These pin the three moving parts of it — the applier is built
and registered at open (and deliberately NOT built in manual mode), a send starts a new
turn, and Cancel reaches the session.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from mooring import config, paths
from mooring.ai.chat import StubChatSession
from mooring.hub.server import Hub, create_app

_NB_SRC = (
    "import marimo\n\n"
    '__generated_with = "0.23.9"\n'
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    seed = 1\n"
    "    return (seed,)\n\n\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)


@pytest.fixture
def hub_client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    monkeypatch.delenv("MOORING_AI_AUTO_APPLY", raising=False)
    (tmp_path / "appdata").mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "nb.py").write_text(_NB_SRC, "utf-8", newline="\n")
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(ws))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws"))
    with TestClient(create_app(hub)) as client:
        yield client, hub, ws


@pytest.fixture
def stub_sessions(monkeypatch):
    """Record what the open route hands the session factory."""
    seen: dict = {}

    def fake_make(self, ctx, ws, nb, **kw):
        seen["applier"] = kw.get("applier")
        return StubChatSession(system_context=ctx)

    monkeypatch.setattr(Hub, "_make_chat_session", fake_make)
    return seen


def _open(client) -> str:
    return client.post("/api/ai/chat/open", json={"notebook": "nb.py"}).json()["sid"]


# -- the injection ------------------------------------------------------------


def test_opening_a_chat_wires_the_applier_and_registers_it(hub_client, stub_sessions):
    client, hub, _ws = hub_client
    sid = _open(client)

    applier = stub_sessions["applier"]
    assert applier is not None  # edit mode: the write tool gets a write-through
    assert hub.chat._appliers[sid] is applier
    # Bound to the session it was built FOR, so its cancel check answers for this chat.
    assert applier._session is hub.chat.get(sid)


def test_manual_mode_registers_no_write_through_at_all(hub_client, stub_sessions):
    """``auto_apply = false`` is structural, not a flag the tool has to remember: with
    no applier the write tool is never built in edit mode in the first place."""
    client, hub, _ws = hub_client
    hub.app_cfg = config.AppConfig(
        repos=hub.app_cfg.repos,
        active_alias=hub.app_cfg.active_alias,
        ai=config.AiConfig(auto_apply=False),
    )
    sid = _open(client)

    assert stub_sessions["applier"] is None
    assert sid not in hub.chat._appliers
    assert hub.chat.begin_turn(sid) == ""  # nothing to tell; not an error either


def test_closing_a_chat_drops_its_applier(hub_client, stub_sessions):
    client, hub, _ws = hub_client
    sid = _open(client)
    hub.chat.close(sid)
    assert sid not in hub.chat._appliers


def test_the_hub_hands_both_new_arguments_to_the_provider(hub_client, monkeypatch):
    """The provider hop. ``ai/`` may not reach up to ``app/`` for an applier, nor down
    to ``config`` for a ceiling, so both are pushed in from here — and if they are not,
    the session opens in propose mode with a default ceiling and nothing says so."""
    seen: dict = {}

    class _Recorder:
        name = "recorder"

        def open_chat(self, **kw):
            seen.update(kw)
            return StubChatSession(system_context=kw["system_context"])

    client, hub, ws = hub_client
    monkeypatch.setattr(Hub, "_provider_for", lambda self: _Recorder())
    hub.app_cfg = config.AppConfig(
        repos=hub.app_cfg.repos,
        active_alias=hub.app_cfg.active_alias,
        ai=config.AiConfig(max_tool_iters=137),
    )
    applier = hub._make_applier(ws, "nb.py")

    hub._make_chat_session("ctx", ws, "nb.py", applier=applier)

    assert seen["applier"] is applier
    assert seen["max_tool_iters"] == 137


def test_the_copilot_provider_forwards_the_applier_to_its_session(monkeypatch, tmp_path):
    from mooring.ai import session as session_mod
    from mooring.ai.copilot import CopilotProvider

    seen: dict = {}

    class _Recorder:
        def __init__(self, **kw):
            seen.update(kw)

        def start(self, *a, **k):
            return None

    monkeypatch.setattr(session_mod, "CopilotChatSession", _Recorder)
    provider = CopilotProvider()
    monkeypatch.setattr(provider, "available", lambda: True)

    sentinel = object()
    provider.open_chat(
        system_context="",
        workspace=tmp_path,
        folders=(),
        notebook_rel="nb.py",
        applier=sentinel,
        max_tool_iters=9,
    )

    assert seen["applier"] is sentinel
    # Not forwarded on purpose: the Copilot SDK drives its own tool loop, so there is no
    # loop here to bound. Pinned so the omission reads as a decision.
    assert "max_tool_iters" not in seen


def test_the_openai_provider_forwards_both(monkeypatch, tmp_path):
    from mooring.ai import openai_session as session_mod
    from mooring.ai.openai_provider import OpenAIProvider

    seen: dict = {}

    class _Recorder:
        def __init__(self, **kw):
            seen.update(kw)

        def start(self, *a, **k):
            return None

    monkeypatch.setattr(session_mod, "OpenAIChatSession", _Recorder)
    provider = OpenAIProvider()
    monkeypatch.setattr(provider, "available", lambda: True)

    sentinel = object()
    provider.open_chat(
        system_context="",
        workspace=tmp_path,
        folders=(),
        notebook_rel="nb.py",
        applier=sentinel,
        max_tool_iters=9,
    )

    assert seen["applier"] is sentinel
    assert seen["max_tool_iters"] == 9  # this backend DOES own its loop


# -- the turn boundary --------------------------------------------------------


def test_sending_starts_a_new_turn(hub_client, stub_sessions):
    """A turn starts when the analyst sends — nothing else does — so this is where the
    undo checkpoint and the receipt group are re-opened."""
    client, hub, _ws = hub_client
    sid = _open(client)
    applier = stub_sessions["applier"]
    first = applier.turn_id

    assert client.post("/api/ai/chat/send", json={"sid": sid, "text": "hello"}).status_code == 200
    second = applier.turn_id
    assert second and second != first

    client.post("/api/ai/chat/send", json={"sid": sid, "text": "again"})
    assert applier.turn_id not in ("", first, second)


def test_an_empty_send_is_refused_before_it_can_burn_a_turn_id(hub_client, stub_sessions):
    # Not a correctness requirement, just a note on ordering: begin_turn runs before the
    # text check, so a rejected send does open a new (unused) turn. Harmless — an empty
    # turn writes nothing — and pinned so the ordering is a decision, not an accident.
    client, hub, _ws = hub_client
    sid = _open(client)
    before = stub_sessions["applier"].turn_id
    assert client.post("/api/ai/chat/send", json={"sid": sid, "text": " "}).status_code == 400
    assert stub_sessions["applier"].turn_id != before


# -- the stop -----------------------------------------------------------------


def test_cancel_reaches_the_session(hub_client, stub_sessions):
    client, hub, _ws = hub_client
    sid = _open(client)
    session = hub.chat.get(sid)
    assert session.cancel_requested() is False

    resp = client.post("/api/ai/chat/cancel", json={"sid": sid})

    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert session.cancel_requested() is True


def test_cancel_on_an_unknown_session_is_a_404_not_a_crash(hub_client, stub_sessions):
    client, _hub, _ws = hub_client
    assert client.post("/api/ai/chat/cancel", json={"sid": "nope"}).status_code == 404


def test_cancel_is_not_gated_by_the_per_notebook_opt_out(hub_client, stub_sessions):
    """Stopping can only ever do LESS, so it is refused for nothing. A Cancel that could
    be turned down would leave a running turn with no way out."""
    from mooring import workspace_config

    client, hub, ws = hub_client
    sid = _open(client)
    (ws / workspace_config.WORKSPACE_CONFIG_NAME).write_text(
        '[ai]\ndisabled_notebooks = ["nb.py"]\n', "utf-8", newline="\n"
    )

    resp = client.post("/api/ai/chat/cancel", json={"sid": sid})

    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert hub.chat.get(sid).cancel_requested() is True
