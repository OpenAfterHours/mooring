"""Run **Verify** over every notebook in the workspace, one at a time.

``Verify`` is a per-notebook question; after a ``mooring deps add`` the interesting question
is about the whole repo — *does the team's work still run against the new lock?* This
orchestrator asks it, and it deliberately adds no new way to run a notebook: each one goes
through :func:`mooring.app.verify_run.run_verified` — the same body a hand Verify runs,
minus only the lock the sweep already holds — so a swept notebook records the
byte-identical receipt a hand-verified one does and badges in the hub exactly the same way
(including auto-clearing on the next edit). The only thing this module adds on top is the
report — see :mod:`mooring.sweep`.

Four decisions worth keeping:

* **Sequential.** N marimo kernels on a laptop is a support tarpit — memory, CPU, and N
  simultaneous connections to whatever the notebooks read. One at a time, in path order.
* **One failure never stops the sweep.** Every per-notebook exception is caught and
  recorded as that notebook's outcome; the point of a sweep is the notebooks *after* the
  broken one.
* **It takes the workspace run lock ONCE, around the whole sweep.** Every whole-notebook
  run — Verify, Deliver, a scheduled refresh, a parameterised fan-out — serializes on
  :func:`mooring.app.notebook_run.workspace_guard`, the one cross-process lockfile. The
  sweep holds it for the WHOLE loop (the fan-out's shape, not Verify's): taking it per
  notebook would let a scheduled refresh interleave halfway through and pull new bytes
  under a sweep that has already reported on the old ones. So the per-notebook body is
  the UNGUARDED :func:`mooring.app.verify_run.run_verified`, which is the same code a hand
  Verify runs — one receipt writer, structurally. A sweep that finds the workspace busy
  raises :class:`mooring.app.notebook_run.RunBusy` rather than racing.
* **Cancel stops the notebook that is running, not just the next one.** The event goes
  to the runner, which kills the process TREE (the same kill its timeout uses) and removes
  the half-written render; the values never reached are recorded as skipped rather than
  quietly dropped. On an operation measured in minutes per notebook, a Cancel that only
  declined to start the next one would be a Cancel in name only.

Value-free throughout: booleans, counts, timestamps, content hashes and curated reasons.
Nothing here reaches the AI copilot, and nothing is written outside ``.mooring/``.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from mooring import gitsha, notebook_template, sweep, sync, telemetry, verify
from mooring.app import notebook_run, verify_run
from mooring.config import Config

# Re-exported so callers need only this module to handle "someone else has the workspace".
# It is notebook_run's lock: a sweep, a refresh, a Verify, a Deliver and a fan-out all
# contend for the same one.
RunBusy = notebook_run.RunBusy

# The runner's cancel exception, and the curated reason a killed notebook records.
_CancelledRun = notebook_run.RunCancelled
_CANCELLED_MIDWAY = "cancelled while it was running"


def notebooks_in(cfg: Config) -> list[str]:
    """Every runnable marimo notebook in the workspace, in sync scope, path-sorted.

    Reuses the sync scope (:func:`mooring.sync.synced_paths`) rather than walking the disk,
    so the sweep covers exactly the files the team shares — a scratch notebook in an
    unsynced folder is nobody else's problem — and reuses
    :func:`mooring.notebook_template.opens_as_notebook` so a plain helper module is never
    executed (running one would run something that was never a notebook)."""
    workspace = cfg.workspace()
    out: list[str] = []
    for rel in sync.synced_paths(workspace, cfg.folders, cfg.exclude):
        if not rel.endswith(".py"):
            continue
        try:
            source = (workspace / rel).read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        if notebook_template.opens_as_notebook(rel, source):
            out.append(rel)
    return sorted(out)


def plan(cfg: Config) -> list[str]:
    """What a sweep would run, so a surface can say what it will cost BEFORE starting.

    A sweep executes every notebook end to end; "this will run 14 notebooks, one at a
    time" is the difference between an informed wait and a hung app."""
    return notebooks_in(cfg)


def estimate_minutes(cfg: Config, total: int) -> int:
    """Roughly how long a sweep of ``total`` notebooks will take, or 0 when unknown.

    ``n × the last sweep's median run`` — measured on this machine against these notebooks,
    which is the only estimate worth printing. "It can take a while" is not a number, and
    the worst case here (``notebook_run.RUN_TIMEOUT`` per notebook) is over an hour."""
    report = sweep.read(cfg.workspace())
    median = report.median_seconds if report is not None else 0
    return (total * median + 59) // 60 if median else 0


