"""The AI batch application service — typed runs around the pure BatchPlanner.

:class:`mooring.ai.batch.BatchPlanner` is pure and dependency-injected (it owns
the build pool and per-job state); what used to live in the web adapter was the
REGISTRY around it — an untyped ``batch_id -> dict`` with its invariants
(applied-once, the open/closed status, teardown) hand-balanced across nine
handlers. Now :class:`BatchRun` carries those invariants as METHODS under its
own lock (single-writer by record), and :class:`BatchService` owns the registry
(register/get/reap/abort-all) plus the first-class :meth:`BatchService.cancel`
— previously the only way to stop a runaway batch was switching repos, which
aborts ALL runs as a side effect.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path


def discard_batch_notebook(workspace: Path, notebook_rel: str) -> None:
    """Best-effort remove the empty skeleton a non-built batch job left behind
    (pii-blocked / failed / empty), so a batch doesn't litter the workspace. Path-
    guarded via the shared resolver; only ever a .py the batch itself just created
    and the builder only PROPOSED into (never wrote), so no analyst work is lost."""
    from mooring.app import notebooks

    try:
        target = notebooks.ws_file(workspace, notebook_rel, suffix=".py")
    except (ValueError, FileNotFoundError):
        return
    with contextlib.suppress(OSError):
        target.unlink()


@dataclass
class BatchRun:
    """One batch run: the broadcaster for SSE progress, the abort event, the
    planner, and the two invariants that used to be raw-dict fields mutated in
    place — the open/closed STATUS and the APPLIED-ONCE set of stable proposal
    ids (pids) — now guarded by the run's own lock."""

    broadcaster: object
    abort: threading.Event
    planner: object
    workspace: str
    status: str = "open"  # "open" | "closed"
    _applied: set = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def already_applied(self, pid) -> bool:
        if pid is None:
            return False
        with self._lock:
            return pid in self._applied

    def mark_applied(self, pid) -> None:
        if pid is None:
            return
        with self._lock:
            self._applied.add(pid)

    def applied_pids(self) -> set:
        """A snapshot for the review tray's ``applied`` flags."""
        with self._lock:
            return set(self._applied)

    def is_reapable(self, timeout: float) -> bool:
        """Caught up (no build in flight) and idle past the timeout. A
        still-building run is never reapable (its job events keep the
        broadcaster fresh); a closed run is already torn down."""
        return (
            self.status != "closed"
            and self.planner.is_idle()  # ty: ignore[unresolved-attribute]
            and self.broadcaster.idle_seconds() > timeout  # ty: ignore[unresolved-attribute]
        )

    def close(self, *, cancel: bool = False) -> None:
        """Tear the run down (idempotent). ``cancel=True`` additionally fires the
        abort event so in-flight builds stop instead of draining; the broadcaster
        close ends any live SSE stream with a ``closed`` event."""
        with self._lock:
            self.status = "closed"
        if cancel:
            self.abort.set()
        with contextlib.suppress(Exception):
            if self.planner is not None:
                self.planner.close(cancel=cancel)  # ty: ignore[unresolved-attribute]
        with contextlib.suppress(Exception):
            self.broadcaster.close()  # ty: ignore[unresolved-attribute]


class BatchService:
    def __init__(self) -> None:
        # Batch runs keyed by a service-minted batch_id. The builder sessions live
        # and die inside the planner thread, never here.
        self._runs: dict[str, BatchRun] = {}
        self._lock = threading.Lock()

    def get(self, batch_id: str) -> BatchRun | None:
        with self._lock:
            return self._runs.get(batch_id)

    def register(self, broadcaster, abort, planner, workspace: Path) -> str:
        """Mint a batch id and register a fresh, open run."""
        batch_id = secrets.token_urlsafe(9)
        run = BatchRun(
            broadcaster=broadcaster, abort=abort, planner=planner, workspace=str(workspace)
        )
        with self._lock:
            self._runs[batch_id] = run
        return batch_id

    def cancel(self, batch_id: str) -> bool:
        """First-class cancel for ONE run — but KEEP the registry entry, so the
        tray answers ``status: closed`` instead of a confusing 404. Returns False
        for an unknown id."""
        with self._lock:
            run = self._runs.get(batch_id)
        if run is None:
            return False
        run.close(cancel=True)
        return True

    def reap_idle(self, timeout: float) -> None:
        """Drop batch runs that are caught up and idle past the timeout, freeing
        their worker pool."""
        with self._lock:
            dead = [bid for bid, run in self._runs.items() if run.is_reapable(timeout)]
            runs = [self._runs.pop(bid) for bid in dead]
        for run in runs:
            run.close()

    def abort_all(self) -> None:
        """Tear down every run (repo switch / reload / shutdown): un-reviewed
        proposals are lost — the UI warns not to switch repos mid-batch."""
        with self._lock:
            runs = list(self._runs.values())
            self._runs.clear()
        for run in runs:
            run.close(cancel=True)
