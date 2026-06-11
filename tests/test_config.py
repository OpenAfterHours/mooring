from pathlib import Path

from mooring.config import load_config


def test_defaults_when_no_user_config(tmp_path):
    cfg = load_config(user_config_path=tmp_path / "missing.toml", env={})
    assert not cfg.is_configured
    assert cfg.branch == "main"
    assert cfg.folders == ("notebooks", "data")
    assert cfg.warn_file_mb == 10


def test_user_config_overrides_defaults(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(
        '[github]\nclient_id = "abc"\nowner = "acme"\nrepo = "nbs"\nbranch = "work"\n'
        "[sync]\nwarn_file_mb = 2\n",
        "utf-8",
    )
    cfg = load_config(user_config_path=user, env={})
    assert cfg.is_configured
    assert cfg.repo_slug == "acme/nbs"
    assert cfg.branch == "work"
    assert cfg.warn_file_mb == 2
    assert cfg.folders == ("notebooks", "data")  # untouched sections keep defaults


def test_env_overrides_everything(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text('[github]\nowner = "acme"\n', "utf-8")
    cfg = load_config(
        user_config_path=user,
        env={"MOORING_OWNER": "other", "MOORING_WORKSPACE": str(tmp_path / "ws")},
    )
    assert cfg.owner == "other"
    assert cfg.workspace() == Path(tmp_path / "ws")
