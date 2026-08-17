"""The `mooring account` command group, driven through cli.main()."""

import tomllib

import pytest

from mooring import auth, cli, config, config_store, paths


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
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
    monkeypatch.setenv("MOORING_TRUSTSTORE", "0")
    return tmp_path


def stub_flow(monkeypatch, login="octo"):
    from mooring.app import accounts

    def fake_start(client_id, session=None, host="github.com", account=""):
        return auth.DeviceCode(
            "d", "ABCD-1234", "https://x/login/device", 5, 900,
            host=host, client_id=client_id, account=account,
        )

    monkeypatch.setattr(auth, "start_device_flow", fake_start)
    monkeypatch.setattr(auth, "poll_for_token", lambda *a, **k: f"tok_{login}")
    monkeypatch.setattr(accounts, "resolve_login", lambda tok, host, session=None: login)


def test_account_add_signs_in_and_records_the_account(capsys, monkeypatch):
    stub_flow(monkeypatch, "a.harrison")
    assert cli.main(["account", "add", "work", "--host", "ghe.example", "--client-id", "cid"]) == 0

    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["accounts"]["work"] == {
        "host": "ghe.example",
        "client_id": "cid",
        "login": "a.harrison",
    }
    assert auth.get_token(env={}, host="ghe.example", login="a.harrison") == "tok_a.harrison"
    assert "Logged in as a.harrison@ghe.example" in capsys.readouterr().out


def test_two_accounts_on_the_same_host_coexist(capsys, monkeypatch):
    """The headline capability: host alone can no longer tell these apart."""
    stub_flow(monkeypatch, "alice")
    cli.main(["account", "add", "alice", "--client-id", "cid"])
    stub_flow(monkeypatch, "bob")
    cli.main(["account", "add", "bob", "--client-id", "cid"])

    assert auth.get_token(env={}, login="alice") == "tok_alice"
    assert auth.get_token(env={}, login="bob") == "tok_bob"
    assert sorted(a.alias for a in config.load_app_config().accounts) == ["alice", "bob"]


def test_account_list_shows_bindings_and_sign_in_state(capsys, monkeypatch):
    stub_flow(monkeypatch, "a.harrison")
    cli.main(["account", "add", "work", "--host", "ghe.example", "--client-id", "cid"])
    cli.main(["repo", "add", "acme/nbs", "--account", "work"])
    capsys.readouterr()

    assert cli.main(["account", "list"]) == 0
    out = capsys.readouterr().out
    assert "a.harrison@ghe.example" in out
    assert "signed in" in out
    assert "nbs" in out  # the repo using it
    assert "tok_" not in out  # never the credential


def test_account_list_is_helpful_when_empty(capsys):
    assert cli.main(["account", "list"]) == 0
    assert "mooring account add" in capsys.readouterr().out


def test_account_add_without_a_client_id_explains_itself(monkeypatch):
    stub_flow(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["account", "add", "work", "--host", "ghe.example"])
    assert "OAuth client id" in str(exc.value)


def test_account_add_reuses_the_client_id_when_re_authenticating(monkeypatch):
    stub_flow(monkeypatch, "a.harrison")
    cli.main(["account", "add", "work", "--host", "ghe.example", "--client-id", "cid"])
    assert cli.main(["account", "add", "work", "--host", "ghe.example"]) == 0  # no --client-id
    assert config.load_app_config().account("work").client_id == "cid"


def test_account_remove_signs_out_and_reports_orphaned_repos(capsys, monkeypatch):
    stub_flow(monkeypatch, "a.harrison")
    cli.main(["account", "add", "work", "--host", "ghe.example", "--client-id", "cid"])
    cli.main(["repo", "add", "acme/nbs", "--account", "work"])
    capsys.readouterr()

    assert cli.main(["account", "remove", "work"]) == 0
    out = capsys.readouterr().out
    assert "no longer have an account" in out and "nbs" in out
    assert auth.get_token(env={}, host="ghe.example", login="a.harrison") is None


def test_account_remove_unknown_alias_lists_the_known_ones(monkeypatch):
    stub_flow(monkeypatch, "a.harrison")
    cli.main(["account", "add", "work", "--client-id", "cid"])
    with pytest.raises(SystemExit) as exc:
        cli.main(["account", "remove", "nope"])
    assert "Known: work" in str(exc.value)


