"""Notebook refresh schedules: what runs on a cadence, when it is next due, and how it went.

A "schedule" says *this notebook should be re-run every weekday at 07:30*. It exists so an
analyst stops having to remember; it is NOT a promise that a machine will be awake to do it.
That distinction drives the whole design:

* **The cadence is a freshness CONTRACT, not a guarantee.** Declaring a cadence is what makes
  "overdue" computable at all — without it, "last run nine days ago" is unclassifiable. The
  clock that fires a run is a separate, best-effort concern (the hub's catch-up sweep, and
  later an OS task); what this module guarantees is that whenever a run *hasn't* happened,
  :func:`is_overdue` says so out loud. A stale refresh must never be a silent one.
* **Catch-up, not backfill.** :func:`is_due` asks only "is there a successful run inside the
  CURRENT cadence window?" — so opening mooring after a week away runs a daily schedule once,
  not seven times. (Airflow's ``catchup=True`` is the well-known version of this footgun.)
* **A data failure is not an infrastructure failure.** ``checks_failed`` — the notebook ran
  but its tie-outs no longer reconcile — is deliberately NOT counted against the consecutive-
  failure budget. Auto-pausing a schedule because the numbers stopped tying out would silence
  precisely the signal the analyst most needs repeated. Only ``failed`` (the notebook did not
  run at all) spends the budget.

Schedules live in the sync-excluded ``.mooring/schedules.json``: a schedule names a
workspace-relative notebook, is per-repo and per-machine by nature, and must never travel to
teammates. ``.mooring`` is excluded structurally by :func:`mooring.sync.is_synced_path` on
both the local scan and the remote tree, so a schedule cannot ride a push.

Every recorded field is VALUE-FREE — booleans, counts, timestamps, and curated reason strings
— never a data value and never a raw exception message. Same contract as :mod:`mooring.verify`
and :mod:`mooring.checks`, and like them this is a lean-core leaf: it imports only
:mod:`mooring.paths` and the standard library, so it carries no path to marimo / the Copilot
SDK / spaCy (locked by the ``frozen-core-is-lean`` import contract).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from mooring.paths import safe_write_bytes

STATE_DIR = ".mooring"
SCHEDULES_NAME = "schedules.json"
FORMAT_VERSION = 1

# The closed cadence vocabulary. Deliberately NOT cron: this audience does not write cron,
# and a closed set is what lets the hub compute and display "next due" without a parser.
CADENCES = ("hourly", "daily", "weekdays", "weekly")

DEFAULT_AT = "07:30"
DEFAULT_GRACE_HOURS = 4
DEFAULT_MAX_FAILURES = 3

# Run outcomes, worst last. `failed` means the notebook did not run; `checks_failed` means it
# ran but a mooring_checks tie-out did not reconcile; `degraded` means it ran clean but
# something about the run was compromised (the pull was skipped, or an input changed).
OK = "ok"
DEGRADED = "degraded"
CHECKS_FAILED = "checks_failed"
FAILED = "failed"

_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ScheduleError(ValueError):
    """A schedule could not be created or amended; ``str(exc)`` is the user-facing reason."""


@dataclass(frozen=True)
class LastRun:
    """The value-free receipt of a schedule's most recent run."""

    at: str = ""  # UTC ISO-8601, as verify.record writes it
    outcome: str = ""  # OK | DEGRADED | CHECKS_FAILED | FAILED, or "" when never run
    checks_failed: int = 0
    inputs_changed: int = 0
    conflicts: int = 0  # files a pull skipped because they are in conflict
    reason: str = ""  # curated, value-free ("GitHub unreachable — ran against the local copy")
    artifact: str = ""  # workspace-relative outbox path, or "" when nothing was delivered

    def to_dict(self) -> dict:
        return {
            "at": self.at,
            "outcome": self.outcome,
            "checks_failed": self.checks_failed,
            "inputs_changed": self.inputs_changed,
            "conflicts": self.conflicts,
            "reason": self.reason,
            "artifact": self.artifact,
        }

    @classmethod
    def from_dict(cls, data) -> LastRun:
        if not isinstance(data, dict):
            return cls()
        return cls(
            at=_str(data.get("at")),
            outcome=_str(data.get("outcome")),
            checks_failed=_int(data.get("checks_failed")),
            inputs_changed=_int(data.get("inputs_changed")),
            conflicts=_int(data.get("conflicts")),
            reason=_str(data.get("reason")),
            artifact=_str(data.get("artifact")),
        )


