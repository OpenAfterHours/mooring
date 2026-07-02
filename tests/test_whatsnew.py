"""The pull digest core (mooring.whatsnew): what changed since YOUR last sync.

All offline against the in-memory FakeClient (whose commit_log/tree snapshots
answer compare + list_commits_for_path). The load-bearing pins: the digest is
strictly read-only (the manifest bytes never change), attribution degrades in
honest steps (compare → per-file commits → bare states) and never raises past
the digest boundary, and a conflict-skipping pull leaves the manifest in
exactly the blank-anchor state the fallback handles.
"""

import dataclasses

import pytest
from conftest import FakeClient, write_local

from mooring import manifest, sync, whatsnew
from mooring.github import GitHubError, NotFound


def _digest(client, cfg):
    report = sync.status(client, cfg)
    return whatsnew.pending_digest(client, cfg, report)


def test_digest_from_a_valid_anchor_attributes_via_compare(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    client.seed("notebooks/a.py", b"v2\n")  # a teammate pushes
    digest = _digest(client, cfg)
    assert digest.source == "compare"
    assert digest.attributed is True and digest.truncated is False
    assert digest.anchor and digest.anchor != digest.head
    [entry] = digest.entries
    assert entry.path == "notebooks/a.py"
    assert entry.state == "remote changed"
    # FakeClient's machine message embeds the path — exact attribution.
    assert entry.authors == ["phil"]
    assert entry.messages == ["Seed notebooks/a.py"]
    assert entry.commits == 1
    assert entry.date
    # The shas the hub's detail endpoint diffs (base -> remote).
    assert entry.base_sha is not None and entry.remote_sha is not None
    assert entry.base_sha != entry.remote_sha


def test_empty_digest_when_anchor_equals_head_and_no_compare_call(cfg, monkeypatch):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)

    def boom(base, head):
        raise AssertionError("compare called for an empty window")

    monkeypatch.setattr(client, "compare", boom)
    digest = _digest(client, cfg)
    assert digest.entries == []
    assert digest.anchor == digest.head != ""


def test_conflict_skipping_pull_blanks_the_anchor_and_the_fallback_serves(cfg):
    # sync.pull deliberately blanks head_commit after skipping a conflict
    # (sync.py) — the fallback path is a first-class path, not an edge case.
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")
    result = sync.pull(client, cfg)  # SKIP strategy: the conflict is skipped
    assert result.skipped_conflicts == ["notebooks/a.py"]
    assert manifest.load(cfg.workspace()).head_commit == ""  # the pin

    digest = _digest(client, cfg)
    assert digest.anchor == ""
    assert digest.source == "commits"
    assert digest.attributed is True
    [entry] = digest.entries
    assert entry.state == "conflict"
    assert entry.authors == ["phil"]
    assert entry.messages == ["Seed notebooks/a.py"]
    # Marked, never resolved: the local edit and the conflict both survive.
    assert (cfg.workspace() / "notebooks/a.py").read_text("utf-8") == "mine\n"


