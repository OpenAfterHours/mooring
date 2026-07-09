"""Local-workspace deletion: single files, PBIP artifacts, safety gates."""

import subprocess
import sys
from pathlib import Path

import pytest

from mooring import deletion


def write(ws: Path, rel: str, text: str = "x") -> None:
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8")


def _link_dir(link: Path, target: Path) -> None:
    """Create a directory link, skipping the test if the platform won't allow it.

    On Windows, a junction (mklink /J) needs no privilege, unlike a symlink —
    and a junction is exactly the reparse point rglob would otherwise follow."""
    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"could not create a junction: {result.stderr.strip()}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted on this platform/account")


def test_delete_single_file(tmp_path):
    write(tmp_path, "notebooks/a.py")
    write(tmp_path, "notebooks/b.py")
    removed = deletion.delete(tmp_path, "notebooks/a.py")
    assert removed == ["notebooks/a.py"]
    assert not (tmp_path / "notebooks/a.py").exists()
    assert (tmp_path / "notebooks/b.py").exists()  # sibling untouched


def test_delete_prunes_empty_dirs_up_to_workspace(tmp_path):
    write(tmp_path, "notebooks/sub/only.py")
    deletion.delete(tmp_path, "notebooks/sub/only.py")
    assert not (tmp_path / "notebooks/sub").exists()
    assert not (tmp_path / "notebooks").exists()
    assert tmp_path.exists()  # never removes the workspace root


def test_delete_keeps_non_empty_parent(tmp_path):
    write(tmp_path, "notebooks/a.py")
    write(tmp_path, "notebooks/keep.py")
    deletion.delete(tmp_path, "notebooks/a.py")
    assert (tmp_path / "notebooks").is_dir()


def test_delete_pbip_artifact_removes_all_members(tmp_path):
    write(tmp_path, "reports/Sales.pbip", "{}")
    write(tmp_path, "reports/Sales.SemanticModel/.platform", "{}")
    write(tmp_path, "reports/Sales.SemanticModel/model.tmdl", "m")
    write(tmp_path, "reports/Sales.Report/report.json", "{}")
    write(tmp_path, "reports/Other.py", "keep")
    removed = deletion.delete(tmp_path, "reports/Sales.pbip")
    assert set(removed) == {
        "reports/Sales.pbip",
        "reports/Sales.SemanticModel/.platform",
        "reports/Sales.SemanticModel/model.tmdl",
        "reports/Sales.Report/report.json",
    }
    assert not (tmp_path / "reports/Sales.SemanticModel").exists()
    assert not (tmp_path / "reports/Sales.Report").exists()
    assert (tmp_path / "reports/Other.py").exists()
    assert (tmp_path / "reports").is_dir()  # other files remain


def test_delete_pbip_also_clears_machine_local_junk(tmp_path):
    write(tmp_path, "reports/Sales.pbip", "{}")
    write(tmp_path, "reports/Sales.SemanticModel/model.tmdl", "m")
    write(tmp_path, "reports/Sales.SemanticModel/.pbi/localSettings.json", "{}")
    deletion.delete(tmp_path, "reports/Sales.pbip")
    assert not (tmp_path / "reports/Sales.SemanticModel").exists()


def test_delete_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        deletion.delete(tmp_path, "notebooks/nope.py")


def test_delete_rejects_traversal(tmp_path):
    (tmp_path.parent / "evil.py").write_text("x", "utf-8")
    with pytest.raises(ValueError, match="escapes the workspace"):
        deletion.delete(tmp_path, "../evil.py")


def test_delete_refuses_non_notebook_paths(tmp_path):
    write(tmp_path, ".mooring/manifest.json", "{}")
    with pytest.raises(ValueError, match="not a notebook"):
        deletion.delete(tmp_path, ".mooring/manifest.json")
    assert (tmp_path / ".mooring/manifest.json").exists()


def test_target_paths_previews_without_deleting(tmp_path):
    write(tmp_path, "reports/Sales.pbip", "{}")
    write(tmp_path, "reports/Sales.SemanticModel/model.tmdl", "m")
    targets = deletion.target_paths(tmp_path, "reports/Sales.pbip")
    assert "reports/Sales.pbip" in targets
    assert "reports/Sales.SemanticModel/model.tmdl" in targets
    assert (tmp_path / "reports/Sales.pbip").exists()  # nothing deleted


def test_delete_trailing_slash_on_pbip_still_expands_artifact(tmp_path):
    write(tmp_path, "reports/Sales.pbip", "{}")
    write(tmp_path, "reports/Sales.SemanticModel/model.tmdl", "m")
    removed = deletion.delete(tmp_path, "reports/Sales.pbip/")  # stray trailing slash
    assert "reports/Sales.pbip" in removed
    assert "reports/Sales.SemanticModel/model.tmdl" in removed
    assert not (tmp_path / "reports/Sales.SemanticModel").exists()


