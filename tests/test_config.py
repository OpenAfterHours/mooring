from pathlib import Path

import platformdirs
import pytest

from mooring import paths
from mooring.ai_config import (
    MAX_TOOL_ITERS_CEILING,
    SELF_CONFIGURED_LABEL,
    AiConfig,
    BatchConfig,
    PiiConfig,
    RoutingConfig,
)
from mooring.config import AppConfig, load_app_config, load_config


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


def test_open_pr_defaults_on_and_can_be_disabled(tmp_path):
    # Propose opens the PR by default (Slice 2); [review] open_pr = false opts out.
    assert load_config(user_config_path=tmp_path / "missing.toml", env={}).open_pr is True
    p = tmp_path / "config.toml"
    p.write_text("[review]\nopen_pr = false\n", "utf-8")
    assert load_config(user_config_path=p, env={}).open_pr is False
    assert load_app_config(user_config_path=p, env={}).config_for().open_pr is False


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


def test_ai_routing_defaults_off_with_no_credentials_in_config(tmp_path):
    app = load_app_config(user_config_path=tmp_path / "missing.toml", env={})
    assert isinstance(app.ai.routing, RoutingConfig)
    assert app.ai.routing == RoutingConfig()
    assert app.ai_routing_enabled is False
    assert app.ai_trusted_base_url == ""
    assert app.ai_trusted_coding_models == ()
    assert app.ai_trusted_profile_label == "Approved AI"
    assert app.ai_trusted_model_preference == ""
    assert app.ai_default_trusted_model == ""
    assert app.ai_routing_preference == "auto"
    assert not hasattr(app.ai.routing, "api_key")


def _local_routing_toml() -> str:
    return (
        "[ai.routing]\n"
        "enabled = true\n"
        'base_url = "https://self.example/v1"\n'
        'api_version = "2026-01-01"\n'
        'classifier_model = "privacy-classifier"\n'
        'coding_model = "self-coder"\n'
        'coding_models = ["self-coder", "self-coder-fast"]\n'
    )


def test_toml_configures_a_local_profile_that_can_never_call_itself_approved(tmp_path):
    """A self-configured profile is usable, but it is not the managed one: it can
    never set the label, which is the whole basis of the chat chrome's wording."""
    user = tmp_path / "config.toml"
    user.write_text(
        _local_routing_toml() + 'profile_label = "Firm Azure OpenAI"\n', "utf-8"
    )

    app = load_app_config(user_config_path=user, env={})

    assert app.ai_routing_source == "local"
    assert app.ai_routing_enabled is True
    assert app.ai_trusted_base_url == "https://self.example/v1"
    assert app.ai_trusted_api_version == "2026-01-01"
    assert app.ai_trusted_classifier_model == "privacy-classifier"
    assert app.ai_trusted_coding_model == "self-coder"
    assert app.ai_trusted_coding_models == ("self-coder", "self-coder-fast")
    # The label the file asked for is DISCARDED — a constant takes its place.
    assert app.ai_trusted_profile_label == SELF_CONFIGURED_LABEL
    assert app.ai_trusted_profile_label != "Firm Azure OpenAI"
    # And a local profile never becomes the managed one.
    assert app.ai.routing.enabled is False
    assert not hasattr(app.ai.routing, "api_key")


