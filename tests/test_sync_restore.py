"""sync.history / sync.restore_version — the git-free time machine's core.

Offline against the in-memory FakeClient, which records a commit log so the
commits-list and contents-at-ref reads behave like GitHub's.
"""

import json

from conftest import FakeClient, read_local, write_local

from mooring import manifest, sync


def _pushed_versions(cfg, client, contents=(b"v1\n", b"v2\n")):
    """Seed + pull, then push edits so notebooks/a.py has real pushed history."""
    sync.pull(client, cfg)
    shas = [client.head]
    for data in contents[1:]:
        write_local(cfg, "notebooks/a.py", data.decode())
        sync.push(client, cfg, throttle=0)
        shas.append(client.head)
    return shas


def test_history_lists_versions_newest_first(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    _pushed_versions(cfg, client)
    versions = sync.history(client, cfg, "notebooks/a.py")
    assert len(versions) == 2  # the seed and the push
    assert versions[0]["message"].startswith("Update notebooks/a.py")
    assert versions[1]["message"].startswith("Seed")
    assert versions[0]["author"] == "phil"
    assert all(v["short"] == v["sha"][:7] for v in versions)


def test_history_untouched_file_is_empty(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    assert sync.history(client, cfg, "notebooks/other.py") == []


def test_restore_as_copy_classifies_new_local(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    old, _new = _pushed_versions(cfg, client)
    result = sync.restore_version(client, cfg, "notebooks/a.py", old, as_copy=True)
    assert result.reverted == 1
    copy_rel = f"notebooks/a.restored-{old[:7]}.py"
    assert read_local(cfg, copy_rel) == "v1\n"
    report = sync.status(client, cfg)
    states = {f.path: f.state for f in report.files}
    assert states[copy_rel] is sync.FileState.NEW_LOCAL
    assert states["notebooks/a.py"] is sync.FileState.SYNCED  # untouched


def test_restore_over_classifies_modified_and_pushes(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    old, _new = _pushed_versions(cfg, client)
    snapshots = []
    result = sync.restore_version(
        client, cfg, "notebooks/a.py", old,
        snapshot_fn=lambda rel, data: snapshots.append((rel, data)),
    )
    assert result.reverted == 1
    assert snapshots == [("notebooks/a.py", b"v2\n")]  # pre-overwrite bytes banked
    assert read_local(cfg, "notebooks/a.py") == "v1\n"
    report = sync.status(client, cfg)
    state = next(f.state for f in report.files if f.path == "notebooks/a.py")
    assert state is sync.FileState.MODIFIED
    pushed = sync.push(client, cfg, throttle=0)
    assert pushed.pushed == 1


def test_restore_over_with_moved_remote_conflicts_and_push_blocks(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    old, _new = _pushed_versions(cfg, client)
    client.seed("notebooks/a.py", b"teammate\n")  # remote moved since our push
    sync.restore_version(client, cfg, "notebooks/a.py", old)
    report = sync.status(client, cfg)
    state = next(f.state for f in report.files if f.path == "notebooks/a.py")
    assert state is sync.FileState.CONFLICT
    result = sync.push(client, cfg, throttle=0)
    assert result.blocked_conflicts == ["notebooks/a.py"]
    assert client.get_blob(client.tree["notebooks/a.py"]) == b"teammate\n"  # theirs intact


def test_restore_never_writes_remote_or_manifest(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    old, _new = _pushed_versions(cfg, client)
    mft_before = json.loads(
        (cfg.workspace() / ".mooring" / "manifest.json").read_text("utf-8")
    )
    head_before = client.head

    def boom(*a, **k):
        raise AssertionError("restore must never write the remote")

    client.put_file = boom
    client.delete_file = boom
    sync.restore_version(client, cfg, "notebooks/a.py", old)
    sync.restore_version(client, cfg, "notebooks/a.py", old, as_copy=True)
    assert client.head == head_before
    mft_after = json.loads(
        (cfg.workspace() / ".mooring" / "manifest.json").read_text("utf-8")
    )
    assert mft_after == mft_before


def test_restore_missing_version_reports_not_touches(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    result = sync.restore_version(client, cfg, "notebooks/never.py", client.head)
    assert result.reverted == 0
    assert "no version at" in result.lines[0]


def test_restore_over_banks_data_files_in_trash(cfg):
    from mooring import trash

    client = FakeClient({"data/x.csv": b"a,b\n1,2\n"})
    sync.pull(client, cfg)
    old = client.head
    write_local(cfg, "data/x.csv", "a,b\n9,9\n")
    sync.push(client, cfg, throttle=0)
    result = sync.restore_version(client, cfg, "data/x.csv", old)
    assert read_local(cfg, "data/x.csv") == "a,b\n1,2\n"
    assert [p for p, _ in result.trashed] == ["data/x.csv"]
    trash.restore(cfg.workspace(), result.trashed[0][1])
    assert read_local(cfg, "data/x.csv") == "a,b\n9,9\n"


def test_restored_copy_is_not_hidden_from_sync(cfg):
    # Unlike the .remote- conflict marker, a .restored- copy must sync normally.
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    old, _ = _pushed_versions(cfg, client)
    sync.restore_version(client, cfg, "notebooks/a.py", old, as_copy=True)
    copy_rel = f"notebooks/a.restored-{old[:7]}.py"
    assert copy_rel in sync.scan_local(cfg.workspace(), cfg.folders, cfg.exclude)
    # And the manifest still round-trips (guard for the earlier no-write pin).
    assert manifest.load(cfg.workspace()).files["notebooks/a.py"]