def test_account_use_sets_the_default_for_new_repos(monkeypatch):
    stub_flow(monkeypatch, "alice")
    cli.main(["account", "add", "alice", "--client-id", "cid"])
    stub_flow(monkeypatch, "bob")
    cli.main(["account", "add", "bob", "--client-id", "cid"])

    assert cli.main(["account", "use", "bob"]) == 0
    cli.main(["repo", "add", "acme/nbs"])  # no --account
    assert config.load_app_config().config_for("nbs").account == "bob"


def test_repo_add_rejects_an_unknown_account(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        cli.main(["repo", "add", "acme/nbs", "--account", "ghost"])
    assert "Unknown account 'ghost'" in str(exc.value)


def test_account_resume_finishes_a_stalled_sign_in(capsys, monkeypatch):
    """The device flow succeeded; only the identity lookup failed. Retrying must
    not require a new code."""
    from mooring.app import accounts
    from mooring.github import GitHubError

    stub_flow(monkeypatch, "a.harrison")
    monkeypatch.setattr(
        accounts,
        "resolve_login",
        lambda *a, **k: (_ for _ in ()).throw(GitHubError("SAML session required")),
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["account", "add", "work", "--host", "ghe.example", "--client-id", "cid"])
    assert "account resume work" in str(exc.value)

    monkeypatch.setattr(accounts, "resolve_login", lambda tok, host, session=None: "a.harrison")
    assert cli.main(["account", "resume", "work"]) == 0
    assert auth.get_token(env={}, host="ghe.example", login="a.harrison") == "tok_a.harrison"


def test_repo_list_shows_which_account_each_repo_signs_in_as(capsys, monkeypatch):
    stub_flow(monkeypatch, "a.harrison")
    cli.main(["account", "add", "work", "--host", "ghe.example", "--client-id", "cid"])
    cli.main(["repo", "add", "acme/nbs", "--account", "work"])
    capsys.readouterr()

    cli.main(["repo", "list"])
    assert "as a.harrison" in capsys.readouterr().out


def test_logout_only_signs_out_the_active_repos_account(capsys, monkeypatch):
    stub_flow(monkeypatch, "alice")
    cli.main(["account", "add", "alice", "--client-id", "cid"])
    stub_flow(monkeypatch, "bob")
    cli.main(["account", "add", "bob", "--client-id", "cid"])
    cli.main(["repo", "add", "acme/a", "--account", "alice"])
    cli.main(["repo", "add", "acme/b", "--account", "bob", "--no-use"])

    assert cli.main(["logout"]) == 0
    assert auth.get_token(env={}, login="alice") is None
    assert auth.get_token(env={}, login="bob") == "tok_bob"  # untouched


def test_repo_add_no_longer_repoints_other_repos(monkeypatch):
    """The bug that motivated all this: --host was written globally, so adding an
    Enterprise repo moved every existing github.com repo onto it."""
    stub_flow(monkeypatch, "alice")
    cli.main(["account", "add", "dotcom", "--client-id", "c1"])
    cli.main(["repo", "add", "acme/first", "--account", "dotcom"])

    stub_flow(monkeypatch, "a.harrison")
    cli.main(["account", "add", "work", "--host", "ghe.example", "--client-id", "c2"])
    cli.main(["repo", "add", "acme/second", "--account", "work"])

    app = config.load_app_config()
    assert app.config_for("first").host == "github.com"
    assert app.config_for("second").host == "ghe.example"


def test_account_aliases_are_validated_like_repo_aliases(monkeypatch):
    stub_flow(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["account", "add", "active", "--client-id", "cid"])
    assert "reserved" in str(exc.value)


def test_config_store_account_alias_rejects_junk():
    with pytest.raises(ValueError):
        config_store.add_account("has space", "github.com")


# -- repo create --------------------------------------------------------------


def _created(owner: str, name: str) -> dict:
    return {
        "name": name,
        "full_name": f"{owner}/{name}",
        "owner": {"login": owner},
        "default_branch": "main",
        "html_url": f"https://github.com/{owner}/{name}",
    }


def test_repo_create_makes_the_repo_seeds_it_and_registers_it(capsys, monkeypatch):
    import responses

    stub_flow(monkeypatch, "phil")
    cli.main(["account", "add", "me", "--client-id", "cid"])
    capsys.readouterr()

    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.POST,
            "https://api.github.com/orgs/acme/repos",
            json=_created("acme", "notebooks"),
            status=201,
        )
        for folder in ("notebooks", "data"):
            rsps.add(
                responses.PUT,
                f"https://api.github.com/repos/acme/notebooks/contents/{folder}/.gitkeep",
                json={"content": {"sha": "s"}, "commit": {"sha": "c"}},
                status=201,
            )
        assert cli.main(["repo", "create", "acme/notebooks", "--account", "me"]) == 0

    cfg = config.load_app_config().config_for("notebooks")
    assert cfg.repo_slug == "acme/notebooks"
    assert cfg.account == "me"
    assert "registered it as 'notebooks'" in capsys.readouterr().out


