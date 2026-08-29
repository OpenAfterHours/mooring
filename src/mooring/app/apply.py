"""THE per-notebook apply/undo write guard — one owner for the one lock.

Three write paths share the per-notebook undo stack and must serialize on the
SAME lock: the AI Apply (the chat AND the batch Apply both route through
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


class ApplyGuard:
    def __init__(self) -> None:
        # Serializes the snapshot+write of an Apply and the restore of an Undo so
        # two near-simultaneous clicks can't race the undo stack (single-user,
        # rare clicks — one global lock is plenty and keeps snapshot/restore atomic).
        self.lock = threading.Lock()

    def apply_with_undo(
        self,
        nb_path: Path,
        workspace: Path,
        notebook_rel: str,
        op_dicts,
        *,
        gate_token: str | None = None,
    ) -> int:
        """Snapshot the notebook, apply the patch, and return the new undo depth.

        Runs in a thread (file IO), serialized with Undo by :attr:`lock`. If the
        patch fails the just-taken snapshot is discarded, so a failed Apply never
        leaves a phantom Undo step.

        Raises :class:`ApplyGateHeld` when the ops are not ``clean`` and ``gate_token``
        is not the token this call derives for them. ``floor`` and ``ask`` are held
        identically here — the difference between them is how the confirmation is
        WORDED, which is the UI's business, not the write path's.
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
            verdict = codeguard.scan_ops(op_dicts)
            if verdict.band != codeguard.BAND_CLEAN:
                expected = codeguard.token(notebook_rel, op_dicts, verdict, notebook_bytes=current)
                # Re-scanned and re-derived server-side every time: the client supplies a
                # token, never a verdict, so "the dialog was shown" is not something it
                # can assert.
                if gate_token != expected:
                    raise ApplyGateHeld(verdict, expected)
            token = notebook_undo.snapshot(workspace, notebook_rel, current)
            try:
                cellwrite.apply_wire_patch(nb_path, op_dicts)
            except BaseException:
                notebook_undo.discard(workspace, notebook_rel, token)
                raise
            return notebook_undo.depth(workspace, notebook_rel)

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
