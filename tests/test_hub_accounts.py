"""Hub account endpoints: the identities the UI can sign in and pick between."""

import pytest
from starlette.testclient import TestClient

from mooring import auth, config, config_store, paths
from mooring.hub.server import Hub, create_app


@pytest.fixture
def hub_client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(auth, "_keyring", lambda: None)  # force the file fallback
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

    config_store.add_account("work", "ghe.example", login="a.harrison", client_id="cid_ghe")
    config_store.add_account("personal", "github.com", login="phil", client_id="cid_dotcom")
    auth.save_token("tok_work", host="ghe.example", login="a.harrison")
    auth.save_token("tok_personal", host="github.com", login="phil")
    config_store.add_repo("analytics", "svc", "notebooks", account="work")
    config_store.add_repo("side", "phil", "scratch", account="personal", make_active=False)

    hub = Hub(config.load_app_config())
    with TestClient(create_app(hub)) as client:
        yield client, hub, tmp_path


class FakeAccountClient:
    """Stands in for github.AccountClient; records which host it was built for."""

    def __init__(self, host="github.com", owners=("phil", "acme"), repos=()):
        self.host = host
        self._owners = list(owners)
        self._repos = list(repos)
        self.created = []

    def list_owners(self, cap_pages=30):
        return list(self._owners), False

    def list_repos(self, owner="", cap_pages=30):
        rows = [r for r in self._repos if not owner or r["owner"]["login"] == owner]
        return rows, False

    def create_repo(self, name, owner="", private=True):
        self.created.append((name, owner, private))
        login = owner or "phil"
        return {
            "name": name,
            "full_name": f"{login}/{name}",
            "owner": {"login": login},
            "default_branch": "main",
            "html_url": f"https://x/{login}/{name}",
        }


def test_state_reports_accounts_and_per_repo_identity(hub_client):
    client, _, _ = hub_client
    state = client.get("/api/state").json()

    by_alias = {a["alias"]: a for a in state["accounts"]}
    assert by_alias["work"]["label"] == "a.harrison@ghe.example"
    assert by_alias["work"]["signed_in"] is True
    assert by_alias["work"]["repos"] == ["analytics"]

    rows = {r["alias"]: r for r in state["repos"]}
    assert rows["analytics"]["host"] == "ghe.example"
    assert rows["analytics"]["account_label"] == "a.harrison@ghe.example"
    assert rows["side"]["host"] == "github.com"
    assert state["account_error"] == ""
    # Never the credential itself.
    assert "tok_work" not in client.get("/api/state").text


def test_accounts_endpoint_lists_without_leaking_tokens(hub_client):
    client, _, _ = hub_client
    body = client.get("/api/accounts").json()
    assert sorted(a["alias"] for a in body["accounts"]) == ["personal", "work"]
    assert "tok_" not in client.get("/api/accounts").text


def test_add_account_requires_a_client_id(hub_client):
    client, _, _ = hub_client
    resp = client.post("/api/accounts/add", json={"alias": "new", "host": "ghe.other"})
    assert resp.status_code == 400
    assert "client id" in resp.json()["error"]


def test_add_account_records_it_without_signing_in(hub_client):
    client, _, _ = hub_client
    resp = client.post(
        "/api/accounts/add",
        json={"alias": "third", "host": "https://GHE.Other/", "client_id": "cid3"},
    )
    assert resp.status_code == 200
    account = config.load_app_config().account("third")
    assert account.host == "ghe.other"
    assert not account.is_signed_in  # signing in is the separate device-flow step


def test_add_account_rejects_a_bad_alias(hub_client):
    client, _, _ = hub_client
    resp = client.post("/api/accounts/add", json={"alias": "active", "client_id": "c"})
    assert resp.status_code == 400


def test_remove_account_signs_out_and_reports_orphans(hub_client):
    client, hub, _ = hub_client
    resp = client.post("/api/accounts/remove", json={"alias": "work"})
    assert resp.json()["orphaned"] == ["analytics"]
    # The orphaned repo keeps its binding and says why it is broken, rather than
    # silently reverting to github.com and the pre-accounts token slot.
    client.post("/api/repo/switch", json={"alias": "analytics"})
    assert "work" in client.get("/api/state").json()["account_error"]
    assert auth.get_token(env={}, host="ghe.example", login="a.harrison") is None
    # The other account is untouched.
    assert auth.get_token(env={}, host="github.com", login="phil") == "tok_personal"


def test_remove_unknown_account_404s(hub_client):
    client, _, _ = hub_client
    assert client.post("/api/accounts/remove", json={"alias": "ghost"}).status_code == 404


