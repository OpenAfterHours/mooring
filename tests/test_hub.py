import tomllib

import pytest
from conftest import FakeClient
from starlette.testclient import TestClient

from mooring import config, paths, reveal, workspace_config
from mooring.hub import server
from mooring.hub.server import Hub, create_app


@pytest.fixture
def unconfigured_client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    monkeypatch.delenv("MOORING_GITHUB_HOST", raising=False)
    # No client_id, so unconfigured â€” but with a tmp workspace to keep file
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


def test_state_includes_ui_theme(unconfigured_client):
    client, _ = unconfigured_client
    assert client.get("/api/state").json()["ui_theme"] == "system"  # default


def test_index_inlines_the_default_theme(unconfigured_client):
    # The pre-paint script's server-default fallback is rendered, not the literal
    # token â€” so a fresh browser paints in the configured theme with no flash.
    client, hub = unconfigured_client
    text = client.get("/").text
    assert "__MOORING_DEFAULT_THEME__" not in text
    assert '|| "system"' in text  # the default

    from dataclasses import replace

    hub.app_cfg = replace(hub.app_cfg, ui_theme="dark")
    assert '|| "dark"' in client.get("/").text


def test_set_theme_persists_and_state_reflects(unconfigured_client):
    client, hub = unconfigured_client
    resp = client.post("/api/ui/theme", json={"theme": "dark"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "theme": "dark"}
    assert hub.app_cfg.ui_theme == "dark"  # live config updated (no full reload)
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["ui"]["theme"] == "dark"  # persisted to the user config
    assert client.get("/api/state").json()["ui_theme"] == "dark"


def test_set_theme_invalid_falls_back_to_system(unconfigured_client):
    client, _ = unconfigured_client
    assert client.post("/api/ui/theme", json={"theme": "neon"}).json()["theme"] == "system"


def test_set_theme_rethemes_running_editor_marimo_config(unconfigured_client):
    client, hub = unconfigured_client
    from mooring.editor import EditorServer

    ws = hub.cfg.workspace()
    ws.mkdir(parents=True, exist_ok=True)
    hub.editors[str(ws)] = EditorServer(ws, theme="light")  # an open editor
    client.post("/api/ui/theme", json={"theme": "dark"})
    data = tomllib.loads((ws / ".marimo.toml").read_text("utf-8"))
    assert data["display"]["theme"] == "dark"  # notebooks follow the hub theme


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


# -- local (no-repo) mode: notebooks usable with no login ---------------------


def test_state_local_mode_lists_notebooks_from_disk(unconfigured_client):
    # With no repo and no token, /api/state reports mode "local" and lists the
    # workspace's notebooks straight off disk (so they can be opened/edited).
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "scratch.py").write_text("import marimo\n", "utf-8")
    state = client.get("/api/state").json()
    assert state["configured"] is False
    assert state["logged_in"] is False
    assert state["mode"] == "local"
    files = {f["path"]: f for f in state["files"]}
    assert files["notebooks/scratch.py"]["state"] == "local"
    assert files["notebooks/scratch.py"]["has_local"] is True


def test_state_local_mode_flags_ai_disabled(unconfigured_client):
    # The per-notebook AI opt-out (synced mooring.toml) is honored in local mode too,
    # so a notebook turned off keeps its AI button hidden with no repo.
    from mooring import workspace_config

    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "a.py").write_text("import marimo\n", "utf-8")
    workspace_config.set_ai_disabled(ws, "notebooks/a.py", True)
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/a.py"].get("ai_disabled") is True


def test_state_flags_shadowing_notebook(unconfigured_client):
    # A notebook whose name shadows an importable package gets a `shadows` field so
    # the front-end can badge it; an innocent sibling does not.
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "polars.py").write_text("import marimo\n", "utf-8")
    (ws / "notebooks" / "clean.py").write_text("import marimo\n", "utf-8")
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/polars.py"].get("shadows") == "polars"
    assert "shadows" not in files["notebooks/clean.py"]


def test_state_shadow_field_absent_when_disabled(unconfigured_client):
    from dataclasses import replace

    client, hub = unconfigured_client
    hub.app_cfg = replace(hub.app_cfg, warn_shadowed_notebooks=False)
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "polars.py").write_text("import marimo\n", "utf-8")
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert "shadows" not in files["notebooks/polars.py"]


def test_open_warning_includes_shadow(unconfigured_client, monkeypatch):
    # Opening an innocent notebook still warns about a shadowing sibling, merged into
    # the single `warning` string the front-end shows.
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "polars.py").write_text("import marimo\n", "utf-8")
    (ws / "notebooks" / "analysis.py").write_text(
        "import marimo\napp = marimo.App()\n", "utf-8"
    )

    class FakeEditor:
        def use_uv(self):
            return True

        def url_for(self, rel):
            return "http://127.0.0.1:9/edit"

    monkeypatch.setattr(hub, "ensure_editor", lambda: FakeEditor())
    data = client.post("/api/open", json={"path": "notebooks/analysis.py"}).json()
    assert "polars" in data.get("warning", "")


def test_local_mode_new_lists_and_opens_without_login(unconfigured_client, monkeypatch):
    # The headline: create a notebook, see it listed as "local", and open it â€” all
    # with no repo and no GitHub token. The editor is faked so no marimo spawns.
    client, _hub = unconfigured_client

    class FakeEditor:
        def __init__(self, workspace, theme="system"):
            self.workspace = workspace

        def ensure_started(self):
            pass  # no-op: in-memory editor double, nothing to launch

        def use_uv(self):
            return False

        def url_for(self, rel_path):
            return f"http://editor/{rel_path}"

        def shutdown(self):
            pass  # no-op: in-memory editor double, nothing to tear down

    monkeypatch.setattr(server, "EditorServer", FakeEditor)

    created = client.post("/api/new", json={"name": "scratch"})
    assert created.status_code == 200
    assert created.json()["url"] == "http://editor/notebooks/scratch.py"

    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/scratch.py"]["state"] == "local"

    opened = client.post("/api/open", json={"path": "notebooks/scratch.py"})
    assert opened.status_code == 200
    assert opened.json()["url"] == "http://editor/notebooks/scratch.py"


def test_new_into_a_package_subfolder_registers_lists_and_opens(unconfigured_client, monkeypatch):
    # Creating a notebook inside a uv-workspace package folder: it records the folder
    # in the synced mooring.toml, then lists and opens like a top-level notebook.
    client, hub = unconfigured_client

    class FakeEditor:
        def __init__(self, workspace, theme="system"):
            self.workspace = workspace

        def ensure_started(self):
            pass

        def use_uv(self):
            return False

        def url_for(self, rel_path):
            return f"http://editor/{rel_path}"

        def shutdown(self):
            pass

    monkeypatch.setattr(server, "EditorServer", FakeEditor)

    rel = "packages/finance/notebooks/sales.py"
    created = client.post("/api/new", json={"name": "packages/finance/notebooks/sales"})
    assert created.status_code == 200
    assert created.json()["url"] == f"http://editor/{rel}"

    # The folder was registered in the synced mooring.toml, so it travels with the repo.
    workspace = hub.cfg.workspace()
    data = tomllib.loads((workspace / "mooring.toml").read_text("utf-8"))
    assert data["sync"]["folders"] == ["packages/finance/notebooks"]

    # And it now lists (folded into the sync scope) and opens.
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files[rel]["state"] == "local"
    opened = client.post("/api/open", json={"path": rel})
    assert opened.status_code == 200


def test_new_rejects_a_path_outside_the_workspace(unconfigured_client):
    client, _ = unconfigured_client
    resp = client.post("/api/new", json={"name": "../escape"})
    assert resp.status_code == 400
    assert "workspace" in resp.json()["error"]


def test_state_env_no_project_lists_top_level_env_packages(unconfigured_client, monkeypatch):
    # No notebook pyproject (uvx/pip): the footer shows the env's top-level packages
    # (e.g. what `uvx --with` added), so an added package actually appears.
    from mooring import pyproject_env

    client, _ = unconfigured_client
    monkeypatch.setattr(pyproject_env, "uv_available", lambda: True)
    monkeypatch.setattr(pyproject_env, "installed_top_level", lambda: ["polars", "seaborn"])
    env = client.get("/api/state").json()["env"]
    assert env["mode"] == "bundle"
    assert env["source"] == "env"
    assert env["packages"] == ["polars", "seaborn"]
    assert "uvx --with" in env["add_hint"]


def test_state_env_uv_project_shows_declared_deps_verbatim(unconfigured_client, monkeypatch):
    # With a workspace pyproject + uv, the footer shows the declared dependency list
    # verbatim (looks like the pyproject) and points at `mooring deps add`.
    from mooring import pyproject_env

    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "pyproject.toml").write_text(
        '[project]\nname = "nb"\nversion = "0"\ndependencies = ["marimo>=0.23.9", "polars"]\n',
        "utf-8",
    )
    monkeypatch.setattr(pyproject_env, "uv_available", lambda: True)
    env = client.get("/api/state").json()["env"]
    assert env["mode"] == "uv"
    assert env["source"] == "pyproject"
    assert env["packages"] == ["marimo>=0.23.9", "polars"]  # verbatim, transitive-free
    assert "mooring deps add" in env["add_hint"]


def test_state_env_frozen_build_notes_rebuild(unconfigured_client, monkeypatch):
    # A frozen build (no uv) can't add packages at runtime: show the declared deps but
    # tell the user to ask their admin to add it to the repo and rebuild.
    from mooring import pyproject_env

    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "pyproject.toml").write_text(
        '[project]\nname = "nb"\nversion = "0"\ndependencies = ["polars"]\n', "utf-8"
    )
    monkeypatch.setattr(pyproject_env, "uv_available", lambda: False)
    env = client.get("/api/state").json()["env"]
    assert env["mode"] == "bundle"
    assert env["source"] == "pyproject"
    assert env["packages"] == ["polars"]
    assert "rebuild" in env["add_hint"]


def test_local_mode_ai_open_surfaces_provider_failure(unconfigured_client, monkeypatch):
    # AI is reachable in local mode (no repo/login); if Copilot isn't available the
    # open fails cleanly as a 502 the chat UI can show â€” not a crash.
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "nb.py").write_text("import marimo\n", "utf-8")

    def boom(self, *a, **k):
        raise RuntimeError(
            "Copilot isn't available. Install the extra: pip install mooring[copilot]"
        )

    monkeypatch.setattr(Hub, "_make_chat_session", boom)
    resp = client.post("/api/ai/chat/open", json={"notebook": "nb.py"})
    assert resp.status_code == 502
    assert "Copilot" in resp.json()["error"]


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


