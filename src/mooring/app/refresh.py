"""Run a notebook on its cadence: pull the team's latest, execute it, report value-free facts.

This is the orchestrator behind ``mooring refresh`` and the hub's catch-up sweep. It is the
only module that reaches both the sync domain and the notebook runner, which is exactly what
``app/`` is for.

**A scheduled run has no write authority.** It may do three things and no more:

1. **Pull** — read-only into the workspace, conflicts SKIPPED (never resolved unattended).
2. **Run the notebook locally**, via the shared hardened runner (:mod:`app.notebook_run`).
3. **Write inside ``.mooring/``** — receipts, and at most one outbox artifact.

It may never push, propose, resolve a conflict, or delete a workspace file, and no channel
here reaches the AI copilot. There is deliberately no import of ``sync.push``/``sync.propose``
in this module — ``test_refresh.py`` pins that by scanning the source, because import-linter
works at module granularity and cannot express it. The consequence is that the worst possible
unattended failure is *a local file that did not get written*: nothing an unattended run does
can corrupt the team repo, because an unattended run cannot write to the team repo.

**The pull is best-effort by design.** "Pull if possible" is the point — a refresh exists to
pick up the team's latest notebook and data — but not being able to pull is a *degraded* run,
never a failed one. Offline, signed out, or a skipped conflict all produce a curated,
value-free reason that rides the receipt and the hub board, so a run against a stale local
copy announces itself rather than passing as clean.

Everything recorded is value-free: booleans, counts, timestamps, and curated reason strings.
A GitHub error contributes its TYPE only — never ``str(exc)``, which can embed a request URL
(the same posture as the reviews route and the history endpoint).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mooring import activity, checks, gitsha, inputs, schedule, sync, telemetry, verify
from mooring.app import deliver, notebook_run, notebooks, verify_run
from mooring.config import Config
from mooring.github import AuthFailed, GitHubError, Unreachable

# One refresh at a time per process. A notebook run is CPU-heavy and a pull writes workspace
# files; two concurrent refreshes would fight over both. The hub's sweep thread and a user's
# "Run now" click serialize here rather than racing.
_run_lock = threading.Lock()

# ...and one per WORKSPACE across processes. Once background refresh is registered (an OS
# task or a sign-in agent — see mooring.schedule_os), the hub's sweep and that background
# process are genuinely separate processes pointed at the same workspace, and a thread lock
# says nothing about them. Two concurrent runs would both pull, both write receipts, and
# fight for CPU.
_LOCK_NAME = "refresh.lock"
# A held lock older than this is assumed to belong to a process that died mid-run (a
# hard reboot, a killed agent). Comfortably above the 300s run timeout so a slow-but-alive
# run is never stolen from.
_LOCK_STALE_S = 900


class RefreshBusy(Exception):
    """Another notebook run already holds this workspace (in this process or a background
    one). Not an error to record — the caller simply steps aside; whatever is already
    running will write the receipt.

    Raised by :func:`workspace_guard`, so it also covers an attended parameterised run
    (:mod:`mooring.app.param_runs`) colliding with a scheduled refresh, in either order."""


_BUSY = (
    "This workspace is already running a notebook (a scheduled refresh or a parameterised "
    "run) — wait for it to finish."
)


@contextlib.contextmanager
def workspace_guard(workspace: Path):
    """Hold the cross-process run lock for ``workspace``, or raise :class:`RefreshBusy`.

    ``O_CREAT | O_EXCL`` is atomic on Windows and POSIX alike, so the file's existence IS the
    lock. A stale lock (older than :data:`_LOCK_STALE_S`) is stolen rather than deadlocking
    forever — a background agent killed mid-run must not wedge every future refresh, which
    would be a silent stop of exactly the kind this feature exists to prevent.

    It is deliberately ONE lock for every kind of whole-notebook run, not one per feature:
    two runs against the same workspace fight over the CPU, over the same throwaway render
    path, and over the files the notebooks read. A fan-out therefore takes this same lock,
    so a scheduled refresh cannot start underneath it (and vice versa)."""
    path = workspace / ".mooring" / _LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _stale(path):
            raise RefreshBusy(_BUSY) from None
        # Steal it: best-effort unlink then one retry. Losing the retry means another process
        # got there first, which is a perfectly good outcome — it is doing the work.
        with contextlib.suppress(OSError):
            path.unlink()
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            raise RefreshBusy(_BUSY) from None
    except OSError as exc:
        # A read-only or otherwise unusable state dir: refuse rather than running unguarded.
        raise RefreshBusy(f"Could not take the workspace run lock: {exc}") from exc
    try:
        with contextlib.suppress(OSError):
            os.write(handle, str(os.getpid()).encode("ascii"))
        os.close(handle)
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def _stale(path: Path) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) > _LOCK_STALE_S
    except OSError:
        return False


class RefreshRefused(Exception):
    """The refresh could not be started (paused, missing, or not a notebook). ``str(exc)`` is
    the user-facing reason. Distinct from a run that started and FAILED — that is a recorded
    outcome, not an exception."""


@dataclass(frozen=True)
class RefreshResult:
    notebook: str
    outcome: str  # schedule.OK | DEGRADED | CHECKS_FAILED | FAILED
    ran: bool
    checks_failed: int = 0
    checks_total: int = 0
    inputs_changed: int = 0
    conflicts: int = 0
    pulled: int = 0
    reason: str = ""  # curated + value-free; "" when nothing to report
    artifact: str = ""  # workspace-relative outbox path, or ""
    ran_at: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == schedule.OK


# -- preflight ---------------------------------------------------------------


@dataclass(frozen=True)
class Preflight:
    may_run: bool
    verified: bool
    reason: str = ""

    @property
    def budget(self) -> int:
        """The consecutive-failure budget this run gets. A notebook whose verification has
        LAPSED (it was edited since it last ran clean) gets one strike, not three: the edit
        may well be fine, so refusing to run would itself be a silent failure — but a
        doubtful notebook must stop shouting into the void after a single bad run."""
        return 3 if self.verified else 1


def preflight(cfg: Config, sched: schedule.Schedule) -> Preflight:
    """Whether ``sched`` may run now, and how much rope it gets.

    Verification is checked through :func:`mooring.verify.read_results`, which only surfaces a
    receipt whose stored content SHA still matches the file. So editing a scheduled notebook
    automatically lapses its verification with no invalidation logic of our own — the same
    rule that auto-clears the trust badge."""
    workspace = cfg.workspace()
    if sched.paused:
        return Preflight(False, False, "paused — resume it to run again")
    if not (workspace / sched.notebook).is_file():
        return Preflight(False, False, "the notebook is no longer in the workspace")
    receipt = verify.read_results(workspace).get(sched.notebook)
    verified = bool(receipt and receipt.get("passed"))
    reason = "" if verified else "edited since it was verified — re-verify to restore full retries"
    return Preflight(True, verified, reason)


def may_auto_run(cfg: Config, sched: schedule.Schedule) -> bool:
    """Whether this schedule may fire WITHOUT a human clicking Run now.

    Auto-running is for the boring case only: a verified notebook that is not paused and did
    not fail last time. Anything doubtful surfaces as "1 refresh due — Run now" instead, so an
    unattended run never surprises someone whose notebook is in a questionable state. This
    reuses the preflight state rather than adding a setting."""
    check = preflight(cfg, sched)
    return check.may_run and check.verified and sched.last_run.outcome != schedule.FAILED


# -- the run -----------------------------------------------------------------


def refresh_notebook(
    cfg: Config,
    rel_path: str,
    *,
    sched: schedule.Schedule | None = None,
    pull: bool | None = None,
    do_deliver: bool | None = None,
) -> RefreshResult:
    """Pull (if possible), run ``rel_path``, and record what happened.

    ``sched`` supplies the cadence settings and receives the receipt; when it is None this is
    an ad-hoc run (``mooring refresh <path>`` on an unscheduled notebook), which is legitimate
    and simply records nothing against a schedule. ``pull`` / ``do_deliver`` override the
    schedule's own flags.

    Raises :class:`RefreshRefused` for a target that must not be run, and ``ValueError`` /
    ``FileNotFoundError`` (from :func:`notebooks.ws_file`) for a bad path."""
    workspace = cfg.workspace()
    rel_posix = verify_run.ensure_runnable(workspace, rel_path, RefreshRefused)
    want_pull = (sched.pull if sched else True) if pull is None else pull
    want_deliver = (sched.deliver if sched else False) if do_deliver is None else do_deliver

    with _run_lock, workspace_guard(workspace):
        return _run_one(cfg, rel_posix, sched, want_pull, want_deliver)


def _run_one(
    cfg: Config,
    rel_posix: str,
    sched: schedule.Schedule | None,
    want_pull: bool,
    want_deliver: bool,
) -> RefreshResult:
    workspace = cfg.workspace()
    started = datetime.now(timezone.utc)
    pulled, conflicts, pull_reason = _try_pull(cfg) if want_pull else (0, 0, "")

    # SHA the bytes we are ABOUT to run, before the run — same fail-safe as verify, so an edit
    # landing mid-run keys the receipt to stale bytes and the badge auto-clears.
    target = workspace / rel_posix
    try:
        sha = gitsha.local_blob_sha(target, rel_posix)
    except OSError as exc:
        raise RefreshRefused(f"Could not read the notebook: {exc}") from exc

    artifact = ""
    # Render into the sync-excluded throwaway path ALWAYS, and promote it into the outbox only
    # on success. That is what makes "a failed run never overwrites a good artifact" structural:
    # the previous good HTML is never even opened unless there is a complete new one to replace
    # it with.
    render = verify.render_target(workspace, rel_posix)
    try:
        outcome = notebook_run.run(
            workspace, rel_posix, render, keep_on_success=want_deliver
        )
        if outcome.ok and want_deliver:
            artifact = _promote(workspace, cfg, rel_posix, render, sched, started)
    except notebook_run.RunError as exc:
        return _record(
            cfg, rel_posix, sched, started,
            outcome=schedule.FAILED, ran=False, reason=str(exc),
            pulled=pulled, conflicts=conflicts,
        )
    finally:
        # Whatever is left of the value-bearing render, on every path.
        with contextlib.suppress(OSError):
            render.unlink()

    ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    verify_run.record_receipt(workspace, rel_posix, outcome, sha=sha, ran_at=ran_at)

    checks_failed, checks_total = _read_checks(workspace, rel_posix, started)
    changed = _read_inputs(workspace, rel_posix, started)

    if not outcome.ok:
        state, reason = schedule.FAILED, _describe_failure(outcome)
    elif checks_failed:
        state, reason = schedule.CHECKS_FAILED, _describe_checks(checks_failed, checks_total)
    elif pull_reason or conflicts:
        state, reason = schedule.DEGRADED, pull_reason or _describe_conflicts(conflicts)
    else:
        state, reason = schedule.OK, ""

    return _record(
        cfg, rel_posix, sched, started,
        outcome=state, ran=True, reason=reason, artifact=artifact,
        checks_failed=checks_failed, checks_total=checks_total,
        inputs_changed=changed, pulled=pulled, conflicts=conflicts, ran_at=ran_at,
    )


def _promote(
    workspace: Path,
    cfg: Config,
    rel_posix: str,
    render: Path,
    sched: schedule.Schedule | None,
    started: datetime,
) -> str:
    """Move a completed render into the outbox and stamp its provenance + freshness footer.

    Best-effort: a failure to promote is not a failed RUN (the notebook executed fine), so it
    downgrades to "no artifact this time" rather than losing the receipt."""
    final = deliver.outbox_target(workspace, rel_posix)
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(render, final)
    except OSError:
        return ""
    note = schedule.freshness_note(sched, started.astimezone()) if sched else ""
    deliver.stamp_provenance(final, cfg, rel_posix, workspace, note=note)
    return final.relative_to(workspace).as_posix()


def _try_pull(cfg: Config) -> tuple[int, int, str]:
    """(pulled, conflicts, reason) — pull the team's latest, degrading rather than failing.

    Conflicts are SKIPPED, never resolved: resolving one unattended would be exactly the kind
    of silent decision mooring exists to prevent. A skipped conflict means the run executed
    against a version that is NOT the team's latest, which is why it produces a reason rather
    than passing quietly.

    A GitHub error contributes its TYPE only — ``str(exc)`` on a NotFound embeds the request
    URL, and paths/URLs never ride a receipt."""
    try:
        client = notebooks.client_for(cfg)
    except notebooks.NotConfigured:  # checked first: it SUBCLASSES AuthFailed
        return 0, 0, "no team repo configured — ran against the local copy"
    except AuthFailed:
        return 0, 0, "not signed in to GitHub — ran against the local copy"
    try:
        result = sync.pull(client, cfg, strategy=sync.ConflictStrategy.SKIP)
    except Unreachable:
        return 0, 0, "GitHub unreachable — ran against the local copy"
    except (GitHubError, OSError) as exc:
        return 0, 0, f"pull failed ({type(exc).__name__}) — ran against the local copy"
    return result.pulled, len(result.skipped_conflicts), ""


# -- reading back the value-free facts ---------------------------------------


def _fresh(updated: str, started: datetime) -> bool:
    """Whether a receipt was (re)written by THIS run.

    Receipts persist between runs, so a notebook that no longer calls ``mooring_checks`` would
    otherwise keep reporting the last run's failures forever — pinning a schedule red on a
    stale file. Comparing against the run's start makes the readback honest."""
    if not updated:
        return False
    try:
        moment = datetime.fromisoformat(updated)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    # One second of slack: the receipt's timestamp has second granularity, so a check written
    # in the same second the run started would otherwise read as stale.
    return moment >= started.replace(microsecond=0)


