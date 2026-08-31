"""The pre-edit snapshot store that backs Apply -> Undo (value-free local files)."""

from __future__ import annotations

from mooring import notebook_undo


def test_snapshot_then_pop_round_trips(tmp_path):
    notebook_undo.snapshot(tmp_path, "notebooks/a.py", b"v1")
    assert notebook_undo.depth(tmp_path, "notebooks/a.py") == 1
    assert notebook_undo.pop(tmp_path, "notebooks/a.py") == b"v1"
    assert notebook_undo.depth(tmp_path, "notebooks/a.py") == 0


def test_pop_is_lifo_across_multiple_snapshots(tmp_path):
    for v in (b"v1", b"v2", b"v3"):
        notebook_undo.snapshot(tmp_path, "a.py", v)
    assert notebook_undo.pop(tmp_path, "a.py") == b"v3"
    assert notebook_undo.pop(tmp_path, "a.py") == b"v2"
    assert notebook_undo.pop(tmp_path, "a.py") == b"v1"
    assert notebook_undo.pop(tmp_path, "a.py") is None  # nothing left to undo


def test_pop_empty_is_none(tmp_path):
    assert notebook_undo.pop(tmp_path, "never.py") is None
    assert notebook_undo.depth(tmp_path, "never.py") == 0


def test_snapshots_live_under_dot_mooring_undo(tmp_path):
    notebook_undo.snapshot(tmp_path, "notebooks/a.py", b"x")
    store = tmp_path / ".mooring" / "undo"
    assert store.is_dir()
    files = [p for p in store.rglob("*.py") if p.is_file()]
    assert len(files) == 1 and files[0].read_bytes() == b"x"


def test_notebooks_do_not_share_a_stack(tmp_path):
    notebook_undo.snapshot(tmp_path, "a.py", b"a1")
    notebook_undo.snapshot(tmp_path, "b.py", b"b1")
    assert notebook_undo.pop(tmp_path, "a.py") == b"a1"
    assert notebook_undo.depth(tmp_path, "b.py") == 1  # untouched
    assert notebook_undo.pop(tmp_path, "b.py") == b"b1"


def test_backslash_and_forward_slash_rel_map_to_the_same_stack(tmp_path):
    # The hub may hand a rel-path with either separator; they must address one stack.
    notebook_undo.snapshot(tmp_path, "sub/a.py", b"one")
    assert notebook_undo.depth(tmp_path, "sub\\a.py") == 1
    assert notebook_undo.pop(tmp_path, "sub\\a.py") == b"one"


def test_slug_colliding_rel_paths_do_not_share_a_stack(tmp_path):
    # 'a/b.py' and 'a_b.py' slug to the same string; the key must still keep them
    # distinct, or an Undo on one could restore the OTHER notebook's bytes.
    notebook_undo.snapshot(tmp_path, "a/b.py", b"AAA")
    notebook_undo.snapshot(tmp_path, "a_b.py", b"BBB")
    assert notebook_undo.pop(tmp_path, "a/b.py") == b"AAA"  # not BBB
    assert notebook_undo.pop(tmp_path, "a_b.py") == b"BBB"


def test_peek_latest_does_not_consume(tmp_path):
    notebook_undo.snapshot(tmp_path, "a.py", b"v1")
    latest = notebook_undo.peek_latest(tmp_path, "a.py")
    assert latest is not None
    token, data = latest
    assert data == b"v1" and notebook_undo.depth(tmp_path, "a.py") == 1  # still there
    notebook_undo.discard(tmp_path, "a.py", token)
    assert notebook_undo.depth(tmp_path, "a.py") == 0
    assert notebook_undo.peek_latest(tmp_path, "a.py") is None


def test_discard_removes_a_specific_snapshot(tmp_path):
    notebook_undo.snapshot(tmp_path, "a.py", b"keep")
    token = notebook_undo.snapshot(tmp_path, "a.py", b"drop")
    notebook_undo.discard(tmp_path, "a.py", token)
    assert notebook_undo.depth(tmp_path, "a.py") == 1
    assert notebook_undo.pop(tmp_path, "a.py") == b"keep"  # the dropped one is gone


