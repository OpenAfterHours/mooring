"""THE per-notebook apply/undo write guard — one owner for the one lock.

Three write paths share the per-notebook undo stack and must serialize on the
SAME lock: the AI Apply (the chat Apply, the batch Apply, and the model's own
in-turn write in :mod:`mooring.app.auto_apply` all route through
:meth:`ApplyGuard.apply_with_undo`), Undo/restore (the chat rollback and
``/api/undo`` route through :meth:`ApplyGuard.restore_undo`), and the sync
rollback (``/api/rollback`` holds :attr:`ApplyGuard.lock` around its
snapshot+revert). The lock used to live loose on the Hub with the discipline
spread across handlers; the guard owning it makes the lock identity structural.
Pinned by ``tests/test_hub.py::test_rollback_apply_and_undo_serialize_on_the_same_lock``.

Owning the one write path is also what makes it the one place to ENFORCE the apply
gate (:mod:`mooring.ai.codeguard`). marimo runs an applied cell the moment the file
changes, and Undo only restores bytes — a complete remedy for a cell that computes,
none at all for one that deleted a file. So a non-clean cell is held here until a
matching confirm token arrives. Held *here*, not in the routes and never in
JavaScript: both Applies (chat and batch) already come through this method, so
neither adapter can forget the gate and no third caller can be added without it.
"""

from __future__ import annotations

import threading
from pathlib import Path

# Sentinel returned by restore_undo when a token-scoped undo can't run because a
# newer snapshot is now on top of the (shared) per-notebook undo stack.
UNDO_SUPERSEDED = object()


class ApplyGateHeld(Exception):
    """The arriving cell does something Undo cannot take back, and no matching
    confirmation came with it — so NOTHING was written: no snapshot, no bytes, no
    undo step. Re-POSTing the same body with :attr:`token` proceeds.

    Carries the value-free :class:`mooring.ai.codeguard.Verdict` and the confirm
    token, which is always DERIVED here and never accepted from the caller. The
    token binds the notebook path, the notebook's bytes AT THE TIME OF THE HOLD, the
    exact ops and the exact finding set, so a confirmation stops matching the moment
    any of those move — an approval can never carry over to code the analyst did not
    see, nor to a notebook that changed underneath the one they approved for.
    """

    def __init__(self, verdict, token: str) -> None:
        super().__init__("apply_gate_held")
        self.verdict = verdict
        self.token = token

    def payload(self) -> dict:
        """The body an adapter answers with (HTTP 428). Shaped HERE, once, so the chat
        route and the batch route cannot drift apart — the same reason both share this
        guard in the first place.

        Each finding carries its OWN band beside the top-level one, which stays the worst
        band across them: a cell that both overwrites a file and deletes one is a single
        ``floor`` prompt, but the analyst should still see which of the two lines is the
        irreversible one. Summarising that away would leave the UI unable to say it.

        Value-free by construction: a finding is ``(line, kind, label, band)`` where the
        label is a fixed string from codeguard's ``KINDS`` table. Nothing read out of the
        analyst's code — no path, no name, no matched substring — is in here.
        """
        return {
            "gate": {
                "band": self.verdict.band,
                "token": self.token,
                "findings": [
                    {"line": f.line, "kind": f.kind, "label": f.label, "band": f.band}
                    for f in self.verdict.findings
                ],
            }
        }


def scan_report(op_dicts) -> tuple[str, tuple[str, ...]]:
    """The band and the value-free finding KINDS for one Apply's ops — what the adapters
    put in the telemetry event and the activity ledger AFTER a successful apply.

    A separate call rather than a second return value from
    :meth:`ApplyGuard.apply_with_undo` because :func:`~mooring.ai.codeguard.scan_ops` is a
    pure function of the ops (it reads no file, no config and no clock), so this recomputes
    exactly the verdict the gate reached under the lock while the guard's return value
    stays the one number its callers actually need. Nothing is ever ENFORCED from here:
    this runs outside the lock and is only reported.
    """
    from mooring.ai import codeguard

    verdict = codeguard.scan_ops(op_dicts)
    return verdict.band, tuple(f.kind for f in verdict.findings)