def test_state_mode_is_repo_when_configured(configured):
    client, _, _, _ = configured
    assert client.get("/api/state").json()["mode"] == "repo"


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

        def __init__(self, workspace, theme="system"):
            self.workspace = workspace
            self.theme = theme
            FakeEditor.instances.append(self)

        def ensure_started(self):
            pass  # no-op: in-memory editor double, nothing to launch

        def use_uv(self):
            return False

        def url_for(self, rel_path):
            return f"http://editor/{rel_path}"

        def shutdown(self):
            pass  # no-op: in-memory editor double, nothing to tear down

    monkeypatch.setattr(server, "EditorServer", FakeEditor)
    write_ws(tmp_path, "ws1", "notebooks/a.py", "import marimo\napp = marimo.App()\n")
    write_ws(tmp_path, "ws2", "notebooks/b.py", "import marimo\napp = marimo.App()\n")

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


# -- rollback (revert to the last synced version) + AI-independent undo ----------


def _seed_and_pull(hub, fake, rel, contents=b"v1\n"):
    """Seed the remote and pull, so `rel` has a manifest base (last-synced) version."""
    from mooring import sync

    fake.seed(rel, contents)
    sync.pull(fake, hub.cfg)


def test_rollback_endpoint_restores_modified(configured):
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py")
    (tmp_path / "ws1" / "notebooks/a.py").write_text("mine\n", "utf-8", newline="\n")
    resp = client.post("/api/rollback", json={"path": "notebooks/a.py"})
    assert resp.status_code == 200
    assert (tmp_path / "ws1" / "notebooks/a.py").read_text("utf-8") == "v1\n"
    state = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert state["notebooks/a.py"]["state"] == "synced"


def test_rollback_endpoint_recreates_deleted_local(configured):
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py")
    (tmp_path / "ws1" / "notebooks/a.py").unlink()
    resp = client.post("/api/rollback", json={"path": "notebooks/a.py"})
    assert resp.status_code == 200
    assert (tmp_path / "ws1" / "notebooks/a.py").read_text("utf-8") == "v1\n"


def test_rollback_returns_undo_token_and_undo_restores_pre_revert_bytes(configured):
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py")
    nb = tmp_path / "ws1" / "notebooks/a.py"
    nb.write_text("mine\n", "utf-8", newline="\n")
    resp = client.post("/api/rollback", json={"path": "notebooks/a.py"})
    assert resp.status_code == 200
    token = resp.json()["undo_token"]  # snapshot token for the AI-independent Undo
    assert token
    assert nb.read_text("utf-8") == "v1\n"
    undo = client.post("/api/undo", json={"path": "notebooks/a.py", "token": token})
    assert undo.status_code == 200 and undo.json()["ok"] is True
    assert nb.read_text("utf-8") == "mine\n"


def test_undo_superseded_when_a_later_snapshot_is_on_top(configured):
    # A second revert (or an AI Apply) pushes a newer snapshot; undoing with the
    # FIRST token must refuse (409) rather than restore the wrong layer.
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py")
    nb = tmp_path / "ws1" / "notebooks/a.py"
    nb.write_text("mine1\n", "utf-8", newline="\n")
    t1 = client.post("/api/rollback", json={"path": "notebooks/a.py"}).json()["undo_token"]
    nb.write_text("mine2\n", "utf-8", newline="\n")
    t2 = client.post("/api/rollback", json={"path": "notebooks/a.py"}).json()["undo_token"]
    assert t1 != t2
    stale = client.post("/api/undo", json={"path": "notebooks/a.py", "token": t1})
    assert stale.status_code == 409  # superseded â€” left the file alone
    assert nb.read_text("utf-8") == "v1\n"
    fresh = client.post("/api/undo", json={"path": "notebooks/a.py", "token": t2})
    assert fresh.status_code == 200
    assert nb.read_text("utf-8") == "mine2\n"


def test_undo_nothing_to_undo_400(configured):
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py")
    resp = client.post("/api/undo", json={"path": "notebooks/a.py"})
    assert resp.status_code == 400


def test_rollback_skips_conflict_unless_flagged(configured):
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py")
    nb = tmp_path / "ws1" / "notebooks/a.py"
    nb.write_text("mine\n", "utf-8", newline="\n")
    fake.seed("notebooks/a.py", b"theirs\n")  # remote moves underneath -> CONFLICT
    client.post("/api/rollback", json={"path": "notebooks/a.py"})
    assert nb.read_text("utf-8") == "mine\n"  # default: left alone
    client.post("/api/rollback", json={"path": "notebooks/a.py", "conflicts": True})
    assert nb.read_text("utf-8") == "v1\n"  # flagged: my edit discarded


def test_undo_rejects_traversal(configured):
    client, _, _, tmp_path = configured
    (tmp_path / "evil.py").write_text("x", "utf-8")
    resp = client.post("/api/undo", json={"path": "../evil.py"})
    assert resp.status_code == 400


class _RecordingLock:
    """A threading.Lock stand-in that counts acquisitions (deterministic â€” no timing)."""

    def __init__(self):
        import threading

        self._inner = threading.Lock()
        self.acquisitions = 0

    def __enter__(self):
        self._inner.acquire()
        self.acquisitions += 1
        return self

    def __exit__(self, *exc):
        self._inner.release()
        return False


def test_rollback_apply_and_undo_serialize_on_the_same_lock(configured):
    """The per-notebook undo stack is shared by THREE write paths â€” sync rollback
    (/api/rollback), AI Apply (apply_with_undo, called by BOTH the chat and the
    batch Apply), and Undo/restore (restore_undo, behind /api/undo and the chat
    rollback). All three must serialize on the SAME lock â€” hub.apply.lock, owned
    by the app/apply.py guard â€” or a concurrent pair can race the snapshot stack.
    Pinned deterministically by swapping in a counting lock and driving each path
    once â€” if a refactor moves any path onto its own lock, its acquisition lands
    on the wrong object and the count here stops adding up."""
    from mooring import notebook_template

    client, hub, fake, tmp_path = configured
    rec = _RecordingLock()
    hub.apply.lock = rec

    # 1. the sync rollback endpoint
    _seed_and_pull(hub, fake, "notebooks/a.py")
    (tmp_path / "ws1" / "notebooks/a.py").write_text("mine\n", "utf-8", newline="\n")
    assert client.post("/api/rollback", json={"path": "notebooks/a.py"}).status_code == 200
    assert rec.acquisitions == 1

    # 2. the shared Apply path (chat Apply and batch Apply both call apply_with_undo)
    ws = hub.cfg.workspace()
    rel = notebook_template.create(ws, "lock guard")
    nb = ws / rel
    hub.apply.apply_with_undo(nb, ws, rel, [{"op": "append", "code": "x = 1"}])
    assert rec.acquisitions == 2

    # 3. the undo/restore path (/api/undo and the chat rollback)
    assert hub.apply.restore_undo(nb, ws, rel) == 0  # consumed the one snapshot from (2)
    assert rec.acquisitions == 3


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


def test_chat_open_reports_ready_for_stub_session(unconfigured_client, stub_chat):
    # The stub session is ready the instant it's constructed, so the open response
    # tells the UI it can enable the input immediately (no "connectingâ€¦" gate).
    client, hub = unconfigured_client
    assert _open_chat(client, hub).json()["ready"] is True


def test_chat_open_defers_live_schema_probe(unconfigured_client, stub_chat, monkeypatch):
    # The live-kernel probe must NOT run during chat-open (it's deferred to the first
    # turn). If it did, this would blow up â€” proving it's off the open critical path.
    from mooring.ai import introspect

    client, hub = unconfigured_client

    def _boom(*a, **k):
        raise AssertionError("live_dataset_schemas must not be called during chat-open")

    monkeypatch.setattr(introspect, "live_dataset_schemas", _boom)
    assert _open_chat(client, hub).status_code == 200


def test_chat_datasets_lists_value_free_paths(unconfigured_client):
    import polars as pl

    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "data").mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"region": ["x"], "amount": [1]}).write_parquet(ws / "data" / "s.parquet")
    resp = client.get("/api/ai/datasets")
    assert resp.status_code == 200
    data = resp.json()
    assert "data/s.parquet" in data["datasets"]
    assert data["ui_theme"] == "system"  # the chat follows the hub theme


def test_chat_datasets_disabled_when_ai_off(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws", ai=config.AiConfig(enabled=False)))
    with TestClient(create_app(hub)) as client:
        assert client.get("/api/ai/datasets").status_code == 404


def test_state_no_longer_walks_datasets(configured, monkeypatch):
    # The recursive dataset walk moved off /api/state (it's only used by the chat,
    # which fetches /api/ai/datasets) â€” so /api/state must not call list_datasets.
    from mooring import schema

    client, _, _, _ = configured
    monkeypatch.setattr(
        schema, "list_datasets", lambda *a, **k: (_ for _ in ()).throw(AssertionError("walked"))
    )
    assert client.get("/api/state").status_code == 200


def test_chat_open_includes_guard_status(unconfigured_client, stub_chat):
    # The open response carries the outbound-PII guard status so the UI can show a
    # before-you-send badge. Default config: the guard is off.
    client, hub = unconfigured_client
    guard = _open_chat(client, hub).json()["guard"]
    assert set(guard) == {"enabled", "block", "names", "names_active", "backend"}
    assert guard["enabled"] is False


def test_pii_status_reflects_config(tmp_path, monkeypatch):
    # Isolate against the developer's real config.toml, then read the guard snapshot
    # the chat badge is built from straight off the Hub.
    from mooring import paths
    from mooring.ai import ner
    from mooring.hub.server import Hub

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "cfg")
    # Force the name pass "not ready" so names_active is deterministic regardless of
    # whether a NER extra/model is installed locally (a ready spaCy model would
    # otherwise flip names_active to True). Backend resolution stays real.
    monkeypatch.setattr(ner, "is_ready", lambda *a, **k: False)

    off = Hub(config.load_app_config(env={}))._pii_status()
    assert off == {
        "enabled": False,
        "block": True,
        "names": False,
        "names_active": False,
        "backend": "",
    }

    on = Hub(
        config.load_app_config(env={"MOORING_AI_PII": "true", "MOORING_AI_PII_NAMES": "true"})
    )._pii_status()
    assert on["enabled"] is True and on["names"] is True and on["block"] is True
    assert on["backend"] in ("gliner", "spacy")  # "auto" resolved to a concrete backend
    # No NER extra is installed in CI, so the name pass can't actually run yet.
    assert on["names_active"] is False


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