def describe_cost(cfg: Config, total: int) -> str:
    """The one "here is what you are about to spend" line, shared by both adapters."""
    noun = "notebook" if total == 1 else "notebooks"
    minutes = estimate_minutes(cfg, total)
    if minutes:
        return (
            f"This runs {total} {noun} on this machine, one at a time — "
            f"roughly {minutes} min, going by your last check."
        )
    return f"This runs {total} {noun} on this machine, one at a time — it can take a while."


def resume_scope(cfg: Config, lock: str | None = None) -> tuple[frozenset[str], str]:
    """What a ``--resume`` may legitimately skip, and why it can't skip more.

    THE correctness rule for resuming. A resume must never lean on a bare verify receipt:
    a receipt is keyed to the NOTEBOOK's bytes and carries no record of the environment it
    ran in, so after ``mooring deps add`` every receipt is still "valid" over a lock nothing
    has been run against. Resuming on those would stamp a fresh lock fingerprint onto an
    old sweep with **zero notebooks executed** — a green report, and a disarmed push gate,
    from one documented command.

    So a resume skips only what the STORED SWEEP recorded clean, and only while that sweep
    was taken under the lock we are about to run under. Anything else resumes nothing and
    runs the lot. Returns ``(skippable, reason)``; ``reason`` is a curated line for the
    surfaces to show when a requested resume can't be honoured."""
    workspace = cfg.workspace()
    current = sweep.lock_fingerprint(workspace) if lock is None else lock
    prior = sweep.read(workspace)
    if prior is None:
        return frozenset(), "there is no previous check to resume"
    if prior.lock != current:
        return frozenset(), "the last check ran against different dependencies"
    # `stale` is already excluded by `of()` — an edited notebook is not resumable.
    skippable = frozenset(i.notebook for i in prior.of(sweep.CLEAN))
    if not skippable:
        return frozenset(), "the last check has nothing still passing to skip"
    return skippable, ""


def sweep_workspace(
    cfg: Config,
    *,
    rels: list[str] | None = None,
    skip_verified: bool = False,
    cancel: threading.Event | None = None,
    on_progress=None,
) -> sweep.SweepReport:
    """Verify every notebook (or just ``rels``) and record one value-free report.

    ``skip_verified`` is the resume, scoped by :func:`resume_scope` — which is what makes
    it safe to expose as a flag rather than a footgun: it can only skip notebooks a
    PREVIOUS SWEEP ran clean **under the same lock**, so a resume across a dependency
    change silently degrades to a full run instead of manufacturing a green report.

    ``cancel`` is a ``threading.Event`` another thread may set to stop the sweep; it is
    checked at each notebook boundary AND handed to the runner, so the notebook already
    executing is killed rather than left to finish. ``on_progress(done, total, item)`` is
    called after each notebook.

    Raises :class:`mooring.app.notebook_run.RunBusy` when a refresh, a fan-out, a Deliver
    or another sweep already holds this workspace."""
    workspace = cfg.workspace()
    targets = list(rels) if rels is not None else notebooks_in(cfg)
    started = datetime.now(timezone.utc)
    # The lock the runs happen under, captured BEFORE any of them: a `deps` command landing
    # mid-sweep must not let the report claim to describe the new lock.
    lock = sweep.lock_fingerprint(workspace)
    skippable = resume_scope(cfg, lock)[0] if skip_verified else frozenset()

    with notebook_run.workspace_guard(workspace):
        items, cancelled = _run_all(cfg, targets, skippable, cancel, on_progress)

    report = sweep.SweepReport(
        items=tuple(items),
        started_at=started.isoformat(timespec="seconds"),
        finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        cancelled=cancelled,
        lock=lock,
    )
    sweep.record(workspace, report)
    # Value-free: counts only, never a path.
    _log(report)
    return report


def _log(report: sweep.SweepReport) -> None:
    telemetry.log_event(
        "sweep",
        total=report.total,
        clean=report.clean,
        failed=report.failed,
        blocked=report.blocked,
        cancelled=int(report.cancelled),
    )


def _run_all(
    cfg: Config, targets: list[str], skippable: frozenset[str], cancel, on_progress
) -> tuple[list[sweep.SweepItem], bool]:
    workspace = cfg.workspace()
    # Belt and braces on top of resume_scope: a resumable notebook must ALSO still hold a
    # SHA-valid passing receipt right now (read_results enforces that), so an edit between
    # the two sweeps re-runs it rather than inheriting the old verdict.
    valid = verify.read_results(workspace) if skippable else {}
    items: list[sweep.SweepItem] = []
    cancelled = False
    total = len(targets)
    for index, rel in enumerate(targets, start=1):
        if cancelled or (cancel is not None and cancel.is_set()):
            cancelled = True
            items.append(
                sweep.SweepItem(
                    notebook=rel,
                    outcome=sweep.SKIPPED,
                    sha=_sha(workspace, rel),
                    reason="cancelled before this notebook ran",
                )
            )
            continue
        receipt = valid.get(rel)
        if rel in skippable and receipt and receipt.get("passed"):
            items.append(
                sweep.SweepItem(
                    notebook=rel,
                    outcome=sweep.CLEAN,
                    sha=_sha(workspace, rel),
                    reason="already checked against these dependencies (not re-run)",
                )
            )
        else:
            item = _run_one(cfg, rel, cancel)
            if item.outcome == sweep.SKIPPED and item.reason == _CANCELLED_MIDWAY:
                cancelled = True  # the runner was killed part-way: the sweep is cancelled
            items.append(item)
        if on_progress is not None:
            on_progress(index, total, items[-1])
    return items, cancelled