def _guard_armed(workspace: Path) -> bool:
    """Whether ``[ai] apply_guard`` is on for ``workspace``, read FRESH from disk.

    Deliberately not a value anyone hands in. It is read exactly where and how
    :func:`mooring.policy.ai_disabled` is read — inside
    :meth:`ApplyGuard.apply_with_undo`'s lock, at the moment of the write — and for the
    same TOCTOU reason: the local config file and the synced ``mooring.toml`` both
    change under a running hub (a ``mooring config set``, a Settings write, a pull that
    brings a new ``[policy]`` block), and a guard decided when the chat opened would
    then be answering a question about a config that no longer exists. A policy pinning
    ``ai.apply_guard = true`` has to bite on the very NEXT Apply, not the next restart.

    ``policy.tighten`` is applied here rather than trusting the local value alone,
    because that is the whole point of the knob being policy-governed: a team may force
    the guard on for everyone, and local config (env vars included) cannot answer back.

    Fails CLOSED. Neither call is expected to raise — ``policy.load`` never does, by
    construction — but a guard that disarms itself on an unreadable config would make
    "corrupt the config" a way past it, and an armed guard's worst case is one extra
    confirm.
    """
    from mooring import config, policy

    try:
        return policy.tighten_app_config(config.load_app_config(), workspace).ai_apply_guard
    except Exception:  # noqa: BLE001  # unreadable config must arm the guard, not skip it
        return True


def _undo_key(workspace: Path, notebook_rel: str) -> tuple[str, str]:
    """The identity of ONE notebook's undo stack, spelled exactly as
    :mod:`mooring.notebook_undo` spells it.

    The turn-checkpoint map below is keyed by this and compared against a token read
    off that stack, so the two must agree about which notebook is which. Anything
    coarser (a case-folded path, say) could make two distinct notebooks share a map
    entry, and their snapshot tokens are plain counters — ``000000000001`` on one
    stack matches ``000000000001`` on the other — so the "is my checkpoint still on
    top?" check would pass against the WRONG stack and skip a snapshot that was
    needed. Mirrors ``notebook_undo._norm``.
    """
    return (str(workspace), str(notebook_rel).replace("\\", "/").strip("/"))


