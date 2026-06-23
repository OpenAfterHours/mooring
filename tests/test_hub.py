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


def test_state_includes_ui_theme(unconfigured_client):
    client, _ = unconfigured_client
    assert client.get("/api/state").json()["ui_theme"] == "system"  # default


def test_index_inlines_the_default_theme(unconfigured_client):
    # The pre-paint script's server-default fallback is rendered, not the literal
    # token — so a fresh browser paints in the configured theme with no flash.
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


def test_local_mode_new_lists_and_opens_without_login(unconfigured_client, monkeypatch):
    # The headline: create a notebook, see it listed as "local", and open it — all
    # with no repo and no GitHub token. The editor is faked so no marimo spawns.
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

    created = client.post("/api/new", json={"name": "scratch"})
    assert created.status_code == 200
    assert created.json()["url"] == "http://editor/notebooks/scratch.py"

    files = {f["path"]: f for f in client.get("/api/state").json()["files"]}
    assert files["notebooks/scratch.py"]["state"] == "local"

    opened = client.post("/api/open", json={"path": "notebooks/scratch.py"})
    assert opened.status_code == 200
    assert opened.json()["url"] == "http://editor/notebooks/scratch.py"


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
    # open fails cleanly as a 502 the chat UI can show — not a crash.
    client, hub = unconfigured_client
    ws = hub.cfg.workspace()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "nb.py").write_text("import marimo\n", "utf-8")

    def boom(self, *a, **k):
        raise RuntimeError("Copilot isn't available. Install the extra: pip install mooring[copilot]")

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
    assert stale.status_code == 409  # superseded — left the file alone
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
    # tells the UI it can enable the input immediately (no "connecting…" gate).
    client, hub = unconfigured_client
    assert _open_chat(client, hub).json()["ready"] is True


def test_chat_open_defers_live_schema_probe(unconfigured_client, stub_chat, monkeypatch):
    # The live-kernel probe must NOT run during chat-open (it's deferred to the first
    # turn). If it did, this would blow up — proving it's off the open critical path.
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
    # which fetches /api/ai/datasets) — so /api/state must not call list_datasets.
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
        "enabled": False, "block": True, "names": False, "names_active": False, "backend": "",
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
    # without reopening — and is not re-injected on later turns while unchanged.
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
    # can never ride into the prefix — only names + dtypes do.
    from mooring.ai import introspect
    from mooring.schema import DatasetSchema

    client, hub = unconfigured_client
    sid = _open_chat(client, hub).json()["sid"]
    # DatasetSchema structurally holds only (name, columns, n_rows) — no values. The
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


def test_chat_apply_edit_op_rewrites_a_cell(unconfigured_client, stub_chat):
    client, hub = unconfigured_client
    sid = _open_chat(client, hub, source=_NB_SRC).json()["sid"]
    resp = client.post(
        "/api/ai/chat/apply",
        json={"sid": sid, "ops": [{"op": "edit", "index": 0, "anchor": "seed = 1", "code": "seed = 42"}]},
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
        json={"sid": sid, "ops": [{"op": "replace_all", "cells": ["import marimo as mo\nreturn (mo,)"]}]},
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
        json={"sid": sid, "ops": [{"op": "edit", "index": 0, "anchor": "WRONG", "code": "seed = 9"}]},
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
    assert roll.status_code == 200 and roll.json() == {"ok": True, "can_undo": False, "undo_depth": 0}
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
        "/api/ai/chat/apply", json={"sid": sid, "ops": [{"op": "edit", "index": 0, "code": "seed = 9"}]}
    )
    assert resp.status_code == 409
    assert "seed = 1" in (hub.cfg.workspace() / "nb.py").read_text("utf-8")  # untouched


def test_chat_open_rejects_dot_state_dir(unconfigured_client, stub_chat):
    # The .mooring state dir (manifest + undo snapshots) must be unreachable via the
    # notebook path, even though a snapshot is a real .py file.
    client, hub = unconfigured_client
    hub.cfg.workspace().mkdir(parents=True, exist_ok=True)
    resp = client.post(
        "/api/ai/chat/open", json={"notebook": ".mooring/undo/x/000000000001.py"}
    )
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
    monkeypatch.setattr(paths, "safe_write_bytes", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
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
    resp = client.post("/api/ai/notebook/toggle", json={"notebook": "../escape.py", "disabled": True})
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
    # Rollback also writes the notebook, so it must be gated like apply — otherwise a
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


def test_chat_models_lists_models(unconfigured_client, monkeypatch):
    client, _ = unconfigured_client
    monkeypatch.setattr("mooring.ai.get_provider", lambda app_cfg: _FakeModelProvider())
    resp = client.get("/api/ai/models")
    assert resp.status_code == 200
    data = resp.json()
    assert [m["id"] for m in data["models"]] == ["auto", "claude-opus-4.8"]
    assert data["models"][1]["efforts"] == ["low", "high", "max"]
    assert "default_model" in data and "default_effort" in data


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
