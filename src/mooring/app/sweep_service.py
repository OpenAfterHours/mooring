"""One running verify sweep, watchable and cancellable — the service around the runner.

:mod:`mooring.app.sweep_run` is synchronous and blocking, which is right for the CLI (you
watch the lines scroll and press Ctrl-C). A browser cannot watch a blocked POST: the hub
disables its whole toolbar for the duration of a request, so a sweep run that way would
give no progress and — worse — no reachable Cancel button, on an operation that runs every
notebook end to end and can take many minutes.

So the hub starts the sweep on a worker thread and polls this snapshot. The registry
invariants (one at a time, the cancel flag, "is it still running") live here as methods
under one lock rather than as loose fields poked from route handlers — the same move
:mod:`mooring.app.batch_service` made around the batch planner.

The snapshot is value-free: counts, curated per-notebook lines, and the shared headline.
"""

from __future__ import annotations

import threading

from mooring import sweep
from mooring.app import notebook_run, sweep_run
from mooring.config import Config


class SweepBusy(Exception):
    """A sweep is already running in this hub; ``str(exc)`` is the user-facing reason."""


class SweepService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict = _idle()

    # -- reading -------------------------------------------------------------

    def snapshot(self) -> dict:
        """The current progress/result, safe to poll on a timer."""
        with self._lock:
            return dict(self._state)

    def running(self) -> bool:
        with self._lock:
            return bool(self._state.get("running"))

    # -- lifecycle -----------------------------------------------------------

    def start(self, cfg: Config, *, resume: bool = False) -> dict:
        """Begin a sweep on a worker thread; returns the initial snapshot.

        Deliberately does NOT enumerate the workspace: this runs inside the request, and
        the walk belongs on the worker (the first ``on_progress`` fills ``total`` in, and
        the client already has the count from ``/api/sweep/plan``, which it needed for the
        cost prompt anyway). One walk per sweep, none of it on the event loop.

        Raises :class:`SweepBusy` if one is already running here, and
        :class:`mooring.app.notebook_run.RunBusy` — surfaced from the worker into the
        snapshot's ``error`` — when another whole-notebook run holds the workspace."""
        with self._lock:
            if self._state.get("running"):
                raise SweepBusy("A check is already running.")
            self._cancel = threading.Event()
            self._state = _idle()
            self._state.update(running=True)
        thread = threading.Thread(
            target=self._run, args=(cfg, resume), name="mooring-sweep", daemon=True
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return self.snapshot()

    def cancel(self) -> None:
        """Stop the sweep — including the notebook currently executing, whose process TREE
        the runner kills. Idempotent, and safe on an idle service."""
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Join the worker — for shutdown and for tests. True when it finished."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # -- the worker ----------------------------------------------------------

    def _run(self, cfg: Config, resume: bool) -> None:
        cancel = self._cancel

        def _progress(done: int, total: int, item) -> None:
            with self._lock:
                self._state["done"] = done
                self._state["total"] = total
                self._state["lines"] = [*self._state["lines"], sweep_run.describe_item(item)]

        try:
            report = sweep_run.sweep_workspace(
                cfg, skip_verified=resume, cancel=cancel, on_progress=_progress
            )
        except notebook_run.RunBusy as exc:
            self._finish({"error": str(exc)})
            return
        except BaseException as exc:  # noqa: BLE001
            # Nobody is watching this thread, and the browser is polling "running": an
            # escaping exception would leave the UI spinning on a sweep that is over.
            # Type only, never str(exc) — an unexpected exception is not a curated string.
            self._finish({"error": f"The check stopped unexpectedly ({type(exc).__name__})."})
            return
        self._finish(
            {
                "summary": sweep.headline(report),
                "warning": sweep.HONESTY_NOTE,
                "total": report.total,
                "clean": report.clean,
                "failed": report.failed,
                "blocked": report.blocked,
                "skipped": report.skipped,
                "cancelled": report.cancelled,
            }
        )

    def _finish(self, fields: dict) -> None:
        with self._lock:
            self._state.update(fields)
            self._state["running"] = False
            self._state["finished"] = True


def _idle() -> dict:
    return {
        "running": False,
        "finished": False,
        "done": 0,
        "total": 0,
        "lines": [],
        "summary": "",
        "warning": "",
        "error": "",
        "clean": 0,
        "failed": 0,
        "blocked": 0,
        "skipped": 0,
        "cancelled": False,
    }
