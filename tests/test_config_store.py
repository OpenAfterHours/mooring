"""Config writer tests: repo registry mutations of the user config.toml."""

import tomllib

import pytest

from mooring import config, config_store, paths
from mooring.cli import _coerce_config_value, main


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    for var in (
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


def test_add_repo_round_trip():
    config_store.add_repo("team", "acme", "nbs", client_id="cid")
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["repos"]["active"] == "team"
    assert data["repos"]["team"] == {"owner": "acme", "repo": "nbs", "branch": "main"}
    app = config.load_app_config()
    assert app.client_id == "cid"
    assert app.config_for(None).repo_slug == "acme/nbs"


def test_add_repo_with_host_round_trip():
    config_store.add_repo("team", "acme", "nbs", host="https://GHE.Example/")
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["github"]["host"] == "ghe.example"
    assert config.load_app_config().host == "ghe.example"


def test_add_repo_without_host_keeps_existing():
    config_store.add_repo("team", "acme", "nbs", host="ghe.example")
    config_store.add_repo("lab", "acme", "lab", make_active=False)
    assert config.load_app_config().host == "ghe.example"


def test_add_account_round_trip_and_binding():
    config_store.add_account("work", "https://GHE.Example/", login="a.h", client_id="cid_ghe")
    config_store.add_repo("team", "acme", "nbs", account="work")
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["accounts"]["work"] == {
        "host": "ghe.example",
        "login": "a.h",
        "client_id": "cid_ghe",
    }
    assert data["accounts"]["active"] == "work"
    assert data["repos"]["team"]["account"] == "work"

    cfg = config.load_app_config().config_for("team")
    assert (cfg.host, cfg.client_id, cfg.account_login) == ("ghe.example", "cid_ghe", "a.h")


def test_add_account_merges_so_the_device_flow_can_fill_the_login_later():
    """The flow writes the record first and the login only once GET /user answers."""
    config_store.add_account("work", "ghe.example", client_id="cid_ghe")
    config_store.add_account("work", "ghe.example", login="a.h")
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["accounts"]["work"] == {
        "host": "ghe.example",
        "client_id": "cid_ghe",
        "login": "a.h",
    }


def test_add_repo_merges_and_never_silently_unbinds_an_account():
    """Re-adding an alias must not drop its account: an unbound repo falls back to
    the pre-accounts host-keyed token, i.e. it would push as the previous user."""
    config_store.add_account("work", "ghe.example", login="a.h", client_id="c")
    config_store.add_repo("team", "acme", "nbs", account="work")
    config_store.add_repo("team", "acme", "nbs", branch="dev")  # no account= given
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["repos"]["team"]["account"] == "work"
    assert data["repos"]["team"]["branch"] == "dev"


def test_remove_account_unbinds_its_repos_and_reports_them():
    config_store.add_account("work", "ghe.example", login="a.h", client_id="c")
    config_store.add_repo("team", "acme", "nbs", account="work")
    config_store.add_repo("lab", "acme", "lab", account="work", make_active=False)
    config_store.add_repo("solo", "acme", "solo", make_active=False)

    assert config_store.remove_account("work") == ("lab", "team")
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert "work" not in data["accounts"]
    assert "account" not in data["repos"]["team"]  # unbound, not deleted
    assert data["repos"]["team"]["owner"] == "acme"  # the repo itself survives


def test_remove_account_rejects_an_unknown_alias():
    config_store.add_account("work", "ghe.example", login="a.h")
    with pytest.raises(KeyError):
        config_store.remove_account("nope")


def test_upgrading_seeds_a_legacy_account_without_claiming_an_identity():
    """A pre-accounts [github] host/client_id becomes a connection record. The login
    stays blank because we do not know who the stored token belongs to — and a blank
    login is treated as 'not signed in', never resolved onto that token."""
    paths.user_config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.user_config_file().write_text(
        '[github]\nclient_id = "cid"\nhost = "ghe.example"\nowner = "acme"\nrepo = "nbs"\n',
        "utf-8",
    )
    config_store.set_active("nbs")  # any write materializes
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["accounts"]["legacy"] == {"host": "ghe.example", "client_id": "cid"}
    assert "login" not in data["accounts"]["legacy"]
    # The repo stays UNBOUND, so its existing host-keyed token keeps working.
    cfg = config.load_app_config().config_for(None)
    assert cfg.account == "" and cfg.token_slot == ("ghe.example", "")


def test_add_preserves_unrelated_sections():
    paths.user_config_dir().mkdir(parents=True)
    paths.user_config_file().write_text("[sync]\nwarn_file_mb = 2\n", "utf-8")
    config_store.add_repo("team", "acme", "nbs")
    app = config.load_app_config()
    assert app.warn_file_mb == 2
    assert app.aliases == ["team"]


def test_second_add_without_use_keeps_active():
    config_store.add_repo("team", "acme", "nbs")
    config_store.add_repo("lab", "acme", "lab", make_active=False)
    app = config.load_app_config()
    assert sorted(app.aliases) == ["lab", "team"]
    assert app.active_alias == "team"


def test_materializes_legacy_github_section():
    paths.user_config_dir().mkdir(parents=True)
    paths.user_config_file().write_text(
        '[github]\nclient_id = "cid"\nowner = "old"\nrepo = "legacy"\n', "utf-8"
    )
    config_store.add_repo("team", "acme", "nbs", make_active=False)
    app = config.load_app_config()
    # the legacy repo was copied into [repos] and survives alongside the new one
    assert sorted(app.aliases) == ["legacy", "team"]
    assert app.active_alias == "legacy"
    # ...which means it can now be removed even though [github] still names it
    config_store.remove_repo("legacy")
    app = config.load_app_config()
    assert app.aliases == ["team"]
    assert app.active_alias == "team"


def test_remove_last_repo_leaves_unconfigured():
    config_store.add_repo("team", "acme", "nbs", client_id="cid")
    config_store.remove_repo("team")
    app = config.load_app_config()
    assert app.repos == ()
    assert not app.config_for(None).is_configured


def test_set_active_and_unknown_alias():
    config_store.add_repo("team", "acme", "nbs")
    config_store.add_repo("lab", "acme", "lab", make_active=False)
    config_store.set_active("lab")
    assert config.load_app_config().active_alias == "lab"
    with pytest.raises(KeyError):
        config_store.set_active("nope")
    with pytest.raises(KeyError):
        config_store.remove_repo("nope")


@pytest.mark.parametrize("alias", ["active", "bad alias!", "", ".hidden", "a/b"])
def test_alias_validation_rejects(alias):
    with pytest.raises(ValueError):
        config_store.add_repo(alias, "acme", "nbs")


def test_set_host_normalizes_and_persists():
    config_store.add_repo("team", "acme", "nbs", client_id="cid")
    assert config_store.set_host("https://GHE.Example.com/") == "ghe.example.com"
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["github"]["host"] == "ghe.example.com"
    assert data["repos"]["active"] == "team"  # registry untouched
    assert config.load_app_config().host == "ghe.example.com"


def test_set_host_rejects_junk():
    with pytest.raises(ValueError):
        config_store.set_host("not a host!")


def test_remove_all_repos_clears_registry_keeps_github():
    config_store.add_repo("team", "acme", "nbs", client_id="cid", host="ghe.example")
    config_store.add_repo("lab", "acme", "lab", make_active=False)
    config_store.remove_all_repos()
    app = config.load_app_config()
    assert app.repos == ()
    assert not app.config_for(None).is_configured
    # [github] (client_id + host) survives the registry wipe
    assert app.client_id == "cid"
    assert app.host == "ghe.example"


# -- generic `mooring config` set/get/unset ------------------------------------


def test_coerce_value_types():
    assert _coerce_config_value(["true"]) is True
    assert _coerce_config_value(["false"]) is False
    assert _coerce_config_value(["5"]) == 5
    assert _coerce_config_value(["0.7"]) == pytest.approx(0.7)
    assert _coerce_config_value(["a", "b", "c"]) == ["a", "b", "c"]
    assert _coerce_config_value(['["a", "b"]']) == ["a", "b"]
    # a path/id is not a TOML literal -> it stays a bare string
    assert _coerce_config_value(["urchade/gliner_multi_pii-v1"]) == "urchade/gliner_multi_pii-v1"


def test_set_value_creates_nested_table_and_preserves_siblings():
    config_store.set_value("github.owner", "Acme")
    config_store.set_value("ai.pii.enabled", True)
    config_store.set_value("ai.pii.name_threshold", 0.5)
    data = config_store.read_user_data()
    assert data["github"]["owner"] == "Acme"
    assert data["ai"]["pii"] == {"enabled": True, "name_threshold": 0.5}


def test_get_value_is_default_merged_with_file():
    assert config_store.get_value("ai.pii.enabled") is False  # packaged default
    config_store.set_value("ai.pii.enabled", True)
    assert config_store.get_value("ai.pii.enabled") is True  # file override


def test_get_value_unknown_key_raises():
    with pytest.raises(KeyError):
        config_store.get_value("ai.pii.nope")


def test_unset_reverts_to_default_and_prunes_empty_tables():
    config_store.set_value("ai.pii.enabled", True)
    assert config_store.unset_value("ai.pii.enabled") is True
    assert "ai" not in config_store.read_user_data()  # empty [ai.pii]/[ai] pruned away
    assert config_store.get_value("ai.pii.enabled") is False  # back to the default
    assert config_store.unset_value("ai.pii.enabled") is False  # already absent


def test_set_value_does_not_materialize_repos():
    config_store.set_value("ai.pii.enabled", True)
    assert "repos" not in config_store.read_user_data()


def test_invalid_dotted_key_rejected():
    with pytest.raises(ValueError):
        config_store.set_value("ai..enabled", True)


def test_cli_set_then_get_roundtrip(capsys):
    assert main(["config", "set", "ai.pii.detect_names", "true"]) == 0
    assert main(["config", "get", "ai.pii.detect_names"]) == 0
    assert capsys.readouterr().out.strip().endswith("true")
    assert config_store.get_value("ai.pii.detect_names") is True


def test_cli_set_list_value():
    assert main(["config", "set", "ai.pii.name_labels", "person", "name", "organization"]) == 0
    assert config_store.get_value("ai.pii.name_labels") == ["person", "name", "organization"]


def test_cli_config_path_prints_user_file(capsys):
    assert main(["config", "path"]) == 0
    assert "config.toml" in capsys.readouterr().out