def test_use_sets_the_default_account(hub_client):
    client, _, _ = hub_client
    assert client.post("/api/accounts/use", json={"alias": "personal"}).status_code == 200
    assert config.load_app_config().active_account == "personal"


def test_owners_are_scoped_to_the_named_account(hub_client, monkeypatch):
    """A merged view across accounts would be a leak of one identity's reach
    into another's picker."""
    from mooring.hub.routes import setup as setup_routes

    seen = {}

    def fake(app_cfg, alias):
        seen["alias"] = alias
        return FakeAccountClient(host="ghe.example", owners=("svc-analytics",))

    monkeypatch.setattr(setup_routes.accounts, "client_for_account", fake)
    body = client_get(hub_client, "/api/accounts/work/owners")
    assert seen["alias"] == "work"
    # The signed-in login is always offered, even with no repo under it yet.
    assert body["owners"] == ["a.harrison", "svc-analytics"]


def client_get(hub_client, url):
    client, _, _ = hub_client
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_repos_listing_is_shaped_for_the_picker(hub_client, monkeypatch):
    from mooring.hub.routes import setup as setup_routes

    repos = [
        {
            "name": "notebooks",
            "full_name": "svc/notebooks",
            "owner": {"login": "svc"},
            "private": True,
            "default_branch": "main",
        }
    ]
    monkeypatch.setattr(
        setup_routes.accounts,
        "client_for_account",
        lambda app_cfg, alias: FakeAccountClient(repos=repos),
    )
    body = client_get(hub_client, "/api/accounts/work/repos?owner=svc")
    assert body["repos"] == [
        {
            "name": "notebooks",
            "full_name": "svc/notebooks",
            "owner": "svc",
            "private": True,
            "default_branch": "main",
        }
    ]
    assert body["truncated"] is False


def test_unknown_account_owners_404s(hub_client):
    client, _, _ = hub_client
    assert client.get("/api/accounts/ghost/owners").status_code == 404


def test_a_not_signed_in_account_reports_502_not_a_crash(hub_client):
    client, _, _ = hub_client
    config_store.add_account("blank", "ghe.other", client_id="c")
    client.post("/api/accounts/use", json={"alias": "blank"})  # forces a reload
    resp = client.get("/api/accounts/blank/owners")
    assert resp.status_code == 502
    assert "not signed in" in resp.json()["error"]


def test_create_repo_registers_it_bound_to_the_account(hub_client, monkeypatch):
    from mooring.hub.routes import setup as setup_routes

    fake = FakeAccountClient(host="ghe.example")
    monkeypatch.setattr(
        setup_routes.accounts, "client_for_account", lambda app_cfg, alias: fake
    )
    monkeypatch.setattr(
        setup_routes.accounts,
        "repo_client_for_account",
        lambda app_cfg, alias, owner, repo: _FakeSeeder(),
    )
    client, _, _ = hub_client
    resp = client.post(
        "/api/accounts/work/repos",
        json={"owner": "svc-analytics", "repo": "fresh", "private": True, "seed": True},
    )
    assert resp.status_code == 200, resp.text
    # Created under the ORG endpoint, since the owner isn't the signed-in login.
    assert fake.created == [("fresh", "svc-analytics", True)]
    cfg = config.load_app_config().config_for("fresh")
    assert cfg.repo_slug == "svc-analytics/fresh"
    assert cfg.account == "work"
    assert cfg.host == "ghe.example"


class _FakeSeeder:
    def __init__(self):
        self.puts = []

    def put_file(self, path, content, message, branch, base_sha=None):
        self.puts.append(path)
        return {}


def test_create_repo_needs_owner_and_name(hub_client):
    client, _, _ = hub_client
    assert client.post("/api/accounts/work/repos", json={"owner": "x"}).status_code == 400


def test_a_dangling_binding_is_reported_not_silently_local(hub_client):
    """Otherwise a falsy client_id drops the page into local mode and the repo
    looks like it vanished."""
    client, hub, _ = hub_client
    config_store.add_repo("analytics", "svc", "notebooks", account="ghost")
    hub.reload()
    state = client.get("/api/state").json()
    assert "ghost" in state["account_error"]


def test_setup_binds_a_new_repo_to_the_chosen_account(hub_client):
    client, _, _ = hub_client
    resp = client.post(
        "/api/setup",
        json={"account": "personal", "owner": "phil", "repo": "another", "branch": "main"},
    )
    assert resp.status_code == 200, resp.text
    cfg = config.load_app_config().config_for("another")
    assert cfg.account == "personal"
    assert cfg.token_slot == ("github.com", "phil")


