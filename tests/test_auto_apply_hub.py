"""The hop that turns the edit loop on, and the control that ends it.

``ai/`` cannot build the applier — it sits BELOW ``app/`` — so a chat opens in propose
mode unless the hub injects one. That makes this wiring load-bearing rather than
cosmetic: with it missing, everything in ``test_auto_apply.py`` still passes and the
feature is entirely off. These pin the three moving parts of it — the applier is built
and registered at open (and deliberately NOT built in manual mode), a send starts a new
turn, and Cancel reaches the session.

Registration is checked by USING the applier the hub built, not by looking at it.
``_make_applier`` hands ``auto_apply.make_applier`` four things, and every one of them
fails SILENTLY when it is wrong: a dead ``editor_fn`` degrades the whole feature to
"could not see"; a wrong ``notebook_rel`` aims every write at a file that does not
exist; a broken ``cfg_fn`` drops the automatic run report; an applier that is never
bound never notices Cancel. None of that raises, and ``applier is not None`` is true in
all four cases — so each is pinned below by a write that has to land, be watched, be
reported and be stoppable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mooring import config, notebook_undo, paths
from mooring.ai import introspect
from mooring.ai.chat import StubChatSession
from mooring.app import run_report
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
_CLEAN_CELL = "total = 41 + 1\n"
_SECOND_CELL = "doubled = 2\n"


def _append(code: str) -> list[dict]:
    return [{"op": "append", "code": code}]


class _FakeEditor:
    """A stand-in for the workspace's live marimo ``EditorServer`` — identity is the
    whole point, so it only has to be recognisable when it reaches the observation."""

    running = True
    port = 65535
    token = "editor-token"


@pytest.fixture
def hub_client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    # The applier re-reads all three knobs off DISK at every write, so an env var in the
    # developer's shell would decide what these exercise.
    for var in ("MOORING_AI_AUTO_APPLY", "MOORING_AI_AUTO_RUN_REPORT", "MOORING_AI_APPLY_GUARD"):
        monkeypatch.delenv(var, raising=False)
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


def test_the_applier_the_hub_built_writes_THIS_chats_notebook(hub_client, stub_sessions):
    """The write has to land, on the right file, through the hub's ONE guard.

    ``notebook_rel`` is the argument nothing else would notice: point it at a file that
    does not exist and every write comes back a polite ``error`` — the model is told
    "the notebook could not be opened" and no test that only looks at the wiring can
    tell the difference.
    """
    client, hub, ws = hub_client
    (ws / "other.py").write_text(_NB_SRC, "utf-8", newline="\n")
    other_before = (ws / "other.py").read_bytes()
    _open(client)

    outcome = stub_sessions["applier"](_append(_CLEAN_CELL), "add a total")

    assert outcome.status == "applied", outcome.text
    assert "total" in (ws / "nb.py").read_text("utf-8")
    assert (ws / "other.py").read_bytes() == other_before  # only the chat's own notebook
    # ...and on the hub's own ApplyGuard, so this write serialises with a manual Apply,
    # an Undo and a sync rollback rather than racing them on a private lock.
    turn = hub.apply._turn_checkpoints[(str(ws), "nb.py")]
    assert turn[0] == stub_sessions["applier"].turn_id
    assert notebook_undo.depth(ws, "nb.py") == 1
    assert hub.apply.restore_undo(ws / "nb.py", ws, "nb.py") == 0
    assert (ws / "nb.py").read_text("utf-8") == _NB_SRC


def test_the_live_editor_reaches_the_observation_and_is_re_read_every_write(
    hub_client, stub_sessions, monkeypatch
):
    """``editor_fn`` is the half of the feature that reads the change back. Hand the
    applier a lambda that always answers ``None`` and every write still succeeds — the
    model is simply told, forever, that mooring could not see it run.

    Looked up per WRITE, not captured at open: an analyst who opens the notebook in the
    middle of a chat must be seen by the next write.
    """
    seen: list = []

    def spy(editor, notebook_rel, expect_names, **kw):
        seen.append((editor, notebook_rel))
        return introspect.Observation(observed=editor is not None, present=tuple(expect_names))

    monkeypatch.setattr(introspect, "observe", spy)
    client, hub, ws = hub_client
    _open(client)
    applier = stub_sessions["applier"]

    applier(_append(_CLEAN_CELL), "")  # nothing open yet
    editor = _FakeEditor()
    hub.editors[str(ws)] = editor  # the analyst opens the notebook mid-chat
    second = applier(_append(_SECOND_CELL), "")

    assert [e for e, _ in seen] == [None, editor]
    assert {nb for _, nb in seen} == {"nb.py"}
    assert "`doubled` is bound" in second.text  # what it saw reached the model


def test_the_hubs_live_config_reaches_the_automatic_run_report(
    hub_client, stub_sessions, monkeypatch
):
    """``cfg_fn`` is only read on the run-report path, and that path swallows every
    exception by design — so a broken one loses the report in silence. It has to be the
    hub's CURRENT config (the hub reloads in place), pointed at this workspace."""
    seen: dict = {}

    def fake_run(session, cfg, notebook_rel, *, cancel=None):
        seen["cfg"] = cfg
        seen["notebook_rel"] = notebook_rel
        return run_report.RunReport(ran_clean=False, cells_failed=1, sent="cell 1 did not run")

    monkeypatch.setattr(run_report, "run_and_collect", fake_run)
    # The report fires only where the observation already said a name is NOT bound.
    monkeypatch.setattr(
        introspect,
        "observe",
        lambda *a, **k: introspect.Observation(observed=True, missing=("total",)),
    )
    client, _hub, ws = hub_client
    _open(client)

    outcome = stub_sessions["applier"](_append(_CLEAN_CELL), "")

    assert outcome.status == "applied"
    assert "cell 1 did not run" in outcome.text  # it reached the model
    assert seen.get("cfg") is not None, "the run report never got a config"
    assert Path(seen["cfg"].workspace_path) == ws
    assert seen["notebook_rel"] == "nb.py"


