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


# -- signing in by borrowing git's credential ----------------------------------
# The method for organisations that restrict OAuth apps (blocking the device flow)
# AND cap personal access token lifetimes (making a pasted token useless). What is
# distinctive here is what does NOT happen: no token is ever stored.


def _borrows(monkeypatch, secret="gho_abc", host="acme.ghe.com"):
    from mooring import credhelper

    monkeypatch.setattr(credhelper, "available", lambda: True)
    monkeypatch.setattr(
        credhelper,
        "borrow",
        lambda h, path="", **kw: credhelper.Credential(h, "phil", secret) if h == host else None,
    )


def test_git_sign_in_records_the_identity_and_stores_no_token(monkeypatch):
    _borrows(monkeypatch)
    _resolves(monkeypatch, "acme_phil")

    account = accounts.sign_in_with_git("work", "acme.ghe.com")
    assert account.login == "acme_phil" and account.auth == config.AUTH_GIT

    stored = config.load_app_config().account("work")
    assert stored.login == "acme_phil" and stored.auth == "git" and stored.client_id == ""
    # The load-bearing part: nothing landed in the credential store, so nothing can
    # go stale there. The credential comes back from git on every read instead.
    assert auth.get_token(env={}, host="acme.ghe.com", login="acme_phil") is None


def test_a_borrowed_account_resolves_a_token_through_git(monkeypatch):
    _borrows(monkeypatch)
    _resolves(monkeypatch, "acme_phil")
    accounts.sign_in_with_git("work", "acme.ghe.com")
    config_store.add_repo("team", "acme", "nbs", account="work")

    cfg = config.load_app_config().config_for("team")
    assert cfg.auth_method == config.AUTH_GIT
    assert cfg.is_configured, "no client id, but a bound account is still a repo"
    assert auth.token_for(cfg.token_slot, env={}, method=cfg.auth_method) == "gho_abc"


def test_git_sign_in_without_git_says_so(monkeypatch):
    from mooring import credhelper

    monkeypatch.setattr(credhelper, "available", lambda: False)
    with pytest.raises(accounts.AccountError, match="git isn't on PATH"):
        accounts.sign_in_with_git("work", "acme.ghe.com")


def test_git_sign_in_with_no_stored_credential_names_the_ssh_trap(monkeypatch):
    from mooring import credhelper

    monkeypatch.setattr(credhelper, "available", lambda: True)
    monkeypatch.setattr(credhelper, "borrow", lambda h, path="", **kw: None)
    with pytest.raises(accounts.AccountError, match="SSH clone"):
        accounts.sign_in_with_git("work", "acme.ghe.com")


def test_a_credential_that_cannot_read_the_user_is_refused(monkeypatch):
    from mooring.github import AuthFailed

    _borrows(monkeypatch)
    _fails(monkeypatch, AuthFailed("401"))
    with pytest.raises(accounts.AccountError, match="couldn't read the account name"):
        accounts.sign_in_with_git("work", "acme.ghe.com")
    # Nothing half-written: a failed borrow leaves no account behind to confuse things.
    assert not config.load_app_config().accounts


def test_git_sign_in_collapses_onto_an_existing_identity(monkeypatch):
    """One record per identity: switching an account to borrowed credentials must
    not leave a second alias for the same person."""
    config_store.add_account("work", "acme.ghe.com", client_id="cid")
    _resolves(monkeypatch, "acme_phil")
    accounts.finish_login(_device(alias="work", host="acme.ghe.com"), "tok")

    _borrows(monkeypatch)
    account = accounts.sign_in_with_git("borrowed", "acme.ghe.com")

    assert account.alias == "work", "it landed on the existing record, not a new one"
    app_cfg = config.load_app_config()
    assert [a.alias for a in app_cfg.accounts] == ["work"]
    assert app_cfg.account("work").auth == config.AUTH_GIT


def test_a_device_re_login_switches_the_method_back(monkeypatch):
    """The reverse move must also stick, or the account would keep borrowing and
    ignore the token the device flow just stored."""
    _borrows(monkeypatch)
    _resolves(monkeypatch, "acme_phil")
    accounts.sign_in_with_git("work", "acme.ghe.com")
    assert config.load_app_config().account("work").auth == config.AUTH_GIT

    accounts.finish_login(_device(alias="work", host="acme.ghe.com"), "tok")
    assert config.load_app_config().account("work").auth == config.AUTH_DEVICE


def test_status_reports_the_method_without_asking_git(monkeypatch):
    from mooring import credhelper

    _borrows(monkeypatch)
    _resolves(monkeypatch, "acme_phil")
    accounts.sign_in_with_git("work", "acme.ghe.com")

    # status() runs on every hub state poll: it must not spawn a subprocess per
    # account per poll. The record is enough to say "signed in".
    def never(*a, **k):
        raise AssertionError("status must not shell out to git")

    monkeypatch.setattr(credhelper, "borrow", never)
    row = accounts.status(config.load_app_config())[0]
    assert row["auth"] == "git" and row["signed_in"] is True


def test_signing_out_of_a_borrowed_account_clears_the_login(monkeypatch):
    """There is no stored token to delete and git's own credential is not ours to
    remove, so signing out has to be recorded on mooring's side."""
    _borrows(monkeypatch)
    _resolves(monkeypatch, "acme_phil")
    accounts.sign_in_with_git("work", "acme.ghe.com")
    config_store.add_repo("team", "acme", "nbs", account="work")

    config_store.clear_account_login("work")
    auth.forget_borrowed("acme.ghe.com")

    cfg = config.load_app_config().config_for("team")
    assert cfg.token_slot is None, "fail closed: no credential may be handed out"
    assert auth.token_for(cfg.token_slot, env={}, method=cfg.auth_method) is None
    # The record survives, so signing back in restores it exactly.
    assert config.load_app_config().account("work").auth == config.AUTH_GIT