def test_setup_defaults_to_the_active_account(hub_client):
    client, _, _ = hub_client
    client.post("/api/accounts/use", json={"alias": "personal"})
    client.post("/api/setup", json={"owner": "phil", "repo": "defaulted"})
    assert config.load_app_config().config_for("defaulted").account == "personal"


def test_setup_rejects_an_unknown_account(hub_client):
    client, _, _ = hub_client
    resp = client.post("/api/setup", json={"account": "ghost", "owner": "o", "repo": "r"})
    assert resp.status_code == 400 and "ghost" in resp.json()["error"]


def test_adding_a_second_repo_leaves_the_first_ones_host_alone(hub_client):
    """The original bug: --host/host was global, so registering an Enterprise repo
    silently moved every github.com repo onto it."""
    client, _, _ = hub_client
    client.post("/api/setup", json={"account": "work", "owner": "svc", "repo": "second"})
    app = config.load_app_config()
    assert app.config_for("side").host == "github.com"
    assert app.config_for("second").host == "ghe.example"


def test_logout_only_signs_out_the_active_repos_account(hub_client):
    client, hub, _ = hub_client
    client.post("/api/repo/switch", json={"alias": "analytics"})
    assert client.post("/api/logout", json={}).status_code == 200
    assert auth.get_token(env={}, host="ghe.example", login="a.harrison") is None
    assert auth.get_token(env={}, host="github.com", login="phil") == "tok_personal"


def test_login_start_targets_the_named_account(hub_client, monkeypatch):
    """Two sign-ins can be pending at once, so the flow must be keyed by alias and
    must carry its OWN client id — polling with one re-read from live config would
    present the wrong OAuth app after a repo switch."""
    from mooring.hub.routes import setup as setup_routes

    seen = {}

    def fake_start(client_id, session=None, host="github.com", account=""):
        seen.update(client_id=client_id, host=host, account=account)
        return auth.DeviceCode(
            "d", "UC", "https://x/device", 5, 900,
            host=host, client_id=client_id, account=account,
        )

    monkeypatch.setattr(setup_routes.auth, "start_device_flow", fake_start)
    client, hub, _ = hub_client
    body = client.post("/api/login/start?account=personal").json()
    assert body["account"] == "personal"
    assert seen == {"client_id": "cid_dotcom", "host": "github.com", "account": "personal"}
    assert set(hub._device) == {"personal"}


def test_login_start_on_an_unknown_account_404s(hub_client):
    client, _, _ = hub_client
    assert client.post("/api/login/start?account=ghost").status_code == 404


def test_repo_switch_drops_the_cached_username(hub_client):
    """Identity is per-repo now; a stale cache would render the previous account."""
    client, hub, _ = hub_client
    hub._user_login["work"] = "stale"
    client.post("/api/repo/switch", json={"alias": "side"})
    assert hub._user_login == {}


# -- signing in with git's stored credential -----------------------------------


def _fake_borrow(monkeypatch, secret="gho_abc", host="ghe.example"):
    from mooring import credhelper

    monkeypatch.setattr(credhelper, "available", lambda: True)
    monkeypatch.setattr(
        credhelper,
        "borrow",
        lambda h, path="", **kw: credhelper.Credential(h, "phil", secret) if h == host else None,
    )


