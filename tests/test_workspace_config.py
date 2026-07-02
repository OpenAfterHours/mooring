"""Tests for the synced per-workspace mooring.toml (the per-notebook AI opt-out)."""

import tomllib

import pytest

from mooring import workspace_config as wc


def test_missing_file_is_empty(tmp_path):
    assert wc.disabled_notebooks(tmp_path) == set()
    assert wc.is_ai_disabled(tmp_path, "notebooks/a.py") is False


def test_set_and_clear_round_trip(tmp_path):
    assert wc.set_ai_disabled(tmp_path, "notebooks/a.py", True) is True
    assert wc.is_ai_disabled(tmp_path, "notebooks/a.py") is True

    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["ai"]["disabled_notebooks"] == ["notebooks/a.py"]

    assert wc.set_ai_disabled(tmp_path, "notebooks/a.py", False) is False
    assert wc.is_ai_disabled(tmp_path, "notebooks/a.py") is False
    # A file left wholly empty is removed, not written empty (it would otherwise show
    # up as a spurious new-local file to sync).
    assert not (tmp_path / "mooring.toml").exists()


def test_enable_noop_leaves_no_file(tmp_path):
    # Disabling then re-enabling (or any disabled=False on a clean workspace) must
    # not leave an empty mooring.toml behind for sync to pick up.
    wc.set_ai_disabled(tmp_path, "notebooks/a.py", False)
    assert not (tmp_path / "mooring.toml").exists()


