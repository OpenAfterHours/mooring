"""Sync engine tests against an in-memory fake of the GitHub client."""

import time
from dataclasses import replace

import pytest
from conftest import FakeClient, read_local, write_local

from mooring import manifest, sync
from mooring.config import Config
from mooring.sync import ConflictStrategy, FileState, classify


# -- the decision matrix -----------------------------------------------------


@pytest.mark.parametrize(
    ("base", "local", "remote", "expected"),
    [
        (None, None, None, None),
        ("b", None, None, None),  # stale manifest entry
        (None, "x", None, FileState.NEW_LOCAL),
        (None, None, "x", FileState.NEW_REMOTE),
        (None, "x", "x", FileState.SYNCED),
        (None, "x", "y", FileState.CONFLICT),  # created independently on both sides
        ("b", "b", "b", FileState.SYNCED),
        ("b", "b", "r", FileState.REMOTE_CHANGED),
        ("b", "b", None, FileState.DELETED_REMOTE),
        ("b", "l", "b", FileState.MODIFIED),
        ("b", None, "b", FileState.DELETED_LOCAL),
        ("b", "x", "x", FileState.SYNCED),  # same change on both sides
        ("b", "l", "r", FileState.CONFLICT),
        ("b", None, "r", FileState.CONFLICT),  # deleted here, changed there
        ("b", "l", None, FileState.CONFLICT),  # changed here, deleted there
    ],
)
def test_classify_matrix(base, local, remote, expected):
    assert classify(base, local, remote) is expected


# -- local (no-repo) listing --------------------------------------------------


def test_local_report_lists_disk_files_with_local_state(cfg):
    write_local(cfg, "notebooks/a.py", "x")
    write_local(cfg, "data/x.csv", "a,b\n1,2\n")
    write_local(cfg, "notebooks/.hidden.py", "x")  # dotfiles excluded, like sync
    write_local(cfg, "README.md", "no")  # outside the synced folders
    report = sync.local_report(cfg.workspace(), cfg.folders, cfg.exclude)
    assert report.head_commit == ""  # no remote: nothing to diff against
    rows = {f.path: f for f in report.files}
    assert set(rows) == {"notebooks/a.py", "data/x.csv"}  # same visibility as scan_local
    assert all(f.state is FileState.LOCAL for f in report.files)
    # Local rows are NOT hashed (presence is carried by the LOCAL state, never diffed).
    assert rows["notebooks/a.py"].local_sha is None
    assert rows["notebooks/a.py"].remote_sha is None


def test_local_report_does_not_hash_files(cfg, monkeypatch):
    # Listing must not read+hash every file: a local scratch workspace can hold large
    # data files and the hub re-lists on every New/Open, so hashing there is pure waste.
    from mooring import gitsha

    write_local(cfg, "notebooks/a.py", "x")
    write_local(cfg, "data/big.csv", "a,b\n1,2\n")
    monkeypatch.setattr(
        gitsha, "local_blob_sha", lambda *a, **k: pytest.fail("hashed a file in local mode")
    )
    report = sync.local_report(cfg.workspace(), cfg.folders, cfg.exclude)
    assert [f.path for f in report.files] == ["data/big.csv", "notebooks/a.py"]  # sorted


def test_local_report_empty_workspace(cfg):
    assert sync.local_report(cfg.workspace(), cfg.folders, cfg.exclude).files == []


# -- pull ---------------------------------------------------------------------


def test_pull_into_empty_workspace(cfg):
    client = FakeClient(
        {"notebooks/a.py": b"print(1)\n", "data/x.csv": b"a,b\n1,2\n", "README.md": b"no"}
    )
    result = sync.pull(client, cfg)
    assert result.pulled == 2  # README.md is outside the synced folders
    assert read_local(cfg, "notebooks/a.py") == "print(1)\n"
    mft = manifest.load(cfg.workspace())
    assert set(mft.files) == {"notebooks/a.py", "data/x.csv"}
    assert mft.head_commit == client.head


def test_pull_is_idempotent(cfg):
    client = FakeClient({"notebooks/a.py": b"print(1)\n"})
    sync.pull(client, cfg)
    result = sync.pull(client, cfg)
    assert result.pulled == 0
    assert result.summary() == "already up to date"