def test_login_git_signs_in_without_an_oauth_app(hub_client, monkeypatch):
    client, hub, _tmp = hub_client
    _fake_borrow(monkeypatch)
    monkeypatch.setattr(
        "mooring.app.accounts.resolve_login", lambda tok, host, session=None: "a.harrison"
    )

    resp = client.post("/api/login/git", json={"account": "work"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] and body["user"] == "a.harrison" and body["account"] == "work"
    # Value-free: the response names the credential's TYPE, never the credential.
    assert body["kind"] == "gho_" and "gho_abc" not in resp.text

    assert config.load_app_config().account("work").auth == "git"


def test_login_git_reports_a_missing_credential_without_500ing(hub_client, monkeypatch):
    from mooring import credhelper

    client, _hub, _tmp = hub_client
    monkeypatch.setattr(credhelper, "available", lambda: True)
    monkeypatch.setattr(credhelper, "borrow", lambda h, path="", **kw: None)

    resp = client.post("/api/login/git", json={"account": "work"})
    assert resp.status_code == 400
    assert "SSH clone" in resp.json()["error"]


def test_login_git_probe_is_value_free(hub_client, monkeypatch):
    client, _hub, _tmp = hub_client
    _fake_borrow(monkeypatch, secret="ghp_capped")

    body = client.get("/api/login/git/probe?account=work").json()
    assert body["found"] and body["git_present"]
    # ghp_ is a personal access token: the thing an enterprise lifetime cap expires.
    # Saying so lets the dialog warn BEFORE the user commits to this method.
    assert body["kind"] == "ghp_" and body["refreshable"] is False
    assert "ghp_capped" not in str(body)


def test_signing_out_of_a_borrowed_account_actually_signs_out(hub_client, monkeypatch):
    """Without a stored token to delete, a naive logout would be a no-op and the
    next poll would silently re-borrow — a Sign out button that does nothing."""
    client, hub, _tmp = hub_client
    _fake_borrow(monkeypatch)
    monkeypatch.setattr(
        "mooring.app.accounts.resolve_login", lambda tok, host, session=None: "a.harrison"
    )
    client.post("/api/login/git", json={"account": "work"})

    assert client.post("/api/logout").status_code == 200
    cfg = config.load_app_config().config_for("analytics")
    assert cfg.token_slot is None
    assert auth.token_for(cfg.token_slot, env={}, method=cfg.auth_method) is None


# -- adding an account on a host the hub doesn't know yet ----------------------
# The gap this closes: /api/login/git can only target an EXISTING account or the
# active repo, and /api/accounts/add demanded an OAuth client id. On an org that
# won't approve an OAuth app there is no client id to give, so the hub had no way
# to reach a second host at all — the one case borrowing exists for.


def test_account_add_from_git_needs_no_oauth_client_id(hub_client, monkeypatch):
    client, _hub, _tmp = hub_client
    _fake_borrow(monkeypatch, host="ghe.service.group")
    monkeypatch.setattr(
        "mooring.app.accounts.resolve_login", lambda tok, host, session=None: "a.harrison"
    )

    resp = client.post(
        "/api/accounts/add",
        json={"alias": "svc", "host": "ghe.service.group", "from_git": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] and body["signed_in"] and body["user"] == "a.harrison"
    assert body["kind"] == "gho_" and "gho_abc" not in resp.text

    # Registered AND signed in as one step — there is no code to authorise, so a
    # record with no login would be a state nobody asked for.
    account = config.load_app_config().account("svc")
    assert (account.host, account.login, account.auth) == ("ghe.service.group", "a.harrison", "git")


def test_account_add_without_from_git_still_requires_a_client_id(hub_client):
    """The device flow's requirement is unchanged — from_git is a separate route,
    not a way around registering an OAuth app when you are actually using one."""
    client, _hub, _tmp = hub_client
    resp = client.post("/api/accounts/add", json={"alias": "svc", "host": "ghe.other"})
    assert resp.status_code == 400
    assert "client id" in resp.json()["error"]


def test_account_add_from_git_reports_a_missing_credential(hub_client, monkeypatch):
    from mooring import credhelper

    client, _hub, _tmp = hub_client
    monkeypatch.setattr(credhelper, "available", lambda: True)
    monkeypatch.setattr(credhelper, "borrow", lambda h, path="", **kw: None)

    resp = client.post(
        "/api/accounts/add",
        json={"alias": "svc", "host": "ghe.service.group", "from_git": True},
    )
    assert resp.status_code == 400
    assert "SSH clone" in resp.json()["error"]
    # A refused sign-in leaves no half-made account behind.
    with pytest.raises(KeyError):
        config.load_app_config().account("svc")


def test_account_add_from_git_rejects_a_bad_alias(hub_client, monkeypatch):
    client, _hub, _tmp = hub_client
    _fake_borrow(monkeypatch, host="ghe.service.group")
    resp = client.post(
        "/api/accounts/add",
        json={"alias": "active", "host": "ghe.service.group", "from_git": True},
    )
    assert resp.status_code == 400


def test_login_git_discover_lists_borrowable_hosts_value_free(hub_client, monkeypatch):
    from mooring import credhelper

    client, _hub, _tmp = hub_client
    monkeypatch.setattr(credhelper, "available", lambda: True)
    monkeypatch.setattr(
        credhelper, "candidate_hosts", lambda extra=(): ["ghe.service.group", "github.com"]
    )
    monkeypatch.setattr(
        credhelper,
        "discover",
        lambda hosts, **kw: [
            credhelper.Probe(h, True, True, "gho_", True) for h in hosts
        ],
    )

    body = client.get("/api/login/git/discover").json()
    assert body["git_present"] is True
    hosts = {h["host"]: h for h in body["hosts"]}
    # github.com already has an account here ("personal", signed in); the enterprise
    # host does not — and that is the one the user could not previously reach.
    assert hosts["github.com"]["signed_in"] is True
    assert hosts["ghe.service.group"]["signed_in"] is False
    assert hosts["ghe.service.group"]["alias"]
    assert "password" not in str(body) and "gho_abc" not in str(body)