@dataclass(frozen=True)
class Schedule:
    notebook: str  # workspace-relative POSIX path
    cadence: str = "daily"
    at: str = DEFAULT_AT  # local wall-clock "HH:MM"
    day: int = 0  # weekly only: 0=Mon .. 6=Sun
    deliver: bool = True  # also render the stakeholder HTML into the outbox
    pull: bool = True  # pull the team's latest before running (degrades if it can't)
    grace_hours: int = DEFAULT_GRACE_HOURS
    max_failures: int = DEFAULT_MAX_FAILURES
    paused: bool = False
    consecutive_failures: int = 0
    last_run: LastRun = field(default_factory=LastRun)

    def to_dict(self) -> dict:
        return {
            "notebook": self.notebook,
            "cadence": self.cadence,
            "at": self.at,
            "day": self.day,
            "deliver": self.deliver,
            "pull": self.pull,
            "grace_hours": self.grace_hours,
            "max_failures": self.max_failures,
            "paused": self.paused,
            "consecutive_failures": self.consecutive_failures,
            "last_run": self.last_run.to_dict(),
        }

    @classmethod
    def from_dict(cls, data) -> Schedule | None:
        """Parse one stored schedule, or None when it is unusable.

        Tolerant by design: a hand-edited or future-version file must degrade to "that one
        schedule is ignored", never to a crashed hub."""
        if not isinstance(data, dict):
            return None
        notebook = _str(data.get("notebook")).replace("\\", "/")
        if not notebook:
            return None
        cadence = _str(data.get("cadence")) or "daily"
        if cadence not in CADENCES:
            return None
        try:
            at = normalize_at(_str(data.get("at")) or DEFAULT_AT)
        except ScheduleError:
            at = DEFAULT_AT
        return cls(
            notebook=notebook,
            cadence=cadence,
            at=at,
            day=min(6, max(0, _int(data.get("day")))),
            deliver=bool(data.get("deliver", True)),
            pull=bool(data.get("pull", True)),
            grace_hours=max(0, _int(data.get("grace_hours"), DEFAULT_GRACE_HOURS)),
            max_failures=max(1, _int(data.get("max_failures"), DEFAULT_MAX_FAILURES)),
            paused=bool(data.get("paused", False)),
            consecutive_failures=max(0, _int(data.get("consecutive_failures"))),
            last_run=LastRun.from_dict(data.get("last_run")),
        )

    def describe_cadence(self) -> str:
        """One human phrase for the cadence — shared by the CLI, the hub card, and the
        artifact's freshness footer, so the three can never word it differently."""
        if self.cadence == "hourly":
            return f"hourly at :{self.at.split(':')[1]}"
        if self.cadence == "weekly":
            return f"every {_WEEKDAY_NAMES[self.day].capitalize()} at {self.at}"
        if self.cadence == "weekdays":
            return f"every weekday at {self.at}"
        return f"daily at {self.at}"