def test_managed_env_profile_wins_over_a_local_one(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(_local_routing_toml(), "utf-8")

    overridden = load_app_config(
        user_config_path=user,
        env={
            "MOORING_AI_ROUTING": "1",
            "MOORING_AI_TRUSTED_BASE_URL": "https://admin.example/openai",
            "MOORING_AI_TRUSTED_API_VERSION": "2026-08-01",
            "MOORING_AI_TRUSTED_CLASSIFIER_MODEL": "admin-classifier",
            "MOORING_AI_TRUSTED_CODING_MODEL": "admin-coder",
        },
    )

    assert overridden.ai_routing_source == "managed"
    assert overridden.ai_trusted_base_url == "https://admin.example/openai"
    assert overridden.ai_trusted_classifier_model == "admin-classifier"
    assert overridden.ai_trusted_coding_models == ("admin-coder",)
    assert overridden.ai_trusted_profile_label == "Approved AI"
    # The local values are still READ (the Settings form renders them) — they are
    # simply not the live profile.
    assert overridden.ai_routing_local_base_url == "https://self.example/v1"


def test_a_launcher_that_switches_routing_off_also_switches_the_local_profile_off(
    tmp_path,
):
    """MOORING_AI_ROUTING is authoritative whenever PRESENT, so a managed
    deployment can forbid the feature without trusting the analyst's config."""
    user = tmp_path / "config.toml"
    user.write_text(_local_routing_toml(), "utf-8")

    app = load_app_config(user_config_path=user, env={"MOORING_AI_ROUTING": "0"})

    assert app.ai_routing_source == "off"
    assert app.ai_routing_enabled is False
    assert app.ai_trusted_base_url == ""
    assert app.ai_trusted_coding_models == ()


def test_a_malformed_local_routing_table_degrades_instead_of_raising(tmp_path):
    """[ai.routing] is hand-editable, so a wrong type must not stop the hub booting."""
    user = tmp_path / "config.toml"
    user.write_text(
        "[ai.routing]\nenabled = true\ncoding_models = 42\n", "utf-8"
    )

    app = load_app_config(user_config_path=user, env={})

    assert app.ai_routing_local_coding_models == ()
    assert app.ai_trusted_coding_models == ()


def test_ai_trusted_model_allowlist_is_trimmed_deduped_and_exact(tmp_path):
    app = load_app_config(
        user_config_path=tmp_path / "missing.toml",
        env={
            "MOORING_AI_ROUTING": "1",
            "MOORING_AI_TRUSTED_BASE_URL": "https://admin.example/openai",
            "MOORING_AI_TRUSTED_CLASSIFIER_MODEL": "classifier",
            "MOORING_AI_TRUSTED_CODING_MODEL": "coder-default",
            "MOORING_AI_TRUSTED_CODING_MODELS": (
                " coder-default, coder-fast, coder-default ,Coder-Fast "
            ),
            "MOORING_AI_TRUSTED_PROFILE_LABEL": " Firm Azure OpenAI ",
        },
    )

    assert app.ai.routing.coding_models == (
        "coder-default",
        "coder-fast",
        "Coder-Fast",
    )
    assert app.ai_trusted_coding_models == app.ai.routing.coding_models
    assert app.ai_trusted_profile_label == "Firm Azure OpenAI"


def test_user_trusted_defaults_are_constrained_by_the_managed_allowlist(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(
        '[ai]\ntrusted_model = "coder-fast"\nrouting_preference = "trusted"\n',
        "utf-8",
    )
    env = {
        "MOORING_AI_ROUTING": "1",
        "MOORING_AI_TRUSTED_CODING_MODEL": "coder-default",
        "MOORING_AI_TRUSTED_CODING_MODELS": "coder-default,coder-fast",
    }

    app = load_app_config(user_config_path=user, env=env)

    assert app.ai_trusted_model_preference == "coder-fast"
    assert app.ai_default_trusted_model == "coder-fast"
    assert app.ai_routing_preference == "trusted"


def test_stale_user_trusted_model_falls_back_and_bad_routing_fails_upward(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(
        '[ai]\ntrusted_model = "removed-model"\nrouting_preference = "general"\n',
        "utf-8",
    )
    app = load_app_config(
        user_config_path=user,
        env={
            "MOORING_AI_ROUTING": "1",
            "MOORING_AI_TRUSTED_CODING_MODEL": "coder-default",
            "MOORING_AI_TRUSTED_CODING_MODELS": "coder-default,coder-fast",
        },
    )

    assert app.ai_trusted_model_preference == "removed-model"
    assert app.ai_default_trusted_model == "coder-default"
    assert app.ai_routing_preference == "trusted"


@pytest.mark.parametrize("models", ["", "other-coder, another-coder"])
def test_ai_trusted_model_allowlist_must_include_default(tmp_path, models):
    with pytest.raises(ValueError, match="must be non-empty and include"):
        load_app_config(
            user_config_path=tmp_path / "missing.toml",
            env={
                "MOORING_AI_TRUSTED_CODING_MODEL": "coder-default",
                "MOORING_AI_TRUSTED_CODING_MODELS": models,
            },
        )


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


def test_ai_auto_apply_defaults_on_with_flat_shims(tmp_path):
    # The copilot's write lands without an Apply click, it may re-run the value-free
    # smoke path to report a failure back, and the tool-call loop has a high ceiling
    # (a runaway backstop, not a work budget — Cancel is the control).
    app = load_app_config(user_config_path=tmp_path / "missing.toml", env={})
    assert app.ai.auto_apply is True and app.ai_auto_apply is True
    assert app.ai.auto_run_report is True and app.ai_auto_run_report is True
    assert app.ai.max_tool_iters == 200 and app.ai_max_tool_iters == 200


def test_ai_auto_apply_toml_and_env_override(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(
        "[ai]\nauto_apply = false\nauto_run_report = false\nmax_tool_iters = 12\n", "utf-8"
    )
    app = load_app_config(user_config_path=user, env={})
    assert app.ai_auto_apply is False
    assert app.ai_auto_run_report is False
    assert app.ai_max_tool_iters == 12
    # MOORING_* is the top LOCAL layer and overrides the file in both directions
    # (policy still sits above all three — see tests/test_policy.py).
    on = load_app_config(
        user_config_path=user,
        env={
            "MOORING_AI_AUTO_APPLY": "1",
            "MOORING_AI_AUTO_RUN_REPORT": "true",
            "MOORING_AI_MAX_TOOL_ITERS": "500",
        },
    )
    assert on.ai_auto_apply is True and on.ai_auto_run_report is True
    assert on.ai_max_tool_iters == 500
    user.write_text("", "utf-8")
    off = load_app_config(
        user_config_path=user,
        env={"MOORING_AI_AUTO_APPLY": "0", "MOORING_AI_AUTO_RUN_REPORT": "off"},
    )
    assert off.ai_auto_apply is False and off.ai_auto_run_report is False


@pytest.mark.parametrize("bad", [0, -1, -200])
def test_max_tool_iters_ignores_a_file_value_that_would_kill_every_turn(tmp_path, bad):
    """config.toml is hand-editable, and a ceiling of 0 is a plausible thing to type.
    It would end every turn BEFORE the model's first tool call — which reads as a
    broken copilot, not as a setting — so it falls back to the shipped default."""
    user = tmp_path / "config.toml"
    user.write_text(f"[ai]\nmax_tool_iters = {bad}\n", "utf-8")
    assert load_app_config(user_config_path=user, env={}).ai_max_tool_iters == 200


@pytest.mark.parametrize("bad", ["0", "-5", "lots", ""])
def test_max_tool_iters_ignores_a_bad_env_value_and_keeps_the_file_choice(tmp_path, bad):
    """The env var is typed at a shell, so a typo must not raise inside the loader
    (that would take the hub out) nor silently zero the ceiling: the file's own good
    value survives."""
    user = tmp_path / "config.toml"
    user.write_text("[ai]\nmax_tool_iters = 50\n", "utf-8")
    app = load_app_config(user_config_path=user, env={"MOORING_AI_MAX_TOOL_ITERS": bad})
    assert app.ai_max_tool_iters == 50


def test_max_tool_iters_is_capped_rather_than_unbounded(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text("[ai]\nmax_tool_iters = 999999999\n", "utf-8")
    app = load_app_config(user_config_path=user, env={})
    assert app.ai_max_tool_iters == MAX_TOOL_ITERS_CEILING


def test_the_tool_call_ceiling_accessor_floors_a_hand_built_config():
    """The loader clamps what it READS; the flat accessor closes the other door, so a
    zero can never reach the tool loop from an AiConfig built in code."""
    assert AppConfig(ai=AiConfig(max_tool_iters=0)).ai_max_tool_iters == 1
    assert AppConfig(ai=AiConfig(max_tool_iters=-9)).ai_max_tool_iters == 1


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


def test_sync_folders_drop_root_sentinels_and_canonicalize(tmp_path):
    # A folder that resolves to the workspace root (".", "") or escapes it ("x/../y")
    # would make the local (filesystem rglob) and remote (path-prefix) scans diverge and
    # delete files on pull, so those are dropped; "reports/" and "./data" canonicalize.
    # Loose root files sync on their own rule, so dropping "." loses no coverage.
    user = tmp_path / "config.toml"
    user.write_text(
        '[sync]\nfolders = [".", "", "notebooks", "reports/", "./data", "x/../y"]\n',
        "utf-8",
    )
    cfg = load_config(user_config_path=user, env={})
    assert cfg.folders == ("notebooks", "reports", "data")


def test_sync_folders_backslash_is_canonicalized(tmp_path):
    # A Windows-style hand edit with a literal backslash must canonicalize to a POSIX
    # sub-path, or the two scan sides disagree about the same folder. A TOML literal
    # string ('...') keeps the backslash verbatim.
    user = tmp_path / "config.toml"
    user.write_text("[sync]\nfolders = ['pkg\\utils']\n", "utf-8")
    assert load_config(user_config_path=user, env={}).folders == ("pkg/utils",)


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


# -- multiple accounts ([accounts] tables, bound per repo) ------------------------


ACCOUNTS_TOML = """
[accounts]
active = "work"

[accounts.work]
host = "ghe.service.group"
login = "a.harrison"
client_id = "cid_ghe"

[accounts.personal]
host = "github.com"
login = "phil"
client_id = "cid_dotcom"

[repos]
active = "analytics"

[repos.analytics]
account = "work"
owner = "service-analytics"
repo = "notebooks"

[repos.side]
account = "personal"
owner = "phil"
repo = "scratch"
"""


def test_each_repo_resolves_its_own_account(tmp_path):
    """The core of the feature: host, client_id and login all follow the repo."""
    user = tmp_path / "config.toml"
    user.write_text(ACCOUNTS_TOML, "utf-8")
    app = load_app_config(user_config_path=user, env={})

    work = app.config_for("analytics")
    assert (work.host, work.client_id) == ("ghe.service.group", "cid_ghe")
    assert work.account_login == "a.harrison"
    assert work.token_slot == ("ghe.service.group", "a.harrison")

    personal = app.config_for("side")
    assert (personal.host, personal.client_id) == ("github.com", "cid_dotcom")
    assert personal.token_slot == ("github.com", "phil")


def test_two_accounts_on_one_host_stay_distinct(tmp_path):
    """Host alone cannot separate these — the login is what makes them two slots."""
    user = tmp_path / "config.toml"
    user.write_text(
        "[accounts.a]\nlogin = 'alice'\nclient_id = 'c'\n"
        "[accounts.b]\nlogin = 'bob'\nclient_id = 'c'\n"
        "[repos]\nactive = 'ra'\n"
        "[repos.ra]\naccount = 'a'\nowner = 'o'\nrepo = 'r'\n"
        "[repos.rb]\naccount = 'b'\nowner = 'o'\nrepo = 'r2'\n",
        "utf-8",
    )
    app = load_app_config(user_config_path=user, env={})
    assert app.config_for("ra").token_slot == ("github.com", "alice")
    assert app.config_for("rb").token_slot == ("github.com", "bob")


def test_repo_bound_to_a_missing_account_degrades_and_never_raises(tmp_path):
    """I1. Hub.app_cfg calls config_for on every read, so raising here would 500
    every route — including the ones that would let the user fix it."""
    user = tmp_path / "config.toml"
    user.write_text(
        "[accounts.work]\nhost = 'ghe.example'\nlogin = 'a'\nclient_id = 'c'\n"
        "[repos]\nactive = 'orphan'\n"
        "[repos.orphan]\naccount = 'gone'\nowner = 'o'\nrepo = 'r'\n",
        "utf-8",
    )
    app = load_app_config(user_config_path=user, env={})
    cfg = app.config_for(None)  # must not raise
    assert "gone" in cfg.account_error
    assert cfg.token_slot is None  # fail closed: no token may be handed out
    assert cfg.owner == "o" and cfg.repo == "r"  # the repo itself still resolves


def test_account_that_never_finished_signing_in_yields_no_token(tmp_path):
    """I2. A blank login must NOT fall through to the pre-accounts host-keyed slot,
    which may still hold a previous user's token."""
    user = tmp_path / "config.toml"
    user.write_text(
        "[accounts.work]\nhost = 'github.com'\nclient_id = 'c'\n"  # no login yet
        "[repos]\nactive = 'r'\n"
        "[repos.r]\naccount = 'work'\nowner = 'o'\nrepo = 'r'\n",
        "utf-8",
    )
    cfg = load_app_config(user_config_path=user, env={}).config_for(None)
    assert cfg.token_slot is None
    assert "not signed in" in cfg.account_error


def test_unbound_repo_still_reads_the_legacy_host_keyed_token(tmp_path):
    """The other half of I2: upgrades stay seamless. A repo with no account is a
    pre-accounts repo and that host-keyed token is genuinely its own."""
    user = tmp_path / "config.toml"
    user.write_text(REPOS_TOML, "utf-8")
    cfg = load_app_config(user_config_path=user, env={}).config_for(None)
    assert cfg.account == "" and cfg.account_error == ""
    assert cfg.token_slot == ("github.com", "")


def test_a_bad_account_host_drops_that_account_only(tmp_path):
    """Tolerant parsing: normalize_host raises, and an exception on every config
    load would wedge every command including the one that fixes it."""
    user = tmp_path / "config.toml"
    user.write_text(
        "[accounts.broken]\nhost = 'not a host'\nlogin = 'x'\n"
        "[accounts.fine]\nhost = 'ghe.example'\nlogin = 'y'\nclient_id = 'c'\n",
        "utf-8",
    )
    app = load_app_config(user_config_path=user, env={})
    assert [a.alias for a in app.accounts] == ["fine"]
    assert app.ignored_accounts and app.ignored_accounts[0][0] == "broken"


def test_accounts_survive_a_cleared_repo_registry(tmp_path):
    """`repo remove --all` writes [repos] = {}. Accounts live in their own section
    precisely so that cannot wipe every credential."""
    user = tmp_path / "config.toml"
    user.write_text("[accounts.work]\nlogin = 'a'\nclient_id = 'c'\n[repos]\n", "utf-8")
    app = load_app_config(user_config_path=user, env={})
    assert app.repos == ()
    assert [a.alias for a in app.accounts] == ["work"]


def test_account_env_overrides_retarget_only_the_active_repos_account(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text(ACCOUNTS_TOML, "utf-8")
    app = load_app_config(
        user_config_path=user,
        env={"MOORING_GITHUB_HOST": "other.example", "MOORING_CLIENT_ID": "cid_env"},
    )
    active = app.config_for(None)  # "analytics", bound to "work"
    assert (active.host, active.client_id) == ("other.example", "cid_env")
    other = app.config_for("side")  # untouched
    assert (other.host, other.client_id) == ("github.com", "cid_dotcom")


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


def test_default_workspace_separates_the_same_slug_on_two_hosts():
    """owner/repo is unique only WITHIN a GitHub instance. Sharing a folder would
    share one .mooring/manifest.json — base SHAs from the wrong remote."""
    dotcom = paths.default_workspace("acme", "notebooks")
    ghe = paths.default_workspace("acme", "notebooks", "ghe.example")
    assert dotcom != ghe
    assert ghe == Path.home() / "PythonProjects" / "mooring" / "ghe.example" / "acme" / "notebooks"


def test_default_workspace_on_github_com_is_unchanged():
    """The back-compat half of the host keying: existing workspaces must not move."""
    assert paths.default_workspace("acme", "nbs", "github.com") == paths.default_workspace(
        "acme", "nbs"
    )


def test_default_workspace_host_port_uses_a_safe_folder_name():
    ws = paths.default_workspace("acme", "nbs", "ghe.example:8443")
    # ":" is not a legal NTFS filename character — the host segment must be sanitised
    # the way auth._token_file already sanitises it.
    assert "ghe.example_8443" in ws.parts
    assert not any(":" in part for part in ws.parts[1:])


def test_legacy_workspaces_offer_the_pre_host_path_on_enterprise():
    """An Enterprise user's workspace moves under the host, so the migration hint
    (runtime.legacy_workspace_hint) has to know where it used to be."""
    olds = paths.legacy_workspaces("acme", "nbs", "ghe.example")
    assert olds[0] == Path.home() / "PythonProjects" / "mooring" / "acme" / "nbs"
    assert paths.legacy_workspaces("acme", "nbs") == olds[1:]


def test_legacy_hint_points_documents_users_to_new_default(tmp_path, monkeypatch):
    from mooring import cli
    from mooring.config import Config

    old = tmp_path / "Documents" / "mooring" / "acme" / "nbs"
    new = tmp_path / "PythonProjects" / "mooring" / "acme" / "nbs"
    (old / ".mooring").mkdir(parents=True)  # existing sync history under Documents
    monkeypatch.setattr(paths, "default_workspace", lambda o, r, h=None: new)
    monkeypatch.setattr(paths, "legacy_workspaces", lambda o, r, h=None: (old,))

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


# -- the credential SOURCE an account uses -------------------------------------


def test_auth_method_defaults_to_the_device_flow(tmp_path):
    """Every config written before the method existed must keep its exact meaning."""
    user = tmp_path / "config.toml"
    user.write_text(
        "[accounts.work]\nhost = 'ghe.example'\nlogin = 'a'\nclient_id = 'c'\n"
        "[repos]\nactive = 'team'\n"
        "[repos.team]\naccount = 'work'\nowner = 'o'\nrepo = 'r'\n",
        "utf-8",
    )
    app = load_app_config(user_config_path=user, env={})
    assert app.account("work").auth == "device"
    assert app.config_for("team").auth_method == "device"


def test_a_borrowed_account_needs_no_client_id_to_be_configured(tmp_path):
    """The 'git' method has no OAuth app at all, so requiring a client id would drop
    the repo into local mode and make it look like it had vanished."""
    user = tmp_path / "config.toml"
    user.write_text(
        "[accounts.work]\nhost = 'acme.ghe.com'\nlogin = 'acme_phil'\nauth = 'git'\n"
        "[repos]\nactive = 'team'\n"
        "[repos.team]\naccount = 'work'\nowner = 'acme'\nrepo = 'nbs'\n",
        "utf-8",
    )
    cfg = load_app_config(user_config_path=user, env={}).config_for("team")
    assert cfg.client_id == "" and cfg.is_configured
    assert cfg.auth_method == "git"
    assert cfg.token_slot == ("acme.ghe.com", "acme_phil")


def test_an_unknown_auth_method_falls_back_to_device(tmp_path):
    """Fail closed on a corrupt or future value: 'device' looks for a STORED token
    and finds none, which reads as not-signed-in — never 'use anything to hand'."""
    user = tmp_path / "config.toml"
    user.write_text(
        "[accounts.work]\nhost = 'ghe.example'\nlogin = 'a'\nauth = 'telepathy'\n"
        "[repos]\nactive = 'team'\n"
        "[repos.team]\naccount = 'work'\nowner = 'o'\nrepo = 'r'\n",
        "utf-8",
    )
    app = load_app_config(user_config_path=user, env={})
    assert app.account("work").auth == "device"


def test_an_unbound_repo_still_needs_a_client_id(tmp_path):
    """The pre-accounts path has no account record to sign in through, so a client
    id remains the only thing that makes it usable."""
    user = tmp_path / "config.toml"
    user.write_text("[github]\nowner = 'o'\nrepo = 'r'\nclient_id = ''\n", "utf-8")
    cfg = load_app_config(user_config_path=user, env={}).config_for(None)
    assert cfg.account == "" and not cfg.is_configured
