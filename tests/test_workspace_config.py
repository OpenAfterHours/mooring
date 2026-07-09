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


# -- team AI context folders (the synced OFFER) --------------------------------


def test_context_folders_missing_is_empty(tmp_path):
    assert wc.context_folders(tmp_path) == ()


def test_context_folder_set_and_clear_round_trip(tmp_path):
    assert wc.set_context_folder(tmp_path, "finance/dict", True) is True
    assert wc.context_folders(tmp_path) == ("finance/dict",)
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["ai"]["context_folders"] == ["finance/dict"]

    assert wc.set_context_folder(tmp_path, "finance/dict", False) is False
    assert wc.context_folders(tmp_path) == ()
    assert not (tmp_path / "mooring.toml").exists()  # pruned empty


def test_context_folders_stored_sorted_and_deduped(tmp_path):
    # An allowlist has no display order (unlike featured_folders) → always sorted.
    wc.set_context_folder(tmp_path, "zeta", True)
    wc.set_context_folder(tmp_path, "alpha", True)
    wc.set_context_folder(tmp_path, "alpha", True)  # dupe is a no-op
    assert wc.context_folders(tmp_path) == ("alpha", "zeta")


def test_context_folders_normalizes_paths(tmp_path):
    wc.set_context_folder(tmp_path, "\\finance\\dict\\", True)
    assert wc.context_folders(tmp_path) == ("finance/dict",)


def test_context_folder_preserves_sibling_ai_keys(tmp_path):
    # Lives under [ai] beside disabled_notebooks — a write must keep the siblings.
    wc.set_ai_disabled(tmp_path, "notebooks/secret.py", True)
    wc.set_context_folder(tmp_path, "context", True)
    assert wc.disabled_notebooks(tmp_path) == {"notebooks/secret.py"}
    assert wc.context_folders(tmp_path) == ("context",)
    # Removing the offer leaves the sibling [ai] key (file not pruned).
    wc.set_context_folder(tmp_path, "context", False)
    assert wc.disabled_notebooks(tmp_path) == {"notebooks/secret.py"}
    assert wc.context_folders(tmp_path) == ()


def test_context_folders_fails_open_on_corrupt_read(tmp_path):
    (tmp_path / "mooring.toml").write_text("this is = not valid = toml", "utf-8")
    assert wc.context_folders(tmp_path) == ()  # read side fails open


def test_context_folder_corrupt_file_not_clobbered_on_write(tmp_path):
    original = "this is = not valid = toml"
    (tmp_path / "mooring.toml").write_text(original, "utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        wc.set_context_folder(tmp_path, "context", True)
    assert (tmp_path / "mooring.toml").read_text("utf-8") == original


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


# -- featured folders ([hub] featured_folders — repo-curated hub display order) ---


def test_featured_missing_is_empty(tmp_path):
    assert wc.featured_folders(tmp_path) == ()


def test_featured_set_and_clear_round_trip(tmp_path):
    assert wc.set_featured_folder(tmp_path, "reports", True) is True
    assert wc.featured_folders(tmp_path) == ("reports",)
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["hub"]["featured_folders"] == ["reports"]

    assert wc.set_featured_folder(tmp_path, "reports", False) is False
    assert wc.featured_folders(tmp_path) == ()
    # A file left wholly empty is removed, not written empty.
    assert not (tmp_path / "mooring.toml").exists()


def test_featured_order_is_preserved_not_sorted(tmp_path):
    # Order is display priority here (unlike [sync] folders, which sorts).
    wc.set_featured_folder(tmp_path, "zeta", True)
    wc.set_featured_folder(tmp_path, "alpha", True)
    assert wc.featured_folders(tmp_path) == ("zeta", "alpha")


def test_featured_normalizes_and_dedupes(tmp_path):
    wc.set_featured_folder(tmp_path, "reports\\", True)  # backslash + trailing slash
    wc.set_featured_folder(tmp_path, "/reports/", True)  # surrounding slashes — same key
    assert wc.featured_folders(tmp_path) == ("reports",)  # one entry


def test_featured_unchanged_is_noop(tmp_path):
    assert wc.set_featured_folder(tmp_path, "reports", True) is True
    before = (tmp_path / "mooring.toml").read_text("utf-8")
    # Re-featuring an already-featured folder must not rewrite the shared file.
    assert wc.set_featured_folder(tmp_path, "reports", True) is True
    assert (tmp_path / "mooring.toml").read_text("utf-8") == before


def test_featured_never_touches_sync_folders(tmp_path):
    # Featuring is display-only — it must NEVER change what actually syncs.
    wc.add_extra_folder(tmp_path, "packages/finance/notebooks")
    wc.set_featured_folder(tmp_path, "reports", True)
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["sync"]["folders"] == ["packages/finance/notebooks"]  # unchanged
    assert data["hub"]["featured_folders"] == ["reports"]
    assert wc.extra_folders(tmp_path) == ("packages/finance/notebooks",)


def test_featured_preserves_unrelated_keys(tmp_path):
    wc.set_ai_disabled(tmp_path, "notebooks/a.py", True)
    wc.set_featured_folder(tmp_path, "reports", True)
    wc.set_featured_folder(tmp_path, "reports", False)  # remove — prunes [hub] only
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert "hub" not in data
    assert data["ai"]["disabled_notebooks"] == ["notebooks/a.py"]  # untouched


def test_featured_read_fails_open_on_malformed_toml(tmp_path):
    (tmp_path / "mooring.toml").write_text("[hub\nfeatured =", "utf-8")
    assert wc.featured_folders(tmp_path) == ()


def test_featured_write_does_not_clobber_corrupt_file(tmp_path):
    original = "this is = not valid = toml"
    (tmp_path / "mooring.toml").write_text(original, "utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        wc.set_featured_folder(tmp_path, "reports", True)
    assert (tmp_path / "mooring.toml").read_text("utf-8") == original


def test_featured_read_fails_open_on_non_utf8(tmp_path):
    # A UTF-16 mooring.toml (a Windows hazard) must fail OPEN, not raise into api_state.
    (tmp_path / "mooring.toml").write_bytes("[hub]\n".encode("utf-16"))
    assert wc.featured_folders(tmp_path) == ()
    assert wc.disabled_notebooks(tmp_path) == set()  # every read-side caller is protected
