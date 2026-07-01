"""THE per-notebook apply/undo write guard — one owner for the one lock.

Three write paths share the per-notebook undo stack and must serialize on the
SAME lock: the AI Apply (the chat AND the batch Apply both route through
:meth:`ApplyGuard.apply_with_undo`), Undo/restore (the chat rollback and
``/api/undo`` route through :meth:`ApplyGuard.restore_undo`), and the sync
rollback (``/api/rollback`` holds :attr:`ApplyGuard.lock` around its
snapshot+revert). The lock used to live loose on the Hub with the discipline
spread across handlers; the guard owning it makes the lock identity structural.
Pinned by ``tests/test_hub.py::test_rollback_apply_and_undo_serialize_on_the_same_lock``.
"""

from __future__ import annotations

import threading
from pathlib import Path

# Sentinel returned by restore_undo when a token-scoped undo can't run because a
# newer snapshot is now on top of the (shared) per-notebook undo stack.
UNDO_SUPERSEDED = object()


class ApplyGuard:
    def __init__(self) -> None:
        # Serializes the snapshot+write of an Apply and the restore of an Undo so
        # two near-simultaneous clicks can't race the undo stack (single-user,
        # rare clicks — one global lock is plenty and keeps snapshot/restore atomic).
        self.lock = threading.Lock()

    def apply_with_undo(
        self, nb_path: Path, workspace: Path, notebook_rel: str, op_dicts
    ) -> int:
        """Snapshot the notebook, apply the patch, and return the new undo depth.

        Runs in a thread (file IO), serialized with Undo by :attr:`lock`. If the
        patch fails the just-taken snapshot is discarded, so a failed Apply never
        leaves a phantom Undo step.
        """
        from mooring import notebook_undo, workspace_config
        from mooring.ai import cellwrite

        with self.lock:
            # Final TOCTOU guard: a concurrent disable writes mooring.toml before it
            # tears sessions down, so an in-flight Apply re-reads it here, under the
            # same lock, and refuses to land on the now-protected notebook.
            if workspace_config.is_ai_disabled(workspace, notebook_rel):
                raise PermissionError("notebook_disabled")
            token = notebook_undo.snapshot(workspace, notebook_rel, nb_path.read_bytes())
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
