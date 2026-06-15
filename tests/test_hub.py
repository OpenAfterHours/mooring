import tomllib

import pytest
from conftest import FakeClient
from starlette.testclient import TestClient

from mooring import config, paths
from mooring.hub import server
from mooring.hub.server import Hub, create_app


@pytest.fixture
def unconfigured_client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    monkeypatch.delenv("MOORING_GITHUB_HOST", raising=False)
    # No client_id, so unconfigured — but with a tmp workspace to keep file
    # endpoints away from the real default workspace folder.
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws"))
    with TestClient(create_app(hub)) as client:
        yield client, hub


def test_index_serves_html(unconfigured_client):
    client, _ = unconfigured_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert "mooring" in resp.text


def test_state_unconfigured(unconfigured_client):
    client, _ = unconfigured_client
    state = client.get("/api/state").json()
    assert state["configured"] is False
    assert state["logged_in"] is False
    assert state["files"] == []


def test_setup_writes_user_config_and_reloads(unconfigured_client):
    client, hub = unconfigured_client
    resp = client.post(
        "/api/setup",
        json={"client_id": "cid", "owner": "acme", "repo": "nbs", "branch": ""},
    )
    assert resp.status_code == 200
    assert paths.user_config_file().is_file()
    assert hub.cfg.repo_slug == "acme/nbs"
    assert hub.cfg.branch == "main"


def test_setup_with_host_persists_normalized(unconfigured_client):
    client, hub = unconfigured_client
    resp = client.post(
        "/api/setup",
        json={"client_id": "cid", "owner": "acme", "repo": "nbs", "host": "https://GHE.Example/"},
    )
    assert resp.status_code == 200
    assert hub.cfg.host == "ghe.example"
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["github"]["host"] == "ghe.example"


def test_setup_with_invalid_host_400s(unconfigured_client):
    client, _ = unconfigured_client
    resp = client.post(
        "/api/setup",
        json={"client_id": "cid", "owner": "acme", "repo": "nbs", "host": "not a host"},
    )
    assert resp.status_code == 400
    assert "Not a valid GitHub host" in resp.json()["error"]


def test_setup_requires_fields(unconfigured_client):
    client, _ = unconfigured_client
    resp = client.post("/api/setup", json={"client_id": "", "owner": "", "repo": ""})
    assert resp.status_code == 400


def test_open_missing_file_404s(unconfigured_client):
    client, _ = unconfigured_client
    resp = client.post("/api/open", json={"path": "notebooks/nope.py"})
    assert resp.status_code == 404


# -- configured hub: repo switching and PBIP artifacts ------------------------------


