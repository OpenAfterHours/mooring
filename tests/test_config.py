from pathlib import Path

import platformdirs
import pytest

from mooring import paths
from mooring.ai_config import AiConfig, BatchConfig, PiiConfig
from mooring.config import load_app_config, load_config


def test_defaults_when_no_user_config(tmp_path):
    cfg = load_config(user_config_path=tmp_path / "missing.toml", env={})
    assert not cfg.is_configured
    assert cfg.branch == "main"
    assert cfg.folders == ("notebooks", "data", "reports")
    assert cfg.exclude == ()
    assert cfg.warn_file_mb == 10
    assert cfg.warn_shadowed_notebooks is True  # the shadow guard is on by default


def test_warn_shadowed_notebooks_can_be_disabled(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[sync]\nwarn_shadowed_notebooks = false\n", "utf-8")
    assert load_config(user_config_path=p, env={}).warn_shadowed_notebooks is False
    app = load_app_config(user_config_path=p, env={})
    assert app.warn_shadowed_notebooks is False
    assert app.config_for().warn_shadowed_notebooks is False


def test_ai_config_is_nested_with_flat_shims(tmp_path):
    # The canonical store is the nested ai/ai.pii config; the flat ai_*/ai_pii_*
    # accessors forward to it, and the guard defaults OFF.
    app = load_app_config(user_config_path=tmp_path / "missing.toml", env={})
    assert isinstance(app.ai, AiConfig) and isinstance(app.ai.pii, PiiConfig)
    assert app.ai.pii.enabled is False  # default OFF preserved
    assert app.ai_enabled is app.ai.enabled
    assert app.ai_pii is app.ai.pii.enabled
    assert app.ai_pii_block_prompt is app.ai.pii.block_prompt
    assert app.ai_pii_name_model == app.ai.pii.name_model
    assert app.ai_pii_name_labels == app.ai.pii.name_labels


def test_ai_pii_toml_and_env_populate_the_nested_object(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text("[ai.pii]\nenabled = true\nblock_prompt = false\n", "utf-8")
    app = load_app_config(user_config_path=user, env={})
    assert app.ai.pii.enabled is True and app.ai.pii.block_prompt is False
    assert app.ai_pii is True  # the flat shim agrees with the nested store
    # env overrides the file, written straight onto the nested object
    app2 = load_app_config(user_config_path=user, env={"MOORING_AI_PII": "0"})
    assert app2.ai.pii.enabled is False


def test_ai_batch_config_defaults_off_with_caps(tmp_path):
    # The batch orchestrator is OFF by default, with conservative resource caps and a
    # non-interactive PII policy that defaults to blocking just the offending job.
    app = load_app_config(user_config_path=tmp_path / "missing.toml", env={})
    assert isinstance(app.ai.batch, BatchConfig)
    assert app.ai.batch.enabled is False
    assert app.ai_batch_enabled is False  # flat shim agrees
    assert app.ai_batch_max_jobs == 20
    assert app.ai_batch_max_concurrency == 3
    assert app.ai_batch_pii_policy == "block_job"


def test_ai_batch_toml_and_env_populate_the_nested_object(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(
        "[ai.batch]\nenabled = true\nmax_jobs = 5\nmax_concurrency = 2\n"
        'pii_policy = "block_batch"\n',
        "utf-8",
    )
    app = load_app_config(user_config_path=user, env={})
    assert app.ai.batch.enabled is True
    assert app.ai_batch_max_jobs == 5 and app.ai_batch_max_concurrency == 2
    assert app.ai_batch_pii_policy == "block_batch"
    # env overrides the file, written straight onto the nested object
    app2 = load_app_config(user_config_path=user, env={"MOORING_AI_BATCH": "0"})
    assert app2.ai.batch.enabled is False


def test_ai_semantic_model_defaults_on_with_flat_shim(tmp_path):
    # Semantic-model reading defaults ON (the content is the notebook-source
    # class — authored code); the flat accessor forwards to the nested store.
    app = load_app_config(user_config_path=tmp_path / "missing.toml", env={})
    assert app.ai.semantic_model is True
    assert app.ai_semantic_model is app.ai.semantic_model


def test_ai_semantic_model_toml_and_env_override(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text("[ai]\nsemantic_model = false\n", "utf-8")
    app = load_app_config(user_config_path=user, env={})
    assert app.ai.semantic_model is False and app.ai_semantic_model is False
    # env overrides the file in both directions
    on = load_app_config(user_config_path=user, env={"MOORING_AI_SEMANTIC_MODEL": "1"})
    assert on.ai_semantic_model is True
    user.write_text("", "utf-8")
    off = load_app_config(user_config_path=user, env={"MOORING_AI_SEMANTIC_MODEL": "0"})
    assert off.ai_semantic_model is False


def test_ai_pii_name_backend_defaults_and_parses(tmp_path):
    app = load_app_config(user_config_path=tmp_path / "missing.toml", env={})
    assert app.ai.pii.name_backend == "auto"  # default: auto-select at runtime
    assert app.ai_pii_name_backend == "auto"  # flat shim agrees
    user = tmp_path / "config.toml"
    user.write_text('[ai.pii]\nname_backend = "spacy"\n', "utf-8")
    assert load_app_config(user_config_path=user, env={}).ai.pii.name_backend == "spacy"
    # env override still wins over the file/default
    assert (
        load_app_config(
            user_config_path=user, env={"MOORING_AI_PII_NAME_BACKEND": "gliner"}
        ).ai.pii.name_backend
        == "gliner"
    )


def test_ui_theme_defaults_to_system(tmp_path):
    app = load_app_config(user_config_path=tmp_path / "missing.toml", env={})
    assert app.ui_theme == "system"  # the shipped default = follow the OS


def test_ui_theme_parses_file_and_env(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text('[ui]\ntheme = "dark"\n', "utf-8")
    assert load_app_config(user_config_path=user, env={}).ui_theme == "dark"
    # env overrides the file
    assert (
        load_app_config(user_config_path=user, env={"MOORING_UI_THEME": "light"}).ui_theme
        == "light"
    )


def test_ui_theme_invalid_falls_back_to_default(tmp_path):
    # A stray/unknown value must never wedge the hub on an invalid appearance.
    user = tmp_path / "config.toml"
    user.write_text('[ui]\ntheme = "neon"\n', "utf-8")
    assert load_app_config(user_config_path=user, env={}).ui_theme == "system"


def test_normalize_theme():
    from mooring.config import DEFAULT_THEME, normalize_theme

    assert normalize_theme("Dark") == "dark"  # case-insensitive, trimmed
    assert normalize_theme("  light ") == "light"
    assert normalize_theme("") == DEFAULT_THEME
    assert normalize_theme(None) == DEFAULT_THEME
    assert normalize_theme("bogus") == DEFAULT_THEME


def test_sync_exclude_is_parsed(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text('[sync]\nexclude = ["*.tmp", "scratch", "reports/drafts/*"]\n', "utf-8")
    cfg = load_config(user_config_path=user, env={})
    assert cfg.exclude == ("*.tmp", "scratch", "reports/drafts/*")


def test_sync_exclude_bare_string_is_single_pattern(tmp_path):
    # `exclude = "*.tmp"` must be one pattern, not the chars ('*','.','t','m','p')
    # — the stray '*' would otherwise match every segment and hide everything.
    user = tmp_path / "config.toml"
    user.write_text('[sync]\nexclude = "*.tmp"\n', "utf-8")
    assert load_config(user_config_path=user, env={}).exclude == ("*.tmp",)


def test_sync_exclude_rejects_non_string_array(tmp_path):
    # An accidental [sync.exclude] table (a dict) or non-string entries should
    # fail loudly rather than coerce to silent garbage patterns.
    table = tmp_path / "table.toml"
    table.write_text("[sync.exclude]\nfoo = 1\n", "utf-8")
    with pytest.raises(ValueError):
        load_config(user_config_path=table, env={})
    nums = tmp_path / "nums.toml"
    nums.write_text("[sync]\nexclude = [1, 2]\n", "utf-8")
    with pytest.raises(ValueError):
        load_config(user_config_path=nums, env={})


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


def test_context_folder_not_synced_when_feature_off(tmp_path):
    # Opt-in: with [ai] context off (the default) the sync surface is exactly
    # [sync] folders, so pull/push behaviour is unchanged.
    app = load_app_config(user_config_path=tmp_path / "missing.toml", env={})
    assert app.ai.context is False
    assert app.sync_folders == ("notebooks", "data", "reports")
    assert app.config_for(None).folders == ("notebooks", "data", "reports")


def test_context_folder_synced_when_feature_on(tmp_path):
    # Enabling the team-context feature folds context_dir into the synced folders,
    # so the folder rides BOTH push and pull without a hand-edited [sync] folders.
    user = tmp_path / "config.toml"
    user.write_text("[ai]\ncontext = true\n", "utf-8")
    app = load_app_config(user_config_path=user, env={})
    assert app.sync_folders == ("notebooks", "data", "reports", "context")
    assert app.config_for(None).folders == ("notebooks", "data", "reports", "context")


def test_context_folder_custom_dir_and_no_duplicate(tmp_path):
    # A custom context_dir is honoured, and a context_dir already listed in
    # [sync] folders is not added twice.
    user = tmp_path / "config.toml"
    user.write_text(
        '[sync]\nfolders = ["notebooks", "team-context"]\n'
        '[ai]\ncontext = true\ncontext_dir = "team-context"\n',
        "utf-8",
    )
    app = load_app_config(user_config_path=user, env={})
    assert app.sync_folders == ("notebooks", "team-context")


def test_context_folder_synced_with_configured_repo(tmp_path):
    # The folder is folded in for an aliased repo's Config too, not just the
    # no-repo path.
    user = tmp_path / "config.toml"
    user.write_text(REPOS_TOML + "\n[ai]\ncontext = true\n", "utf-8")
    app = load_app_config(user_config_path=user, env={})
    assert "context" in app.config_for("team").folders


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


def test_ai_reasoning_effort_from_config_and_env(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text('[ai]\nreasoning_effort = "high"\n', "utf-8")
    assert load_app_config(user_config_path=user, env={}).ai_reasoning_effort == "high"
    # env overrides the file
    app = load_app_config(user_config_path=user, env={"MOORING_AI_REASONING_EFFORT": "xhigh"})
    assert app.ai_reasoning_effort == "xhigh"
    # default is empty (= the model's default)
    assert (
        load_app_config(user_config_path=tmp_path / "missing.toml", env={}).ai_reasoning_effort
        == ""
    )
