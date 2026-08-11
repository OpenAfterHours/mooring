"""The shared account service: device-flow completion, identity binding, removal."""

import pytest

from mooring import auth, config, config_store, paths
from mooring.app import accounts


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
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
    return tmp_path


def _device(alias="work", host="ghe.example", client_id="cid"):
    return auth.DeviceCode(
        device_code="dc",
        user_code="UC",
        verification_uri="https://x/device",
        interval=5,
        expires_in=900,
        host=host,
        client_id=client_id,
        account=alias,
    )


def _resolves(monkeypatch, login):
    monkeypatch.setattr(accounts, "resolve_login", lambda tok, host, session=None: login)


def _fails(monkeypatch, exc):
    def boom(tok, host, session=None):
        raise exc

    monkeypatch.setattr(accounts, "resolve_login", boom)


def test_finish_login_files_the_token_under_the_resolved_identity(monkeypatch):
    config_store.add_account("work", "ghe.example", client_id="cid")
    _resolves(monkeypatch, "a.harrison")

    account = accounts.finish_login(_device(), "gho_tok")

    assert account.login == "a.harrison"
    assert auth.get_token(env={}, host="ghe.example", login="a.harrison") == "gho_tok"
    assert config.load_app_config().account("work").login == "a.harrison"


def test_a_failed_identity_lookup_parks_the_token_instead_of_losing_it(monkeypatch):
    """The device flow already succeeded — the user must not have to get a new code."""
    config_store.add_account("work", "ghe.example", client_id="cid")
    _fails(monkeypatch, accounts.GitHubError("403 SAML session required"))

    with pytest.raises(accounts.AccountError, match="account resume"):
        accounts.finish_login(_device(), "gho_tok")

    _resolves(monkeypatch, "a.harrison")
    account = accounts.resume_login("work")
    assert account.login == "a.harrison"
    assert auth.get_token(env={}, host="ghe.example", login="a.harrison") == "gho_tok"


def test_parking_never_overwrites_a_pre_accounts_token(monkeypatch):
    """The provisional slot must not be the host-keyed one: an unbound repo may still
    be reading that, and clobbering it would sign the previous user out."""
    auth.save_token("gho_legacy", host="ghe.example")
    config_store.add_account("work", "ghe.example", client_id="cid")
    _fails(monkeypatch, accounts.GitHubError("boom"))

    with pytest.raises(accounts.AccountError):
        accounts.finish_login(_device(), "gho_new")

    assert auth.get_token(env={}, host="ghe.example") == "gho_legacy"


def test_the_pending_slot_is_cleaned_up_on_success(monkeypatch):
    config_store.add_account("work", "ghe.example", client_id="cid")
    _resolves(monkeypatch, "a.harrison")
    accounts.finish_login(_device(), "gho_tok")
    assert auth.get_token(env={}, host="ghe.example", login="~pending~work") is None


def test_signing_in_twice_as_the_same_identity_collapses_to_one_account(monkeypatch):
    """Otherwise two aliases share one keyring slot and removing either logs the
    other out."""
    config_store.add_account("first", "ghe.example", client_id="cid")
    _resolves(monkeypatch, "a.harrison")
    accounts.finish_login(_device(alias="first"), "gho_tok")

    config_store.add_account("second", "ghe.example", client_id="cid")
    account = accounts.finish_login(_device(alias="second"), "gho_tok2")

    assert account.alias == "first"
    assert [a.alias for a in config.load_app_config().accounts] == ["first"]
    assert auth.get_token(env={}, host="ghe.example", login="a.harrison") == "gho_tok2"


def test_same_login_on_a_different_host_stays_a_separate_account(monkeypatch):
    config_store.add_account("ghe", "ghe.example", client_id="c1")
    _resolves(monkeypatch, "phil")
    accounts.finish_login(_device(alias="ghe", host="ghe.example"), "gho_a")

    config_store.add_account("dotcom", "github.com", client_id="c2")
    accounts.finish_login(_device(alias="dotcom", host="github.com", client_id="c2"), "gho_b")

    assert sorted(a.alias for a in config.load_app_config().accounts) == ["dotcom", "ghe"]
    assert auth.get_token(env={}, host="ghe.example", login="phil") == "gho_a"
    assert auth.get_token(env={}, host="github.com", login="phil") == "gho_b"