def _sha(workspace: Path, rel: str) -> str:
    try:
        return gitsha.local_blob_sha(workspace / rel, rel)
    except OSError:
        return ""  # unreadable: no SHA means the item can never vouch (sweep._moved)


def _run_one(cfg: Config, rel: str, cancel=None) -> sweep.SweepItem:
    """One notebook, through the shared attended-verify body — under the lock the SWEEP
    already holds, never taking it again. Never raises: whatever went wrong becomes this
    notebook's recorded outcome so the sweep carries on."""
    began = time.monotonic()
    try:
        rel_posix = verify_run.ensure_runnable(
            cfg.workspace(), rel, verify_run.VerifyError
        )
        result = verify_run.run_verified(cfg, rel_posix, cancel=cancel)
    except _CancelledRun:
        # Killed part-way by Cancel. NOT a failing notebook and not a blocked one — it was
        # simply never given the chance to answer, which is what SKIPPED means here.
        return sweep.SweepItem(
            notebook=rel,
            outcome=sweep.SKIPPED,
            sha=_sha(cfg.workspace(), rel),
            reason=_CANCELLED_MIDWAY,
            seconds=_elapsed(began),
        )
    except verify_run.VerifyError as exc:
        # "Could not be run at all" — a missing renderer, a timeout, a broken environment.
        # notebook_run deliberately does NOT badge this as a failing notebook, and neither
        # do we: it is its own outcome, with a curated (value-free) reason.
        return sweep.SweepItem(
            notebook=rel,
            outcome=sweep.BLOCKED,
            sha=_sha(cfg.workspace(), rel),
            reason=_blocked_reason(exc),
            seconds=_elapsed(began),
        )
    except (ValueError, FileNotFoundError):
        return sweep.SweepItem(
            notebook=rel,
            outcome=sweep.SKIPPED,
            reason="not a notebook mooring can run",
        )
    return sweep.SweepItem(
        notebook=rel,
        outcome=sweep.CLEAN if result.passed else sweep.FAILED,
        # The PRE-run SHA the receipt was keyed to (verify_run.VerifyResult.sha) — never a
        # re-hash: an edit landing mid-run must key this item to bytes the run never
        # executed, so sweep.read() drops it into `stale` exactly as the badge clears.
        sha=result.sha,
        cells_failed=result.cells_failed,
        reason="" if result.passed else _describe_failure(result.cells_failed),
        seconds=_elapsed(began),
    )


def _elapsed(began: float) -> int:
    return max(1, round(time.monotonic() - began))


def _blocked_reason(exc: Exception) -> str:
    """A curated line for "it could not be run at all".

    ``notebook_run``'s own messages are curated with one exception: the OSError path
    interpolates ``str(exc)``, which on Windows carries an absolute path. That was fine
    while it only reached a console; the sweep PERSISTS its reasons to
    ``.mooring/sweep.json``, so the exception contributes its TYPE only — the same posture
    the refresh orchestrator takes with GitHub errors."""
    cause = exc
    for _ in range(4):  # VerifyError -> RunError -> OSError
        cause = getattr(cause, "__cause__", None)
        if cause is None:
            break
        if isinstance(cause, OSError):
            return f"it could not be started ({type(cause).__name__})"
    return str(exc)


def _describe_failure(cells_failed: int | None) -> str:
    if cells_failed:
        cells = "cell" if cells_failed == 1 else "cells"
        return f"{cells_failed} {cells} failed to run"
    return "it failed to run"


def describe_item(item: sweep.SweepItem) -> str:
    """One human line per notebook — shared by both adapters."""
    if item.outcome == sweep.CLEAN:
        return f"{item.notebook} — ran clean." if not item.reason else (
            f"{item.notebook} — {item.reason}."
        )
    if item.outcome == sweep.SKIPPED:
        return f"{item.notebook} — skipped ({item.reason})."
    return f"{item.notebook} — {item.reason}"