def test_chat_send_refreshes_live_schema(unconfigured_client, stub_chat, monkeypatch):
    # A dataframe added to the kernel AFTER chat-open is picked up on the next turn,
    # without reopening â€” and is not re-injected on later turns while unchanged.
    from mooring.ai import introspect
    from mooring.schema import DatasetSchema

    client, hub = unconfigured_client
    sid = _open_chat(client, hub).json()["sid"]
    frames: list = []
    monkeypatch.setattr(introspect, "live_dataset_schemas", lambda *a, **k: list(frames))

    # No live frames yet -> the turn is forwarded as-is.
    client.post("/api/ai/chat/send", json={"sid": sid, "text": "hello"})
    assert hub._chats[sid].last_sent == "hello"

    # The analyst loads a dataframe in the kernel -> the next turn carries its schema.
    frames.append(
        DatasetSchema(name="orders", columns=(("id", "Int64"), ("region", "String")), n_rows=10)
    )
    client.post("/api/ai/chat/send", json={"sid": sid, "text": "now what?"})
    sent = hub._chats[sid].last_sent
    assert "UPDATED LIVE NOTEBOOK DATAFRAMES" in sent
    assert "orders" in sent and "region" in sent and "now what?" in sent

    # Unchanged kernel -> no redundant re-injection.
    client.post("/api/ai/chat/send", json={"sid": sid, "text": "again"})
    assert hub._chats[sid].last_sent == "again"


def test_chat_send_live_schema_value_free(unconfigured_client, stub_chat, monkeypatch):
    # The per-turn refresh reuses the value-free introspect render, so a data value
    # can never ride into the prefix â€” only names + dtypes do.
    from mooring.ai import introspect
    from mooring.schema import DatasetSchema

    client, hub = unconfigured_client
    sid = _open_chat(client, hub).json()["sid"]
    # DatasetSchema structurally holds only (name, columns, n_rows) â€” no values. The
    # render must surface the column NAME but nothing that looks like a value.
    frames = [DatasetSchema(name="t", columns=(("acct", "String"),), n_rows=7)]
    monkeypatch.setattr(introspect, "live_dataset_schemas", lambda *a, **k: list(frames))
    client.post("/api/ai/chat/send", json={"sid": sid, "text": "SECRET_VALUE_DO_NOT_LEAK?"})
    sent = hub._chats[sid].last_sent
    assert "acct" in sent  # the column name is shown
    assert sent.count("SECRET_VALUE_DO_NOT_LEAK") == 1  # only the analyst's own prompt