def test_forget_deletes_the_token_and_leaves_its_repos_reporting_why(monkeypatch):
    config_store.add_account("work", "ghe.example", client_id="cid")
    _resolves(monkeypatch, "a.harrison")
    accounts.finish_login(_device(), "gho_tok")
    config_store.add_repo("team", "acme", "nbs", account="work")

    assert accounts.forget("work") == ("team",)
    assert auth.get_token(env={}, host="ghe.example", login="a.harrison") is None
    cfg = config.load_app_config().config_for("team")
    assert cfg.owner == "acme"  # the repo and its files survive
    assert cfg.token_slot is None  # ...but it can no longer produce a credential
    assert "work" in cfg.account_error


def test_forget_rejects_an_unknown_alias():
    with pytest.raises(accounts.AccountError, match="Unknown account"):
        accounts.forget("nope")


def test_start_login_refuses_without_a_client_id():
    with pytest.raises(accounts.AccountError, match="OAuth client id"):
        accounts.start_login("work", "ghe.example", "")


def test_start_login_records_the_account_before_the_flow_completes(monkeypatch):
    """So an abandoned login leaves a visible, fixable record — not a blank slate."""
    monkeypatch.setattr(
        auth, "start_device_flow", lambda cid, session=None, host="", account="": _device()
    )
    accounts.start_login("work", "https://GHE.Example/", "cid")
    account = config.load_app_config().account("work")
    assert (account.host, account.client_id) == ("ghe.example", "cid")
    assert not account.is_signed_in  # no login yet → treated as not signed in


def test_status_reports_bindings_without_leaking_tokens(monkeypatch):
    config_store.add_account("work", "ghe.example", client_id="cid")
    _resolves(monkeypatch, "a.harrison")
    accounts.finish_login(_device(), "gho_tok")
    config_store.add_repo("team", "acme", "nbs", account="work")

    (row,) = accounts.status(config.load_app_config())
    assert row["label"] == "a.harrison@ghe.example"
    assert row["signed_in"] is True
    assert row["repos"] == ("team",)
    assert "gho_tok" not in repr(row)


# -- end to end: identity follows the repo ------------------------------------


def test_a_repo_never_reads_another_accounts_token(monkeypatch):
    """The whole point of binding. Two accounts, two repos, two tokens — and
    resolving one repo must never hand back the other's credential."""
    from mooring.app import notebooks

    config_store.add_account("work", "ghe.service.group", client_id="cid_ghe")
    _resolves(monkeypatch, "a.harrison")
    accounts.finish_login(_device(alias="work", host="ghe.service.group"), "tok_work")

    config_store.add_account("personal", "github.com", client_id="cid_dotcom")
    _resolves(monkeypatch, "phil")
    accounts.finish_login(
        _device(alias="personal", host="github.com", client_id="cid_dotcom"), "tok_personal"
    )

    config_store.add_repo("analytics", "service-analytics", "notebooks", account="work")
    config_store.add_repo("side", "phil", "scratch", account="personal", make_active=False)

    app_cfg = config.load_app_config()
    work = app_cfg.config_for("analytics")
    side = app_cfg.config_for("side")

    assert auth.token_for(work.token_slot) == "tok_work"
    assert auth.token_for(side.token_slot) == "tok_personal"

    # ...and the clients each adapter builds carry the right instance.
    assert notebooks.client_for(work).host == "ghe.service.group"
    assert notebooks.client_for(side).host == "github.com"

    # Separate workspaces, so the two manifests can never be confused.
    assert work.workspace() != side.workspace()


def test_a_broken_binding_explains_itself_instead_of_looking_unconfigured(monkeypatch):
    from mooring.app import notebooks

    config_store.add_account("work", "ghe.example", client_id="cid")
    _resolves(monkeypatch, "a.harrison")
    accounts.finish_login(_device(), "tok")
    config_store.add_repo("team", "acme", "nbs", account="work")
    accounts.forget("work")

    cfg = config.load_app_config().config_for("team")
    with pytest.raises(notebooks.NotConfigured, match="work"):
        notebooks.client_for(cfg)

    # Re-adding the account restores the repo exactly as it was — the binding
    # was never thrown away.
    config_store.add_account("work", "ghe.example", login="a.harrison", client_id="cid")
    auth.save_token("tok2", host="ghe.example", login="a.harrison")
    cfg = config.load_app_config().config_for("team")
    assert cfg.token_slot == ("ghe.example", "a.harrison")