def test_consecutive_same_author_same_message_commits_group(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n", "notebooks/b.py": b"v1\n"})
    sync.pull(client, cfg)
    # A teammate's two-file push: one commit per file (the Contents API), the
    # same push note on both — the digest must read it as ONE human push.
    client.put_file(
        "notebooks/a.py", b"v2\n", "fix the June totals", "main",
        base_sha=client.tree["notebooks/a.py"],
    )
    client.put_file(
        "notebooks/b.py", b"v2\n", "fix the June totals", "main",
        base_sha=client.tree["notebooks/b.py"],
    )
    digest = _digest(client, cfg)
    assert digest.source == "compare"
    [group] = digest.groups
    assert (group.author, group.message, group.count) == ("phil", "fix the June totals", 2)
    # The note doesn't embed paths, but a single-author window still names who.
    assert [e.path for e in digest.entries] == ["notebooks/a.py", "notebooks/b.py"]
    assert all(e.authors == ["phil"] for e in digest.entries)


def test_distinct_pushes_stay_separate_groups(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    client.put_file(
        "notebooks/a.py", b"v2\n", "fix totals", "main",
        base_sha=client.tree["notebooks/a.py"],
    )
    client.put_file(
        "notebooks/a.py", b"v3\n", "add the region split", "main",
        base_sha=client.tree["notebooks/a.py"],
    )
    digest = _digest(client, cfg)
    assert [(g.message, g.count) for g in digest.groups] == [
        ("add the region split", 1),  # newest first
        ("fix totals", 1),
    ]


def test_scope_filtering_matches_sync_visibility(cfg):
    # Out-of-folder and [sync]-excluded remote changes are invisible to sync,
    # so they must be invisible to the digest too (the is_synced_path /
    # within_folders symmetry).
    cfg = dataclasses.replace(cfg, exclude=("*.secret",))
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    client.seed("notebooks/a.py", b"v2\n")
    client.seed("notebooks/x.secret", b"hidden\n")  # excluded by pattern
    client.seed("scratch/y.py", b"outside\n")  # outside the synced folders
    digest = _digest(client, cfg)
    assert [e.path for e in digest.entries] == ["notebooks/a.py"]


def test_digest_never_writes_anything(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    client.seed("notebooks/a.py", b"v2\n")
    mft_path = manifest.manifest_path(cfg.workspace())
    before = mft_path.read_bytes()
    _digest(client, cfg)
    assert mft_path.read_bytes() == before


def test_lost_anchor_notfound_degrades_to_the_per_file_fallback(cfg, monkeypatch):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    client.seed("notebooks/a.py", b"v2\n")

    def gone(base, head):  # force-pushed/GC'd anchor
        raise NotFound("no common ancestor")

    monkeypatch.setattr(client, "compare", gone)
    digest = _digest(client, cfg)
    assert digest.source == "commits"
    assert digest.attributed is True
    [entry] = digest.entries
    assert entry.authors == ["phil"]


def test_truncated_compare_window_falls_back_to_exact_lookups(cfg, monkeypatch):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    client.seed("notebooks/a.py", b"v2\n")

    def truncated(base, head):  # the API listed fewer commits than the window
        return {"total_commits": 999, "commits": [], "files": []}

    monkeypatch.setattr(client, "compare", truncated)
    digest = _digest(client, cfg)
    assert digest.truncated is True
    assert digest.source == "commits"  # never attribute from a wrong subset
    [entry] = digest.entries
    assert entry.authors == ["phil"]


def test_total_attribution_failure_degrades_to_states_and_never_raises(cfg, monkeypatch):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    client.seed("notebooks/a.py", b"v2\n")
    report = sync.status(client, cfg)

    def boom(*args, **kwargs):
        raise GitHubError("read failed")

    monkeypatch.setattr(client, "compare", boom)
    monkeypatch.setattr(client, "list_commits_for_path", boom)
    digest = whatsnew.pending_digest(client, cfg, report)
    assert digest.attributed is False
    assert digest.source == "states"
    [entry] = digest.entries
    assert entry.state == "remote changed"
    assert entry.authors == [] and entry.messages == []


# -- summarize_diff (the pure line-count seam; the hub prefers celldiff) ------


def test_summarize_diff_counts_lines():
    out = whatsnew.summarize_diff(b"a\nb\n", b"a\nc\nd\n", "data/x.csv")
    assert out == {"kind": "lines", "added": 2, "removed": 1}


def test_summarize_diff_new_and_deleted_sides():
    assert whatsnew.summarize_diff(None, b"a\nb\n", "x.txt") == {
        "kind": "lines", "added": 2, "removed": 0,
    }
    assert whatsnew.summarize_diff(b"a\nb\n", None, "x.txt") == {
        "kind": "lines", "added": 0, "removed": 2,
    }


def test_summarize_diff_normalizes_py_line_endings():
    # A CRLF flip is not a change on the push path (gitsha LF-normalizes .py).
    out = whatsnew.summarize_diff(b"x = 1\r\n", b"x = 1\n", "notebooks/a.py")
    assert out == {"kind": "lines", "added": 0, "removed": 0}


def test_summarize_diff_binary_degrades_to_sizes():
    out = whatsnew.summarize_diff(b"\xff\xfe\x00\x01", b"\xff\x00", "data/x.bin")
    assert out["kind"] == "binary"
    assert (out["base_size"], out["head_size"]) == (4, 2)


def test_summarize_diff_both_sides_missing_raises():
    with pytest.raises(ValueError):
        whatsnew.summarize_diff(None, None, "x.txt")