def _read_checks(workspace: Path, rel_posix: str, started: datetime) -> tuple[int, int]:
    entry = checks.read_results(workspace).get(rel_posix)
    if not entry or not _fresh(entry.get("updated", ""), started):
        return 0, 0
    return int(entry.get("failed", 0)), int(entry.get("total", 0))


def _read_inputs(workspace: Path, rel_posix: str, started: datetime) -> int:
    entry = inputs.read_results(workspace).get(rel_posix)
    if not entry or not _fresh(entry.get("updated", ""), started):
        return 0
    return int(entry.get("changed", 0))


# -- recording ---------------------------------------------------------------


def _record(
    cfg: Config,
    rel_posix: str,
    sched: schedule.Schedule | None,
    started: datetime,
    *,
    outcome: str,
    ran: bool,
    reason: str = "",
    artifact: str = "",
    checks_failed: int = 0,
    checks_total: int = 0,
    inputs_changed: int = 0,
    pulled: int = 0,
    conflicts: int = 0,
    ran_at: str = "",
) -> RefreshResult:
    workspace = cfg.workspace()
    ran_at = ran_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    if sched is not None:
        schedule.record_run(
            workspace,
            sched.notebook,
            outcome=outcome,
            checks_failed=checks_failed,
            inputs_changed=inputs_changed,
            conflicts=conflicts,
            reason=reason,
            artifact=artifact,
            ran_at=ran_at,
            budget=preflight(cfg, sched).budget,
        )
    activity.record(workspace, "refresh", path=rel_posix, ok=(outcome == schedule.OK))
    # Value-free: the outcome name only, never a path.
    telemetry.log_event("refresh", outcome=outcome)
    return RefreshResult(
        notebook=rel_posix,
        outcome=outcome,
        ran=ran,
        checks_failed=checks_failed,
        checks_total=checks_total,
        inputs_changed=inputs_changed,
        conflicts=conflicts,
        pulled=pulled,
        reason=reason,
        artifact=artifact,
        ran_at=ran_at,
    )


