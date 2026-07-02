"""Characterization guard for the hub's route table, plus endpoint tests for
routes added after the split (the semantic-model toggle + artifact summary).

The exhaustive (path, methods, endpoint-name) enumeration below pins every route
the hub registers, so a mechanical split of hub/server.py into per-concern
routers (the architecture plan's P2) cannot silently drop, rename, or re-method
an endpoint and still pass CI. When you add or remove a route on purpose,
update the table here in the same PR — that is the point of the test.
"""

import tomllib

import pytest
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from mooring import config, paths
from mooring.hub.server import Hub, create_app

# (path, non-HEAD methods sorted, endpoint function name) for every Route.
EXPECTED_ROUTES = {
    ("/", ("GET",), "index_page"),
    ("/api/state", ("GET",), "api_state"),
    ("/api/setup", ("POST",), "api_setup"),
    ("/api/repo/switch", ("POST",), "api_repo_switch"),
    ("/api/repo/remove", ("POST",), "api_repo_remove"),
    ("/api/ui/theme", ("POST",), "api_set_theme"),
    ("/api/doctor", ("POST",), "api_doctor"),  # the on-demand health check
    ("/settings", ("GET",), "settings_page"),
    ("/api/settings", ("GET",), "api_get_settings"),
    ("/api/settings", ("POST",), "api_set_settings"),
    ("/api/settings/reset", ("POST",), "api_reset_settings"),
    ("/api/login/start", ("POST",), "api_login_start"),
    ("/api/login/poll", ("GET",), "api_login_poll"),
    ("/api/logout", ("POST",), "api_logout"),
    ("/api/discover", ("GET",), "api_discover"),
    # The pull digest — "what changed while you were away" (roadmap: pull-digest).
    ("/api/whatsnew", ("GET",), "api_whatsnew"),
    ("/api/whatsnew/detail", ("POST",), "api_whatsnew_detail"),
    ("/api/freshness", ("GET",), "api_freshness"),  # staleness guard's near-open head check
    ("/api/adopt", ("POST",), "api_adopt"),
    ("/api/pull", ("POST",), "api_pull"),
    ("/api/push", ("POST",), "api_push"),
    ("/api/propose", ("POST",), "api_propose"),
    ("/api/resolve", ("POST",), "api_resolve"),
    ("/api/recall", ("POST",), "api_recall"),  # push guard's "recall last push"
    ("/api/new", ("POST",), "api_new"),
    ("/api/duplicate", ("POST",), "api_duplicate"),  # the fearless personal draft copy
    ("/api/open", ("POST",), "api_open"),
    ("/api/reveal", ("POST",), "api_reveal"),
    ("/api/delete", ("POST",), "api_delete"),
    ("/api/rollback", ("POST",), "api_rollback"),
    ("/api/undo", ("POST",), "api_undo"),
    # The git-free time machine (roadmap: version-history).
    ("/api/history", ("GET",), "api_history"),
    ("/api/history/file", ("GET",), "api_history_file"),
    ("/api/restore", ("POST",), "api_restore"),
    # The cell-aware pre-push diff (roadmap: review-my-changes).
    ("/api/diff", ("POST",), "api_diff"),
    # The local safety net: the trash + activity ledger (roadmap: local-safety-net).
    ("/activity", ("GET",), "activity_page"),
    ("/api/trash", ("GET",), "api_trash"),
    ("/api/trash/restore", ("POST",), "api_trash_restore"),
    ("/api/activity", ("GET",), "api_activity"),
    ("/ai/chat", ("GET",), "chat_page"),
    ("/api/ai/datasets", ("GET",), "api_chat_datasets"),
    ("/api/ai/models", ("GET",), "api_chat_models"),
    ("/api/ai/status", ("GET",), "api_ai_status"),
    ("/api/ai/login/start", ("POST",), "api_ai_login_start"),
    ("/api/ai/login/poll", ("GET",), "api_ai_login_poll"),
    ("/api/ai/chat/open", ("POST",), "api_chat_open"),
    ("/api/ai/chat/stream/{sid}", ("GET",), "api_chat_stream"),
    ("/api/ai/chat/send", ("POST",), "api_chat_send"),
    ("/api/ai/chat/apply", ("POST",), "api_chat_apply"),
    ("/api/ai/chat/rollback", ("POST",), "api_chat_rollback"),
    ("/api/ai/notebook/toggle", ("POST",), "api_notebook_ai_toggle"),
    # Per-model AI opt-out for Power BI semantic models (roadmap: pbi-semantic-model).
    ("/api/ai/model/toggle", ("POST",), "api_model_ai_toggle"),
    ("/ai/batch", ("GET",), "batch_page"),
    ("/api/ai/batch/state", ("GET",), "api_batch_state"),
    ("/api/ai/batch/open", ("POST",), "api_batch_open"),
    ("/api/ai/batch/add", ("POST",), "api_batch_add"),
    ("/api/ai/batch/stream/{batch_id}", ("GET",), "api_batch_stream"),
    ("/api/ai/batch/tray/{batch_id}", ("GET",), "api_batch_tray"),
    ("/api/ai/batch/apply", ("POST",), "api_batch_apply"),
    ("/api/ai/batch/refine", ("POST",), "api_batch_refine"),
    ("/api/ai/batch/force", ("POST",), "api_batch_force"),
    ("/api/ai/batch/cancel", ("POST",), "api_batch_cancel"),  # added by P4 (first-class cancel)
}