def test_corrupt_file_is_not_clobbered_on_write(tmp_path):
    # The write path must NOT fail open (that would silently drop unrelated keys).
    original = "this is = not valid = toml"
    (tmp_path / "mooring.toml").write_text(original, "utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        wc.set_ai_disabled(tmp_path, "notebooks/a.py", True)
    assert (tmp_path / "mooring.toml").read_text("utf-8") == original  # untouched


def test_normalization_and_dedupe(tmp_path):
    wc.set_ai_disabled(tmp_path, "notebooks\\a.py", True)  # backslash
    wc.set_ai_disabled(tmp_path, "/notebooks/a.py/", True)  # surrounding slashes
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["ai"]["disabled_notebooks"] == ["notebooks/a.py"]  # one entry
    assert wc.is_ai_disabled(tmp_path, "notebooks/a.py")


def test_sorted_stable_order(tmp_path):
    wc.set_ai_disabled(tmp_path, "notebooks/z.py", True)
    wc.set_ai_disabled(tmp_path, "notebooks/a.py", True)
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["ai"]["disabled_notebooks"] == ["notebooks/a.py", "notebooks/z.py"]


def test_preserves_unrelated_keys_and_sections(tmp_path):
    (tmp_path / "mooring.toml").write_text(
        '[other]\nkeep = "me"\n\n[ai]\nsomething_else = 7\n', "utf-8"
    )
    wc.set_ai_disabled(tmp_path, "notebooks/a.py", True)
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["other"] == {"keep": "me"}
    assert data["ai"]["something_else"] == 7
    assert data["ai"]["disabled_notebooks"] == ["notebooks/a.py"]

    # Removing the opt-out prunes only disabled_notebooks; the sibling [ai] key stays.
    wc.set_ai_disabled(tmp_path, "notebooks/a.py", False)
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["ai"] == {"something_else": 7}
    assert data["other"] == {"keep": "me"}


def test_bare_string_value_tolerated(tmp_path):
    (tmp_path / "mooring.toml").write_text('[ai]\ndisabled_notebooks = "notebooks/a.py"\n', "utf-8")
    assert wc.disabled_notebooks(tmp_path) == {"notebooks/a.py"}


def test_corrupt_file_fails_open(tmp_path):
    (tmp_path / "mooring.toml").write_text("this is = not valid = toml", "utf-8")
    assert wc.disabled_notebooks(tmp_path) == set()
    assert wc.is_ai_disabled(tmp_path, "notebooks/a.py") is False


# -- per-model AI opt-out (Power BI semantic models) ----------------------------


def test_semantic_models_missing_file_is_empty(tmp_path):
    assert wc.disabled_semantic_models(tmp_path) == set()
    assert wc.is_semantic_model_disabled(tmp_path, "reports/Sales") is False


def test_semantic_model_set_and_clear_round_trip(tmp_path):
    assert wc.set_semantic_model_disabled(tmp_path, "reports/Sales", True) is True
    assert wc.is_semantic_model_disabled(tmp_path, "reports/Sales") is True
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["ai"]["disabled_semantic_models"] == ["reports/Sales"]

    assert wc.set_semantic_model_disabled(tmp_path, "reports/Sales", False) is False
    assert not (tmp_path / "mooring.toml").exists()  # pruned, nothing spurious to sync


def test_semantic_model_normalization_sort_and_dedupe(tmp_path):
    wc.set_semantic_model_disabled(tmp_path, "reports\\Zeta", True)  # backslash
    wc.set_semantic_model_disabled(tmp_path, "/reports/Alpha/", True)  # surrounding slashes
    wc.set_semantic_model_disabled(tmp_path, "reports/Zeta", True)  # duplicate of the first
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["ai"]["disabled_semantic_models"] == ["reports/Alpha", "reports/Zeta"]


def test_semantic_model_round_trip_preserves_other_keys(tmp_path):
    (tmp_path / "mooring.toml").write_text(
        '[other]\nkeep = "me"\n\n[ai]\ndisabled_notebooks = ["notebooks/a.py"]\n', "utf-8"
    )
    wc.set_semantic_model_disabled(tmp_path, "reports/Sales", True)
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["other"] == {"keep": "me"}
    assert data["ai"]["disabled_notebooks"] == ["notebooks/a.py"]  # sibling key untouched
    assert data["ai"]["disabled_semantic_models"] == ["reports/Sales"]

    # Removing the model opt-out prunes only its key; the notebook opt-out stays.
    wc.set_semantic_model_disabled(tmp_path, "reports/Sales", False)
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["ai"] == {"disabled_notebooks": ["notebooks/a.py"]}
    assert data["other"] == {"keep": "me"}


def test_semantic_model_write_does_not_clobber_corrupt_file(tmp_path):
    original = "this is = not valid = toml"
    (tmp_path / "mooring.toml").write_text(original, "utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        wc.set_semantic_model_disabled(tmp_path, "reports/Sales", True)
    assert (tmp_path / "mooring.toml").read_text("utf-8") == original  # untouched


def test_semantic_model_read_fails_open_on_corrupt_file(tmp_path):
    (tmp_path / "mooring.toml").write_text("this is = not valid = toml", "utf-8")
    assert wc.disabled_semantic_models(tmp_path) == set()


# -- synced extra folders -----------------------------------------------------


def test_extra_folders_missing_is_empty(tmp_path):
    assert wc.extra_folders(tmp_path) == ()


def test_add_extra_folder_round_trip(tmp_path):
    wc.add_extra_folder(tmp_path, "packages/finance/notebooks")
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["sync"]["folders"] == ["packages/finance/notebooks"]
    assert wc.extra_folders(tmp_path) == ("packages/finance/notebooks",)


def test_add_extra_folder_normalizes_dedupes_and_sorts(tmp_path):
    wc.add_extra_folder(tmp_path, "packages\\sales\\notebooks")  # backslashes
    wc.add_extra_folder(tmp_path, "/packages/finance/notebooks/")  # surrounding slashes
    wc.add_extra_folder(tmp_path, "packages/sales/notebooks")  # duplicate of the first
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["sync"]["folders"] == ["packages/finance/notebooks", "packages/sales/notebooks"]


def test_add_extra_folder_preserves_unrelated_sections(tmp_path):
    wc.set_ai_disabled(tmp_path, "notebooks/a.py", True)
    wc.add_extra_folder(tmp_path, "packages/x/notebooks")
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["ai"]["disabled_notebooks"] == ["notebooks/a.py"]  # untouched
    assert data["sync"]["folders"] == ["packages/x/notebooks"]


def test_merge_extra_folders_unions_without_duplicates(tmp_path):
    wc.add_extra_folder(tmp_path, "packages/x/notebooks")
    merged = wc.merge_extra_folders(("notebooks", "data"), tmp_path)
    assert merged == ("notebooks", "data", "packages/x/notebooks")
    # An already-present folder is not duplicated.
    assert wc.merge_extra_folders(("notebooks", "packages/x/notebooks"), tmp_path) == (
        "notebooks",
        "packages/x/notebooks",
    )


# -- shadow-guard ignore list -------------------------------------------------


def test_shadow_ignored_missing_is_empty(tmp_path):
    assert wc.shadow_ignored(tmp_path) == set()


def test_set_shadow_ignored_round_trip(tmp_path):
    assert wc.set_shadow_ignored(tmp_path, "notebooks/polars.py", True) is True
    assert wc.shadow_ignored(tmp_path) == {"notebooks/polars.py"}
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["shadow"]["ignore"] == ["notebooks/polars.py"]

    assert wc.set_shadow_ignored(tmp_path, "notebooks/polars.py", False) is False
    # An emptied list removes the file (no spurious new-local file to sync).
    assert not (tmp_path / "mooring.toml").exists()


def test_set_shadow_ignored_normalizes_sorts_and_dedupes(tmp_path):
    wc.set_shadow_ignored(tmp_path, "notebooks\\z.py", True)  # backslash
    wc.set_shadow_ignored(tmp_path, "/notebooks/a.py/", True)  # surrounding slashes
    wc.set_shadow_ignored(tmp_path, "notebooks/z.py", True)  # duplicate of the first
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["shadow"]["ignore"] == ["notebooks/a.py", "notebooks/z.py"]


def test_shadow_ignore_preserves_unrelated_sections(tmp_path):
    wc.set_ai_disabled(tmp_path, "notebooks/a.py", True)
    wc.set_shadow_ignored(tmp_path, "notebooks/polars.py", True)
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["ai"]["disabled_notebooks"] == ["notebooks/a.py"]  # untouched
    assert data["shadow"]["ignore"] == ["notebooks/polars.py"]

    # Removing the shadow ignore prunes only [shadow]; the [ai] section stays.
    wc.set_shadow_ignored(tmp_path, "notebooks/polars.py", False)
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert "shadow" not in data
    assert data["ai"]["disabled_notebooks"] == ["notebooks/a.py"]


def test_shadow_ignore_read_fails_open_on_corrupt_file(tmp_path):
    (tmp_path / "mooring.toml").write_text("this is = not valid = toml", "utf-8")
    assert wc.shadow_ignored(tmp_path) == set()


def test_shadow_ignore_write_does_not_clobber_corrupt_file(tmp_path):
    original = "this is = not valid = toml"
    (tmp_path / "mooring.toml").write_text(original, "utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        wc.set_shadow_ignored(tmp_path, "notebooks/polars.py", True)
    assert (tmp_path / "mooring.toml").read_text("utf-8") == original


def test_guard_mode_defaults_and_parses(tmp_path):
    from mooring import workspace_config

    assert workspace_config.guard_mode(tmp_path) == "warn"  # no file
    (tmp_path / "mooring.toml").write_text('[guard]\npush = "block"\n', "utf-8")
    assert workspace_config.guard_mode(tmp_path) == "block"
    (tmp_path / "mooring.toml").write_text('[guard]\npush = "BLOCK"\n', "utf-8")
    assert workspace_config.guard_mode(tmp_path) == "block"  # case-tolerant
    (tmp_path / "mooring.toml").write_text('[guard]\npush = "nonsense"\n', "utf-8")
    assert workspace_config.guard_mode(tmp_path) == "warn"  # unknown -> default


def test_guard_mode_fails_open_on_malformed_toml(tmp_path):
    from mooring import workspace_config

    (tmp_path / "mooring.toml").write_text("[guard\npush =", "utf-8")
    assert workspace_config.guard_mode(tmp_path) == "warn"
