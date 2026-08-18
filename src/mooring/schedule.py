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
#
# ``once`` is the odd one out and deliberately so: a one-shot ("run the year-end pack on the
# 20th") is the same machinery — pull, run, receipts, an artifact with a provenance footer —
# aimed at a single fixed instant instead of a repeating window. It carries a ``date``, its
# window never moves, and once it has run CLEAN it is COMPLETE (see :func:`is_complete`)
# rather than waiting for a next tick that will never come. Clean is the operative word: a
# one-shot whose run FAILED is not finished, it is broken, and it goes amber like anything else.
CADENCES = ("hourly", "daily", "weekdays", "weekly", "once")

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
    date: str = ""  # "once" only: the local calendar date "YYYY-MM-DD" it fires on
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
            "date": self.date,
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
        raw_date = _str(data.get("date"))
        try:
            date = normalize_date(raw_date) if raw_date else ""
        except ScheduleError:
            date = ""
        if cadence == "once" and not date:
            # A one-shot with no usable date cannot be placed on the clock at all: there is no
            # window to be due in and no next tick to fall back to. Same tolerance rule as an
            # unknown cadence — drop this ONE schedule, never crash.
            return None
        return cls(
            notebook=notebook,
            cadence=cadence,
            at=at,
            day=min(6, max(0, _int(data.get("day")))),
            date=date,
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
        if self.cadence == "once":
            return f"once on {self.date} at {self.at}"
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


# The years this module is willing to put on the clock. Localising a naive datetime (see
# :func:`_once_start`) goes through the C runtime's local-time conversion, and on WINDOWS —
# the primary platform — that raises ``OSError [Errno 22]`` outside roughly 1970..3000. So a
# mistyped year is not a schedule that fires late, it is a schedule whose every clock call
# raises: the board, the sweep, ``schedule list`` and ``doctor`` all go through the same
# arithmetic. Refusing it at the boundary turns a typo into one actionable error message.
#
# The floor is NOT the epoch, and the difference is the whole point: 1970-01-01 cannot be
# localised in ANY zone. Ahead of UTC it is below the epoch outright (Britain spent 1970 on
# permanent BST); at or behind UTC the conversion still fails, because resolving the DST fold
# probes a day either side of the instant and that probe lands below the epoch. 1971 is the
# first January every zone can place, so that is the floor. A bound that admitted a date the
# clock cannot place would defeat its own purpose — blessing the schedule here and then
# silently parking it a century out in :func:`_once_start`, which is precisely the "stored and
# forgotten" outcome this check exists to turn into one actionable error message.
#
# The ceiling carries the mirror-image exposure for a zone behind UTC (3000-12-31 23:00 local
# is 3001 in UTC), but it is left at 3000: unlike the floor it is not reachable by a plausible
# typo, and _once_start's guard already degrades it to "parked", never to a raise.
#
# Deliberately a FIXED range rather than a runtime probe: the refusal has to be identical on
# every platform and reproducible in CI, not "whatever this machine's libc happens to accept".
#
# This also self-heals an entry a previous version already persisted, for free:
# :meth:`Schedule.from_dict` catches :class:`ScheduleError`, blanks the date, and then drops a
# dateless ``once`` outright — so the bad row leaves the board instead of taking it down.
MIN_YEAR = 1971
MAX_YEAR = 3000


def normalize_date(text: str) -> str:
    """Validate/normalise a local calendar date "YYYY-MM-DD"; raise :class:`ScheduleError`
    if bad. Only the ``once`` cadence uses one, and for that cadence it is REQUIRED — a
    one-shot with no date has no instant to fire at, so a blank is refused here rather than
    quietly becoming a schedule that never runs. A year outside
    :data:`MIN_YEAR`..:data:`MAX_YEAR` is refused for the same reason: it cannot be placed on
    this machine's clock at all (see those constants)."""
    value = text.strip()
    if not value:
        raise ScheduleError("A one-off schedule needs a date (YYYY-MM-DD).")
    try:
        moment = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ScheduleError(f"Date must look like YYYY-MM-DD (got {text!r}).") from None
    if not MIN_YEAR <= moment.year <= MAX_YEAR:
        raise ScheduleError(
            f"Date must be between {MIN_YEAR}-01-01 and {MAX_YEAR}-12-31 (got {text!r}) — "
            "check the year."
        )
    return moment.strftime("%Y-%m-%d")


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


def _once_start(sched: Schedule, now: datetime) -> datetime:
    """The single instant a one-shot is owed, as a timezone-aware LOCAL datetime.

    Built from the naive date+time and then localised, so the offset is the one in force ON
    THAT DATE rather than today's — a one-shot booked across a DST boundary still fires at the
    wall-clock time the user typed.

    :meth:`Schedule.from_dict` already drops a stored ``once`` whose date is missing or
    unparseable, and :func:`normalize_date` refuses a year this machine cannot localise, so
    the fallback here only guards a hand-built Schedule: it places the instant a century out,
    i.e. the schedule never becomes due, rather than firing an unattended run nobody asked for.

    The guard catches OSError/OverflowError as well as ValueError because the localisation
    step is the part that can fail: on Windows ``astimezone()`` raises ``OSError [Errno 22]``
    for a year outside roughly 1970..3000 (see :data:`MIN_YEAR`). This clock must be TOTAL —
    every route, the sweep and the CLI call it per row, so one unrepresentable date must never
    be able to raise out of here and take the whole board (or every other schedule) with it."""
    try:
        year, month, day = (int(p) for p in sched.date.split("-"))
        hour, minute = (int(p) for p in sched.at.split(":"))
        return datetime(year, month, day, hour, minute).astimezone()
    except (ValueError, OSError, OverflowError):
        return now + timedelta(days=36500)


def window_start(sched: Schedule, now: datetime | None = None) -> datetime:
    """The beginning of the cadence window ``now`` falls in — the moment this schedule most
    recently became due. A run at or after this instant satisfies the current window.

    For ``once`` the window has a fixed start and no end: the schedule became due at its
    instant and stays in that one window forever after."""
    now = now or _now()
    if sched.cadence == "once":
        return _once_start(sched, now)
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
    """When this schedule is next owed a run, after the current window.

    For ``once`` that IS the window start — there is nothing after a one-shot, so the honest
    answer is the single instant itself rather than an invented next tick."""
    now = now or _now()
    start = window_start(sched, now)
    if sched.cadence == "once":
        return start
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
    """The receipt's instant as an aware datetime, or None when there isn't a usable one.

    Same totality rule as :func:`_once_start`, and for the same reason: this is called per
    row by every clock predicate, so one corrupt receipt must never raise out and take the
    board (or the sweep, or ``schedule list``) down with it. A receipt mooring wrote is
    always UTC ISO-8601, but a hand-edited one can be anything — and localising a NAIVE
    stamp is the step that can fail, on Windows with ``OSError [Errno 22]`` outside roughly
    1970..3000 (see :data:`MIN_YEAR`), so the same three exceptions are caught here.

    None means "no usable run", which is the loud direction: the schedule reads as due and
    goes overdue on its grace, rather than a broken receipt quietly satisfying a window."""
    if not sched.last_run.at:
        return None
    try:
        moment = datetime.fromisoformat(sched.last_run.at)
        return moment if moment.tzinfo is not None else moment.astimezone()
    except (ValueError, OSError, OverflowError):
        return None


def ran_this_window(sched: Schedule, now: datetime | None = None) -> bool:
    """Whether a run has already been ATTEMPTED inside the current window.

    Attempted, not succeeded: a failed run still consumes the window, so a notebook that is
    broken is retried on the next cadence tick rather than hammered on every hub open. The
    failure budget and the overdue banner are what surface it in the meantime."""
    moment = _ran_at(sched)
    return moment is not None and moment >= window_start(sched, now)


def is_complete(sched: Schedule, now: datetime | None = None) -> bool:
    """Whether a one-shot has already had its run and is finished for good.

    DERIVED, never stored, so it cannot drift out of step with the history the way a persisted
    ``done`` flag would. Two clauses beyond "this is a ``once``", and both are load-bearing:

    * **WINDOW-RELATIVE, not "any receipt".** Both add paths deliberately carry the old
      ``last_run`` across an amendment, so a re-dated one-shot — or a daily switched to
      ``once`` — arrives here holding a receipt that predates the instant it now claims.
      Asking :func:`ran_this_window` instead of "is there any receipt at all" is what stops
      such a schedule advertising an instant it would never honour: its old run is outside its
      new window, so it becomes due at that instant, goes amber after its grace, and the sweep
      picks it up.
    * **A FAILED run is not a finished one.** ``complete`` suppresses both alarms below, and a
      one-shot whose only run failed is exactly the case that needs them: the notebook did not
      run, and there is no next tick coming to try again. Excluding :data:`FAILED` here is what
      makes a one-shot behave like every other cadence in that state — a failed daily reports
      overdue, and so must this — rather than going quiet for the run that most needed the
      noise. (:data:`CHECKS_FAILED` is NOT excluded: the notebook ran, the numbers merely
      stopped tying out, and that is the board's red badge to carry, not the clock's.)

    Always False for a repeating cadence."""
    return (
        sched.cadence == "once"
        and ran_this_window(sched, now)
        and sched.last_run.outcome != FAILED
    )


def is_due(sched: Schedule, now: datetime | None = None) -> bool:
    """Whether ``sched`` is owed a run right now. Paused schedules are never due.

    This is the catch-up test: it compares against the CURRENT window only, so a week of
    missed windows collapses into one run, never seven.

    There is deliberately no :func:`is_complete` test here: it would be redundant (complete
    implies ``ran_this_window``, which the last line already refuses on) and two predicates
    that must agree are two predicates that can drift. The redundancy is why a FAILED one-shot
    stays NOT due under the rule above — its failed run consumed the only window it will ever
    have, so it must not auto-refire; :func:`is_overdue` is what says it out loud instead."""
    if sched.paused:
        return False
    now = now or _now()
    # Nothing is due BEFORE its window opens. Load-bearing for ``once``: a one-shot dated next
    # month has no run preceding its window, so without this it would read as due immediately
    # and fire today. Free for the repeating cadences — their window_start is always <= now.
    if now < window_start(sched, now):
        return False
    return not ran_this_window(sched, now)


def is_overdue(sched: Schedule, now: datetime | None = None) -> bool:
    """Whether the grace period on the current window has expired without a successful run.

    This — not :func:`is_due` — is what turns the board amber and what the artifact footer's
    "next refresh due" clause lets a stakeholder check for themselves. A schedule can be due
    (it is 07:31 and the run has not started) without being overdue."""
    # Resolved BEFORE the is_complete call so an injected clock stays authoritative all the way
    # down: is_complete asks ran_this_window, which places the window itself.
    now = now or _now()
    if is_complete(sched, now):
        return False  # a one-shot that ran clean in its own window is finished, not late
    start = window_start(sched, now)
    # Nothing can be LATE before it was ever owed — the mirror of the same guard in
    # :func:`is_due`, and it has to sit ABOVE the paused test rather than below it. ``once``
    # is the first cadence whose window can be entirely in the future, so without this a
    # one-shot booked for next month and paused today turns the board, the rail dot and
    # ``mooring doctor`` red immediately, for a run that is not owed for weeks. Free for the
    # repeating cadences — their window_start is always <= now — so the paused rule below
    # keeps its full force for every schedule whose window has actually opened.
    if now < start:
        return False
    if sched.paused:
        # A paused schedule is by definition not being kept fresh. Note where a one-shot the
        # FAILURE BUDGET auto-paused now lands: it is no longer complete (its run failed), so
        # it falls through to here and reports overdue — which is the correct answer, and the
        # one the banner, the rail count and the severity dot all read.
        return True
    deadline = start + timedelta(hours=sched.grace_hours)
    if now < deadline:
        return False
    moment = _ran_at(sched)
    if moment is None or moment < start:
        return True
    return sched.last_run.outcome == FAILED


def due(schedules: list[Schedule], now: datetime | None = None) -> list[Schedule]:
    return [s for s in schedules if is_due(s, now)]


def describe_next_due(sched: Schedule, now: datetime | None = None) -> str:
    """When the next run is owed, worded for a human — or "" when none is coming.

    The companion to :meth:`Schedule.describe_cadence`, and here for the same reason: the
    CLI and the hub must not be able to describe one schedule two ways. Two rules, both of
    which ``once`` is what forced:

    * **A finished one-shot says nothing**, rather than repeating its own instant as though
      it were a future tick. (:func:`next_due` honestly answers "the instant itself" for a
      ``once``; it is this layer's job not to *promise* it. ``schedule_fmt.nextDue`` draws
      the same line for the board.)
    * **A one-shot names its DATE.** A weekday alone is enough for every repeating cadence,
      whose next tick is always inside seven days — but "Thu 15:00" for a job four months
      out reads as *this* Thursday, which is the one thing it must not say."""
    now = now or _now()
    if is_complete(sched, now):
        return ""
    moment = next_due(sched, now)
    if sched.cadence == "once":
        return f"{moment:%a %d %b %Y %H:%M}"
    return f"{moment:%a %H:%M}"


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
    repo, or the analyst. Which is exactly why the clause has to be TRUE: it is the sentence
    they read to decide whether the numbers in front of them are current.

    One shape, one date arithmetic, one branch — and only ``once`` can ever take it. Its
    "next" tick IS its own single instant (:func:`next_due` says so honestly), so for the
    run that fulfils it that instant is already behind us: stamping "next refresh due" on a
    delivered one-off would date it to the day it was made and read as stale from the day
    after. It says what is true instead — there is no refresh coming. A one-shot still ahead
    of its instant (delivered early by a manual run) genuinely does have one coming, and
    keeps the shared clause. ``schedule_fmt.nextDue`` draws the same line on the board."""
    now = now or _now()
    upcoming = next_due(sched, now)
    if sched.cadence == "once" and upcoming <= now:
        return f"scheduled {sched.describe_cadence()} · a one-off — these numbers will not refresh"
    return f"scheduled {sched.describe_cadence()} · next refresh due {upcoming:%Y-%m-%d}"