def _describe_failure(outcome: notebook_run.RunOutcome) -> str:
    if outcome.cells_failed:
        cells = "cell" if outcome.cells_failed == 1 else "cells"
        return f"{outcome.cells_failed} {cells} failed to run"
    return "the notebook failed to run"


def _describe_checks(failed: int, total: int) -> str:
    return f"{failed} of {total} tie-out check(s) failing"


def _describe_conflicts(conflicts: int) -> str:
    files = "file" if conflicts == 1 else "files"
    return f"{conflicts} {files} in conflict were not updated — ran against your copy"


# -- the sweep ---------------------------------------------------------------


def run_due(
    cfg: Config, *, now: datetime | None = None, auto_only: bool = False
) -> list[RefreshResult]:
    """Run every schedule that is due, oldest window first. Returns one result per run.

    This is the catch-up sweep. :func:`mooring.schedule.is_due` compares only against the
    CURRENT cadence window, so returning after a week away runs a daily schedule once, not
    seven times.

    ``auto_only`` restricts the sweep to schedules that may fire without a human (see
    :func:`may_auto_run`) — what the hub's background sweep passes. A run that raises is
    recorded as a failure and never stops the sweep: one broken notebook must not prevent the
    others from refreshing.

    A :class:`RefreshBusy` ends the sweep quietly rather than recording anything: another
    process (an OS task, the sign-in agent, or the hub) already holds the workspace and is
    doing this work. Recording a failure there would invent a problem that does not exist —
    and, worse, spend the failure budget on it."""
    workspace = cfg.workspace()
    results: list[RefreshResult] = []
    for sched in schedule.due(schedule.load(workspace), now):
        if auto_only and not may_auto_run(cfg, sched):
            continue
        try:
            results.append(refresh_notebook(cfg, sched.notebook, sched=sched))
        except RefreshBusy:
            break
        except (RefreshRefused, ValueError, FileNotFoundError) as exc:
            results.append(
                _record(
                    cfg, sched.notebook, sched, datetime.now(timezone.utc),
                    outcome=schedule.FAILED, ran=False, reason=str(exc),
                )
            )
    return results


def describe_result(result: RefreshResult) -> str:
    """One human line per run — shared by the CLI and the hub so they can never word it
    differently."""
    head = f"{result.notebook} — "
    if result.outcome == schedule.OK:
        line = "refreshed clean"
        if result.checks_total:
            line += f", {result.checks_total} check(s) passing"
        if result.inputs_changed:
            line += f" ({result.inputs_changed} input(s) changed)"
        return head + line + "."
    if result.outcome == schedule.CHECKS_FAILED:
        return head + f"ran, but {result.reason}."
    if result.outcome == schedule.DEGRADED:
        return head + f"refreshed, but {result.reason}."
    return head + f"did not refresh — {result.reason}"