class ApplyGuard:
    def __init__(self) -> None:
        # Serializes the snapshot+write of an Apply and the restore of an Undo so
        # two near-simultaneous clicks can't race the undo stack (single-user,
        # rare clicks — one global lock is plenty and keeps snapshot/restore atomic).
        self.lock = threading.Lock()
        # The TURN checkpoint: notebook -> (turn_id, the snapshot token taken for it).
        # Read and written only under `lock`, so it moves in step with the stack it
        # describes. Deliberately in memory: a hub restart just means the next write
        # opens a fresh checkpoint, which is the safe direction (one extra undo step,
        # never a missing one).
        self._turn_checkpoints: dict[tuple[str, str], tuple[str, str]] = {}

    def apply_with_undo(
        self,
        nb_path: Path,
        workspace: Path,
        notebook_rel: str,
        op_dicts,
        *,
        gate_token: str | None = None,
        turn_id: str | None = None,
    ) -> int:
        """Snapshot the notebook, apply the patch, and return the new undo depth.

        Runs in a thread (file IO), serialized with Undo by :attr:`lock`. If the
        patch fails the just-taken snapshot is discarded, so a failed Apply never
        leaves a phantom Undo step.

        Raises :class:`ApplyGateHeld` when the ops are not ``clean`` and ``gate_token``
        is not the token this call derives for them. ``floor`` and ``ask`` are held
        identically here — the difference between them is how the confirmation is
        WORDED, which is the UI's business, not the write path's.

        With ``[ai] apply_guard`` off there is no scan and no token: every cell applies
        the way it did before the gate existed.

        ``turn_id`` makes the checkpoint TURN-scoped. When it names the turn that took
        the snapshot currently on top of this notebook's stack, no second snapshot is
        taken — the existing one is EXTENDED, so the whole turn reverts as one step.
        That is the unit an analyst thinks in ("undo what the assistant just did"), and
        it matters now that one turn can write several times: a per-write stack would
        turn one piece of work into a run of near-identical steps and, bounded at 25,
        could push a real checkpoint off the end. ``None`` (a manual Apply) always
        snapshots, exactly as before.

        The extend decision is made HERE, under the lock, against what is actually on
        top of the stack — never from the map alone. A manual Apply or an Undo landing
        mid-turn moves the top, and a checkpoint that no longer describes the pre-turn
        bytes must not be extended: reverting it would restore the wrong layer.
        """
        from mooring import notebook_undo, policy
        from mooring.ai import cellwrite, codeguard

        with self.lock:
            # Final TOCTOU guard: a concurrent disable writes mooring.toml before it
            # tears sessions down, so an in-flight Apply re-reads it here, under the
            # same lock, and refuses to land on the now-protected notebook. Stays FIRST:
            # a notebook the copilot may not touch at all is not a thing to ask about.
            if policy.ai_disabled(workspace, notebook_rel):
                raise PermissionError("notebook_disabled")
            # One read of the notebook, used for both the gate's token and the snapshot,
            # so the bytes the analyst confirmed FOR are exactly the bytes rolled back TO.
            current = nb_path.read_bytes()
            # The gate: inside the lock because it re-scans the ops and re-derives the
            # token against the file as it is at the instant of the write — outside, a
            # concurrent apply could land between the check and the patch and make the
            # confirmation stale. Before the snapshot because a held Apply must leave NO
            # trace: an undo step for a write that never happened would be a phantom the
            # analyst could "undo" into a state they never had.
            # ...and armed at the same instant, read the SAME way as ai_disabled above:
            # from disk, from THIS workspace, under THIS lock. Never a value the route
            # captured when the chat opened or the hub booted — a policy that arrives by
            # pull mid-session must arm the guard for the very next Apply, and the
            # config a captured value came from is exactly the thing an attacker-supplied
            # mooring.toml tightens. The TOCTOU window the comment above describes is the
            # same window here.
            if _guard_armed(workspace):
                verdict = codeguard.scan_ops(op_dicts)
                if verdict.band != codeguard.BAND_CLEAN:
                    expected = codeguard.token(
                        notebook_rel, op_dicts, verdict, notebook_bytes=current
                    )
                    # Re-scanned and re-derived server-side every time: the client supplies
                    # a token, never a verdict, so "the dialog was shown" is not something
                    # it can assert.
                    if gate_token != expected:
                        raise ApplyGateHeld(verdict, expected)
            # AFTER the disable re-check and AFTER the gate, unchanged: a held Apply
            # still leaves no snapshot, no bytes and no undo step.
            key = _undo_key(workspace, notebook_rel)
            token = None
            if not self._extends_turn(workspace, notebook_rel, turn_id, key):
                token = notebook_undo.snapshot(workspace, notebook_rel, current)
            try:
                cellwrite.apply_wire_patch(nb_path, op_dicts)
            except BaseException:
                if token is not None:
                    notebook_undo.discard(workspace, notebook_rel, token)
                raise
            if turn_id and token is not None:
                # Only ever recorded for a snapshot that is now genuinely on top, so
                # the map can never point at a layer the stack does not have.
                self._turn_checkpoints[key] = (turn_id, token)
            return notebook_undo.depth(workspace, notebook_rel)

    def _extends_turn(self, workspace: Path, notebook_rel: str, turn_id, key) -> bool:
        """Whether this write joins the checkpoint an earlier write in the SAME turn
        already took. Call under :attr:`lock` only.

        Two things must both hold: the turn matches, and the snapshot that turn took is
        still the newest on the stack. The second is what keeps the map honest — a
        manual Apply, an Undo, or a sync rollback landing between two model writes moves
        the top, and extending then would leave the turn's Revert pointing at a state
        the analyst never had.
        """
        from mooring import notebook_undo

        if not turn_id:
            return False
        recorded = self._turn_checkpoints.get(key)
        if recorded is None or recorded[0] != turn_id:
            return False
        peeked = notebook_undo.peek_latest(workspace, notebook_rel)
        return peeked is not None and peeked[0] == recorded[1]

    def restore_undo(
        self, nb_path: Path, workspace: Path, notebook_rel: str, *, expect_token: str | None = None
    ):
        """Restore the most recent snapshot (the editor's --watch reloads it). Returns
        the remaining undo depth, ``None`` when there is nothing to undo, or
        :data:`UNDO_SUPERSEDED` when ``expect_token`` is given but no longer the newest
        snapshot (a later write is on top — restoring it would revert the wrong layer).

        Write-then-discard: the snapshot is only consumed AFTER it is safely written
        back, so a failed restore leaves the undo step intact to retry (symmetric with
        the discard-on-failure in :meth:`apply_with_undo`)."""
        from mooring import notebook_undo
        from mooring.paths import safe_write_bytes

        with self.lock:
            peeked = notebook_undo.peek_latest(workspace, notebook_rel)
            if peeked is None:
                return None
            token, prior = peeked
            if expect_token is not None and token != expect_token:
                return UNDO_SUPERSEDED
            safe_write_bytes(nb_path, prior)  # raises before the snapshot is consumed
            notebook_undo.discard(workspace, notebook_rel, token)
            return notebook_undo.depth(workspace, notebook_rel)