def test_cancelling_the_chat_stops_the_appliers_next_write(hub_client, stub_sessions):
    """The bind, checked through the behaviour it exists for: an applier that was never
    handed its session cannot see the analyst's stop, and writes straight through it."""
    client, hub, ws = hub_client
    sid = _open(client)
    before = (ws / "nb.py").read_bytes()
    assert client.post("/api/ai/chat/cancel", json={"sid": sid}).status_code == 200

    outcome = stub_sessions["applier"](_append(_CLEAN_CELL), "")

    assert outcome.status == "cancelled"
    assert (ws / "nb.py").read_bytes() == before
    assert notebook_undo.depth(ws, "nb.py") == 0


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


def test_the_open_response_says_which_mode_the_chat_is_in(hub_client, stub_sessions):
    """``[ai] auto_apply`` defaults ON, so without this the page has no way to tell the
    analyst that the copilot now writes for itself — their copilot simply stops asking
    and the first evidence is a receipt for a change that has already landed and run.

    Read off the APPLIER, not the config, so it cannot disagree with what the session
    will actually do: no applier IS manual mode.
    """
    client, hub, _ws = hub_client

    body = client.post("/api/ai/chat/open", json={"notebook": "nb.py"}).json()

    assert body["auto_apply"] is True
    assert stub_sessions["applier"] is not None


def test_the_open_response_says_so_in_manual_mode_too(hub_client, stub_sessions):
    client, hub, _ws = hub_client
    hub.app_cfg = config.AppConfig(
        repos=hub.app_cfg.repos,
        active_alias=hub.app_cfg.active_alias,
        ai=config.AiConfig(auto_apply=False),
    )

    body = client.post("/api/ai/chat/open", json={"notebook": "nb.py"}).json()

    assert body["auto_apply"] is False
    assert stub_sessions["applier"] is None


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
    # Forwarded on BOTH backends. The Copilot SDK drives its own tool loop, so there is
    # no loop here to bound — but mooring owns the tool boundary that loop calls through
    # and the ceiling is enforced there, which is the only runaway bound this backend
    # has. One number meaning the same thing on each backend is the point.
    assert seen["max_tool_iters"] == 9


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


def test_a_send_that_never_happens_does_not_rotate_the_turn(hub_client, stub_sessions):
    """``begin_turn`` runs on the paths that actually FORWARD text, not before the
    checks.

    It used to be minted up front, so anything that stopped the send still rotated the
    turn id. The empty message is the harmless version; the one that costs something is a
    second message typed while the assistant is still working, which a routed session
    refuses outright — the running turn's next write then no longer matched the
    checkpoint map, took a snapshot of its own, and "revert what the assistant just did"
    put back only part of it.
    """
    client, hub, _ws = hub_client
    sid = _open(client)
    before = stub_sessions["applier"].turn_id

    assert client.post("/api/ai/chat/send", json={"sid": sid, "text": " "}).status_code == 400

    assert stub_sessions["applier"].turn_id == before


def test_the_pii_holds_confirm_opens_a_turn_of_its_own(hub_client, stub_sessions):
    """Both paths that forward text mint a turn — the confirm branch returns before the
    ordinary send is ever reached, so moving the call down must not skip it. A confirmed
    prompt that wrote into the PREVIOUS turn's checkpoint would fold two separate pieces
    of work into one Revert."""
    client, hub, _ws = hub_client
    sid = _open(client)
    session = hub.chat.get(sid)
    session.send_confirmed = lambda *_a, **_kw: None
    before = stub_sessions["applier"].turn_id

    resp = client.post("/api/ai/chat/send", json={"sid": sid, "confirm_token": "tok"})

    assert resp.status_code == 200
    assert stub_sessions["applier"].turn_id not in ("", before)


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
