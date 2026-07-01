"""The AI batch application service — the run registry around BatchPlanner.

:class:`mooring.ai.batch.BatchPlanner` is pure and dependency-injected (it owns
the build pool and per-job state); what used to live in the web adapter was the
REGISTRY around it — the ``batch_id -> run`` dict, its lock, idle reaping, and
the abort-on-repo-switch teardown. That state now has one owner here. The run is
still a plain dict (``broadcaster/abort/planner/status/applied/workspace``); the
architecture plan's P5 types it as a ``BatchRun``.

Also adds the first-class :meth:`BatchService.cancel` — previously the only way
to stop a runaway batch was switching repos (which aborts ALL runs as a side
effect).
"""

from __future__ import annotations

import contextlib
import secrets
import threading
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


class BatchService:
    def __init__(self) -> None:
        # Batch notebook-generation runs, keyed by a service-minted batch_id. Each
        # holds a ChatBroadcaster for SSE progress, an abort Event, and (when
        # finished) the value-free per-job results the review tray + per-notebook
        # Apply read. The builder sessions live and die inside the planner thread.
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, batch_id: str) -> dict | None:
        with self._lock:
            return self._runs.get(batch_id)

    def register(self, broadcaster, abort, planner, workspace: Path) -> str:
        """Mint a batch id and register a fresh, open run."""
        batch_id = secrets.token_urlsafe(9)
        run = {
            "broadcaster": broadcaster,
            "abort": abort,
            "planner": planner,
            "status": "open",
            "applied": set(),
            "workspace": str(workspace),
        }
        with self._lock:
            self._runs[batch_id] = run
        return batch_id

    # -- the applied-once bookkeeping (typed into BatchRun by P5) ---------------

    def already_applied(self, run: dict, pid) -> bool:
        with self._lock:
            return pid is not None and pid in run["applied"]

    def mark_applied(self, run: dict, pid) -> None:
        with self._lock:
            if pid is not None:
                run["applied"].add(pid)

    # -- lifecycle ---------------------------------------------------------------

    def cancel(self, batch_id: str) -> bool:
        """First-class cancel for ONE run: stop the builds (abort event + planner
        close), end the SSE stream (broadcaster close emits ``closed``), and mark
        the run closed — but KEEP the registry entry, so the tray answers
        ``status: closed`` instead of a confusing 404. Returns False for an
        unknown id."""
        with self._lock:
            run = self._runs.get(batch_id)
            if run is None:
                return False
            run["status"] = "closed"
        run["abort"].set()
        with contextlib.suppress(Exception):
            run["planner"].close(cancel=True)
        with contextlib.suppress(Exception):
            run["broadcaster"].close()
        return True

    def reap_idle(self, timeout: float) -> None:
        """Drop batch runs that are caught up (no build in flight) and have had no
        activity for the idle timeout, freeing their worker pool. A still-building
        run is never reaped (its job events keep the broadcaster fresh)."""
        with self._lock:
            dead = [
                bid
                for bid, run in self._runs.items()
                if run["status"] != "closed"
                and run["planner"].is_idle()
                and run["broadcaster"].idle_seconds() > timeout
            ]
            runs = [self._runs.pop(bid) for bid in dead]
        for run in runs:
            run["status"] = "closed"
            with contextlib.suppress(Exception):
                run["planner"].close()
            with contextlib.suppress(Exception):
                run["broadcaster"].close()

    def abort_all(self) -> None:
        """Tear down every run (repo switch / reload / shutdown): un-reviewed
        proposals are lost — the UI warns not to switch repos mid-batch."""
        with self._lock:
            runs = list(self._runs.values())
            self._runs.clear()
        for run in runs:
            run["abort"].set()
            with contextlib.suppress(Exception):
                if run.get("planner") is not None:
                    run["planner"].close(cancel=True)
            with contextlib.suppress(Exception):
                run["broadcaster"].close()
