"""Characterization guard for the hub's route table.

The exhaustive (path, methods, endpoint-name) enumeration below pins every route
the hub registers, so a mechanical split of hub/server.py into per-concern
routers (the architecture plan's P2) cannot silently drop, rename, or re-method
an endpoint and still pass CI. When you add or remove a route on purpose,
update the table here in the same PR — that is the point of the test.
"""

from starlette.routing import Mount, Route

from mooring import config
from mooring.hub.server import Hub, create_app

# (path, non-HEAD methods sorted, endpoint function name) for every Route.
EXPECTED_ROUTES = {
    ("/", ("GET",), "index_page"),
    ("/api/state", ("GET",), "api_state"),
    ("/api/setup", ("POST",), "api_setup"),
    ("/api/repo/switch", ("POST",), "api_repo_switch"),
    ("/api/repo/remove", ("POST",), "api_repo_remove"),
    ("/api/ui/theme", ("POST",), "api_set_theme"),
    ("/settings", ("GET",), "settings_page"),
    ("/api/settings", ("GET",), "api_get_settings"),
    ("/api/settings", ("POST",), "api_set_settings"),
    ("/api/settings/reset", ("POST",), "api_reset_settings"),
    ("/api/login/start", ("POST",), "api_login_start"),
    ("/api/login/poll", ("GET",), "api_login_poll"),
    ("/api/logout", ("POST",), "api_logout"),
    ("/api/discover", ("GET",), "api_discover"),
    ("/api/adopt", ("POST",), "api_adopt"),
    ("/api/pull", ("POST",), "api_pull"),
    ("/api/push", ("POST",), "api_push"),
    ("/api/propose", ("POST",), "api_propose"),
    ("/api/resolve", ("POST",), "api_resolve"),
    ("/api/new", ("POST",), "api_new"),
    ("/api/open", ("POST",), "api_open"),
    ("/api/reveal", ("POST",), "api_reveal"),
    ("/api/delete", ("POST",), "api_delete"),
    ("/api/rollback", ("POST",), "api_rollback"),
    ("/api/undo", ("POST",), "api_undo"),
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