CONFIG_TEMPLATE = """
[github]
client_id = "cid"

[repos]
active = "team"

[repos.team]
owner = "acme"
repo = "nbs"
workspace = '{ws1}'

[repos.sandbox]
owner = "acme"
repo = "lab"
workspace = '{ws2}'
"""


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    for var in (
        "MOORING_TOKEN",
        "MOORING_CLIENT_ID",
        "MOORING_OWNER",
        "MOORING_REPO",
        "MOORING_BRANCH",
        "MOORING_WORKSPACE",
        "MOORING_ACTIVE_REPO",
        "MOORING_GITHUB_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "appdata").mkdir()
    paths.user_config_file().write_text(
        CONFIG_TEMPLATE.format(ws1=tmp_path / "ws1", ws2=tmp_path / "ws2"), "utf-8"
    )
    fake = FakeClient()
    monkeypatch.setattr(Hub, "client", lambda self: fake)
    monkeypatch.setattr(server.auth, "get_token", lambda host=None: "t")
    hub = Hub(config.load_app_config())
    with TestClient(create_app(hub)) as client:
        yield client, hub, fake, tmp_path


def write_ws(tmp_path, ws, rel_path, text=""):
    target = tmp_path / ws / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8")


def test_state_lists_repos_and_active(configured):
    client, _, _, _ = configured
    state = client.get("/api/state").json()
    assert state["active_repo"] == "team"
    assert state["repo"] == "acme/nbs"
    assert state["host"] == "github.com"
    assert [(r["alias"], r["active"]) for r in state["repos"]] == [
        ("sandbox", False),
        ("team", True),
    ]


def test_repo_switch_persists_and_changes_state(configured):
    client, _, _, _ = configured
    resp = client.post("/api/repo/switch", json={"alias": "sandbox"})
    assert resp.status_code == 200
    state = client.get("/api/state").json()
    assert state["active_repo"] == "sandbox"
    assert state["repo"] == "acme/lab"
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["repos"]["active"] == "sandbox"


def test_repo_switch_unknown_alias_400s(configured):
    client, _, _, _ = configured
    resp = client.post("/api/repo/switch", json={"alias": "nope"})
    assert resp.status_code == 400


def test_repo_remove_keeps_workspace(configured):
    client, _, _, tmp_path = configured
    write_ws(tmp_path, "ws2", "notebooks/keep.py", "x")
    resp = client.post("/api/repo/remove", json={"alias": "sandbox"})
    assert resp.status_code == 200
    state = client.get("/api/state").json()
    assert [r["alias"] for r in state["repos"]] == ["team"]
    assert (tmp_path / "ws2" / "notebooks/keep.py").exists()


def test_setup_adds_second_repo_without_clobbering_first(configured):
    client, _, _, _ = configured
    resp = client.post("/api/setup", json={"owner": "acme", "repo": "extra", "alias": "x"})
    assert resp.status_code == 200
    state = client.get("/api/state").json()
    assert sorted(r["alias"] for r in state["repos"]) == ["sandbox", "team", "x"]
    assert state["active_repo"] == "x"


def test_switch_changes_editor_workspace(configured, monkeypatch):
    client, _, _, tmp_path = configured

    class FakeEditor:
        instances = []

        def __init__(self, workspace):
            self.workspace = workspace
            FakeEditor.instances.append(self)

        def ensure_started(self):
            pass

        def use_uv(self):
            return False

        def url_for(self, rel_path):
            return f"http://editor/{rel_path}"

        def shutdown(self):
            pass

    monkeypatch.setattr(server, "EditorServer", FakeEditor)
    write_ws(tmp_path, "ws1", "notebooks/a.py", "a")
    write_ws(tmp_path, "ws2", "notebooks/b.py", "b")

    assert client.post("/api/open", json={"path": "notebooks/a.py"}).status_code == 200
    client.post("/api/repo/switch", json={"alias": "sandbox"})
    assert client.post("/api/open", json={"path": "notebooks/b.py"}).status_code == 200
    assert [e.workspace for e in FakeEditor.instances] == [
        tmp_path / "ws1",
        tmp_path / "ws2",
    ]


def test_state_groups_pbip_artifacts(configured):
    client, _, _, tmp_path = configured
    write_ws(tmp_path, "ws1", "reports/Sales.pbip", "{}")
    write_ws(tmp_path, "ws1", "reports/Sales.SemanticModel/.platform", "{}")
    write_ws(tmp_path, "ws1", "reports/Sales.SemanticModel/model.tmdl", "m")
    write_ws(tmp_path, "ws1", "notebooks/a.py", "a")
    state = client.get("/api/state").json()
    assert state["logged_in"] is True
    [artifact] = state["artifacts"]
    assert artifact["key"] == "reports/Sales"
    assert artifact["state"] == "modified"  # everything is new local
    assert artifact["to_push"] == 3
    grouped = {f["path"]: f.get("artifact") for f in state["files"]}
    assert grouped["reports/Sales.pbip"] == "reports/Sales"
    assert grouped["notebooks/a.py"] is None


def test_propose_endpoint_and_state_review(configured):
    client, _, fake, tmp_path = configured
    write_ws(tmp_path, "ws1", "notebooks/a.py", "a")
    resp = client.post("/api/propose", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_branch"].startswith("mooring/phil/")
    assert body["compare_url"].startswith("https://github.com/acme/nbs/compare/main...")
    state = client.get("/api/state").json()
    assert [f["state"] for f in state["files"]] == ["in review"]
    assert state["review"]["branch"] == body["review_branch"]
    assert state["review"]["compare_url"] == body["compare_url"]
    assert fake.tree == {}  # nothing reached main


def test_propose_compare_url_on_enterprise_host(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    for var in (
        "MOORING_TOKEN",
        "MOORING_CLIENT_ID",
        "MOORING_OWNER",
        "MOORING_REPO",
        "MOORING_BRANCH",
        "MOORING_WORKSPACE",
        "MOORING_ACTIVE_REPO",
        "MOORING_GITHUB_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "appdata").mkdir()
    paths.user_config_file().write_text(
        CONFIG_TEMPLATE.format(ws1=tmp_path / "ws1", ws2=tmp_path / "ws2").replace(
            'client_id = "cid"', 'client_id = "cid"\nhost = "ghe.example"'
        ),
        "utf-8",
    )
    fake = FakeClient()
    monkeypatch.setattr(Hub, "client", lambda self: fake)
    monkeypatch.setattr(server.auth, "get_token", lambda host=None: "t")
    hub = Hub(config.load_app_config())
    with TestClient(create_app(hub)) as client:
        write_ws(tmp_path, "ws1", "notebooks/a.py", "a")
        body = client.post("/api/propose", json={}).json()
        assert body["compare_url"].startswith("https://ghe.example/acme/nbs/compare/main...")
        state = client.get("/api/state").json()
        assert state["host"] == "ghe.example"
        assert state["review"]["compare_url"] == body["compare_url"]


def test_state_pbip_artifact_fully_proposed_shows_in_review(configured):
    client, _, _, tmp_path = configured
    write_ws(tmp_path, "ws1", "reports/Sales.pbip", "{}")
    client.post("/api/propose", json={})
    state = client.get("/api/state").json()
    [artifact] = state["artifacts"]
    assert artifact["state"] == "in review"
    assert artifact["to_push"] == 0


def test_open_pbip_calls_launch(configured, monkeypatch):
    client, _, _, tmp_path = configured
    write_ws(tmp_path, "ws1", "reports/Sales.pbip", "{}")
    opened = []
    monkeypatch.setattr(server.pbip, "launch", opened.append)
    resp = client.post("/api/open", json={"path": "reports/Sales.pbip"})
    assert resp.status_code == 200
    body = resp.json()
    assert "url" not in body  # nothing for the browser to open
    assert opened == [(tmp_path / "ws1" / "reports/Sales.pbip").resolve()]


def test_open_rejects_traversal(configured, monkeypatch):
    client, _, _, tmp_path = configured
    (tmp_path / "evil.py").write_text("x", "utf-8")
    resp = client.post("/api/open", json={"path": "../evil.py"})
    assert resp.status_code == 400


def test_delete_endpoint_removes_file(configured):
    client, _, _, tmp_path = configured
    write_ws(tmp_path, "ws1", "notebooks/a.py", "a")
    resp = client.post("/api/delete", json={"path": "notebooks/a.py"})
    assert resp.status_code == 200
    assert resp.json()["lines"] == ["deleted notebooks/a.py"]
    assert not (tmp_path / "ws1" / "notebooks/a.py").exists()
    # A never-synced file just disappears (nothing left for the team to remove).
    state = client.get("/api/state").json()
    assert state["files"] == []


def test_delete_endpoint_pbip_artifact(configured):
    client, _, _, tmp_path = configured
    write_ws(tmp_path, "ws1", "reports/Sales.pbip", "{}")
    write_ws(tmp_path, "ws1", "reports/Sales.SemanticModel/model.tmdl", "m")
    resp = client.post("/api/delete", json={"path": "reports/Sales.pbip"})
    assert resp.status_code == 200
    assert not (tmp_path / "ws1" / "reports/Sales.pbip").exists()
    assert not (tmp_path / "ws1" / "reports/Sales.SemanticModel").exists()


def test_delete_endpoint_rejects_traversal(configured):
    client, _, _, tmp_path = configured
    (tmp_path / "evil.py").write_text("x", "utf-8")
    resp = client.post("/api/delete", json={"path": "../evil.py"})
    assert resp.status_code == 400
    assert (tmp_path / "evil.py").exists()


def test_delete_endpoint_missing_404s(configured):
    client, _, _, _ = configured
    resp = client.post("/api/delete", json={"path": "notebooks/nope.py"})
    assert resp.status_code == 404


def test_state_reports_has_local_flag(configured):
    client, _, fake, tmp_path = configured
    write_ws(tmp_path, "ws1", "notebooks/local.py", "a")
    fake.seed("notebooks/remote.py", b"r")  # exists only on the remote
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/local.py"]["has_local"] is True
    assert files["notebooks/remote.py"]["state"] == "new remote"
    assert files["notebooks/remote.py"]["has_local"] is False


# -- AI copilot chat (stub turn + file-write Apply) -------------------------------

# A valid marimo notebook so cellwrite can parse + append a cell on Apply.
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
def stub_chat(monkeypatch):
    """Use the no-LLM stub session so chat tests don't need the Copilot SDK/auth."""
    from mooring.ai.chat import StubChatSession

    monkeypatch.setattr(
        Hub,
        "_make_chat_session",
        lambda self, ctx, ws, nb, **kw: StubChatSession(system_context=ctx),
    )


def _open_chat(client, hub, notebook="nb.py", dataset="", source="import marimo\n"):
    ws = hub.cfg.workspace()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / notebook).write_text(source, "utf-8")
    body = {"notebook": notebook}
    if dataset:
        body["dataset"] = dataset
    resp = client.post("/api/ai/chat/open", json=body)
    return resp


def test_chat_open_context_is_value_free(unconfigured_client, stub_chat):
    import polars as pl

    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "data").mkdir(parents=True, exist_ok=True)
    secret = "SECRET_VALUE_DO_NOT_LEAK"
    pl.DataFrame({"region": [secret], "amount": [123456]}).write_parquet(ws / "data" / "s.parquet")
    resp = _open_chat(client, hub, dataset="data/s.parquet", source="import marimo\n# my code\n")
    assert resp.status_code == 200
    ctx = hub._chats[resp.json()["sid"]].system_context
    assert "region" in ctx and "amount" in ctx  # schema column names present
    assert "import marimo" in ctx  # notebook source present
    assert secret not in ctx and "123456" not in ctx  # data VALUES never present


def test_chat_open_rejects_traversal(unconfigured_client):
    client, hub = unconfigured_client
    hub.cfg.workspace().mkdir(parents=True, exist_ok=True)
    resp = client.post("/api/ai/chat/open", json={"notebook": "../escape.py"})
    assert resp.status_code == 400


def test_chat_send_streams_events(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid = _open_chat(client, hub).json()["sid"]
    q = hub._chats[sid].subscribe()  # subscribe before sending, like the SSE client
    assert client.post("/api/ai/chat/send", json={"sid": sid, "text": "hi"}).json()["ok"]
    kinds = []
    while True:
        ev = q.get(timeout=2)
        kinds.append(ev.kind)
        if ev.kind == "idle":
            break
    assert "delta" in kinds and "message" in kinds and "proposal" in kinds


def test_chat_apply_writes_cell_into_notebook(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    resp = client.post("/api/ai/chat/apply", json={"sid": sid, "code": "result = 41 + 1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # The cell was written into the .py source (marimo --watch shows it in the tab).
    nb = (hub.cfg.workspace() / "nb.py").read_text("utf-8")
    assert "result = 41 + 1" in nb
    assert "﻿" not in nb  # no BOM (the marimo parser rejects it)


def test_chat_apply_rejects_empty_code(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    resp = client.post("/api/ai/chat/apply", json={"sid": sid, "code": "   "})
    assert resp.status_code == 400


def test_chat_apply_unknown_sid_404(unconfigured_client, stub_chat):
    client, _ = unconfigured_client
    resp = client.post("/api/ai/chat/apply", json={"sid": "nope", "code": "x = 1"})
    assert resp.status_code == 404


def test_chat_stream_emits_sse_frames(unconfigured_client, monkeypatch):
    from mooring.ai.chat import ChatBroadcaster, ChatEvent

    class QuickSession(ChatBroadcaster):
        def subscribe(self):
            import queue as _q

            qq = _q.Queue()
            for ev in (
                ChatEvent("delta", {"text": "hi "}),
                ChatEvent("message", {"text": "hi"}),
                ChatEvent("proposal", {"code": "x=1"}),
                ChatEvent("idle"),
                ChatEvent("closed"),
            ):
                qq.put(ev)
            return qq

        def send(self, text):
            pass

    client, hub = unconfigured_client
    monkeypatch.setattr(Hub, "_make_chat_session", lambda self, *a, **k: QuickSession())
    sid = _open_chat(client, hub).json()["sid"]
    resp = client.get(f"/api/ai/chat/stream/{sid}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "event: delta" in body
    assert "event: proposal" in body
    assert "event: closed" in body


def test_chat_stream_unknown_sid_404(unconfigured_client):
    client, _ = unconfigured_client
    assert client.get("/api/ai/chat/stream/nope").status_code == 404


def test_chat_disabled_when_ai_off(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws", ai_enabled=False))
    with TestClient(create_app(hub)) as client:
        assert client.get("/ai/chat").status_code == 404
        assert client.post("/api/ai/chat/open", json={"notebook": "nb.py"}).status_code == 404
        assert client.get("/api/state").json()["ai_chat"] is False


# -- AI copilot model/effort controls --------------------------------------------


class _FakeModelProvider:
    def list_models(self, force=False):
        return [
            {"id": "auto", "name": "Auto", "efforts": [], "default_effort": "", "multiplier": None},
            {
                "id": "claude-opus-4.8",
                "name": "Claude Opus 4.8",
                "efforts": ["low", "high", "max"],
                "default_effort": "medium",
                "multiplier": 1,
            },
        ]


def test_chat_models_lists_models(unconfigured_client, monkeypatch):
    client, _ = unconfigured_client
    monkeypatch.setattr("mooring.ai.get_provider", lambda app_cfg: _FakeModelProvider())
    resp = client.get("/api/ai/models")
    assert resp.status_code == 200
    data = resp.json()
    assert [m["id"] for m in data["models"]] == ["auto", "claude-opus-4.8"]
    assert data["models"][1]["efforts"] == ["low", "high", "max"]
    assert "default_model" in data and "default_effort" in data


def test_chat_models_disabled_when_ai_off(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws", ai_enabled=False))
    with TestClient(create_app(hub)) as client:
        assert client.get("/api/ai/models").status_code == 404


def test_chat_open_threads_model_and_effort(unconfigured_client, monkeypatch):
    client, hub = unconfigured_client
    seen = {}

    def fake_make(self, ctx, ws, nb, model="", reasoning_effort=None):
        from mooring.ai.chat import StubChatSession

        seen["model"] = model
        seen["effort"] = reasoning_effort
        return StubChatSession(system_context=ctx)

    monkeypatch.setattr(Hub, "_make_chat_session", fake_make)
    _open_chat(client, hub, source=_NB_SRC)  # default body: no model/effort
    client.post(
        "/api/ai/chat/open",
        json={"notebook": "nb.py", "model": "claude-opus-4.8", "reasoning_effort": "high"},
    )
    assert seen == {"model": "claude-opus-4.8", "effort": "high"}
