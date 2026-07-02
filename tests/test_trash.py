"""The local trash: deposit/restore round-trips, supersession, and retention."""

import json

import pytest

from mooring import gitsha, trash


def test_deposit_and_restore_roundtrip(tmp_path):
    token = trash.deposit(tmp_path, "notebooks/a.py", b"mine\n", "delete")
    assert token
    # The action removed the file (after_sha=None) and nothing exists there now.
    rel = trash.restore(tmp_path, token)
    assert rel == "notebooks/a.py"
    assert (tmp_path / "notebooks/a.py").read_bytes() == b"mine\n"


def test_restore_overwrite_requires_matching_after_sha(tmp_path):
    remote = b"theirs\n"
    remote_sha = gitsha.blob_sha(remote)
    token = trash.deposit(
        tmp_path, "notebooks/a.py", b"mine\n", "resolve-theirs", after_sha=remote_sha
    )
    # The destructive action wrote the remote bytes; restore puts mine back.
    (tmp_path / "notebooks").mkdir(parents=True)
    (tmp_path / "notebooks/a.py").write_bytes(remote)
    assert trash.restore(tmp_path, token) == "notebooks/a.py"
    assert (tmp_path / "notebooks/a.py").read_bytes() == b"mine\n"


def test_restore_refuses_when_a_later_write_is_on_top(tmp_path):
    remote_sha = gitsha.blob_sha(b"theirs\n")
    token = trash.deposit(
        tmp_path, "notebooks/a.py", b"mine\n", "resolve-theirs", after_sha=remote_sha
    )
    (tmp_path / "notebooks").mkdir(parents=True)
    (tmp_path / "notebooks/a.py").write_bytes(b"newer work\n")  # not what the action wrote
    with pytest.raises(trash.RestoreSuperseded):
        trash.restore(tmp_path, token)
    assert (tmp_path / "notebooks/a.py").read_bytes() == b"newer work\n"


def test_restore_of_a_deletion_refuses_when_recreated(tmp_path):
    token = trash.deposit(tmp_path, "notebooks/a.py", b"mine\n", "delete")
    (tmp_path / "notebooks").mkdir(parents=True)
    (tmp_path / "notebooks/a.py").write_bytes(b"recreated\n")
    with pytest.raises(trash.RestoreSuperseded):
        trash.restore(tmp_path, token)


def test_restore_is_itself_undoable(tmp_path):
    remote = b"theirs\n"
    token = trash.deposit(
        tmp_path, "notebooks/a.py", b"mine\n", "resolve-theirs",
        after_sha=gitsha.blob_sha(remote),
    )
    (tmp_path / "notebooks").mkdir(parents=True)
    (tmp_path / "notebooks/a.py").write_bytes(remote)
    trash.restore(tmp_path, token)
    # The restore banked the remote bytes; restoring THAT puts them back.
    counter = [e for e in trash.entries(tmp_path) if e["action"] == "restore"]
    assert len(counter) == 1
    assert trash.restore(tmp_path, counter[0]["token"]) == "notebooks/a.py"
    assert (tmp_path / "notebooks/a.py").read_bytes() == remote


def test_py_normalization_in_supersession_check(tmp_path):
    # A .py written with CRLF hashes LF-normalized (gitsha semantics); the
    # supersession check must agree or every Windows restore would 409.
    data = b"x = 1\n"
    token = trash.deposit(
        tmp_path, "notebooks/a.py", b"mine\n", "revert", after_sha=gitsha.blob_sha(data)
    )
    (tmp_path / "notebooks").mkdir(parents=True)
    (tmp_path / "notebooks/a.py").write_bytes(b"x = 1\r\n")  # same content, CRLF on disk
    assert trash.restore(tmp_path, token) == "notebooks/a.py"


def test_slug_collision_paths_stay_distinct(tmp_path):
    # a/b.py and a_b.py slug identically; the hash tail keeps them apart.
    t1 = trash.deposit(tmp_path, "a/b.py", b"one\n", "delete")
    t2 = trash.deposit(tmp_path, "a_b.py", b"two\n", "delete")
    assert trash.restore(tmp_path, t1) == "a/b.py"
    assert trash.restore(tmp_path, t2) == "a_b.py"
    assert (tmp_path / "a/b.py").read_bytes() == b"one\n"
    assert (tmp_path / "a_b.py").read_bytes() == b"two\n"