def test_chat_apply_writes_cell_into_notebook(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    resp = client.post("/api/ai/chat/apply", json={"sid": sid, "code": "result = 41 + 1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # The cell was written into the .py source (marimo --watch shows it in the tab).
    nb = (hub.cfg.workspace() / "nb.py").read_text("utf-8")
    assert "result = 41 + 1" in nb
    assert "ï»¿" not in nb  # no BOM (the marimo parser rejects it)


def test_chat_apply_rejects_empty_code(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    resp = client.post("/api/ai/chat/apply", json={"sid": sid, "code": "   "})
    assert resp.status_code == 400


def test_chat_apply_unknown_sid_404(unconfigured_client, stub_chat):
    client, _ = unconfigured_client
    resp = client.post("/api/ai/chat/apply", json={"sid": "nope", "code": "x = 1"})
    assert resp.status_code == 404


def test_chat_apply_edit_op_rewrites_a_cell(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    resp = client.post(
        "/api/ai/chat/apply",
        json={
            "sid": sid,
            "ops": [{"op": "edit", "index": 0, "anchor": "seed = 1", "code": "seed = 42"}],
        },
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    nb = (hub.cfg.workspace() / "nb.py").read_text("utf-8")
    assert "seed = 42" in nb and "seed = 1" not in nb


def test_chat_apply_rewrite_with_returns_succeeds(unconfigured_client, stub_chat):
    # End-to-end of the user's bug: a rewrite whose cell bodies still carry the
    # auto-generated `return` lines now applies cleanly (normalized server-side).
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    resp = client.post(
        "/api/ai/chat/apply",
        json={
            "sid": sid,
            "ops": [{"op": "replace_all", "cells": ["import marimo as mo\nreturn (mo,)"]}],
        },
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    nb = (hub.cfg.workspace() / "nb.py").read_text("utf-8")
    assert "import marimo as mo" in nb


def test_chat_apply_stale_anchor_is_409(unconfigured_client, stub_chat):
    # The analyst changed the cell between propose and Apply -> a loud conflict, not
    # a silent clobber.
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    resp = client.post(
        "/api/ai/chat/apply",
        json={
            "sid": sid,
            "ops": [{"op": "edit", "index": 0, "anchor": "WRONG", "code": "seed = 9"}],
        },
    )
    assert resp.status_code == 409
    assert "seed = 1" in (hub.cfg.workspace() / "nb.py").read_text("utf-8")  # untouched


def test_chat_apply_then_rollback_restores_byte_for_byte(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    nb = hub.cfg.workspace() / "nb.py"
    original = nb.read_text("utf-8")
    applied = client.post("/api/ai/chat/apply", json={"sid": sid, "code": "added = 1"}).json()
    assert applied["ok"] is True and applied["can_undo"] is True
    assert "added = 1" in nb.read_text("utf-8")
    roll = client.post("/api/ai/chat/rollback", json={"sid": sid})
    assert roll.status_code == 200 and roll.json() == {
        "ok": True,
        "can_undo": False,
        "undo_depth": 0,
    }
    assert nb.read_text("utf-8") == original  # back to the original


def test_chat_rollback_is_multi_level(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    nb = hub.cfg.workspace() / "nb.py"
    original = nb.read_text("utf-8")
    client.post("/api/ai/chat/apply", json={"sid": sid, "code": "a = 1"})
    after_one = nb.read_text("utf-8")
    client.post("/api/ai/chat/apply", json={"sid": sid, "code": "b = 2"})
    assert client.post("/api/ai/chat/rollback", json={"sid": sid}).json()["undo_depth"] == 1
    assert nb.read_text("utf-8") == after_one  # undid only the second Apply
    assert client.post("/api/ai/chat/rollback", json={"sid": sid}).json()["undo_depth"] == 0
    assert nb.read_text("utf-8") == original


def test_chat_rollback_nothing_to_undo_is_400(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    resp = client.post("/api/ai/chat/rollback", json={"sid": sid})
    assert resp.status_code == 400 and resp.json()["ok"] is False


def test_chat_rollback_unknown_sid_404(unconfigured_client, stub_chat):
    client, _ = unconfigured_client
    assert client.post("/api/ai/chat/rollback", json={"sid": "nope"}).status_code == 404


def test_chat_apply_anchorless_edit_is_409(unconfigured_client, stub_chat):
    # A missing anchor must be a conflict, never a silent index-based clobber.
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    resp = client.post(
        "/api/ai/chat/apply",
        json={"sid": sid, "ops": [{"op": "edit", "index": 0, "code": "seed = 9"}]},
    )
    assert resp.status_code == 409
    assert "seed = 1" in (hub.cfg.workspace() / "nb.py").read_text("utf-8")  # untouched


def test_chat_open_rejects_dot_state_dir(unconfigured_client, stub_chat):
    # The .mooring state dir (manifest + undo snapshots) must be unreachable via the
    # notebook path, even though a snapshot is a real .py file.
    client, hub = unconfigured_client
    hub.cfg.workspace().mkdir(parents=True, exist_ok=True)
    resp = client.post("/api/ai/chat/open", json={"notebook": ".mooring/undo/x/000000000001.py"})
    assert resp.status_code == 400


def test_chat_rollback_write_failure_keeps_snapshot(unconfigured_client, stub_chat, monkeypatch):
    # A failed restore write returns 502 AND keeps the snapshot, so the undo is
    # retryable (symmetric with apply's discard-on-failure).
    from mooring import paths

    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    nb = hub.cfg.workspace() / "nb.py"
    client.post("/api/ai/chat/apply", json={"sid": sid, "code": "added = 1"})

    orig = paths.safe_write_bytes
    monkeypatch.setattr(
        paths, "safe_write_bytes", lambda *a, **k: (_ for _ in ()).throw(OSError("busy"))
    )
    assert client.post("/api/ai/chat/rollback", json={"sid": sid}).status_code == 502

    monkeypatch.setattr(paths, "safe_write_bytes", orig)  # transient failure cleared
    roll = client.post("/api/ai/chat/rollback", json={"sid": sid})
    assert roll.status_code == 200 and roll.json()["ok"] is True
    assert "added = 1" not in nb.read_text("utf-8")  # the retry actually undid it


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
            pass  # no-op: test double

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
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws", ai=config.AiConfig(enabled=False)))
    with TestClient(create_app(hub)) as client:
        assert client.get("/ai/chat").status_code == 404
        assert client.post("/api/ai/chat/open", json={"notebook": "nb.py"}).status_code == 404
        assert client.get("/api/state").json()["ai_chat"] is False


# -- per-notebook AI off-switch (synced mooring.toml) ----------------------------


def test_state_reports_per_notebook_ai_disabled(configured):
    from mooring import workspace_config

    client, hub, _, tmp_path = configured
    write_ws(tmp_path, "ws1", "notebooks/a.py", "a")
    write_ws(tmp_path, "ws1", "notebooks/b.py", "b")
    workspace_config.set_ai_disabled(hub.cfg.workspace(), "notebooks/a.py", True)
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/a.py"].get("ai_disabled") is True
    assert "ai_disabled" not in files["notebooks/b.py"]  # absence == enabled


def test_notebook_ai_toggle_round_trip(unconfigured_client):
    from mooring import workspace_config

    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "nb.py").write_text("import marimo\n", "utf-8")

    resp = client.post("/api/ai/notebook/toggle", json={"notebook": "nb.py", "disabled": True})
    assert resp.status_code == 200 and resp.json()["ai_disabled"] is True
    assert workspace_config.is_ai_disabled(ws, "nb.py")

    resp = client.post("/api/ai/notebook/toggle", json={"notebook": "nb.py", "disabled": False})
    assert resp.json()["ai_disabled"] is False
    assert not workspace_config.is_ai_disabled(ws, "nb.py")


def test_notebook_ai_toggle_rejects_traversal(unconfigured_client):
    client, hub = unconfigured_client
    hub.cfg.workspace().mkdir(parents=True, exist_ok=True)
    resp = client.post(
        "/api/ai/notebook/toggle", json={"notebook": "../escape.py", "disabled": True}
    )
    assert resp.status_code == 400


def test_notebook_ai_toggle_allows_absent_notebook(unconfigured_client):
    # Disabling must work for a notebook not pulled yet, and re-enabling must stay
    # possible after the file was renamed/deleted (to clear a stale opt-out).
    from mooring import workspace_config

    client, hub = unconfigured_client
    hub.cfg.workspace().mkdir(parents=True, exist_ok=True)
    resp = client.post("/api/ai/notebook/toggle", json={"notebook": "ghost.py", "disabled": True})
    assert resp.status_code == 200 and resp.json()["ai_disabled"] is True
    assert workspace_config.is_ai_disabled(hub.cfg.workspace(), "ghost.py")
    resp = client.post("/api/ai/notebook/toggle", json={"notebook": "ghost.py", "disabled": False})
    assert resp.status_code == 200
    assert not workspace_config.is_ai_disabled(hub.cfg.workspace(), "ghost.py")


def test_notebook_ai_toggle_corrupt_file_is_409(unconfigured_client):
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "nb.py").write_text("import marimo\n", "utf-8")
    (ws / "mooring.toml").write_text("bad = = toml", "utf-8")  # corrupt: don't clobber
    resp = client.post("/api/ai/notebook/toggle", json={"notebook": "nb.py", "disabled": True})
    assert resp.status_code == 409
    assert (ws / "mooring.toml").read_text("utf-8") == "bad = = toml"  # left intact


def test_notebook_ai_toggle_disabled_when_ai_off(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws", ai=config.AiConfig(enabled=False)))
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/ai/notebook/toggle", json={"notebook": "nb.py", "disabled": True})
        assert resp.status_code == 404


def test_chat_open_blocked_for_disabled_notebook(unconfigured_client, stub_chat):
    from mooring import workspace_config

    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "nb.py").write_text("import marimo\n", "utf-8")
    workspace_config.set_ai_disabled(ws, "nb.py", True)
    resp = client.post("/api/ai/chat/open", json={"notebook": "nb.py"})
    assert resp.status_code == 403 and resp.json()["reason"] == "notebook_disabled"


def test_chat_send_blocked_after_disable_and_session_closed(unconfigured_client, stub_chat):
    # A window opened while enabled must not reach the model once the notebook is
    # turned off (from the hub, or a teammate's sync). The _chat_targets re-check.
    from mooring import workspace_config

    client, hub = unconfigured_client
    sid = _open_chat(client, hub).json()["sid"]
    workspace_config.set_ai_disabled(hub.cfg.workspace(), "nb.py", True)
    resp = client.post("/api/ai/chat/send", json={"sid": sid, "text": "hi"})
    assert resp.status_code == 403 and resp.json()["reason"] == "notebook_disabled"
    assert sid not in hub._chats  # session torn down


def test_chat_apply_blocked_after_disable_leaves_file_untouched(unconfigured_client, stub_chat):
    from mooring import workspace_config

    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    nb = hub.cfg.workspace() / "nb.py"
    original = nb.read_text("utf-8")
    workspace_config.set_ai_disabled(hub.cfg.workspace(), "nb.py", True)
    resp = client.post("/api/ai/chat/apply", json={"sid": sid, "code": "result = 1"})
    assert resp.status_code == 403 and resp.json()["reason"] == "notebook_disabled"
    assert nb.read_text("utf-8") == original  # apply gate protected the notebook
    assert sid not in hub._chats


def test_chat_rollback_blocked_after_disable(unconfigured_client, stub_chat):
    # Rollback also writes the notebook, so it must be gated like apply â€” otherwise a
    # disabled notebook could still be rewritten through the undo path.
    from mooring import workspace_config

    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    client.post("/api/ai/chat/apply", json={"sid": sid, "code": "added = 1"})  # one undo step
    workspace_config.set_ai_disabled(hub.cfg.workspace(), "nb.py", True)
    resp = client.post("/api/ai/chat/rollback", json={"sid": sid})
    assert resp.status_code == 403 and resp.json()["reason"] == "notebook_disabled"
    assert sid not in hub._chats
    assert "added = 1" in (hub.cfg.workspace() / "nb.py").read_text("utf-8")  # not reverted


def test_toggle_closes_open_sessions_for_notebook(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid1 = _open_chat(client, hub).json()["sid"]
    sid2 = _open_chat(client, hub).json()["sid"]  # second window, same notebook
    assert sid1 in hub._chats and sid2 in hub._chats
    # Spy on close() so we assert the provider teardown actually ran, not just that
    # the dict entry was dropped (the whole point of closing on disable).
    closed = []
    for s in (hub._chats[sid1], hub._chats[sid2]):
        orig = s.close
        s.close = lambda _o=orig: (closed.append(1), _o())
    resp = client.post("/api/ai/notebook/toggle", json={"notebook": "nb.py", "disabled": True})
    assert resp.json()["closed_sessions"] == 2
    assert sid1 not in hub._chats and sid2 not in hub._chats
    assert len(closed) == 2  # close() invoked on both sessions


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


class _FakeUnauthorizedProvider:
    """Signed in, but the account can't USE Copilot: list_models returns [] and
    reports WHY (a 403), exactly like CopilotProvider after a models.list 403. The
    auth probe still says "connected" â€” authorization is a separate gate."""

    def list_models(self, force=False):
        return []

    def models_error(self):
        return (
            "Copilot rejected the request: this account isn't authorized for the "
            "Copilot SDK/agent feature. A GitHub org/enterprise admin must enable it."
        )

    def status(self, force=False):
        from mooring.ai.base import ProviderStatus

        return ProviderStatus("copilot", available=True, connected=True, account="phil")

    def cached_status(self):
        return self.status()


def test_chat_models_lists_models(unconfigured_client, monkeypatch):
    client, _ = unconfigured_client
    monkeypatch.setattr("mooring.ai.get_provider", lambda app_cfg: _FakeModelProvider())
    resp = client.get("/api/ai/models")
    assert resp.status_code == 200
    data = resp.json()
    assert [m["id"] for m in data["models"]] == ["auto", "claude-opus-4.8"]
    assert data["models"][1]["efforts"] == ["low", "high", "max"]
    assert "default_model" in data and "default_effort" in data
    assert "error" not in data  # a clean list carries no error


def test_chat_models_surfaces_authorization_error(unconfigured_client, monkeypatch):
    # An unlicensed account must not silently get an empty picker â€” the 403 reason
    # rides along so the settings page / chat can tell the user to fix access.
    client, _ = unconfigured_client
    monkeypatch.setattr("mooring.ai.get_provider", lambda app_cfg: _FakeUnauthorizedProvider())
    data = client.get("/api/ai/models").json()
    assert data["models"] == []
    assert "authorized" in data["error"]


def test_ai_status_reports_authz_error_for_unlicensed_account(unconfigured_client, monkeypatch):
    # The auth probe alone reports "connected" for a signed-in-but-unlicensed
    # account, so the menu must ALSO surface the authorization failure (its Switch
    # account button is the fix).
    client, _ = unconfigured_client
    monkeypatch.setattr("mooring.ai.get_provider", lambda app_cfg: _FakeUnauthorizedProvider())
    data = client.get("/api/ai/status?probe=1").json()
    assert data["connected"] is True
    assert "authorized" in data["authz_error"]


def test_provider_is_built_once_and_reused(unconfigured_client, monkeypatch):
    # The provider is cached on the Hub so its auth/model-list TTL caches actually
    # survive across requests (the whole point of those caches) instead of being
    # rebuilt and discarded every open/models call.
    client, _ = unconfigured_client
    built = []

    def counting_get_provider(app_cfg):
        built.append(1)
        return _FakeModelProvider()

    monkeypatch.setattr("mooring.ai.get_provider", counting_get_provider)
    client.get("/api/ai/models")
    client.get("/api/ai/models")
    assert len(built) == 1  # built once, reused on the second call


def test_provider_cache_drops_on_reload(configured, monkeypatch):
    # A config reload (repo switch/setup) may change provider/model, so the cached
    # provider must be dropped and rebuilt.
    client, hub, _, _ = configured
    built = []
    monkeypatch.setattr(
        "mooring.ai.get_provider", lambda app_cfg: built.append(1) or _FakeModelProvider()
    )
    client.get("/api/ai/models")
    hub.reload()
    client.get("/api/ai/models")
    assert len(built) == 2  # rebuilt after reload


def test_chat_models_disabled_when_ai_off(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws", ai=config.AiConfig(enabled=False)))
    with TestClient(create_app(hub)) as client:
        assert client.get("/api/ai/models").status_code == 404


def test_chat_open_threads_model_and_effort(unconfigured_client, monkeypatch):
    client, hub = unconfigured_client
    seen = {}

    def fake_make(self, ctx, ws, nb, model="", reasoning_effort=None, dictionary=None):
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


# -- Copilot sign-in endpoints (separate from the GitHub login) ----------------


class _FakeAuthProvider:
    """A controllable stand-in for CopilotProvider's auth surface."""

    def __init__(self):
        from mooring.ai.base import ProviderStatus  # noqa: F401  # referenced below

        self.connected = False
        self.running = False
        self.probed = False
        self.connect_host = "UNSET"
        self.available_flag = True

    def _status(self):
        from mooring.ai.base import ProviderStatus

        return ProviderStatus(
            "copilot",
            available=self.available_flag,
            connected=self.connected,
            account="phil" if self.connected else "",
            detail="Connected as phil." if self.connected else "Not connected.",
        )

    def cached_status(self):
        return self._status() if self.probed else None

    def status(self, force=False):
        self.probed = True
        return self._status()

    def connect(self, host=None):
        from mooring.ai.base import ProviderStatus

        self.running = True
        self.connect_host = host
        return ProviderStatus("copilot", available=True, connected=False, detail="Browser openingâ€¦")

    def login_state(self):
        return {"running": self.running, "output": ["visit https://github.com/login/device"]}


def _use_auth_provider(monkeypatch):
    fake = _FakeAuthProvider()
    monkeypatch.setattr("mooring.ai.get_provider", lambda app_cfg: fake)
    return fake


def test_ai_status_cached_unknown_then_probe(unconfigured_client, monkeypatch):
    # Cached status never spawns the CLI: until a probe runs, it's "unchecked".
    client, _ = unconfigured_client
    fake = _use_auth_provider(monkeypatch)
    data = client.get("/api/ai/status").json()
    assert data["checked"] is False and data["connected"] is False
    # A forced probe returns the real status (here: connected as @phil).
    fake.connected = True
    data = client.get("/api/ai/status?probe=1").json()
    assert data["checked"] is True
    assert data["connected"] is True
    assert data["account"] == "phil"


def test_ai_status_404_when_ai_off(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws", ai=config.AiConfig(enabled=False)))
    with TestClient(create_app(hub)) as client:
        assert client.get("/api/ai/status").status_code == 404
        assert client.post("/api/ai/login/start", json={}).status_code == 404
        assert client.get("/api/ai/login/poll").status_code == 404


def test_ai_login_start_invokes_connect_with_host(unconfigured_client, monkeypatch):
    client, _ = unconfigured_client
    fake = _use_auth_provider(monkeypatch)
    resp = client.post("/api/ai/login/start", json={"host": "ghe.example.com"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert fake.connect_host == "ghe.example.com"


def test_ai_login_start_no_host_passes_none(unconfigured_client, monkeypatch):
    client, _ = unconfigured_client
    fake = _use_auth_provider(monkeypatch)
    assert client.post("/api/ai/login/start", json={}).status_code == 200
    assert fake.connect_host is None


def test_ai_login_poll_pending_then_ok(unconfigured_client, monkeypatch):
    client, _ = unconfigured_client
    fake = _use_auth_provider(monkeypatch)
    client.post("/api/ai/login/start", json={})
    pending = client.get("/api/ai/login/poll").json()
    assert pending["status"] == "pending"  # browser still open
    # The captured CLI output (where `copilot login` prints the device code + URL)
    # MUST ride along so the UI can show it â€” switching account is impossible
    # otherwise. ChatCore.parseDeviceLogin pulls the code out of these lines.
    assert pending["output"] == ["visit https://github.com/login/device"]
    # The user authorised in the browser; the CLI exited and the account is connected.
    fake.running = False
    fake.connected = True
    data = client.get("/api/ai/login/poll").json()
    assert data["status"] == "ok" and data["account"] == "phil"


def test_ai_login_poll_error_when_not_connected(unconfigured_client, monkeypatch):
    # The login process exited without connecting -> a clear error outcome.
    client, _ = unconfigured_client
    _use_auth_provider(monkeypatch)
    data = client.get("/api/ai/login/poll").json()
    assert data["status"] == "error"


def test_ai_login_start_surfaces_connect_failure_as_502(unconfigured_client, monkeypatch):
    client, _ = unconfigured_client
    from mooring.ai.base import AIError

    class _Boom:
        def connect(self, host=None):
            raise AIError("The Copilot CLI is not available in this build.")

    monkeypatch.setattr("mooring.ai.get_provider", lambda app_cfg: _Boom())
    resp = client.post("/api/ai/login/start", json={})
    assert resp.status_code == 502
    assert "Copilot CLI" in resp.json()["error"]


# -- AI batch (orchestrator) ------------------------------------------------


@pytest.fixture
def batch_client(tmp_path, monkeypatch):
    """A hub with batch ENABLED, whose builder is the no-LLM stub (each turn proposes
    one cell), so the full open -> build -> tray -> apply loop runs without Copilot."""
    from mooring.ai.chat import StubChatSession
    from mooring.ai_config import AiConfig, BatchConfig

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    monkeypatch.delenv("MOORING_GITHUB_HOST", raising=False)
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    ai = AiConfig(batch=BatchConfig(enabled=True, max_jobs=5, max_concurrency=2, job_timeout=3))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws", ai=ai))
    hub.cfg.workspace().mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        Hub,
        "_make_batch_session",
        lambda self, ctx, nb, model="", reasoning_effort=None, dictionary=None: StubChatSession(
            system_context=ctx
        ),
    )
    with TestClient(create_app(hub)) as client:
        yield client, hub


def _wait_caught_up(client, batch_id, timeout=15):
    """Poll the live tray until the queue is caught up (no build pending)."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        tray = client.get(f"/api/ai/batch/tray/{batch_id}").json()
        if tray.get("pending", 1) == 0 and tray["jobs"]:
            return tray
        time.sleep(0.05)
    raise AssertionError("batch did not finish in time")


def _run_batch(client, jobs, timeout=15):
    opened = client.post("/api/ai/batch/open", json={"jobs": jobs})
    assert opened.status_code == 200, opened.text
    batch_id = opened.json()["batch_id"]
    return batch_id, _wait_caught_up(client, batch_id, timeout)


def test_batch_open_disabled_by_default_403(unconfigured_client):
    client, _ = unconfigured_client  # batch defaults OFF
    resp = client.post("/api/ai/batch/open", json={"jobs": [{"brief": "x"}]})
    assert resp.status_code == 403
    assert resp.json()["reason"] == "batch_disabled"


def test_batch_builds_notebooks_and_tray_lists_them(batch_client):
    client, hub = batch_client
    _bid, tray = _run_batch(
        client,
        [{"name": "sales", "brief": "summarise sales"}, {"name": "churn", "brief": "model churn"}],
    )
    assert tray["status"] == "open"  # the queue stays open so the user can add more
    by_name = {j["name"]: j for j in tray["jobs"]}
    assert by_name["sales"]["status"] == "built" and by_name["churn"]["status"] == "built"
    # Each job created a fresh notebook with at least one reviewable proposal.
    assert by_name["sales"]["notebook"] == "notebooks/sales.py"
    assert by_name["sales"]["proposals"] and by_name["sales"]["proposals"][0]["code"]
    assert (hub.cfg.workspace() / "notebooks/sales.py").is_file()


def test_batch_apply_writes_the_proposal_into_the_notebook(batch_client):
    client, hub = batch_client
    bid, tray = _run_batch(client, [{"name": "rev", "brief": "chart revenue"}])
    job = tray["jobs"][0]
    assert job["status"] == "built"
    resp = client.post("/api/ai/batch/apply", json={"batch_id": bid, "job": 0, "proposal": 0})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    nb = (hub.cfg.workspace() / "notebooks/rev.py").read_text("utf-8")
    assert "df.describe()" in nb  # the stub's proposed cell landed
    assert "ï»¿" not in nb  # no BOM


def test_batch_apply_unknown_batch_404(batch_client):
    client, _ = batch_client
    resp = client.post("/api/ai/batch/apply", json={"batch_id": "nope", "job": 0, "proposal": 0})
    assert resp.status_code == 404


def test_batch_refine_folds_note_into_brief_and_rebuilds(batch_client):
    # Iterate on a built notebook's proposal before applying: the note is folded into the
    # brief and the notebook is re-built, all without writing the file.
    client, hub = batch_client
    bid, tray = _run_batch(client, [{"name": "rev", "brief": "chart revenue"}])
    assert tray["jobs"][0]["status"] == "built"
    resp = client.post(
        "/api/ai/batch/refine", json={"batch_id": bid, "job": 0, "feedback": "use a bar chart"}
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    tray2 = _wait_caught_up(client, bid)
    job = tray2["jobs"][0]
    assert job["status"] == "built"
    assert "use a bar chart" in job["brief"]  # the revision note was folded in
    # The notebook on disk is still the skeleton â€” nothing applied yet.
    nb = (hub.cfg.workspace() / "notebooks/rev.py").read_text("utf-8")
    assert "df.describe()" not in nb


def test_batch_refine_unknown_batch_404(batch_client):
    client, _ = batch_client
    resp = client.post("/api/ai/batch/refine", json={"batch_id": "nope", "job": 0, "feedback": "x"})
    assert resp.status_code == 404


def test_batch_threads_per_job_model_and_effort(batch_client, monkeypatch):
    # The batch page lets the analyst pick a model/effort; it must reach the builder.
    from mooring.ai.chat import StubChatSession

    client, _ = batch_client
    seen = []

    def fake(self, ctx, nb, model="", reasoning_effort=None, dictionary=None):
        seen.append((model, reasoning_effort))
        return StubChatSession(system_context=ctx)

    monkeypatch.setattr(Hub, "_make_batch_session", fake)
    _run_batch(
        client,
        [{"name": "m", "brief": "x", "model": "claude-opus", "reasoning_effort": "high"}],
    )
    assert ("claude-opus", "high") in seen


def test_batch_force_overrides_a_pii_block_and_makes_it_appliable(batch_client, monkeypatch):
    # A flagged brief blocks the job; "Build anyway" (the human override) rebuilds it, the
    # tray shows it built with the override kept visible, and the proposal is appliable.
    from mooring.ai.chat import StubChatSession

    client, hub = batch_client
    monkeypatch.setattr(
        Hub,
        "_make_batch_session",
        lambda self, ctx, nb, model="", reasoning_effort=None, dictionary=None: StubChatSession(
            system_context=ctx, pii_enabled=True, pii_block=True
        ),
    )
    bid, tray = _run_batch(client, [{"name": "leak", "brief": "email me at a@b.com about it"}])
    job = tray["jobs"][0]
    assert job["status"] == "pii_blocked" and job["pii"]  # value-free findings surfaced

    forced = client.post("/api/ai/batch/force", json={"batch_id": bid, "job": 0})
    assert forced.status_code == 200 and forced.json()["ok"] is True
    tray2 = _wait_caught_up(client, bid)
    job2 = tray2["jobs"][0]
    assert job2["status"] == "built"
    assert job2["pii"]  # the override stays visible on the built job
    assert job2["proposals"] and job2["proposals"][0]["applied"] is False

    applied = client.post("/api/ai/batch/apply", json={"batch_id": bid, "job": 0, "proposal": 0})
    assert applied.status_code == 200 and applied.json()["ok"] is True
    nb = (hub.cfg.workspace() / "notebooks/leak.py").read_text("utf-8")
    assert "df.describe()" in nb  # the overridden proposal actually wrote


def test_batch_force_unknown_batch_404(batch_client):
    client, _ = batch_client
    resp = client.post("/api/ai/batch/force", json={"batch_id": "nope", "job": 0})
    assert resp.status_code == 404


def test_batch_refine_inherits_a_forced_jobs_override(batch_client, monkeypatch):
    # A force-built job stays overridden: refining it must NOT re-block on the same flagged
    # brief â€” the revision auto-confirms, and the override stays visible on the result.
    from mooring.ai.chat import StubChatSession

    client, _ = batch_client
    monkeypatch.setattr(
        Hub,
        "_make_batch_session",
        lambda self, ctx, nb, model="", reasoning_effort=None, dictionary=None: StubChatSession(
            system_context=ctx, pii_enabled=True, pii_block=True
        ),
    )
    bid, tray = _run_batch(client, [{"name": "leak", "brief": "email me at a@b.com"}])
    assert tray["jobs"][0]["status"] == "pii_blocked"
    assert client.post("/api/ai/batch/force", json={"batch_id": bid, "job": 0}).status_code == 200
    built = _wait_caught_up(client, bid)
    assert built["jobs"][0]["status"] == "built" and built["jobs"][0]["pii"]

    refined = client.post(
        "/api/ai/batch/refine", json={"batch_id": bid, "job": 0, "feedback": "add a chart"}
    )
    assert refined.status_code == 200
    after = _wait_caught_up(client, bid)
    job = after["jobs"][0]
    assert job["status"] == "built"  # NOT re-blocked
    assert "add a chart" in job["brief"]
    assert job["pii"]  # the override stays sticky and visible


def test_batch_apply_then_refine_then_apply_the_revision(batch_client):
    # Regression: after applying a proposal, a refine REPLACES it; the revised proposal must
    # be appliable again (not stuck "Applied" from the old positional key).
    client, hub = batch_client
    bid, tray = _run_batch(client, [{"name": "rev", "brief": "chart revenue"}])
    assert tray["jobs"][0]["status"] == "built"

    first = client.post("/api/ai/batch/apply", json={"batch_id": bid, "job": 0, "proposal": 0})
    assert first.status_code == 200 and first.json().get("noop") is not True
    applied_tray = client.get(f"/api/ai/batch/tray/{bid}").json()
    assert applied_tray["jobs"][0]["proposals"][0]["applied"] is True

    refined = client.post(
        "/api/ai/batch/refine", json={"batch_id": bid, "job": 0, "feedback": "use a bar chart"}
    )
    assert refined.status_code == 200
    tray2 = _wait_caught_up(client, bid)
    assert tray2["jobs"][0]["proposals"][0]["applied"] is False  # iterated -> appliable again

    again = client.post("/api/ai/batch/apply", json={"batch_id": bid, "job": 0, "proposal": 0})
    assert again.status_code == 200 and again.json().get("noop") is not True
    nb = (hub.cfg.workspace() / "notebooks/rev.py").read_text("utf-8")
    assert nb.count("df.describe()") == 2  # the original and the revised cell both landed


def test_batch_apply_is_idempotent(batch_client):
    # A repeat apply (e.g. a tray re-render re-armed the button) is a no-op, so the same
    # cell can never be appended twice.
    client, hub = batch_client
    bid, tray = _run_batch(client, [{"name": "rev", "brief": "chart revenue"}])
    assert tray["jobs"][0]["status"] == "built"
    first = client.post("/api/ai/batch/apply", json={"batch_id": bid, "job": 0, "proposal": 0})
    assert first.status_code == 200 and first.json().get("noop") is not True
    second = client.post("/api/ai/batch/apply", json={"batch_id": bid, "job": 0, "proposal": 0})
    assert second.status_code == 200 and second.json().get("noop") is True
    nb = (hub.cfg.workspace() / "notebooks/rev.py").read_text("utf-8")
    assert nb.count("df.describe()") == 1  # applied exactly once


def test_batch_cancel_stops_the_run_and_keeps_the_tray(batch_client):
    # First-class cancel (P4): stops the run without switching repos. The registry
    # entry is kept, so the tray answers "closed" rather than a confusing 404, and
    # further work on the run is refused like any finished batch.
    client, _hub = batch_client
    bid, _tray = _run_batch(client, [{"name": "c", "brief": "chart revenue"}])
    resp = client.post("/api/ai/batch/cancel", json={"batch_id": bid})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert client.get(f"/api/ai/batch/tray/{bid}").json()["status"] == "closed"
    add = client.post("/api/ai/batch/add", json={"batch_id": bid, "jobs": [{"brief": "x"}]})
    assert add.status_code == 409
    # And the SSE stream of a cancelled run says "closed" instead of pinging forever.
    with client.stream("GET", f"/api/ai/batch/stream/{bid}") as stream:
        head = next(stream.iter_lines())
        assert head.startswith(": connected")


def test_batch_cancel_unknown_batch_404(batch_client):
    client, _hub = batch_client
    assert client.post("/api/ai/batch/cancel", json={"batch_id": "nope"}).status_code == 404


def test_batch_open_rejects_more_than_max_jobs(batch_client):
    client, _ = batch_client  # max_jobs = 5
    jobs = [{"brief": f"job {i}"} for i in range(6)]
    resp = client.post("/api/ai/batch/open", json={"jobs": jobs})
    assert resp.status_code == 400 and "limit is 5" in resp.json()["error"]


def test_batch_open_requires_a_brief_per_job(batch_client):
    client, _ = batch_client
    resp = client.post("/api/ai/batch/open", json={"jobs": [{"name": "x"}]})
    assert resp.status_code == 400


def test_batch_state_reports_caps_and_datasets(batch_client):
    client, _ = batch_client
    state = client.get("/api/ai/batch/state").json()
    assert state["enabled"] is True
    assert state["max_jobs"] == 5 and state["max_concurrency"] == 2
    assert "datasets" in state


def test_state_reports_ai_batch_flag(unconfigured_client, batch_client):
    # The hub's "Batch build" button shows only when the opt-in orchestrator is on.
    plain_client, _ = unconfigured_client  # batch defaults OFF
    assert plain_client.get("/api/state").json()["ai_batch"] is False
    on_client, _ = batch_client  # [ai.batch] enabled
    assert on_client.get("/api/state").json()["ai_batch"] is True


def test_batch_page_served_when_ai_enabled(unconfigured_client):
    client, _ = unconfigured_client
    resp = client.get("/ai/batch")
    assert resp.status_code == 200 and "batch builder" in resp.text


def test_batch_page_404_when_ai_off(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws", ai=config.AiConfig(enabled=False)))
    with TestClient(create_app(hub)) as client:
        assert client.get("/ai/batch").status_code == 404
        assert client.post("/api/ai/batch/open", json={"jobs": [{"brief": "x"}]}).status_code == 404


def test_batch_add_appends_more_jobs_to_an_open_run(batch_client):
    # Kick off one job, then add another to the SAME run while it builds â€” the tray
    # accumulates both. This is the "write the next while the first runs" workflow.
    client, hub = batch_client
    opened = client.post("/api/ai/batch/open", json={"jobs": [{"name": "first", "brief": "do a"}]})
    bid = opened.json()["batch_id"]
    added = client.post(
        "/api/ai/batch/add", json={"batch_id": bid, "jobs": [{"name": "second", "brief": "do b"}]}
    )
    assert added.status_code == 200 and added.json()["added"] == 1
    tray = _wait_caught_up(client, bid)
    names = {j["name"] for j in tray["jobs"]}
    assert names == {"first", "second"}
    assert all(j["status"] == "built" for j in tray["jobs"])
    assert (hub.cfg.workspace() / "notebooks/first.py").is_file()
    assert (hub.cfg.workspace() / "notebooks/second.py").is_file()


def test_batch_add_enforces_cumulative_cap(batch_client):
    client, _ = batch_client  # max_jobs = 5
    bid = client.post(
        "/api/ai/batch/open", json={"jobs": [{"brief": f"j{i}"} for i in range(4)]}
    ).json()["batch_id"]
    _wait_caught_up(client, bid)
    # 4 already + 2 more would be 6 > 5
    resp = client.post(
        "/api/ai/batch/add", json={"batch_id": bid, "jobs": [{"brief": "x"}, {"brief": "y"}]}
    )
    assert resp.status_code == 400 and "limit of 5" in resp.json()["error"]


def test_batch_add_unknown_batch_404(batch_client):
    client, _ = batch_client
    resp = client.post("/api/ai/batch/add", json={"batch_id": "nope", "jobs": [{"brief": "x"}]})
    assert resp.status_code == 404


def test_batch_stream_unknown_batch_404(batch_client):
    client, _ = batch_client
    assert client.get("/api/ai/batch/stream/nope").status_code == 404


# -- discover + adopt: folders outside the synced scope ------------------------


def test_discover_lists_unsynced_folders(configured):
    client, _, fake, _ = configured
    fake.seed("notebooks/a.py", b"x\n")  # in scope
    fake.seed("analysis/q1.py", b"y\n")  # out of scope
    fake.seed("lib/helpers.py", b"z\n")
    body = client.get("/api/discover").json()
    found = {c["folder"]: c for c in body["candidates"]}
    assert set(found) == {"analysis", "lib"}
    assert found["analysis"]["py_files"] == 1 and found["analysis"]["files"] == 1


def test_discover_empty_when_all_in_scope(configured):
    client, _, fake, _ = configured
    fake.seed("notebooks/a.py", b"x\n")
    assert client.get("/api/discover").json()["candidates"] == []


def test_adopt_registers_and_pulls(configured):
    client, hub, fake, _ = configured
    fake.seed("analysis/q1.py", b"y\n")
    resp = client.post("/api/adopt", json={"folders": ["analysis"]})
    assert resp.status_code == 200
    ws = hub.cfg.workspace()
    assert "analysis" in workspace_config.extra_folders(ws)
    assert (ws / "analysis/q1.py").read_text("utf-8") == "y\n"
    # Now that it is adopted, discovery no longer lists it.
    assert client.get("/api/discover").json()["candidates"] == []


def test_adopt_rejects_unknown_folder(configured):
    client, hub, fake, _ = configured
    fake.seed("analysis/q1.py", b"y\n")
    resp = client.post("/api/adopt", json={"folders": ["nope"]})
    assert resp.status_code == 400
    assert workspace_config.extra_folders(hub.cfg.workspace()) == ()


def test_adopt_requires_folders(configured):
    client, _, _, _ = configured
    assert client.post("/api/adopt", json={"folders": []}).status_code == 400


# -- notebook vs module: flags, open guard, declared folders ------------------


def test_state_flags_notebook_vs_module(unconfigured_client):
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "real.py").write_text("import marimo\napp = marimo.App()\n", "utf-8")
    (ws / "notebooks" / "helpers.py").write_text("def f():\n    return 1\n", "utf-8")
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/real.py"].get("is_notebook") is True
    assert "is_module" not in files["notebooks/real.py"]
    assert files["notebooks/helpers.py"].get("is_module") is True
    assert "is_notebook" not in files["notebooks/helpers.py"]


def test_open_refuses_a_plain_module(unconfigured_client):
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "helpers.py").write_text("def f():\n    return 1\n", "utf-8")
    resp = client.post("/api/open", json={"path": "notebooks/helpers.py"})
    assert resp.status_code == 400
    assert "module" in resp.json()["error"].lower()


def test_state_includes_declared_folders(unconfigured_client):
    client, _ = unconfigured_client
    assert client.get("/api/state").json()["folders"] == ["notebooks", "data", "reports"]


def test_blank_stub_py_is_a_notebook_not_a_module(unconfigured_client):
    # A blank/whitespace-only .py opens as a fresh notebook (the open guards allow it),
    # so the listing must classify it as a notebook â€” never badge it 'module' while
    # /api/open would happily open it.
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "draft.py").write_text("   \n", "utf-8")
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/draft.py"].get("is_notebook") is True
    assert "is_module" not in files["notebooks/draft.py"]


def test_empty_init_py_is_a_module_not_a_notebook(unconfigured_client):
    # An empty __init__.py is a package marker, not a nascent notebook: it must be badged
    # 'module' (no Open) so marimo can't rewrite it into notebook form and break imports.
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "__init__.py").write_text("", "utf-8")
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/__init__.py"].get("is_module") is True
    assert "is_notebook" not in files["notebooks/__init__.py"]


def test_open_refuses_an_empty_init_py(unconfigured_client):
    # Backstop for a direct call / stale client: even though the hub hides Open on the
    # module row, /api/open on an empty __init__.py must still refuse (400 'module').
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "__init__.py").write_text("", "utf-8")
    resp = client.post("/api/open", json={"path": "notebooks/__init__.py"})
    assert resp.status_code == 400
    assert "module" in resp.json()["error"].lower()


def test_reveal_calls_launcher(unconfigured_client, monkeypatch):
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "helpers.py").write_text("def f():\n    return 1\n", "utf-8")
    revealed = []
    monkeypatch.setattr(reveal, "reveal", revealed.append)
    resp = client.post("/api/reveal", json={"path": "notebooks/helpers.py"})
    assert resp.status_code == 200
    assert "url" not in resp.json()  # nothing for the browser to open
    assert revealed == [(ws / "notebooks" / "helpers.py").resolve()]


def test_reveal_missing_file_404s(unconfigured_client):
    client, _ = unconfigured_client
    resp = client.post("/api/reveal", json={"path": "notebooks/nope.py"})
    assert resp.status_code == 404


def test_reveal_rejects_traversal(unconfigured_client):
    client, _ = unconfigured_client
    resp = client.post("/api/reveal", json={"path": "../evil.py"})
    assert resp.status_code == 400


def test_reveal_rejects_dot_state_dir(unconfigured_client):
    # .mooring/ (manifest + undo snapshots) must stay unreachable through reveal.
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / ".mooring").mkdir(parents=True, exist_ok=True)
    (ws / ".mooring" / "manifest.json").write_text("{}", "utf-8")
    resp = client.post("/api/reveal", json={"path": ".mooring/manifest.json"})
    assert resp.status_code == 400


def test_reveal_surfaces_launcher_error(unconfigured_client, monkeypatch):
    # On non-Windows (or if Explorer can't launch), RevealError becomes a friendly 400.
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "helpers.py").write_text("x = 1\n", "utf-8")

    def boom(_path):
        raise reveal.RevealError("needs Windows")

    monkeypatch.setattr(reveal, "reveal", boom)
    resp = client.post("/api/reveal", json={"path": "notebooks/helpers.py"})
    assert resp.status_code == 400
    assert "windows" in resp.json()["error"].lower()


def test_state_adds_github_url_for_remote_files(configured):
    client, _, fake, tmp_path = configured
    # A file that exists on the remote branch but not on disk (new-remote).
    fake.seed("notebooks/shared.py", b"import marimo\napp = marimo.App()\n")
    # A file that exists only locally, never pushed (new-local) â€” no remote blob.
    write_ws(tmp_path, "ws1", "notebooks/localonly.py", "x = 1\n")
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/shared.py"]["github_url"] == (
        "https://github.com/acme/nbs/blob/main/notebooks/shared.py"
    )
    assert "github_url" not in files["notebooks/localonly.py"]


def test_state_no_github_url_in_local_mode(unconfigured_client):
    # No repo configured â†’ no remote â†’ no View-on-GitHub link.
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    (ws / "notebooks" / "helpers.py").write_text("x = 1\n", "utf-8")
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert "github_url" not in files["notebooks/helpers.py"]


# -- staleness guard: remote_sha rows + the /api/freshness near-open check --------


def test_state_adds_remote_sha_exactly_when_remote_exists(configured):
    client, _, fake, tmp_path = configured
    fake.seed("notebooks/shared.py", b"import marimo\napp = marimo.App()\n")
    write_ws(tmp_path, "ws1", "notebooks/localonly.py", "x = 1\n")
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    # The remote-existing row carries the remote blob sha (the staleness dialog's
    # dismissal key); a never-pushed local file has no remote blob, so no key.
    assert files["notebooks/shared.py"]["remote_sha"] == fake.tree["notebooks/shared.py"]
    assert "remote_sha" not in files["notebooks/localonly.py"]


def test_freshness_fresh_after_state_and_stale_after_remote_moves(configured):
    client, _, fake, _ = configured
    client.get("/api/state")  # records the rendered head
    data = client.get("/api/freshness").json()
    assert data == {"fresh": True, "head": fake.head}
    fake.seed("notebooks/new.py", b"x\n")  # a teammate pushes â†’ the head moves
    data = client.get("/api/freshness").json()
    assert data["fresh"] is False
    client.get("/api/state")  # re-render â€” the client caught up
    assert client.get("/api/freshness").json()["fresh"] is True


def test_freshness_before_any_state_render_reports_fresh(configured):
    # Nothing rendered yet â†’ nothing cached to be stale against.
    client, _, _, _ = configured
    assert client.get("/api/freshness").json()["fresh"] is True


def test_freshness_github_error_maps_502(configured, monkeypatch):
    from mooring.github import GitHubError

    client, _, fake, _ = configured

    def boom(branch):
        raise GitHubError("rate limited")

    monkeypatch.setattr(fake, "get_branch_head", boom)
    resp = client.get("/api/freshness")
    assert resp.status_code == 502
    assert "rate limited" in resp.json()["error"]


def test_freshness_local_mode_is_always_fresh(unconfigured_client):
    # No repo â†’ nothing can be stale; the endpoint must not try to reach GitHub.
    client, _ = unconfigured_client
    assert client.get("/api/freshness").json() == {"fresh": True, "head": ""}


def test_open_has_no_server_side_staleness_gate(configured, monkeypatch):
    """Invariant pin: the staleness guard is purely client-side and advisory â€”
    a `remote changed` file still opens through /api/open with no new gate."""

    class FakeEditor:
        def __init__(self, workspace, theme="system"):
            self.workspace = workspace

        def ensure_started(self):
            pass  # no-op editor double

        def use_uv(self):
            return True

        def url_for(self, rel_path):
            return f"http://editor/{rel_path}"

        def shutdown(self):
            pass  # no-op editor double

    monkeypatch.setattr(server, "EditorServer", FakeEditor)
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py", b"import marimo\napp = marimo.App()\n")
    fake.seed("notebooks/a.py", b"import marimo\napp = marimo.App()\nx = 1\n")
    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/a.py"]["state"] == "remote changed"
    resp = client.post("/api/open", json={"path": "notebooks/a.py"})
    assert resp.status_code == 200
    assert resp.json()["url"] == "http://editor/notebooks/a.py"


# -- the local safety net: trash endpoints + the activity ledger -------------


def test_resolve_theirs_response_carries_trash_token_and_restores(configured):
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py")
    (tmp_path / "ws1" / "notebooks/a.py").write_text("mine\n", "utf-8", newline="\n")
    fake.seed("notebooks/a.py", b"theirs\n")  # -> CONFLICT
    body = client.post(
        "/api/resolve", json={"path": "notebooks/a.py", "strategy": "theirs"}
    ).json()
    assert body["trashed"][0]["path"] == "notebooks/a.py"
    token = body["trashed"][0]["token"]
    assert (tmp_path / "ws1" / "notebooks/a.py").read_text("utf-8") == "theirs\n"
    # The trash lists it, and the token-exact restore puts the user's bytes back.
    listed = client.get("/api/trash").json()["entries"]
    assert any(e["token"] == token for e in listed)
    resp = client.post("/api/trash/restore", json={"token": token})
    assert resp.status_code == 200
    assert (tmp_path / "ws1" / "notebooks/a.py").read_text("utf-8") == "mine\n"


def test_trash_restore_refuses_superseded_with_409(configured):
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py")
    (tmp_path / "ws1" / "notebooks/a.py").write_text("mine\n", "utf-8", newline="\n")
    fake.seed("notebooks/a.py", b"theirs\n")
    body = client.post(
        "/api/resolve", json={"path": "notebooks/a.py", "strategy": "theirs"}
    ).json()
    token = body["trashed"][0]["token"]
    # A LATER write lands on top; the stale toast must refuse, not clobber it.
    (tmp_path / "ws1" / "notebooks/a.py").write_text("newer work\n", "utf-8", newline="\n")
    resp = client.post("/api/trash/restore", json={"token": token})
    assert resp.status_code == 409
    assert (tmp_path / "ws1" / "notebooks/a.py").read_text("utf-8") == "newer work\n"


def test_trash_restore_unknown_token_404s(configured):
    client, _, _, _ = configured
    assert client.post("/api/trash/restore", json={"token": "nope"}).status_code == 404


def test_delete_response_carries_trash_tokens(configured):
    client, _, _, tmp_path = configured
    write_ws(tmp_path, "ws1", "notebooks/a.py", "mine")
    body = client.post("/api/delete", json={"path": "notebooks/a.py"}).json()
    assert body["trashed"][0]["path"] == "notebooks/a.py"
    resp = client.post("/api/trash/restore", json={"token": body["trashed"][0]["token"]})
    assert resp.status_code == 200
    assert (tmp_path / "ws1" / "notebooks/a.py").read_text("utf-8") == "mine"


def test_activity_ledger_records_and_filters(configured):
    client, _, fake, tmp_path = configured
    write_ws(tmp_path, "ws1", "notebooks/a.py", "mine")
    client.post("/api/delete", json={"path": "notebooks/a.py"})
    client.post("/api/pull", json={})
    entries = client.get("/api/activity").json()["entries"]
    assert [e["op"] for e in entries][:2] == ["pull", "delete"]
    only_a = client.get("/api/activity?path=notebooks/a.py").json()["entries"]
    assert [e["op"] for e in only_a] == ["delete"]
    assert only_a[0]["trashed"][0]["path"] == "notebooks/a.py"


# -- the push guard at the hub seam + recall ----------------------------------


_SECRETY = 'TOKEN = "ghp_' + "a" * 40 + '"\n'


def test_push_guard_409_with_findings_and_confirm_flow(configured):
    client, hub, fake, tmp_path = configured
    write_ws(tmp_path, "ws1", "notebooks/leaky.py", _SECRETY)
    write_ws(tmp_path, "ws1", "notebooks/clean.py", "x = 1\n")
    resp = client.post("/api/push", json={})
    assert resp.status_code == 409
    body = resp.json()
    assert body["needs_confirm"] is True
    assert body["guard_mode"] == "warn"
    flagged = {f["path"]: f for f in body["guard_findings"]}
    assert set(flagged) == {"notebooks/leaky.py"}
    assert flagged["notebooks/leaky.py"]["findings"][0]["kind"] == "GitHub token"
    # The clean file went; the flagged one was withheld.
    assert "notebooks/clean.py" in fake.tree
    assert "notebooks/leaky.py" not in fake.tree
    # Findings are value-free: the secret never appears in the payload.
    assert "ghp_" + "a" * 40 not in resp.text
    # "Push anyway" with the per-file token completes the push.
    tokens = [f["token"] for f in body["guard_findings"]]
    resp2 = client.post("/api/push", json={"confirm_tokens": tokens})
    assert resp2.status_code == 200
    assert "notebooks/leaky.py" in fake.tree


def test_push_guard_stale_token_re409s(configured):
    client, _, fake, tmp_path = configured
    write_ws(tmp_path, "ws1", "notebooks/leaky.py", _SECRETY)
    body = client.post("/api/push", json={}).json()
    tokens = [f["token"] for f in body["guard_findings"]]
    # The file changes between the 409 and the confirm: the old token no longer
    # matches, so the confirm must NOT cover the new bytes.
    write_ws(tmp_path, "ws1", "notebooks/leaky.py", _SECRETY + "extra = 1\n")
    resp = client.post("/api/push", json={"confirm_tokens": tokens})
    assert resp.status_code == 409
    assert "notebooks/leaky.py" not in fake.tree


def test_push_guard_block_mode_refuses_tokens(configured):
    client, _, fake, tmp_path = configured
    write_ws(tmp_path, "ws1", "mooring.toml", '[guard]\npush = "block"\n')
    write_ws(tmp_path, "ws1", "notebooks/leaky.py", _SECRETY)
    body = client.post("/api/push", json={}).json()
    assert body["needs_confirm"] is False
    assert body["guard_mode"] == "block"
    tokens = [f["token"] for f in body["guard_findings"]]
    resp = client.post("/api/push", json={"confirm_tokens": tokens})
    assert resp.status_code == 409  # tokens ignored in block mode
    assert "notebooks/leaky.py" not in fake.tree


def test_push_guard_pragma_suppresses(configured):
    client, _, fake, tmp_path = configured
    write_ws(tmp_path, "ws1", "notebooks/ok.py", _SECRETY.rstrip() + "  # mooring: push-ok\n")
    resp = client.post("/api/push", json={})
    assert resp.status_code == 200
    assert "notebooks/ok.py" in fake.tree


def test_state_reports_can_recall_and_api_recall_works(configured):
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py")
    assert client.get("/api/state").json()["can_recall"] is False
    (tmp_path / "ws1" / "notebooks/a.py").write_text("v2\n", "utf-8", newline="\n")
    client.post("/api/push", json={})
    assert client.get("/api/state").json()["can_recall"] is True
    resp = client.post("/api/recall", json={})
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert any("recalled" in line for line in lines)
    assert any("history" in line for line in lines)
    assert fake.get_blob(fake.tree["notebooks/a.py"]) == b"v1\n"
    assert client.get("/api/state").json()["can_recall"] is False


# -- version history: /api/history, /api/history/file, /api/restore ----------


def _with_pushed_history(client, hub, fake, tmp_path):
    _seed_and_pull(hub, fake, "notebooks/a.py")
    old_head = fake.head
    (tmp_path / "ws1" / "notebooks/a.py").write_text("v2\n", "utf-8", newline="\n")
    assert client.post("/api/push", json={}).status_code == 200
    return old_head


def test_history_lists_versions(configured):
    client, hub, fake, tmp_path = configured
    _with_pushed_history(client, hub, fake, tmp_path)
    body = client.get("/api/history?path=notebooks/a.py").json()
    assert len(body["versions"]) == 2
    assert body["versions"][0]["author"] == "phil"


def test_history_rejects_traversal(configured):
    client, _, _, _ = configured
    assert client.get("/api/history?path=../evil.py").status_code == 400


def test_history_file_is_read_only_view_and_diff(configured):
    client, hub, fake, tmp_path = configured
    old_head = _with_pushed_history(client, hub, fake, tmp_path)
    body = client.get(
        f"/api/history/file?path=notebooks/a.py&at={old_head}"
    ).json()
    assert body["source"] == "v1\n"
    assert "-v1" in body["diff"] and "+v2" in body["diff"]
    # Read-only pin: no editor spawned, workspace bytes untouched.
    assert hub.editors == {}
    assert (tmp_path / "ws1" / "notebooks/a.py").read_text("utf-8") == "v2\n"


def test_restore_as_copy_endpoint(configured):
    client, hub, fake, tmp_path = configured
    old_head = _with_pushed_history(client, hub, fake, tmp_path)
    resp = client.post(
        "/api/restore", json={"path": "notebooks/a.py", "at": old_head, "copy": True}
    )
    assert resp.status_code == 200
    copy = tmp_path / "ws1" / f"notebooks/a.restored-{old_head[:7]}.py"
    assert copy.read_text("utf-8") == "v1\n"


def test_restore_over_endpoint_returns_undo_token(configured):
    client, hub, fake, tmp_path = configured
    old_head = _with_pushed_history(client, hub, fake, tmp_path)
    resp = client.post("/api/restore", json={"path": "notebooks/a.py", "at": old_head})
    assert resp.status_code == 200
    body = resp.json()
    assert (tmp_path / "ws1" / "notebooks/a.py").read_text("utf-8") == "v1\n"
    token = body["undo_token"]
    # The undo round-trip brings v2 back; a stale token would 409 (shared stack).
    undo = client.post("/api/undo", json={"path": "notebooks/a.py", "token": token})
    assert undo.status_code == 200
    assert (tmp_path / "ws1" / "notebooks/a.py").read_text("utf-8") == "v2\n"


# -- the health check endpoint (mooring doctor) --------------------------------


def test_api_doctor_returns_results_and_redacted_report(configured, monkeypatch):
    from mooring import doctor

    canned = [
        doctor.ProbeResult("python", "Python runtime", "pass", "Python 3.13, uv project."),
        doctor.ProbeResult("auth", "GitHub login", "fail", "Expired.", "Log in again."),
    ]
    monkeypatch.setattr(doctor, "run_probes", lambda cfg, extra=(): canned)
    client, _, _, _ = configured
    resp = client.post("/api/doctor", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert [r["status"] for r in body["results"]] == ["pass", "fail"]
    assert body["results"][1]["fix"] == "Log in again."
    assert body["report"].startswith("mooring doctor report")


def test_api_doctor_appends_copilot_probe_when_ai_enabled(configured, monkeypatch):
    from mooring import doctor

    seen = {}

    def fake_run(cfg, extra=()):
        seen["extra"] = list(extra)
        return [e() for e in extra]

    class FakeProvider:
        def status(self, force=False):
            from mooring.ai.base import ProviderStatus

            return ProviderStatus("copilot", available=True, connected=True, account="phil")

    monkeypatch.setattr(doctor, "run_probes", fake_run)
    client, hub, _, _ = configured
    monkeypatch.setattr(type(hub), "_provider_for", lambda self: FakeProvider())
    resp = client.post("/api/doctor", json={})
    body = resp.json()
    assert len(seen["extra"]) == 1  # the adapter appended the Copilot probe
    assert body["results"][0]["id"] == "copilot"
    assert body["results"][0]["status"] == "pass"
    assert "@phil" in body["results"][0]["detail"]


def test_appjs_element_ids_all_exist_in_index_html():
    """Wiring pin: every element id app.js looks up must exist in index.html.
    A load-time addEventListener on a missing element throws and kills the whole
    hub frontend, so this catches renamed/forgotten ids before a browser does."""
    import re
    from importlib import resources

    static = resources.files("mooring.hub").joinpath("static")
    app_js = (static / "app.js").read_text("utf-8")
    index_html = (static / "index.html").read_text("utf-8")
    ids = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', app_js))
    assert ids  # the pattern must keep matching if $() changes shape
    created = set(re.findall(r'\.id = "([A-Za-z0-9_-]+)"', app_js))  # built by app.js itself
    missing = [i for i in sorted(ids - created) if f'id="{i}"' not in index_html]
    assert not missing, f"app.js references ids missing from index.html: {missing}"


def test_resolve_push_copy_goes_through_the_guard(configured):
    client, hub, fake, tmp_path = configured
    _seed_and_pull(hub, fake, "notebooks/a.py")
    (tmp_path / "ws1" / "notebooks/a.py").write_text(_SECRETY, "utf-8", newline="\n")
    fake.seed("notebooks/a.py", b"theirs\n")  # -> CONFLICT
    resp = client.post("/api/resolve", json={"path": "notebooks/a.py", "strategy": "push-copy"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["guard_findings"][0]["path"] == "notebooks/a-phil.py"
    assert "notebooks/a-phil.py" not in fake.tree  # withheld, not uploaded
    # Acknowledge with the token -> the resolution completes.
    tokens = [f["token"] for f in body["guard_findings"]]
    resp2 = client.post(
        "/api/resolve",
        json={"path": "notebooks/a.py", "strategy": "push-copy", "confirm_tokens": tokens},
    )
    assert resp2.status_code == 200
    assert "notebooks/a-phil.py" in fake.tree
