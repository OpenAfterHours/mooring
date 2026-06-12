"""Config writer tests: repo registry mutations of the user config.toml."""

import tomllib

import pytest

from mooring import config, config_store, paths


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    for var in ("MOORING_CLIENT_ID", "MOORING_OWNER", "MOORING_REPO",
                "MOORING_BRANCH", "MOORING_WORKSPACE", "MOORING_ACTIVE_REPO",
                "MOORING_GITHUB_HOST"):
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