def test_route_table_is_exactly_the_expected_set(tmp_path):
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws"))
    app = create_app(hub)

    actual = set()
    mounts = []
    for route in app.routes:
        if isinstance(route, Route):
            methods = tuple(sorted(m for m in (route.methods or ()) if m != "HEAD"))
            actual.add((route.path, methods, route.endpoint.__name__))
        elif isinstance(route, Mount):
            mounts.append(route.path)

    missing = EXPECTED_ROUTES - actual
    extra = actual - EXPECTED_ROUTES
    assert not missing and not extra, (
        f"Route table drifted.\nMissing (dropped/renamed): {sorted(missing)}\n"
        f"Extra (add to EXPECTED_ROUTES deliberately): {sorted(extra)}"
    )
    assert mounts == ["/static"]  # the bundled frontend assets


# -- the semantic-model surface: /api/state artifact field + /api/ai/model/toggle --


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    """A local-mode (no repo, no login) hub over a tmp workspace — /api/state then
    lists straight off disk, which exercises _files_artifacts without GitHub."""
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws"))
    with TestClient(create_app(hub)) as client:
        yield client, hub, tmp_path / "ws"


def _write(ws, rel, text):
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8", newline="\n")


def _pbip_project(ws):
    _write(ws, "reports/Sales.pbip", "{}")
    _write(
        ws,
        "reports/Sales.SemanticModel/definition/tables/Sales.tmdl",
        "table Sales\n"
        "\tmeasure 'Total Sales' = SUM(Sales[Amount])\n"
        "\t\tformatString: #,0\n"
        "\tcolumn Amount\n"
        "\t\tdataType: decimal\n",
    )


def test_state_artifact_carries_the_model_summary(local_client):
    client, _hub, ws = local_client
    _pbip_project(ws)
    state = client.get("/api/state").json()
    (artifact,) = state["artifacts"]
    assert artifact["key"] == "reports/Sales"
    assert artifact["model"] == {"tables": 1, "measures": 1}
    assert "ai_model_disabled" not in artifact  # not opted out


def test_state_artifact_without_definition_has_no_model_field(local_client):
    client, _hub, ws = local_client
    _write(ws, "reports/Report.pbip", "{}")
    _write(ws, "reports/Report.Report/definition.pbir", "{}")  # report-only project
    state = client.get("/api/state").json()
    (artifact,) = state["artifacts"]
    assert "model" not in artifact


def test_model_toggle_round_trip_and_state_flag(local_client):
    client, _hub, ws = local_client
    _pbip_project(ws)
    resp = client.post("/api/ai/model/toggle", json={"model": "reports/Sales", "disabled": True})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "model": "reports/Sales", "ai_model_disabled": True}
    data = tomllib.loads((ws / "mooring.toml").read_text("utf-8"))
    assert data["ai"]["disabled_semantic_models"] == ["reports/Sales"]
    (artifact,) = client.get("/api/state").json()["artifacts"]
    assert artifact["ai_model_disabled"] is True
    assert artifact["model"] == {"tables": 1, "measures": 1}  # summary still shown

    resp = client.post("/api/ai/model/toggle", json={"model": "reports/Sales", "disabled": False})
    assert resp.status_code == 200
    assert not (ws / "mooring.toml").exists()  # pruned empty, nothing spurious to sync
    (artifact,) = client.get("/api/state").json()["artifacts"]
    assert "ai_model_disabled" not in artifact


def test_model_toggle_requires_a_model(local_client):
    client, _hub, _ws = local_client
    assert client.post("/api/ai/model/toggle", json={}).status_code == 400


def test_model_toggle_rejects_escaping_paths(local_client):
    client, _hub, _ws = local_client
    resp = client.post("/api/ai/model/toggle", json={"model": "../outside", "disabled": True})
    assert resp.status_code == 400


def test_model_toggle_409_on_corrupt_mooring_toml(local_client):
    client, _hub, ws = local_client
    _write(ws, "mooring.toml", "this is = not valid = toml")
    resp = client.post("/api/ai/model/toggle", json={"model": "reports/Sales", "disabled": True})
    assert resp.status_code == 409
    # The corrupt file is refused, never clobbered (unrelated keys survive).
    assert (ws / "mooring.toml").read_text("utf-8") == "this is = not valid = toml"


def test_model_summary_is_cached_by_definition_signature(local_client):
    client, hub, ws = local_client
    _pbip_project(ws)
    client.get("/api/state")
    assert len(hub._model_summary_cache) == 1
    (sig1, summary1) = next(iter(hub._model_summary_cache.values()))
    client.get("/api/state")  # unchanged tree -> same cache entry, no re-parse
    assert next(iter(hub._model_summary_cache.values())) == (sig1, summary1)
    # A definition edit (new file) invalidates via the stat signature.
    _write(
        ws,
        "reports/Sales.SemanticModel/definition/tables/Date.tmdl",
        "table Date\n\tcolumn DateKey\n\t\tdataType: int64\n",
    )
    (artifact,) = client.get("/api/state").json()["artifacts"]
    assert artifact["model"] == {"tables": 2, "measures": 1}