# -- parsing helpers ---------------------------------------------------------


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _int(value, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def normalize_at(text: str) -> str:
    """Validate/normalise a local wall-clock "HH:MM"; raise :class:`ScheduleError` if bad."""
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ScheduleError(f"Time must look like HH:MM (got {text!r}).")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ScheduleError(f"Time must look like HH:MM (got {text!r}).") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ScheduleError(f"Time must be between 00:00 and 23:59 (got {text!r}).")
    return f"{hour:02d}:{minute:02d}"


def normalize_day(text: str) -> int:
    """Map a weekday name (or 0-6) to an index; raise :class:`ScheduleError` if unknown."""
    value = text.strip().lower()
    if value[:3] in _WEEKDAY_NAMES:
        return _WEEKDAY_NAMES.index(value[:3])
    try:
        index = int(value)
    except ValueError:
        raise ScheduleError(f"Unknown day {text!r} — use mon/tue/…/sun.") from None
    if not 0 <= index <= 6:
        raise ScheduleError(f"Day must be 0 (Mon) to 6 (Sun), got {index}.")
    return index


def normalize_cadence(text: str) -> str:
    value = text.strip().lower()
    if value not in CADENCES:
        raise ScheduleError(f"Unknown cadence {text!r} — use one of {', '.join(CADENCES)}.")
    return value


# -- storage -----------------------------------------------------------------


def schedules_file(workspace: Path | str) -> Path:
    return Path(workspace) / STATE_DIR / SCHEDULES_NAME


def load(workspace: Path | str) -> list[Schedule]:
    """Every stored schedule, ordered by notebook path. Best-effort: a missing, unreadable,
    or corrupt file yields an empty list rather than raising — a broken schedules file must
    degrade to "no schedules", never to a hub that won't start."""
    path = schedules_file(workspace)
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("schedules")
    if not isinstance(raw, list):
        return []
    out = [s for s in (Schedule.from_dict(entry) for entry in raw) if s is not None]
    # One schedule per notebook: a duplicated entry (hand-edited file) keeps the first.
    seen: set[str] = set()
    unique = []
    for sched in out:
        if sched.notebook in seen:
            continue
        seen.add(sched.notebook)
        unique.append(sched)
    return sorted(unique, key=lambda s: s.notebook)


def save(workspace: Path | str, schedules: list[Schedule]) -> None:
    """Write the whole schedule set atomically. Raises OSError if the write genuinely
    fails — unlike a receipt, losing a schedule the user just created must not be silent."""
    target = schedules_file(workspace)
    payload = {
        "version": FORMAT_VERSION,
        "schedules": [s.to_dict() for s in sorted(schedules, key=lambda s: s.notebook)],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_write_bytes(target, json.dumps(payload, indent=2).encode("utf-8"))


def get(workspace: Path | str, notebook: str) -> Schedule | None:
    want = notebook.replace("\\", "/")
    for sched in load(workspace):
        if sched.notebook == want:
            return sched
    return None


def put(workspace: Path | str, sched: Schedule) -> Schedule:
    """Insert or replace ``sched`` (keyed on its notebook) and persist. Returns it."""
    others = [s for s in load(workspace) if s.notebook != sched.notebook]
    save(workspace, [*others, sched])
    return sched


def remove(workspace: Path | str, notebook: str) -> bool:
    """Drop ``notebook``'s schedule. Returns whether one was there."""
    want = notebook.replace("\\", "/")
    current = load(workspace)
    remaining = [s for s in current if s.notebook != want]
    if len(remaining) == len(current):
        return False
    save(workspace, remaining)
    return True


def set_paused(workspace: Path | str, notebook: str, paused: bool) -> Schedule | None:
    """Pause/resume one schedule. Resuming also clears the consecutive-failure counter, so
    a schedule that auto-paused gets a full budget again rather than re-pausing on the next
    single failure."""
    sched = get(workspace, notebook)
    if sched is None:
        return None
    updated = replace(
        sched, paused=paused, consecutive_failures=0 if not paused else sched.consecutive_failures
    )
    return put(workspace, updated)


# -- the clock ---------------------------------------------------------------


def _now() -> datetime:
    """Timezone-AWARE local now. Aware throughout so a window start (local wall clock) can be
    compared directly against a stored ``ran_at`` (UTC) without a manual offset dance."""
    return datetime.now().astimezone()


def _at_time(moment: datetime, at: str) -> datetime:
    hour, minute = (int(p) for p in at.split(":"))
    return moment.replace(hour=hour, minute=minute, second=0, microsecond=0)


def window_start(sched: Schedule, now: datetime | None = None) -> datetime:
    """The beginning of the cadence window ``now`` falls in — the moment this schedule most
    recently became due. A run at or after this instant satisfies the current window."""
    now = now or _now()
    if sched.cadence == "hourly":
        minute = int(sched.at.split(":")[1])
        start = now.replace(minute=minute, second=0, microsecond=0)
        return start if start <= now else start - timedelta(hours=1)
    if sched.cadence == "weekly":
        start = _at_time(now, sched.at)
        delta = (now.weekday() - sched.day) % 7
        start -= timedelta(days=delta)
        return start if start <= now else start - timedelta(days=7)
    start = _at_time(now, sched.at)
    if start > now:
        start -= timedelta(days=1)
    if sched.cadence == "weekdays":
        # Walk back to the most recent Mon-Fri occurrence, so a Sunday morning open does not
        # think it missed a Saturday run that was never owed.
        while start.weekday() >= 5:
            start -= timedelta(days=1)
    return start


def next_due(sched: Schedule, now: datetime | None = None) -> datetime:
    """When this schedule is next owed a run, after the current window."""
    now = now or _now()
    start = window_start(sched, now)
    if sched.cadence == "hourly":
        return start + timedelta(hours=1)
    if sched.cadence == "weekly":
        return start + timedelta(days=7)
    nxt = start + timedelta(days=1)
    if sched.cadence == "weekdays":
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    return nxt


def _ran_at(sched: Schedule) -> datetime | None:
    if not sched.last_run.at:
        return None
    try:
        moment = datetime.fromisoformat(sched.last_run.at)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.astimezone()


def ran_this_window(sched: Schedule, now: datetime | None = None) -> bool:
    """Whether a run has already been ATTEMPTED inside the current window.

    Attempted, not succeeded: a failed run still consumes the window, so a notebook that is
    broken is retried on the next cadence tick rather than hammered on every hub open. The
    failure budget and the overdue banner are what surface it in the meantime."""
    moment = _ran_at(sched)
    return moment is not None and moment >= window_start(sched, now)


def is_due(sched: Schedule, now: datetime | None = None) -> bool:
    """Whether ``sched`` is owed a run right now. Paused schedules are never due.

    This is the catch-up test: it compares against the CURRENT window only, so a week of
    missed windows collapses into one run, never seven."""
    if sched.paused:
        return False
    return not ran_this_window(sched, now)


def is_overdue(sched: Schedule, now: datetime | None = None) -> bool:
    """Whether the grace period on the current window has expired without a successful run.

    This — not :func:`is_due` — is what turns the board amber and what the artifact footer's
    "next refresh due" clause lets a stakeholder check for themselves. A schedule can be due
    (it is 07:31 and the run has not started) without being overdue."""
    if sched.paused:
        return True  # a paused schedule is by definition not being kept fresh
    now = now or _now()
    deadline = window_start(sched, now) + timedelta(hours=sched.grace_hours)
    if now < deadline:
        return False
    moment = _ran_at(sched)
    if moment is None or moment < window_start(sched, now):
        return True
    return sched.last_run.outcome == FAILED


def due(schedules: list[Schedule], now: datetime | None = None) -> list[Schedule]:
    return [s for s in schedules if is_due(s, now)]


# -- recording ---------------------------------------------------------------


def record_run(
    workspace: Path | str,
    notebook: str,
    *,
    outcome: str,
    checks_failed: int = 0,
    inputs_changed: int = 0,
    conflicts: int = 0,
    reason: str = "",
    artifact: str = "",
    ran_at: str,
    budget: int | None = None,
) -> Schedule | None:
    """Record a run against ``notebook``'s schedule and apply the failure budget.

    Returns the updated schedule, or None when there is no schedule for that notebook (an
    ad-hoc ``mooring refresh <path>`` on an unscheduled notebook is legitimate and simply
    records nothing). ``budget`` overrides ``max_failures`` — the caller passes 1 for a
    notebook whose verification has lapsed, so a doubtful notebook pauses on its FIRST
    failure instead of its third.

    Only :data:`FAILED` spends the budget. A :data:`CHECKS_FAILED` run is a *data* problem,
    and auto-pausing on it would mute the very alarm the analyst needs to keep hearing.

    A successful run CLEARS an auto-pause. ``consecutive_failures > 0`` is what distinguishes
    a schedule the failure budget paused from one the user deliberately paused (the CLI and
    the board already read it that way), so a manual "Run now" that finally succeeds re-arms
    the former — you fixed it, it works again — while never overriding the latter."""
    sched = get(workspace, notebook)
    if sched is None:
        return None
    if outcome == FAILED:
        failures = sched.consecutive_failures + 1
    else:
        failures = 0
    limit = sched.max_failures if budget is None else max(1, budget)
    auto_paused = sched.paused and sched.consecutive_failures > 0
    still_paused = (sched.paused and not auto_paused) or failures >= limit
    updated = replace(
        sched,
        consecutive_failures=failures,
        paused=still_paused,
        last_run=LastRun(
            at=ran_at,
            outcome=outcome,
            checks_failed=checks_failed,
            inputs_changed=inputs_changed,
            conflicts=conflicts,
            reason=reason,
            artifact=artifact,
        ),
    )
    return put(workspace, updated)


def freshness_note(sched: Schedule, now: datetime | None = None) -> str:
    """The clause stamped into a delivered artifact's provenance footer.

    This is the mechanism that makes staleness travel WITH the output: a stakeholder holding
    the emailed HTML three weeks later can see it is overdue without access to mooring, the
    repo, or the analyst."""
    return f"scheduled {sched.describe_cadence()} · next refresh due {next_due(sched, now):%Y-%m-%d}"