def test_pull_applies_remote_update_and_delete(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n", "notebooks/b.py": b"keep\n"})
    sync.pull(client, cfg)
    client.seed("notebooks/a.py", b"v2\n")
    client.remove("notebooks/b.py")
    result = sync.pull(client, cfg)
    assert result.pulled == 2
    assert read_local(cfg, "notebooks/a.py") == "v2\n"
    assert not (cfg.workspace() / "notebooks/b.py").exists()


def test_widening_folders_pulls_already_present_folder(cfg):
    # A folder added to [sync] folders AFTER the head already caught up must still
    # be pulled. Previously the head-unchanged fast path returned the manifest's
    # narrower file set, so a folder pushed by a teammate (e.g. the AI context
    # folder) stayed invisible to pull forever. Regression for that.
    client = FakeClient(
        {"notebooks/a.py": b"print(1)\n", "context/instructions.md": b"# rules\n"}
    )
    narrow = replace(cfg, folders=("notebooks",))
    sync.pull(client, narrow)
    # The manifest head now equals the remote head, so the next pull would short-circuit.
    assert manifest.load(narrow.workspace()).head_commit == client.head

    wide = replace(cfg, folders=("notebooks", "context"))
    result = sync.pull(client, wide)
    assert result.pulled == 1
    assert read_local(wide, "context/instructions.md") == "# rules\n"
    # Scope is recorded, so a third pull short-circuits again (no spurious refetch).
    assert sync.pull(client, wide).pulled == 0


def test_pre_scope_manifest_refetches_then_records_scope(cfg):
    # A manifest written before the scope field existed (scope_folders is None)
    # must not be trusted by the fast path: force a refetch once, then record the
    # scope so steady-state pulls short-circuit again.
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    mft = manifest.load(cfg.workspace())
    mft.scope_folders = None  # simulate an old manifest
    mft.scope_exclude = None
    manifest.save(cfg.workspace(), mft)

    # Head is unchanged, but the unknown scope forces a real tree fetch that still
    # reconciles correctly, then stamps the scope.
    assert sync.pull(client, cfg).pulled == 0
    reloaded = manifest.load(cfg.workspace())
    assert reloaded.scope_folders == cfg.folders


def test_root_pyproject_and_lock_sync_like_any_file(cfg):
    # The repo's dependency project lives at the workspace ROOT, outside the
    # configured folders, but still rides push/pull (sync.PROJECT_FILES).
    write_local(cfg, "pyproject.toml", "[project]\nname = 'x'\n")
    write_local(cfg, "uv.lock", "version = 1\n")
    write_local(cfg, "notebooks/a.py", "print(1)\n")
    client = FakeClient()
    result = sync.push(client, cfg)
    assert result.pushed == 3
    assert "pyproject.toml" in client.tree
    assert "uv.lock" in client.tree

    # A teammate pulls them into a fresh workspace.
    cfg2 = replace(cfg, workspace_path=str(cfg.workspace().parent / "ws2"))
    sync.pull(client, cfg2)
    assert read_local(cfg2, "pyproject.toml") == "[project]\nname = 'x'\n"
    assert read_local(cfg2, "uv.lock") == "version = 1\n"


def test_root_mooring_toml_syncs_like_any_file(cfg):
    # The synced per-workspace settings file (mooring.workspace_config) lives at
    # the workspace ROOT, like pyproject.toml, and rides push/pull so the
    # per-notebook AI opt-out travels to teammates (sync.SYNCED_ROOT_FILES).
    from mooring import workspace_config

    write_local(cfg, "mooring.toml", '[ai]\ndisabled_notebooks = ["notebooks/a.py"]\n')
    write_local(cfg, "notebooks/a.py", "print(1)\n")
    assert "mooring.toml" in sync.scan_local(cfg.workspace(), cfg.folders, cfg.exclude)

    client = FakeClient()
    result = sync.push(client, cfg)
    assert result.pushed == 2
    assert "mooring.toml" in client.tree

    cfg2 = replace(cfg, workspace_path=str(cfg.workspace().parent / "ws2"))
    sync.pull(client, cfg2)
    assert workspace_config.is_ai_disabled(cfg2.workspace(), "notebooks/a.py")


def test_pull_never_overwrites_local_edits_by_default(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")
    result = sync.pull(client, cfg)
    assert result.skipped_conflicts == ["notebooks/a.py"]
    assert read_local(cfg, "notebooks/a.py") == "mine\n"


def test_pull_theirs_overwrites(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")
    sync.pull(client, cfg, strategy=ConflictStrategy.THEIRS)
    assert read_local(cfg, "notebooks/a.py") == "theirs\n"
    # resolved: file is in sync again
    assert sync.status(client, cfg).by_state(FileState.CONFLICT) == []


def test_pull_keep_both_saves_remote_copy_and_keeps_mine_pushable(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")
    sync.pull(client, cfg, strategy=ConflictStrategy.KEEP_BOTH)
    assert read_local(cfg, "notebooks/a.py") == "mine\n"
    copies = list((cfg.workspace() / "notebooks").glob("a.remote-*.py"))
    assert len(copies) == 1
    assert copies[0].read_text("utf-8") == "theirs\n"
    # my version is now MODIFIED against the new remote base — pushable
    report = sync.status(client, cfg)
    assert [f.state for f in report.files if f.path == "notebooks/a.py"] == [FileState.MODIFIED]


# -- push -----------------------------------------------------------------------


def test_push_new_and_modified_files(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    write_local(cfg, "notebooks/new.py", "fresh\n")
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 2
    assert client.blobs[client.tree["notebooks/a.py"]] == b"v2\n"
    assert client.blobs[client.tree["notebooks/new.py"]] == b"fresh\n"
    # baselines updated: everything in sync now
    assert sync.status(client, cfg).summary().startswith("2 in sync, 0 to push")


def test_push_local_delete(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n", "notebooks/b.py": b"v1\n"})
    sync.pull(client, cfg)
    (cfg.workspace() / "notebooks/b.py").unlink()
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 1
    assert "notebooks/b.py" not in client.tree
    assert "notebooks/b.py" not in manifest.load(cfg.workspace()).files


def test_delete_then_push_removes_remote(cfg):
    """End-to-end: deletion.delete() leaves the file as DELETED_LOCAL, and the
    next push removes it from the team repo — the deletion module's contract."""
    from mooring import deletion

    client = FakeClient({"notebooks/a.py": b"v1\n", "notebooks/b.py": b"v1\n"})
    sync.pull(client, cfg)
    deletion.delete(cfg.workspace(), "notebooks/b.py")
    states = {f.path: f.state for f in sync.status(client, cfg).files}
    assert states["notebooks/b.py"] is FileState.DELETED_LOCAL
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 1
    assert "notebooks/b.py" not in client.tree
    assert "notebooks/a.py" in client.tree
    assert "notebooks/b.py" not in manifest.load(cfg.workspace()).files


def test_delete_pbip_then_push_removes_all_members(cfg):
    from mooring import deletion

    client = FakeClient(
        {
            "reports/Sales.pbip": b"{}\n",
            "reports/Sales.SemanticModel/model.tmdl": b"m\n",
            "reports/Sales.Report/report.json": b"{}\n",
            "notebooks/keep.py": b"k\n",
        }
    )
    sync.pull(client, cfg)
    removed = deletion.delete(cfg.workspace(), "reports/Sales.pbip")
    assert len(removed) == 3
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 3
    assert not any(p.startswith("reports/Sales") for p in client.tree)
    assert "notebooks/keep.py" in client.tree


def test_delete_proposed_new_file_withdraws_it_from_review(cfg):
    """A brand-new file proposed for review, then deleted locally, must be
    withdrawn from the open PR — not silently dropped (it was never on
    cfg.branch, so classify would otherwise omit it entirely)."""
    from mooring import deletion

    client = FakeClient()
    write_local(cfg, "notebooks/new.py", "fresh\n")
    sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    assert "notebooks/new.py" in client.trees[BRANCH1]
    deletion.delete(cfg.workspace(), "notebooks/new.py")
    states = {f.path: f.state for f in sync.status(client, cfg).files}
    assert states["notebooks/new.py"] is FileState.DELETED_LOCAL  # not dropped
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 1
    assert "notebooks/new.py" not in client.trees[BRANCH1]  # gone from the PR
    sync.status(client, cfg)  # reconciles the now-empty proposal away
    mft = manifest.load(cfg.workspace())
    assert mft.review_files == {}
    assert mft.review_branch == ""


def test_push_blocks_conflicts(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 0
    assert result.blocked_conflicts == ["notebooks/a.py"]
    assert client.blobs[client.tree["notebooks/a.py"]] == b"theirs\n"  # untouched


def test_push_refuses_oversized_files(cfg):
    client = FakeClient()
    big = b"x" * (2 * 1024 * 1024)
    write_local(cfg, "data/big.bin", "")
    (cfg.workspace() / "data/big.bin").write_bytes(big)
    small_cfg = Config(
        client_id="cid", owner="acme", repo="nbs",
        workspace_path=cfg.workspace_path, max_file_mb=1,
    )
    result = sync.push(client, small_cfg, sleep=lambda s: None)
    assert result.pushed == 0
    assert "data/big.bin" not in client.tree
    assert any("refused" in line for line in result.lines)


def test_propose_refuses_oversized_files(cfg):
    # propose shares push's size limit via the same _read_checked helper: an
    # oversized candidate is refused and no review branch is created for it.
    client = FakeClient()
    write_local(cfg, "data/big.bin", "")
    (cfg.workspace() / "data/big.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    small_cfg = Config(
        client_id="cid", owner="acme", repo="nbs",
        workspace_path=cfg.workspace_path, max_file_mb=1,
    )
    result = sync.propose(client, small_cfg, sleep=lambda s: None)
    assert result.proposed == 0
    assert result.review_branch == ""
    assert any("refused" in line for line in result.lines)


def test_push_specific_paths_only(cfg):
    client = FakeClient()
    write_local(cfg, "notebooks/a.py", "a\n")
    write_local(cfg, "notebooks/b.py", "b\n")
    result = sync.push(client, cfg, paths=["notebooks/a.py"], sleep=lambda s: None)
    assert result.pushed == 1
    assert "notebooks/a.py" in client.tree
    assert "notebooks/b.py" not in client.tree


# -- propose (push to a review branch) -------------------------------------------


def _at(hour, minute):
    return lambda: time.struct_time((2026, 6, 12, hour, minute, 0, 3, 163, -1))


NOW1 = _at(9, 0)
NOW2 = _at(10, 30)
BRANCH1 = "mooring/phil/20260612-0900"


def test_propose_happy_path(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    result = sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    assert result.proposed == 1
    assert result.review_branch == BRANCH1
    assert result.compare_url == (
        f"https://github.com/acme/nbs/compare/main...{BRANCH1}?expand=1"
    )
    assert any(result.compare_url in line for line in result.lines)
    # main untouched, review branch carries the edit
    assert client.blobs[client.tree["notebooks/a.py"]] == b"v1\n"
    assert client.blobs[client.trees[BRANCH1]["notebooks/a.py"]] == b"v2\n"
    # the sync base stays pointed at main
    mft = manifest.load(cfg.workspace())
    assert mft.files["notebooks/a.py"] == client.tree["notebooks/a.py"]
    assert mft.head_commit == client.head
    assert mft.review_branch == BRANCH1
    report = sync.status(client, cfg)
    assert [f.state for f in report.files] == [FileState.IN_REVIEW]
    assert report.review_branch == BRANCH1
    assert "1 in review" in report.summary()


def test_propose_compare_url_uses_enterprise_host(cfg):
    import dataclasses

    ghes_cfg = dataclasses.replace(cfg, host="ghe.example")
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, ghes_cfg)
    write_local(ghes_cfg, "notebooks/a.py", "v2\n")
    result = sync.propose(client, ghes_cfg, sleep=lambda s: None, now=NOW1)
    assert result.compare_url == (
        f"https://ghe.example/acme/nbs/compare/main...{BRANCH1}?expand=1"
    )


def test_repeat_propose_reuses_branch(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    write_local(cfg, "notebooks/a.py", "v3\n")
    result = sync.propose(client, cfg, sleep=lambda s: None, now=NOW2)
    assert result.proposed == 1
    assert result.review_branch == BRANCH1  # same proposal, same branch
    assert client.blobs[client.trees[BRANCH1]["notebooks/a.py"]] == b"v3\n"
    assert [b for b in client.trees if b.startswith("mooring/")] == [BRANCH1]


def test_propose_with_nothing_new_still_returns_link(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    result = sync.propose(client, cfg, sleep=lambda s: None, now=NOW2)
    assert result.proposed == 0
    assert result.review_branch == BRANCH1
    assert result.compare_url
    assert [b for b in client.trees if b.startswith("mooring/")] == [BRANCH1]


def test_merge_observed_clears_review_and_next_propose_is_fresh(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    client.merge(BRANCH1)  # PR merged on GitHub
    report = sync.status(client, cfg)
    assert [f.state for f in report.files] == [FileState.SYNCED]
    assert report.review_branch == ""
    assert manifest.load(cfg.workspace()).review_branch == ""
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v3\n")
    result = sync.propose(client, cfg, sleep=lambda s: None, now=NOW2)
    assert result.review_branch == "mooring/phil/20260612-1030"


def test_merge_then_keep_editing_pushes_cleanly(cfg):
    """After a proposal merges, editing the notebook again and pushing must go
    straight to main without a spurious conflict — the sync base advanced to the
    merged content rather than staying at the pre-proposal blob."""
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    client.merge(BRANCH1)  # PR merged to main
    write_local(cfg, "notebooks/a.py", "v3\n")  # keep working on the same notebook
    report = sync.status(client, cfg)
    assert [f.state for f in report.files] == [FileState.MODIFIED]  # not CONFLICT
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.blocked_conflicts == []
    assert result.pushed == 1
    assert client.blobs[client.tree["notebooks/a.py"]] == b"v3\n"
    mft = manifest.load(cfg.workspace())
    assert mft.review_branch == ""
    assert mft.files["notebooks/a.py"] == client.tree["notebooks/a.py"]


def test_deleted_review_branch_clears_review(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    client.delete_branch(BRANCH1)  # PR closed without merging
    report = sync.status(client, cfg)
    assert [f.state for f in report.files] == [FileState.MODIFIED]
    assert manifest.load(cfg.workspace()).review_branch == ""


def test_propose_branch_name_collision_appends_suffix(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    client.merge(BRANCH1)  # merged, but the branch is left in place
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v3\n")
    result = sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)  # same minute
    assert result.review_branch == f"{BRANCH1}-2"


def test_propose_blocks_conflicts_and_creates_no_branch(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")
    result = sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    assert result.proposed == 0
    assert result.blocked_conflicts == ["notebooks/a.py"]
    assert not any(b.startswith("mooring/") for b in client.trees)


def test_propose_local_delete(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n", "notebooks/b.py": b"v1\n"})
    sync.pull(client, cfg)
    (cfg.workspace() / "notebooks/b.py").unlink()
    result = sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    assert result.proposed == 1
    assert "notebooks/b.py" in client.tree  # main untouched
    assert "notebooks/b.py" not in client.trees[BRANCH1]
    assert manifest.load(cfg.workspace()).review_files == {"notebooks/b.py": None}
    states = {f.path: f.state for f in sync.status(client, cfg).files}
    assert states["notebooks/b.py"] is FileState.IN_REVIEW
    client.merge(BRANCH1)
    sync.status(client, cfg)
    assert manifest.load(cfg.workspace()).review_branch == ""
    sync.pull(client, cfg)
    assert "notebooks/b.py" not in manifest.load(cfg.workspace()).files


def test_edit_after_propose_reverts_to_modified(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    write_local(cfg, "notebooks/a.py", "v3\n")
    report = sync.status(client, cfg)
    assert [f.state for f in report.files] == [FileState.MODIFIED]


def test_push_skips_in_review_files(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    write_local(cfg, "notebooks/new.py", "fresh\n")
    sync.propose(client, cfg, paths=["notebooks/a.py"], sleep=lambda s: None, now=NOW1)
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 1  # only new.py; the proposal is not bypassed
    assert client.blobs[client.tree["notebooks/a.py"]] == b"v1\n"
    result = sync.push(client, cfg, paths=["notebooks/a.py"], sleep=lambda s: None)
    assert result.pushed == 0
    assert any("in review" in line for line in result.lines)
    assert client.blobs[client.tree["notebooks/a.py"]] == b"v1\n"


def test_push_routes_in_review_edits_to_review_branch(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    write_local(cfg, "notebooks/a.py", "v3\n")  # further edit while the PR is open
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 1
    # main is untouched; the edit lands on the review branch (the open PR)
    assert client.blobs[client.tree["notebooks/a.py"]] == b"v1\n"
    assert client.blobs[client.trees[BRANCH1]["notebooks/a.py"]] == b"v3\n"
    # review state is preserved and updated, sync base unchanged
    mft = manifest.load(cfg.workspace())
    assert mft.review_branch == BRANCH1
    assert mft.review_files["notebooks/a.py"] == client.trees[BRANCH1]["notebooks/a.py"]
    assert mft.files["notebooks/a.py"] == client.tree["notebooks/a.py"]
    # the PR link is surfaced and the file settles back to in-review
    assert result.review_branch == BRANCH1
    assert result.compare_url
    report = sync.status(client, cfg)
    assert [f.state for f in report.files] == [FileState.IN_REVIEW]


def test_push_in_review_does_not_advance_main(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    head_before = manifest.load(cfg.workspace()).head_commit
    write_local(cfg, "notebooks/a.py", "v3\n")
    sync.push(client, cfg, sleep=lambda s: None)
    assert manifest.load(cfg.workspace()).head_commit == head_before


def test_push_mixed_routes_each_to_its_branch(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    sync.propose(client, cfg, paths=["notebooks/a.py"], sleep=lambda s: None, now=NOW1)
    write_local(cfg, "notebooks/a.py", "v3\n")  # in-review file, edited again
    write_local(cfg, "notebooks/b.py", "new\n")  # brand-new, not part of the PR
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 2
    # in-review edit went to the PR branch; the new file went straight to main
    assert client.blobs[client.trees[BRANCH1]["notebooks/a.py"]] == b"v3\n"
    assert client.blobs[client.tree["notebooks/a.py"]] == b"v1\n"
    assert client.blobs[client.tree["notebooks/b.py"]] == b"new\n"
    assert "notebooks/b.py" not in client.trees[BRANCH1]


def test_push_in_review_delete_targets_review_branch(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n", "notebooks/b.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/b.py", "v2\n")
    sync.propose(client, cfg, paths=["notebooks/b.py"], sleep=lambda s: None, now=NOW1)
    (cfg.workspace() / "notebooks/b.py").unlink()  # delete the proposed file
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 1
    assert "notebooks/b.py" not in client.trees[BRANCH1]  # removed on the PR branch
    assert "notebooks/b.py" in client.tree  # main untouched
    mft = manifest.load(cfg.workspace())
    assert mft.review_branch == BRANCH1
    assert mft.review_files["notebooks/b.py"] is None


def test_propose_create_conflict_on_stale_manifest_self_heals(cfg):
    """A manifest whose `files` lost a path that is still on cfg.branch (e.g. an
    external tool like OneDrive reverted it) made propose mis-see the file as new,
    fork a branch that already had it, and fail to create it. The conflict must
    now say 'pull first' and invalidate the head cache so the next pull heals it."""
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    # Corrupt the manifest: drop a.py but keep head_commit pointing at the same
    # commit, so _remote_entries serves the stale cache (no live refetch).
    mft = manifest.load(cfg.workspace())
    assert mft.head_commit == client.head
    del mft.files["notebooks/a.py"]
    manifest.save(cfg.workspace(), mft)

    result = sync.propose(client, cfg, sleep=lambda s: None, now=NOW1)
    assert result.proposed == 0
    assert result.blocked_conflicts == ["notebooks/a.py"]
    assert any("already on the remote" in line for line in result.lines)
    assert manifest.load(cfg.workspace()).head_commit == ""  # cache invalidated

    # The next pull refetches the live tree and rebuilds a consistent manifest.
    sync.pull(client, cfg)
    healed = manifest.load(cfg.workspace())
    assert "notebooks/a.py" in healed.files
    assert healed.head_commit == client.head
    assert sync.status(client, cfg).by_state(FileState.CONFLICT) == []


def test_push_create_conflict_on_stale_manifest_invalidates_cache(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    mft = manifest.load(cfg.workspace())
    del mft.files["notebooks/a.py"]
    manifest.save(cfg.workspace(), mft)

    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 0
    assert result.blocked_conflicts == ["notebooks/a.py"]
    assert any("already on the remote" in line for line in result.lines)
    assert manifest.load(cfg.workspace()).head_commit == ""


# -- resolve --------------------------------------------------------------------


def _make_conflict(cfg, client):
    client.seed("notebooks/a.py", b"v1\n")
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")


def test_resolve_push_copy(cfg):
    client = FakeClient()
    _make_conflict(cfg, client)
    result = sync.resolve(
        client, cfg, "notebooks/a.py", ConflictStrategy.PUSH_COPY, username="phil"
    )
    assert result.pushed == 1
    # my version published under a new name, original restored to remote
    assert client.blobs[client.tree["notebooks/a-phil.py"]] == b"mine\n"
    assert read_local(cfg, "notebooks/a.py") == "theirs\n"
    assert read_local(cfg, "notebooks/a-phil.py") == "mine\n"
    assert sync.status(client, cfg).by_state(FileState.CONFLICT) == []


def test_resolve_theirs(cfg):
    client = FakeClient()
    _make_conflict(cfg, client)
    sync.resolve(client, cfg, "notebooks/a.py", ConflictStrategy.THEIRS)
    assert read_local(cfg, "notebooks/a.py") == "theirs\n"
    assert sync.status(client, cfg).by_state(FileState.CONFLICT) == []


def test_resolve_keep_both_saves_remote_copy(cfg):
    # The shared _apply_remote_or_keep_both path: remote still exists, so keep mine
    # and save theirs as a .remote-<sha> copy; my file becomes MODIFIED (pushable).
    client = FakeClient()
    _make_conflict(cfg, client)
    sync.resolve(client, cfg, "notebooks/a.py", ConflictStrategy.KEEP_BOTH)
    assert read_local(cfg, "notebooks/a.py") == "mine\n"
    copies = list((cfg.workspace() / "notebooks").glob("a.remote-*.py"))
    assert len(copies) == 1 and copies[0].read_text("utf-8") == "theirs\n"
    report = sync.status(client, cfg)
    assert [f.state for f in report.files if f.path == "notebooks/a.py"] == [FileState.MODIFIED]


def test_resolve_keep_both_remote_deleted_keeps_local(cfg):
    # The resolve-only branch the helper declines: the remote was deleted, so keep
    # my file and drop the base — it survives as a NEW_LOCAL, pushable file.
    client = FakeClient()
    client.seed("notebooks/a.py", b"v1\n")
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")  # changed here
    client.remove("notebooks/a.py")  # deleted there -> CONFLICT
    result = sync.resolve(client, cfg, "notebooks/a.py", ConflictStrategy.KEEP_BOTH)
    assert read_local(cfg, "notebooks/a.py") == "mine\n"
    assert any("remote deleted it" in line for line in result.lines)
    report = sync.status(client, cfg)
    assert [f.state for f in report.files if f.path == "notebooks/a.py"] == [FileState.NEW_LOCAL]
    assert report.by_state(FileState.CONFLICT) == []


# -- revert (roll back to the last synced checkpoint) ---------------------------


def test_revert_modified_restores_base_bytes(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")  # MODIFIED
    result = sync.revert(client, cfg, "notebooks/a.py")
    assert result.reverted == 1
    assert read_local(cfg, "notebooks/a.py") == "v1\n"  # back to last sync
    # restored bytes re-hash to base, so the file is in sync again with no save
    assert sync.status(client, cfg).by_state(FileState.MODIFIED) == []


def test_revert_recreates_locally_deleted_file(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    (cfg.workspace() / "notebooks/a.py").unlink()  # DELETED_LOCAL
    result = sync.revert(client, cfg, "notebooks/a.py")
    assert result.reverted == 1
    assert read_local(cfg, "notebooks/a.py") == "v1\n"
    assert any("was deleted locally" in line for line in result.lines)


def test_revert_takes_a_snapshot_before_overwriting(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    saved = []
    sync.revert(client, cfg, "notebooks/a.py", snapshot_fn=lambda rel, data: saved.append((rel, data)))
    assert saved == [("notebooks/a.py", b"mine\n")]  # current bytes captured pre-revert


def test_revert_new_local_is_left_alone(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/new.py", "fresh\n")  # NEW_LOCAL — no checkpoint
    result = sync.revert(client, cfg, "notebooks/new.py")
    assert result.reverted == 0
    assert read_local(cfg, "notebooks/new.py") == "fresh\n"  # not deleted
    assert any("never synced" in line for line in result.lines)


def test_revert_skips_conflict_by_default_but_discards_with_flag(cfg):
    client = FakeClient()
    _make_conflict(cfg, client)  # local "mine\n" vs remote "theirs\n"
    skipped = sync.revert(client, cfg, "notebooks/a.py")
    assert skipped.reverted == 0
    assert read_local(cfg, "notebooks/a.py") == "mine\n"  # untouched
    done = sync.revert(client, cfg, "notebooks/a.py", include_conflict=True)
    assert done.reverted == 1
    assert read_local(cfg, "notebooks/a.py") == "v1\n"  # back to base
    # only my side was dropped: the remote change is now a clean pull
    report = sync.status(client, cfg)
    assert report.by_state(FileState.CONFLICT) == []
    assert [f.state for f in report.files if f.path == "notebooks/a.py"] == [
        FileState.REMOTE_CHANGED
    ]


def test_revert_synced_file_is_a_noop(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    result = sync.revert(client, cfg, "notebooks/a.py")
    assert result.reverted == 0
    assert any("already at the last synced version" in line for line in result.lines)


def test_revert_unknown_path_is_reported(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    result = sync.revert(client, cfg, "notebooks/nope.py")
    assert result.reverted == 0
    assert any("not a tracked file" in line for line in result.lines)


# -- hygiene ----------------------------------------------------------------------


def test_scan_skips_scratch_and_hidden_files(cfg):
    write_local(cfg, "notebooks/a.py", "a\n")
    write_local(cfg, "notebooks/a.remote-1234567.py", "scratch\n")
    write_local(cfg, "notebooks/__pycache__/a.cpython-312.pyc", "junk")
    write_local(cfg, "notebooks/__marimo__/session.json", "junk")  # marimo session state
    write_local(cfg, "notebooks/.hidden", "junk")
    found = sync.scan_local(cfg.workspace(), cfg.folders)
    assert set(found) == {"notebooks/a.py"}


def test_custom_exclude_hides_local_files(cfg):
    cfg = replace(cfg, exclude=("*.tmp", "scratch", "data/secret/*"))
    write_local(cfg, "notebooks/keep.py", "a\n")
    write_local(cfg, "notebooks/build.tmp", "junk\n")  # bare *.tmp glob, any depth
    write_local(cfg, "notebooks/scratch/draft.py", "junk\n")  # bare name matches a folder
    write_local(cfg, "data/secret/key.csv", "junk\n")  # path glob with "/"
    write_local(cfg, "data/public.csv", "ok\n")
    found = sync.scan_local(cfg.workspace(), cfg.folders, cfg.exclude)
    assert set(found) == {"notebooks/keep.py", "data/public.csv"}


def test_exclude_applies_to_remote_tree_on_pull(cfg):
    # Built-in (__marimo__) and configured (*.tmp) excludes must hide remote
    # files too, or pull would record them and the next push delete them.
    cfg = replace(cfg, exclude=("*.tmp",))
    client = FakeClient(
        {
            "notebooks/a.py": b"print(1)\n",
            "notebooks/__marimo__/session.json": b"{}",
            "notebooks/build.tmp": b"junk",
        }
    )
    result = sync.pull(client, cfg)
    assert result.pulled == 1
    assert set(manifest.load(cfg.workspace()).files) == {"notebooks/a.py"}


def test_trailing_slash_exclude_matches_like_bare_form(cfg):
    # "scratch/" is the gitignore directory idiom; it must behave like "scratch".
    cfg = replace(cfg, exclude=("scratch/",))
    write_local(cfg, "notebooks/keep.py", "a\n")
    write_local(cfg, "notebooks/scratch/draft.py", "junk\n")
    found = sync.scan_local(cfg.workspace(), cfg.folders, cfg.exclude)
    assert set(found) == {"notebooks/keep.py"}


def test_slash_exclude_applies_to_remote_tree_on_pull(cfg):
    # The "/"-form branch of the matcher must filter the remote tree too, not
    # just the local scan (which test_custom_exclude_hides_local_files covers).
    cfg = replace(cfg, exclude=("reports/drafts/*",))
    client = FakeClient(
        {
            "notebooks/a.py": b"print(1)\n",
            "reports/drafts/x.py": b"draft\n",  # under the slash glob
            "reports/drafts/sub/deep.py": b"deep\n",  # "*" spans "/", so also hidden
            "reports/final.md": b"keep\n",  # sibling outside the glob
        }
    )
    result = sync.pull(client, cfg)
    assert result.pulled == 2
    assert set(manifest.load(cfg.workspace()).files) == {"notebooks/a.py", "reports/final.md"}
    assert "reports/drafts/x.py" not in [f.path for f in sync.status(client, cfg).files]


def test_exclude_added_after_sync_not_phantom_deleted(cfg):
    # An exclude added once a file is already in the manifest forces the
    # _remote_entries head==mft.head_commit short-circuit to filter it. If that
    # branch dropped the filter, the path would surface as DELETED_LOCAL and the
    # next push would delete a teammate's file remotely (the thrash bug).
    client = FakeClient({"notebooks/a.py": b"a\n", "notebooks/old.tmp": b"junk"})
    sync.pull(client, cfg)  # no exclude yet: both land in the manifest
    assert manifest.load(cfg.workspace()).head_commit == client.head
    cfg = replace(cfg, exclude=("*.tmp",))  # added later; branch head unchanged
    report = sync.status(client, cfg)
    assert "notebooks/old.tmp" not in {f.path for f in report.files}
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 0
    assert "notebooks/old.tmp" in client.tree  # not phantom-deleted


def test_exclude_added_mid_proposal_keeps_delete_review_record(cfg):
    # Excluding a path with an open delete-proposal must not be misread as a
    # merge: the absence from `remote` is the filter's doing, not the PR's.
    client = FakeClient({"notebooks/a.py": b"a\n", "notebooks/b.py": b"b\n"})
    sync.pull(client, cfg)
    (cfg.workspace() / "notebooks/b.py").unlink()  # delete locally
    sync.propose(client, cfg, sleep=lambda s: None)  # proposes the deletion
    mft = manifest.load(cfg.workspace())
    assert mft.review_branch and "notebooks/b.py" in mft.review_files
    # Add an exclude matching the proposed-for-deletion path while the PR is open.
    cfg = replace(cfg, exclude=("b.py",))
    sync.status(client, cfg)
    after = manifest.load(cfg.workspace())
    assert after.review_branch == mft.review_branch  # PR tracking survives
    assert "notebooks/b.py" in after.review_files


def test_crlf_local_file_counts_as_synced(cfg):
    client = FakeClient({"notebooks/a.py": b"line1\nline2\n"})
    sync.pull(client, cfg)
    # simulate a Windows editor rewriting the file with CRLF
    (cfg.workspace() / "notebooks/a.py").write_bytes(b"line1\r\nline2\r\n")
    report = sync.status(client, cfg)
    assert [f.state for f in report.files] == [FileState.SYNCED]


# -- Power BI project (PBIP) files ------------------------------------------------


def test_scan_includes_platform_excludes_pbi_dir(cfg):
    write_local(cfg, "reports/Sales.pbip", "{}")
    write_local(cfg, "reports/Sales.SemanticModel/.platform", "{}")
    write_local(cfg, "reports/Sales.SemanticModel/definition/model.tmdl", "model\n")
    write_local(cfg, "reports/Sales.SemanticModel/.pbi/cache.abf", "binary junk")
    write_local(cfg, "reports/Sales.Report/.pbi/localSettings.json", "{}")
    found = sync.scan_local(cfg.workspace(), cfg.folders)
    assert set(found) == {
        "reports/Sales.pbip",
        "reports/Sales.SemanticModel/.platform",
        "reports/Sales.SemanticModel/definition/model.tmdl",
    }


def test_remote_dotfile_ignored_not_deleted(cfg):
    """Regression: a dotfile committed remotely (via real git) must be invisible
    on both sides — previously it was pulled into the manifest, then looked
    locally deleted, and the next push deleted it from the repo."""
    client = FakeClient(
        {
            "notebooks/a.py": b"a\n",
            "reports/Sales.Report/.pbi/localSettings.json": b"{}",
        }
    )
    report = sync.status(client, cfg)
    assert [f.path for f in report.files] == ["notebooks/a.py"]

    sync.pull(client, cfg)
    assert not (cfg.workspace() / "reports/Sales.Report/.pbi/localSettings.json").exists()

    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 0
    assert "reports/Sales.Report/.pbi/localSettings.json" in client.tree  # untouched


def test_remote_platform_file_syncs(cfg):
    client = FakeClient({"reports/Sales.SemanticModel/.platform": b'{"logicalId": "x"}'})
    report = sync.status(client, cfg)
    assert [f.state for f in report.files] == [FileState.NEW_REMOTE]
    sync.pull(client, cfg)
    assert read_local(cfg, "reports/Sales.SemanticModel/.platform") == '{"logicalId": "x"}'
    assert sync.status(client, cfg).by_state(FileState.SYNCED) != []


def test_pbip_bytes_are_faithful(cfg):
    # Power BI Desktop writes UTF-8 BOM and CRLF; non-.py files must round-trip
    # byte-for-byte or every sync would see phantom changes.
    bom_crlf = b"\xef\xbb\xbfmodel\r\n\tculture: en-US\r\n"
    client = FakeClient({"reports/S.SemanticModel/definition/model.tmdl": bom_crlf})
    sync.pull(client, cfg)
    report = sync.status(client, cfg)
    assert [f.state for f in report.files] == [FileState.SYNCED]

    edited = bom_crlf + b"\tnewline\r\n"
    (cfg.workspace() / "reports/S.SemanticModel/definition/model.tmdl").write_bytes(edited)
    sync.push(client, cfg, sleep=lambda s: None)
    assert client.blobs[client.tree["reports/S.SemanticModel/definition/model.tmdl"]] == edited
