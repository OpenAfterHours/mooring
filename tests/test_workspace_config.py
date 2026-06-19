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