def test_delete_restricted_to_synced_folders(tmp_path):
    # A file in a non-synced SUB-folder is out of scope — refused.
    write(tmp_path, "misc/secret.env", "x")
    folders = ("notebooks", "data", "reports")
    with pytest.raises(ValueError, match="not a notebook"):
        deletion.delete(tmp_path, "misc/secret.env", folders=folders)
    assert (tmp_path / "misc/secret.env").exists()


def test_delete_allows_loose_root_file(tmp_path):
    # Loose top-level files sync by default, so the hub can delete them too.
    write(tmp_path, "helpers.py", "def f(): ...\n")
    folders = ("notebooks", "data", "reports")
    removed = deletion.delete(tmp_path, "helpers.py", folders=folders)
    assert removed == ["helpers.py"]
    assert not (tmp_path / "helpers.py").exists()


def test_delete_allows_nested_file_under_multisegment_folder(tmp_path):
    # A multi-segment synced folder (e.g. an adopted notebooks/team-a) is in scope and the
    # hub lists its files, so deletion must accept them — a top-segment membership test
    # would wrongly refuse a file the workspace does sync.
    write(tmp_path, "notebooks/team-a/x.py", "print(1)\n")
    removed = deletion.delete(tmp_path, "notebooks/team-a/x.py", folders=("notebooks/team-a",))
    assert removed == ["notebooks/team-a/x.py"]
    assert not (tmp_path / "notebooks/team-a/x.py").exists()


def test_delete_refuses_excluded_path(tmp_path):
    write(tmp_path, "reports/scratch.py", "x")
    with pytest.raises(ValueError, match="not a notebook"):
        deletion.delete(tmp_path, "reports/scratch.py", exclude=("scratch.py",))
    assert (tmp_path / "reports/scratch.py").exists()


def test_delete_does_not_follow_symlink_out_of_workspace(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("important", "utf-8")
    write(tmp_path, "reports/Sales.pbip", "{}")
    write(tmp_path, "reports/Sales.SemanticModel/model.tmdl", "m")
    _link_dir(tmp_path / "reports/Sales.SemanticModel/escape", outside)
    removed = deletion.delete(tmp_path, "reports/Sales.pbip")
    assert victim.exists()  # a file outside the workspace is never touched
    assert "reports/Sales.SemanticModel/escape/victim.txt" not in removed
    assert "reports/Sales.SemanticModel/model.tmdl" in removed  # in-workspace file still deleted


# -- the local safety net: every removed file is banked in the trash ---------


def test_delete_banks_every_removed_file(tmp_path):
    from mooring import trash

    write(tmp_path, "notebooks/a.py", "mine")
    collected = []
    deletion.delete(tmp_path, "notebooks/a.py", on_trash=lambda rel, tok: collected.append((rel, tok)))
    assert [rel for rel, _ in collected] == ["notebooks/a.py"]
    assert trash.entries(tmp_path)[0]["action"] == "delete"
    # One restore brings it back byte-for-byte.
    assert trash.restore(tmp_path, collected[0][1]) == "notebooks/a.py"
    assert (tmp_path / "notebooks/a.py").read_text("utf-8") == "mine"


def test_delete_banks_all_pbip_members(tmp_path):
    from mooring import trash

    write(tmp_path, "reports/Sales.pbip", "{}")
    write(tmp_path, "reports/Sales.SemanticModel/.platform", "{}")
    write(tmp_path, "reports/Sales.SemanticModel/model.tmdl", "m")
    collected = []
    removed = deletion.delete(
        tmp_path, "reports/Sales.pbip", on_trash=lambda rel, tok: collected.append((rel, tok))
    )
    assert sorted(rel for rel, _ in collected) == sorted(removed)
    # Restoring every member reassembles the artifact.
    for _, token in collected:
        trash.restore(tmp_path, token)
    assert (tmp_path / "reports/Sales.SemanticModel/model.tmdl").read_text("utf-8") == "m"


def test_delete_over_cap_file_still_deletes_without_banking(tmp_path):
    write(tmp_path, "notebooks/big.py", "x" * 64)
    collected = []
    deletion.delete(
        tmp_path, "notebooks/big.py", trash_cap_mb=0,
        on_trash=lambda rel, tok: collected.append((rel, tok)),
    )
    assert collected == []  # over the cap: not banked...
    assert not (tmp_path / "notebooks/big.py").exists()  # ...but still deleted
