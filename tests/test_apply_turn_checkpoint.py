"""The turn checkpoint, and the one thing it is allowed to conclude from a token.

``ApplyGuard`` groups every write in one model turn onto ONE undo step by asking "is
the snapshot my turn took still the newest on this notebook's stack?" — a question it
answers by comparing the snapshot TOKEN it recorded against the token now on top.
``restore_undo(expect_token=...)`` asks the same question for the browser's Undo.

Both are only sound while a token names ONE snapshot for the life of the notebook. The
tokens used to be a bare counter reset from the files present, so it restarted at 1
every time the stack drained — and an ordinary Revert drains it. These pin the
orderings where that difference is the difference between an analyst keeping their own
work and losing it silently.

``test_auto_apply.py`` covers the same rule from the applier's side, but only in the
ordering where the counter happens not to repeat (nothing drains the stack in between).
The interesting orderings are here, driven straight against the guard.
"""

from __future__ import annotations

import pytest

from mooring import notebook_undo, paths
from mooring.app.apply import UNDO_SUPERSEDED, ApplyGuard

_NB_SRC = (
    "import marimo\n\n"
    '__generated_with = "0.23.9"\n'
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    seed = 1\n"
    "    return (seed,)\n\n\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace plus an isolated per-machine config.toml — ``apply_with_undo`` reads
    ``[ai] apply_guard`` off DISK at the write, so the developer's own config must not
    be the thing deciding what these tests exercise."""
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_AI_APPLY_GUARD", raising=False)
    (tmp_path / "appdata").mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "nb.py").write_text(_NB_SRC, "utf-8", newline="\n")
    return workspace


def _append(code: str) -> list[dict]:
    return [{"op": "append", "code": code}]


def _apply(guard, ws, ops, turn_id=None) -> int:
    return guard.apply_with_undo(ws / "nb.py", ws, "nb.py", ops, turn_id=turn_id)


# -- the checkpoint may never extend onto a stranger's snapshot ----------------


def test_a_revert_then_a_manual_apply_mid_turn_does_not_get_extended_onto(ws):
    """The losing sequence, all of it ordinary clicks inside ONE turn:

    the model writes; the analyst Reverts it (which DRAINS the stack); the analyst
    Applies a held card of their own; the model writes again. If that last write
    extends the turn's original checkpoint, the analyst's own change has no undo step
    of its own and the next Revert throws it away without a trace.
    """
    guard = ApplyGuard()
    nb = ws / "nb.py"
    pre_turn = nb.read_bytes()

    _apply(guard, ws, _append("model_one = 1\n"), turn_id="turnA")
    # Revert — the chat rollback, which passes no expect_token. The stack is now EMPTY,
    # which is exactly what used to make the next token repeat the popped one.
    assert guard.restore_undo(nb, ws, "nb.py") == 0
    assert nb.read_bytes() == pre_turn
    assert notebook_undo.depth(ws, "nb.py") == 0

    # The analyst's own Apply: a manual write, no turn.
    _apply(guard, ws, _append("mine = 2\n"))
    mine = nb.read_bytes()
    assert "mine" in mine.decode("utf-8")

    # The same turn writes again. It must NOT treat the analyst's snapshot as its own.
    _apply(guard, ws, _append("model_two = 3\n"), turn_id="turnA")

    assert notebook_undo.depth(ws, "nb.py") == 2
    assert guard.restore_undo(nb, ws, "nb.py") == 1
    assert nb.read_bytes() == mine  # the analyst's change survived the Revert


def test_the_analyst_change_is_still_reachable_after_that_revert(ws):
    """The other half of the same defect: not just "the right bytes came back once",
    but that the analyst's write kept an undo step of its OWN, so the stack still
    describes every state they went through."""
    guard = ApplyGuard()
    nb = ws / "nb.py"
    pre_turn = nb.read_bytes()

    _apply(guard, ws, _append("model_one = 1\n"), turn_id="turnA")
    guard.restore_undo(nb, ws, "nb.py")
    _apply(guard, ws, _append("mine = 2\n"))
    before_mine = pre_turn  # what the manual Apply snapshotted
    _apply(guard, ws, _append("model_two = 3\n"), turn_id="turnA")

    guard.restore_undo(nb, ws, "nb.py")  # undoes the model's second write
    assert guard.restore_undo(nb, ws, "nb.py") == 0  # undoes the analyst's own
    assert nb.read_bytes() == before_mine


def test_two_writes_in_one_turn_still_share_one_step(ws):
    """The extend itself still works — the fix must not turn every write into a step."""
    guard = ApplyGuard()
    nb = ws / "nb.py"
    pre_turn = nb.read_bytes()

    _apply(guard, ws, _append("one = 1\n"), turn_id="turnA")
    depth = _apply(guard, ws, _append("two = 2\n"), turn_id="turnA")

    assert depth == 1 and notebook_undo.depth(ws, "nb.py") == 1
    assert guard.restore_undo(nb, ws, "nb.py") == 0
    assert nb.read_bytes() == pre_turn


def test_a_hub_restart_mid_turn_opens_a_fresh_checkpoint(ws):
    """The map is in memory on purpose. A new guard (a restarted hub) knows nothing
    about the turn, so it snapshots — one extra undo step, never a missing one — and
    the token it mints must not be mistakable for the one the old guard recorded."""
    nb = ws / "nb.py"
    first = ApplyGuard()
    _apply(first, ws, _append("one = 1\n"), turn_id="turnA")
    mid = nb.read_bytes()

    second = ApplyGuard()  # same turn id, brand-new process
    _apply(second, ws, _append("two = 2\n"), turn_id="turnA")

    assert notebook_undo.depth(ws, "nb.py") == 2
    assert second.restore_undo(nb, ws, "nb.py") == 1
    assert nb.read_bytes() == mid


# -- the same weakness, on the browser's Undo ---------------------------------


def test_a_stale_undo_token_cannot_be_satisfied_by_a_later_snapshot(ws):
    """``expect_token`` is the browser's "undo the revert I just did". After the stack
    drains and refills, a token from the old stack must still be SUPERSEDED — with a
    restarting counter it matched the new layer and restored the wrong version."""
    guard = ApplyGuard()
    nb = ws / "nb.py"

    _apply(guard, ws, _append("one = 1\n"))
    peeked = notebook_undo.peek_latest(ws, "nb.py")
    assert peeked is not None
    stale_token = peeked[0]
    guard.restore_undo(nb, ws, "nb.py")  # drains the stack
    _apply(guard, ws, _append("two = 2\n"))
    latest = nb.read_bytes()

    outcome = guard.restore_undo(nb, ws, "nb.py", expect_token=stale_token)

    assert outcome is UNDO_SUPERSEDED
    assert nb.read_bytes() == latest  # refused, and left the file alone
    assert notebook_undo.depth(ws, "nb.py") == 1  # and the real step is still there