def test_repo_create_under_your_own_login_uses_the_user_endpoint(capsys, monkeypatch):
    """A personal repo is a different creation endpoint from an org repo."""
    import responses

    stub_flow(monkeypatch, "phil")
    cli.main(["account", "add", "me", "--client-id", "cid"])

    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.POST,
            "https://api.github.com/user/repos",
            json=_created("phil", "scratch"),
            status=201,
        )
        assert cli.main(["repo", "create", "phil/scratch", "--account", "me", "--no-seed"]) == 0

    assert config.load_app_config().config_for("scratch").repo_slug == "phil/scratch"


def test_repo_create_needs_an_account():
    with pytest.raises(SystemExit) as exc:
        cli.main(["repo", "create", "acme/notebooks"])
    assert "needs an account" in str(exc.value)


def test_repo_create_refuses_when_the_account_is_not_signed_in(monkeypatch):
    config_store.add_account("work", "ghe.example", client_id="cid")  # no login yet
    with pytest.raises(SystemExit) as exc:
        cli.main(["repo", "create", "acme/nbs", "--account", "work"])
    assert "not signed in" in str(exc.value)


# -- `mooring account discover` ------------------------------------------------


def _stub_discovery(monkeypatch, rows):
    from mooring.app import accounts

    monkeypatch.setattr(accounts, "discover_git_hosts", lambda app_cfg, **kw: list(rows))


def _row(host, alias, known=False, signed_in=False, kind="gho_"):
    from mooring.app.accounts import BorrowableHost

    return BorrowableHost(
        host=host, kind=kind, refreshable=True, alias=alias, known=known, signed_in=signed_in
    )


def test_account_discover_names_the_host_that_is_not_set_up(monkeypatch, capsys):
    _stub_discovery(
        monkeypatch,
        [_row("ghe.service.group", "work", known=True, signed_in=True), _row("github.com", "github")],
    )
    assert cli.main(["account", "discover"]) == 0
    out = capsys.readouterr().out
    assert "ghe.service.group" in out and "already set up as 'work'" in out
    assert "github.com" in out and "NOT set up" in out
    assert "--add" in out  # tells the user how to act on it


def test_account_discover_explains_itself_when_it_finds_nothing(monkeypatch, capsys):
    _stub_discovery(monkeypatch, [])
    assert cli.main(["account", "discover"]) == 0
    out = capsys.readouterr().out
    # The honest bit: a null result does NOT mean there is no credential, because
    # the credential protocol cannot enumerate. Say so, and give the manual route.
    assert "--from-git" in out


def test_account_discover_add_signs_in_to_the_pending_hosts(monkeypatch, capsys):
    _stub_discovery(monkeypatch, [_row("ghe.service.group", "svc"), _row("github.com", "github")])
    signed = []
    monkeypatch.setattr(
        cli, "_git_login", lambda alias, host: signed.append((alias, host)) or _account(alias, host)
    )
    assert cli.main(["account", "discover", "--add"]) == 0
    assert signed == [("svc", "ghe.service.group"), ("github", "github.com")]


def test_account_discover_add_keeps_going_when_one_host_refuses(monkeypatch, capsys):
    """One dead host must not take out the rest of the sweep."""
    _stub_discovery(monkeypatch, [_row("dead.example.com", "dead"), _row("github.com", "github")])
    signed = []

    def flaky(alias, host):
        if host == "dead.example.com":
            raise SystemExit("no credential")
        signed.append((alias, host))
        return _account(alias, host)

    monkeypatch.setattr(cli, "_git_login", flaky)
    assert cli.main(["account", "discover", "--add"]) == 0
    assert signed == [("github", "github.com")]
    assert "dead.example.com" in capsys.readouterr().out


def _account(alias, host):
    return config.Account(alias=alias, host=host, login="phil", auth="git")