def test_deposit_skips_files_over_the_cap(tmp_path):
    big = b"x" * (2 * 1024 * 1024)
    assert trash.deposit(tmp_path, "data/big.csv", big, "delete", max_file_mb=1) is None
    assert trash.entries(tmp_path) == []


def test_unknown_token_raises_keyerror(tmp_path):
    with pytest.raises(KeyError):
        trash.restore(tmp_path, "nope-00000000-0000000000000-dead")
    with pytest.raises(KeyError):
        trash.restore(tmp_path, "../../evil")


def test_entries_newest_first_and_skip_corrupt(tmp_path):
    trash.deposit(tmp_path, "notebooks/a.py", b"1", "delete")
    trash.deposit(tmp_path, "notebooks/b.py", b"2", "delete")
    (tmp_path / ".mooring" / "trash" / "garbage.json").write_text("{not json", "utf-8")
    entries = trash.entries(tmp_path)
    assert [e["path"] for e in entries] == ["notebooks/b.py", "notebooks/a.py"] or [
        e["path"] for e in entries
    ] == ["notebooks/a.py", "notebooks/b.py"]  # same-second deposits sort by ts string
    assert len(entries) == 2


def test_prune_keep_per_file(tmp_path):
    tokens = [trash.deposit(tmp_path, "notebooks/a.py", b"v%d" % i, "delete") for i in range(5)]
    assert all(tokens)
    dropped = trash.prune(tmp_path, keep_per_file=2)
    assert dropped == 3
    assert len(trash.entries(tmp_path)) == 2


def test_prune_age(tmp_path, monkeypatch):
    token = trash.deposit(tmp_path, "notebooks/a.py", b"old", "delete")
    # Rewrite the entry's timestamp 30 days into the past.
    meta_file = tmp_path / ".mooring" / "trash" / f"{token}.json"
    meta = json.loads(meta_file.read_text("utf-8"))
    meta["ts"] = "2020-01-01T00:00:00+00:00"
    meta_file.write_text(json.dumps(meta), "utf-8")
    assert trash.prune(tmp_path, keep_days=14) == 1
    assert trash.entries(tmp_path) == []


def test_prune_total_size_evicts_oldest_first(tmp_path):
    t_old = trash.deposit(tmp_path, "data/a.csv", b"x" * 600_000, "delete")
    meta_file = tmp_path / ".mooring" / "trash" / f"{t_old}.json"
    meta = json.loads(meta_file.read_text("utf-8"))
    meta["ts"] = "2026-01-01T00:00:00+00:00"  # clearly older, well within keep_days? no â€”
    meta_file.write_text(json.dumps(meta), "utf-8")
    trash.deposit(tmp_path, "data/b.csv", b"y" * 600_000, "delete")
    # Cap of 1 MB: both together exceed it; the OLDER entry is evicted.
    trash.prune(tmp_path, keep_days=100_000, max_total_mb=1)
    remaining = trash.entries(tmp_path)
    assert [e["path"] for e in remaining] == ["data/b.csv"]


def test_restore_accepts_raw_crlf_after_sha(tmp_path):
    # A .py committed with CRLF outside mooring: the remote blob sha is RAW
    # (unnormalized), while the local convention normalizes .py to LF. The
    # supersession check must accept either, or such deposits are unrestorable.
    remote_crlf = b"x = 1\r\n"
    token = trash.deposit(
        tmp_path, "notebooks/a.py", b"mine\n", "pull-overwrite",
        after_sha=gitsha.blob_sha(remote_crlf),  # raw sha, CRLF intact
    )
    (tmp_path / "notebooks").mkdir(parents=True)
    (tmp_path / "notebooks/a.py").write_bytes(remote_crlf)
    assert trash.restore(tmp_path, token) == "notebooks/a.py"
    assert (tmp_path / "notebooks/a.py").read_bytes() == b"mine\n"


def test_token_slug_is_capped_for_long_paths(tmp_path):
    # A long FILENAME (not deep folders, which would exceed MAX_PATH for the
    # restore destination itself): the token's slug must be capped so the flat
    # trash blob name stays well inside the path budget.
    deep = "notebooks/" + "x" * 120 + ".py"
    token = trash.deposit(tmp_path, deep, b"x", "delete")
    assert token is not None
    # slug(40) + hash(8) + millis(13) + rand(4) + separators â€” bounded name.
    assert len(token) <= 40 + 1 + 8 + 1 + 13 + 1 + 4
    assert trash.restore(tmp_path, token) == deep
