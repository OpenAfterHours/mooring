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
    # endpoints away from the real Documents folder.
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
    for var in ("MOORING_TOKEN", "MOORING_CLIENT_ID", "MOORING_OWNER", "MOORING_REPO",
                "MOORING_BRANCH", "MOORING_WORKSPACE", "MOORING_ACTIVE_REPO",
                "MOORING_GITHUB_HOST"):
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
    for var in ("MOORING_TOKEN", "MOORING_CLIENT_ID", "MOORING_OWNER", "MOORING_REPO",
                "MOORING_BRANCH", "MOORING_WORKSPACE", "MOORING_ACTIVE_REPO",
                "MOORING_GITHUB_HOST"):
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
