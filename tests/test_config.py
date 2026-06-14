from pathlib import Path

import platformdirs
import pytest

from mooring import paths
from mooring.config import load_app_config, load_config


def test_defaults_when_no_user_config(tmp_path):
    cfg = load_config(user_config_path=tmp_path / "missing.toml", env={})
    assert not cfg.is_configured
    assert cfg.branch == "main"
    assert cfg.folders == ("notebooks", "data", "reports")
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
    assert cfg.folders == ("notebooks", "data", "reports")  # untouched sections keep defaults


def test_host_defaults_and_normalizes_on_load(tmp_path):
    assert load_config(user_config_path=tmp_path / "missing.toml", env={}).host == "github.com"
    user = tmp_path / "config.toml"
    user.write_text('[github]\nhost = "https://GHE.Service.Group/"\n', "utf-8")
    cfg = load_config(user_config_path=user, env={})
    assert cfg.host == "ghe.service.group"


def test_host_env_override_and_config_for_passthrough(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(
        REPOS_TOML.replace('client_id = "cid"', 'client_id = "cid"\nhost = "ghe.example"'),
        "utf-8",
    )
    app = load_app_config(user_config_path=user, env={"MOORING_GITHUB_HOST": "other.example"})
    assert app.host == "other.example"
    assert app.config_for("team").host == "other.example"


def test_invalid_host_raises_value_error(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text('[github]\nhost = "not a host"\n', "utf-8")
    with pytest.raises(ValueError, match="Not a valid GitHub host"):
        load_config(user_config_path=user, env={})


def test_env_overrides_everything(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text('[github]\nowner = "acme"\n', "utf-8")
    cfg = load_config(
        user_config_path=user,
        env={"MOORING_OWNER": "other", "MOORING_WORKSPACE": str(tmp_path / "ws")},
    )
    assert cfg.owner == "other"
    assert cfg.workspace() == Path(tmp_path / "ws")


# -- multi-repo ([repos] tables and the legacy [github] section) -------------------


REPOS_TOML = """
[github]
client_id = "cid"

[repos]
active = "sandbox"

[repos.team]
owner = "acme"
repo = "notebooks"

[repos.sandbox]
owner = "phil"
repo = "notebooks"
branch = "dev"
"""


def test_legacy_github_section_synthesizes_single_repo(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(
        '[github]\nclient_id = "cid"\nowner = "acme"\nrepo = "nbs"\nbranch = "work"\n', "utf-8"
    )
    app = load_app_config(user_config_path=user, env={})
    assert app.aliases == ["nbs"]
    assert app.active_alias == "nbs"
    cfg = app.config_for(None)
    assert cfg.is_configured
    assert cfg.repo_slug == "acme/nbs"
    assert cfg.branch == "work"


def test_repos_tables_parse_and_active_selection(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(REPOS_TOML, "utf-8")
    app = load_app_config(user_config_path=user, env={})
    assert app.aliases == ["sandbox", "team"]
    assert app.active_alias == "sandbox"
    assert app.config_for(None).repo_slug == "phil/notebooks"
    assert app.config_for(None).branch == "dev"
    assert app.config_for("team").repo_slug == "acme/notebooks"


def test_repos_present_disables_legacy_github_repo(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(
        '[github]\nclient_id = "cid"\nowner = "old"\nrepo = "legacy"\n'
        '[repos]\nactive = "team"\n[repos.team]\nowner = "acme"\nrepo = "nbs"\n',
        "utf-8",
    )
    app = load_app_config(user_config_path=user, env={})
    assert app.aliases == ["team"]  # "legacy" is not synthesized
    assert app.client_id == "cid"  # but client_id is still read from [github]


def test_unknown_active_falls_back_to_first_alias(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text('[repos]\nactive = "nope"\n[repos.team]\nowner = "a"\nrepo = "b"\n', "utf-8")
    app = load_app_config(user_config_path=user, env={})
    assert app.active_alias == "team"


def test_config_for_unknown_alias_raises(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(REPOS_TOML, "utf-8")
    app = load_app_config(user_config_path=user, env={})
    with pytest.raises(KeyError):
        app.config_for("nope")


def test_empty_repos_section_disables_legacy_github_owner_repo(tmp_path):
    """A present-but-empty [repos] section is authoritative: it must NOT
    resurrect the legacy [github] owner/repo into a phantom repo.

    Regression for the 'phantom notebooks repo' bug: after clearing every repo
    (remove_all_repos writes [repos]={}), the still-populated legacy [github]
    owner/repo must stay disabled, so the hub shows no repo and 'repo remove'
    has nothing left to contradict itself over.
    """
    user = tmp_path / "config.toml"
    user.write_text(
        '[github]\nclient_id = "cid"\nowner = "ShipsAfterHours"\nrepo = "notebooks"\n'
        'branch = "master"\n[repos]\n',
        "utf-8",
    )
    app = load_app_config(user_config_path=user, env={})
    assert app.aliases == []  # no phantom "notebooks"
    assert app.active_alias == ""
    assert app.repos == ()
    assert not app.config_for(None).is_configured
    assert app.client_id == "cid"  # the global [github] client_id is still honoured


def test_env_owner_repo_defines_oneoff_even_with_empty_repos_section(tmp_path):
    """An empty [repos] disables the legacy [github] repo, but an explicit
    MOORING_OWNER/MOORING_REPO env override can still mint a one-off repo."""
    user = tmp_path / "config.toml"
    user.write_text('[github]\nowner = "old"\nrepo = "legacy"\n[repos]\n', "utf-8")
    app = load_app_config(
        user_config_path=user,
        env={"MOORING_OWNER": "envowner", "MOORING_REPO": "envrepo"},
    )
    assert app.aliases == ["envrepo"]  # env wins, not the legacy old/legacy
    assert app.config_for(None).repo_slug == "envowner/envrepo"


def test_active_repo_env_override(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(REPOS_TOML, "utf-8")
    app = load_app_config(user_config_path=user, env={"MOORING_ACTIVE_REPO": "team"})
    assert app.active_alias == "team"
    # field overrides apply to the env-selected active repo
    app2 = load_app_config(
        user_config_path=user,
        env={"MOORING_ACTIVE_REPO": "team", "MOORING_BRANCH": "feature"},
    )
    assert app2.config_for(None).branch == "feature"
    assert app2.config_for("sandbox").branch == "dev"  # untouched


# -- central logging ([logging] section) ------------------------------------


def test_logging_defaults_off(tmp_path):
    app = load_app_config(user_config_path=tmp_path / "missing.toml", env={})
    assert app.log_endpoint == ""
    assert app.log_level == "info"


def test_logging_from_user_config(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(
        '[logging]\nendpoint = "https://collector.example/m"\nlevel = "error"\n', "utf-8"
    )
    app = load_app_config(user_config_path=user, env={})
    assert app.log_endpoint == "https://collector.example/m"
    assert app.log_level == "error"


def test_logging_env_overrides(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text('[logging]\nendpoint = "https://baked.example"\n', "utf-8")
    app = load_app_config(
        user_config_path=user,
        env={"MOORING_LOG_ENDPOINT": r"\\server\share\logs", "MOORING_LOG_LEVEL": "error"},
    )
    assert app.log_endpoint == r"\\server\share\logs"
    assert app.log_level == "error"


def test_default_workspace_keyed_by_owner():
    a = paths.default_workspace("acme", "notebooks")
    b = paths.default_workspace("phil", "notebooks")
    assert a != b
    assert a.name == "notebooks" and a.parent.name == "acme"


def test_default_workspace_under_pythonprojects():
    ws = paths.default_workspace("acme", "notebooks")
    assert ws == Path.home() / "PythonProjects" / "mooring" / "acme" / "notebooks"
    # The default must stay out of Documents (Windows redirects it into OneDrive).
    assert "Documents" not in ws.parts


def test_legacy_workspaces_point_at_documents():
    owner_keyed, repo_keyed = paths.legacy_workspaces("acme", "notebooks")
    docs = Path(platformdirs.user_documents_dir())
    assert owner_keyed == docs / "mooring" / "acme" / "notebooks"
    assert repo_keyed == docs / "mooring" / "notebooks"


def test_legacy_hint_points_documents_users_to_new_default(tmp_path, monkeypatch):
    from mooring import cli
    from mooring.config import Config

    old = tmp_path / "Documents" / "mooring" / "acme" / "nbs"
    new = tmp_path / "PythonProjects" / "mooring" / "acme" / "nbs"
    (old / ".mooring").mkdir(parents=True)  # existing sync history under Documents
    monkeypatch.setattr(paths, "default_workspace", lambda o, r: new)
    monkeypatch.setattr(paths, "legacy_workspaces", lambda o, r: (old,))

    cfg = Config(owner="acme", repo="nbs")
    hint = cli.legacy_workspace_hint(cfg)
    assert str(old) in hint and str(new) in hint

    # Once the files live at the new default, the hint goes quiet.
    (new / ".mooring").mkdir(parents=True)
    assert cli.legacy_workspace_hint(cfg) == ""