def test_a_token_is_never_reused_once_the_stack_drains(tmp_path):
    """The invariant two callers depend on: ``ApplyGuard._extends_turn`` and
    ``restore_undo(expect_token=...)`` both decide "is the layer I remember still the
    one on top?" by comparing tokens. A counter that restarts at 1 whenever the stack
    empties answers YES for a DIFFERENT snapshot — so a token has to name one snapshot
    for the life of the notebook, not one slot in the current stack."""
    seen = set()
    for _ in range(10):
        token = notebook_undo.snapshot(tmp_path, "a.py", b"x")
        assert token not in seen
        seen.add(token)
        notebook_undo.pop(tmp_path, "a.py")  # drain it: the next push starts over
    assert len(seen) == 10


def test_tokens_are_unique_across_notebooks_too(tmp_path):
    # Distinct notebooks keep distinct stacks, but a token that leaked across them
    # would still have to fail to match — the map that holds them is keyed by
    # workspace+notebook and compared against whatever is on the stack it names.
    a = notebook_undo.snapshot(tmp_path, "a.py", b"a")
    b = notebook_undo.snapshot(tmp_path, "b.py", b"b")
    assert a != b


def test_identical_bytes_get_distinct_tokens(tmp_path):
    """Why identity is the FILENAME and not the content: an Undo restores exactly the
    bytes it snapshotted, so the very next snapshot after one holds identical content.
    Those are the two layers that most need telling apart."""
    first = notebook_undo.snapshot(tmp_path, "a.py", b"same")
    notebook_undo.pop(tmp_path, "a.py")
    second = notebook_undo.snapshot(tmp_path, "a.py", b"same")
    assert first != second


def test_pop_order_survives_a_partial_drain(tmp_path):
    # The sequence half of the token still orders the stack after tokens stop being
    # bare integers — including when the stack has been popped down and refilled.
    for v in (b"v1", b"v2", b"v3"):
        notebook_undo.snapshot(tmp_path, "a.py", v)
    notebook_undo.pop(tmp_path, "a.py")  # drop v3
    notebook_undo.snapshot(tmp_path, "a.py", b"v4")
    assert notebook_undo.pop(tmp_path, "a.py") == b"v4"
    assert notebook_undo.pop(tmp_path, "a.py") == b"v2"
    assert notebook_undo.pop(tmp_path, "a.py") == b"v1"


def test_a_stack_written_before_the_suffix_still_reads_and_extends(tmp_path):
    """Upgrade path: an analyst's existing undo dir holds bare-numeric stems. They must
    still list, sort and pop, and a new snapshot must land ON TOP of them rather than
    beside them."""
    d = tmp_path / ".mooring" / "undo" / notebook_undo._key("a.py")
    d.mkdir(parents=True)
    (d / "000000000001.py").write_bytes(b"old1")
    (d / "000000000002.py").write_bytes(b"old2")
    assert notebook_undo.depth(tmp_path, "a.py") == 2

    notebook_undo.snapshot(tmp_path, "a.py", b"new")

    assert notebook_undo.pop(tmp_path, "a.py") == b"new"
    assert notebook_undo.pop(tmp_path, "a.py") == b"old2"
    assert notebook_undo.pop(tmp_path, "a.py") == b"old1"


def test_discard_ignores_a_token_this_module_did_not_mint(tmp_path):
    # The token shape reaches the browser and comes back (`undo_token`); nothing may
    # turn one into a path outside the stack.
    notebook_undo.snapshot(tmp_path, "a.py", b"keep")
    victim = tmp_path / "victim.py"
    victim.write_bytes(b"not a snapshot")

    notebook_undo.discard(tmp_path, "a.py", "../../../victim")

    assert victim.exists()
    assert notebook_undo.depth(tmp_path, "a.py") == 1


def test_stack_is_bounded(tmp_path):
    for i in range(notebook_undo._MAX_SNAPSHOTS + 5):
        notebook_undo.snapshot(tmp_path, "a.py", str(i).encode())
    assert notebook_undo.depth(tmp_path, "a.py") == notebook_undo._MAX_SNAPSHOTS
    # The most recent snapshot is preserved; the oldest were pruned.
    last = notebook_undo._MAX_SNAPSHOTS + 4
    assert notebook_undo.pop(tmp_path, "a.py") == str(last).encode()
