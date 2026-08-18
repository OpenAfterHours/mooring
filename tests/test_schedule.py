"""The schedule model: cadence arithmetic, the catch-up rule, and the failure budget.

These pin the three properties the design leans on: a stale refresh is always *detectable*
(is_overdue), a week away collapses into ONE run rather than seven backfills (is_due), and a
DATA failure never silences itself the way an infrastructure failure does (record_run's
budget). Plus the usual: the store is structurally unsyncable and every field is value-free.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from mooring import schedule, sync

# A Thursday 09:00 local, so weekday/weekend walks are unambiguous.
NOW = datetime(2026, 7, 30, 9, 0).astimezone()


def _sched(**kw) -> schedule.Schedule:
    return schedule.Schedule(notebook=kw.pop("notebook", "notebooks/board.py"), **kw)


def _ran(moment: datetime, outcome: str = schedule.OK) -> schedule.LastRun:
    return schedule.LastRun(at=moment.isoformat(timespec="seconds"), outcome=outcome)


# -- storage -----------------------------------------------------------------


def test_schedules_file_is_structurally_unsyncable():
    # A schedule names a local notebook and is per-machine; it must never ride a push,
    # even against a custom exclude list.
    assert sync.is_synced_path(".mooring/schedules.json") is False
    assert sync.is_synced_path(".mooring/schedules.json", exclude=("*.py",)) is False


def test_round_trips_and_orders_by_notebook(tmp_path):
    schedule.put(tmp_path, _sched(notebook="z.py"))
    schedule.put(tmp_path, _sched(notebook="a.py", cadence="weekly", at="18:05", day=4))
    loaded = schedule.load(tmp_path)
    assert [s.notebook for s in loaded] == ["a.py", "z.py"]
    assert loaded[0].cadence == "weekly" and loaded[0].at == "18:05" and loaded[0].day == 4


def test_stored_record_is_value_free(tmp_path):
    schedule.put(tmp_path, _sched())
    schedule.record_run(
        tmp_path, "notebooks/board.py", outcome=schedule.OK, ran_at="2026-07-30T07:30:00+00:00"
    )
    stored = json.loads((tmp_path / ".mooring" / "schedules.json").read_text("utf-8"))
    entry = stored["schedules"][0]
    # Settings, counters, booleans and curated strings — no data value anywhere.
    assert set(entry) == {
        "notebook", "cadence", "at", "day", "date", "deliver", "pull", "grace_hours",
        "max_failures", "paused", "consecutive_failures", "last_run",
    }
    assert set(entry["last_run"]) == {
        "at", "outcome", "checks_failed", "inputs_changed", "conflicts", "reason", "artifact",
    }
    assert all(not isinstance(v, str) or "SECRET" not in v for v in entry["last_run"].values())


def test_a_corrupt_store_degrades_to_no_schedules(tmp_path):
    # A hand-edited or truncated file must never stop the hub coming up.
    path = schedule.schedules_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert schedule.load(tmp_path) == []


def test_an_unusable_entry_is_skipped_not_fatal(tmp_path):
    path = schedule.schedules_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "schedules": [
                    {"notebook": "good.py", "cadence": "daily"},
                    {"cadence": "daily"},  # no notebook
                    {"notebook": "bad.py", "cadence": "fortnightly"},  # unknown cadence
                ],
            }
        ),
        encoding="utf-8",
    )
    assert [s.notebook for s in schedule.load(tmp_path)] == ["good.py"]


def test_remove_and_pause(tmp_path):
    schedule.put(tmp_path, _sched())
    assert schedule.set_paused(tmp_path, "notebooks/board.py", True).paused is True
    assert schedule.remove(tmp_path, "notebooks/board.py") is True
    assert schedule.remove(tmp_path, "notebooks/board.py") is False


def test_resume_clears_the_failure_counter(tmp_path):
    # An auto-paused schedule must get a FULL budget back on resume, or it re-pauses on the
    # very next single failure and the user can never recover it from the UI.
    schedule.put(tmp_path, _sched(consecutive_failures=3, paused=True))
    resumed = schedule.set_paused(tmp_path, "notebooks/board.py", False)
    assert resumed.paused is False
    assert resumed.consecutive_failures == 0


# -- cadence arithmetic ------------------------------------------------------


def test_daily_window_and_next_due():
    sched = _sched(cadence="daily", at="07:30")
    assert schedule.window_start(sched, NOW) == NOW.replace(hour=7, minute=30, second=0, microsecond=0)
    assert schedule.next_due(sched, NOW).day == 31


def test_daily_before_the_time_uses_yesterdays_window():
    sched = _sched(cadence="daily", at="07:30")
    early = NOW.replace(hour=6)
    assert schedule.window_start(sched, early).day == 29


def test_weekdays_skips_the_weekend_backwards_and_forwards():
    sched = _sched(cadence="weekdays", at="07:30")
    sunday = datetime(2026, 8, 2, 9, 0).astimezone()
    # The most recent weekday window is Friday — a Sunday open must not think it missed a
    # Saturday run that was never owed.
    assert schedule.window_start(sched, sunday).strftime("%a") == "Fri"
    assert schedule.next_due(sched, sunday).strftime("%a") == "Mon"


def test_weekly_uses_the_configured_day():
    sched = _sched(cadence="weekly", at="07:30", day=2)  # Wednesday
    start = schedule.window_start(sched, NOW)
    assert start.strftime("%a") == "Wed" and start.day == 29
    assert schedule.next_due(sched, NOW).day == 5  # the following Wednesday


def test_hourly_uses_the_minute_only():
    sched = _sched(cadence="hourly", at="07:15")
    assert schedule.window_start(sched, NOW).strftime("%H:%M") == "08:15"
    assert schedule.next_due(sched, NOW).strftime("%H:%M") == "09:15"


# -- the catch-up rule -------------------------------------------------------


def test_a_week_away_collapses_into_one_run_not_seven():
    # THE anti-backfill guard. is_due compares only against the CURRENT window, so opening
    # mooring after a week owes exactly one run.
    sched = _sched(cadence="daily", at="07:30", last_run=_ran(NOW - timedelta(days=7)))
    assert schedule.is_due(sched, NOW) is True
    # ...and once that single run lands, nothing more is owed until tomorrow.
    after = sched.__class__(**{**sched.__dict__, "last_run": _ran(NOW)})
    assert schedule.is_due(after, NOW) is False


def test_a_run_inside_the_window_satisfies_it():
    sched = _sched(cadence="daily", at="07:30", last_run=_ran(NOW.replace(hour=7, minute=45)))
    assert schedule.is_due(sched, NOW) is False


def test_a_failed_run_still_consumes_the_window():
    # A broken notebook is retried on the next cadence tick, not hammered on every hub open.
    # The overdue banner and the failure budget are what surface it in the meantime.
    sched = _sched(
        cadence="daily", at="07:30", last_run=_ran(NOW.replace(hour=8), schedule.FAILED)
    )
    assert schedule.is_due(sched, NOW) is False
    assert schedule.is_overdue(sched, NOW.replace(hour=12)) is True


def test_a_paused_schedule_is_never_due_but_is_always_overdue():
    # Paused means "not being kept fresh" — the board must say so rather than showing green.
    sched = _sched(paused=True, last_run=_ran(NOW))
    assert schedule.is_due(sched, NOW) is False
    assert schedule.is_overdue(sched, NOW) is True


# -- overdue (the freshness contract) ----------------------------------------


def test_due_but_within_grace_is_not_yet_overdue():
    sched = _sched(cadence="daily", at="07:30", grace_hours=4)
    assert schedule.is_due(sched, NOW) is True  # 09:00, no run yet
    assert schedule.is_overdue(sched, NOW) is False  # ...but the 11:30 deadline hasn't passed


def test_past_the_grace_period_with_no_run_is_overdue():
    sched = _sched(cadence="daily", at="07:30", grace_hours=4)
    assert schedule.is_overdue(sched, NOW.replace(hour=12)) is True


def test_a_successful_run_in_the_window_is_not_overdue():
    sched = _sched(cadence="daily", at="07:30", last_run=_ran(NOW.replace(hour=7, minute=31)))
    assert schedule.is_overdue(sched, NOW.replace(hour=23)) is False


# -- the failure budget ------------------------------------------------------


def test_repeated_failures_auto_pause(tmp_path):
    schedule.put(tmp_path, _sched(max_failures=3))
    for _ in range(2):
        updated = schedule.record_run(
            tmp_path, "notebooks/board.py", outcome=schedule.FAILED, ran_at="2026-07-30T07:30:00+00:00"
        )
        assert updated.paused is False
    updated = schedule.record_run(
        tmp_path, "notebooks/board.py", outcome=schedule.FAILED, ran_at="2026-07-30T07:30:00+00:00"
    )
    assert updated.consecutive_failures == 3 and updated.paused is True


def test_a_failing_tie_out_check_never_spends_the_budget(tmp_path):
    # THE distinction that makes the feature worth having: "the numbers stopped tying out" is
    # the signal the analyst most needs REPEATED. Auto-pausing on it would mute the alarm.
    schedule.put(tmp_path, _sched(max_failures=1))
    for _ in range(5):
        updated = schedule.record_run(
            tmp_path,
            "notebooks/board.py",
            outcome=schedule.CHECKS_FAILED,
            checks_failed=2,
            ran_at="2026-07-30T07:30:00+00:00",
        )
    assert updated.paused is False
    assert updated.consecutive_failures == 0
    assert updated.last_run.checks_failed == 2


def test_a_good_run_resets_the_counter(tmp_path):
    schedule.put(tmp_path, _sched(consecutive_failures=2))
    updated = schedule.record_run(
        tmp_path, "notebooks/board.py", outcome=schedule.OK, ran_at="2026-07-30T07:30:00+00:00"
    )
    assert updated.consecutive_failures == 0


def test_a_successful_manual_retry_clears_an_AUTO_pause(tmp_path):
    # You fixed the notebook and clicked Run now: an auto-pause must let go, or the user has
    # to find a second, non-obvious button before the schedule resumes on its own.
    schedule.put(tmp_path, _sched(paused=True, consecutive_failures=3))
    updated = schedule.record_run(
        tmp_path, "notebooks/board.py", outcome=schedule.OK, ran_at="2026-07-30T07:30:00+00:00"
    )
    assert updated.paused is False and updated.consecutive_failures == 0


def test_a_successful_run_never_overrides_a_DELIBERATE_pause(tmp_path):
    # A user-chosen pause carries no failure count, which is exactly what distinguishes it.
    # Running it by hand must not silently re-arm something the user switched off.
    schedule.put(tmp_path, _sched(paused=True, consecutive_failures=0))
    updated = schedule.record_run(
        tmp_path, "notebooks/board.py", outcome=schedule.OK, ran_at="2026-07-30T07:30:00+00:00"
    )
    assert updated.paused is True


def test_a_lapsed_verification_pauses_on_the_first_failure(tmp_path):
    # budget=1 is what app/refresh passes for a notebook edited since it last ran clean.
    schedule.put(tmp_path, _sched(max_failures=3))
    updated = schedule.record_run(
        tmp_path,
        "notebooks/board.py",
        outcome=schedule.FAILED,
        ran_at="2026-07-30T07:30:00+00:00",
        budget=1,
    )
    assert updated.paused is True


def test_recording_against_an_unscheduled_notebook_is_a_no_op(tmp_path):
    # `mooring refresh <path>` on a notebook with no schedule is legitimate.
    assert schedule.record_run(
        tmp_path, "ad/hoc.py", outcome=schedule.OK, ran_at="2026-07-30T07:30:00+00:00"
    ) is None


# -- parsing + wording -------------------------------------------------------


@pytest.mark.parametrize("bad", ["7:30pm", "25:00", "07:99", "0730", ""])
def test_bad_times_are_refused(bad):
    with pytest.raises(schedule.ScheduleError):
        schedule.normalize_at(bad)


def test_times_are_normalised():
    assert schedule.normalize_at("7:5") == "07:05"


def test_days_and_cadences():
    assert schedule.normalize_day("Wednesday") == 2
    assert schedule.normalize_day("4") == 4
    assert schedule.normalize_cadence("WEEKDAYS") == "weekdays"
    with pytest.raises(schedule.ScheduleError):
        schedule.normalize_cadence("fortnightly")
    with pytest.raises(schedule.ScheduleError):
        schedule.normalize_day("someday")


def test_dates_are_normalised():
    assert schedule.normalize_date(" 2026-8-2 ") == "2026-08-02"


@pytest.mark.parametrize("bad", ["", "   ", "20 Aug 2026", "2026-13-40", "2026/08/20", "tomorrow"])
def test_bad_dates_are_refused(bad):
    with pytest.raises(schedule.ScheduleError):
        schedule.normalize_date(bad)


@pytest.mark.parametrize("bad", ["1970-12-31", "0226-08-20", "3001-01-01", "9999-12-31"])
def test_a_date_this_machine_cannot_put_on_a_clock_is_refused(bad):
    # Localising a naive datetime raises OSError [Errno 22] on Windows outside the range,
    # so a mistyped year is not a schedule that fires late — it is one whose EVERY later
    # clock call raises. Refused at the boundary, with a message pointing at the year.
    with pytest.raises(schedule.ScheduleError) as exc:
        schedule.normalize_date(bad)
    assert "check the year" in str(exc.value)


def test_the_year_bounds_themselves_are_still_accepted():
    # A fixed range, not a runtime probe: the same answer on every platform and in CI.
    assert schedule.normalize_date(f"{schedule.MIN_YEAR}-01-01") == "1971-01-01"
    assert schedule.normalize_date(f"{schedule.MAX_YEAR}-12-31") == "3000-12-31"


def test_the_floor_year_is_one_the_clock_can_actually_PLACE():
    # THE property the floor exists for, and the reason it is not the epoch: 1970-01-01
    # cannot be localised in ANY zone (ahead of UTC it is below the epoch outright; at or
    # behind it, resolving the DST fold probes a day either side and that probe is). A floor
    # that admitted such a date would bless the schedule in normalize_date and then have
    # _once_start silently park it a century out — the "stored and forgotten" outcome the
    # check exists to prevent. So the floor has to survive the round trip, not just parse.
    floor = _once(f"{schedule.MIN_YEAR}-01-01", at="00:30")
    start = schedule.window_start(floor, NOW)
    assert start.strftime("%Y-%m-%d %H:%M") == f"{schedule.MIN_YEAR}-01-01 00:30"
    # ...i.e. NOT the "unplaceable, park it" fallback, which is what 1970 would have hit.
    assert start < NOW


def test_an_already_stored_out_of_range_date_self_heals_on_load():
    # No migration needed for an entry a previous version persisted: from_dict catches the
    # ScheduleError, blanks the date, and then refuses a dateless `once` outright — so the
    # unplaceable row leaves the board rather than taking it down.
    entry = {"notebook": "n.py", "cadence": "once", "date": "9999-12-31"}
    assert schedule.Schedule.from_dict(entry) is None
    # ...and a repeating cadence that merely carried a stray date is unharmed by the same rule.
    kept = schedule.Schedule.from_dict({**entry, "cadence": "daily"})
    assert kept is not None and kept.date == ""


def test_once_is_part_of_the_cadence_vocabulary():
    assert schedule.normalize_cadence("Once") == "once"
    assert "once" in schedule.CADENCES


def test_freshness_note_carries_the_cadence_and_the_next_due_date():
    # This clause is stamped into the delivered artifact, so a stakeholder holding the
    # emailed HTML can see it is overdue with no access to mooring at all.
    note = schedule.freshness_note(_sched(cadence="weekdays", at="07:30"), NOW)
    assert "scheduled every weekday at 07:30" in note
    assert "next refresh due 2026-07-31" in note


def test_the_footer_never_promises_a_refresh_a_one_shot_will_not_do():
    # The clause a stakeholder reads to decide whether the numbers are current, so it has to
    # be TRUE. A one-shot's "next" tick IS its own instant, which the run that produced this
    # artifact has just consumed — stamping "next refresh due" would date the pack to the day
    # it was made and read as stale from the day after.
    delivered = schedule.freshness_note(_once("2026-07-30", at="07:30"), NOW)
    assert "scheduled once on 2026-07-30 at 07:30" in delivered  # the shared cadence wording
    assert "next refresh due" not in delivered
    assert "one-off" in delivered and "will not refresh" in delivered
    # ...but a one-shot still AHEAD of its instant (delivered early by a manual run) really
    # does have a refresh coming, and keeps the clause the repeating cadences use.
    early = schedule.freshness_note(_once("2026-08-20", at="15:00"), NOW)
    assert "next refresh due 2026-08-20" in early


# -- the one-shot cadence ----------------------------------------------------
#
# A `once` is the same machinery aimed at a single fixed instant. The properties that make it
# safe are all here: it does not fire early, it fires exactly once, and when it is finished it
# says so rather than sitting on the board forever as "overdue".


def _once(date: str, at: str = "07:30", **kw) -> schedule.Schedule:
    return _sched(cadence="once", date=date, at=at, **kw)


def test_a_one_shot_lands_on_its_local_instant():
    sched = _once("2026-08-20", at="15:00")
    start = schedule.window_start(sched, NOW)
    assert start.strftime("%Y-%m-%d %H:%M") == "2026-08-20 15:00"
    assert start.tzinfo is not None  # aware, so it compares against a UTC last_run directly
    # There is nothing after a one-shot: "next due" is the instant itself, not an invented tick.
    assert schedule.next_due(sched, NOW) == start


def test_a_future_one_shot_is_not_due_before_its_instant():
    # THE guard: no run precedes a one-shot's window, so without the "now >= window_start"
    # test a job booked for next month would fire the moment it was created.
    sched = _once("2026-08-20", at="15:00")
    assert schedule.is_due(sched, NOW) is False
    assert schedule.is_overdue(sched, NOW) is False
    # ...right up to the minute before.
    almost = datetime(2026, 8, 20, 14, 59).astimezone()
    assert schedule.is_due(sched, almost) is False


def test_a_one_shot_is_due_at_and_after_its_instant():
    sched = _once("2026-08-20", at="15:00")
    assert schedule.is_due(sched, datetime(2026, 8, 20, 15, 0).astimezone()) is True
    assert schedule.is_due(sched, datetime(2026, 8, 21, 9, 0).astimezone()) is True


def test_a_past_dated_one_shot_is_due_immediately():
    # A back-dated one-shot is legal and means "catch up" — the same rule as any other
    # cadence whose window opened while nothing was awake.
    assert schedule.is_due(_once("2026-07-01"), NOW) is True


def test_a_one_shot_runs_once_and_never_twice():
    sched = _once("2026-07-01")
    assert schedule.is_due(sched, NOW) is True
    after = _once("2026-07-01", last_run=_ran(NOW))
    assert schedule.is_due(after, NOW) is False
    # ...and still not next month, which is what separates a one-shot from a daily.
    assert schedule.is_due(after, NOW + timedelta(days=30)) is False


def test_a_complete_one_shot_is_neither_due_nor_overdue():
    ran = _ran(datetime(2026, 7, 1, 8, 0).astimezone())
    done = _once("2026-07-01", grace_hours=4, last_run=ran)
    assert schedule.is_complete(done, NOW) is True
    assert schedule.is_due(done, NOW) is False
    assert schedule.is_overdue(done, NOW) is False  # weeks later, and still not "late"


def test_an_incomplete_one_shot_past_its_grace_is_overdue():
    # The freshness contract still applies BEFORE it runs: a one-shot that nothing was awake
    # to run must go amber rather than passing quietly.
    sched = _once("2026-07-30", at="00:30", grace_hours=4)  # deadline 04:30, NOW is 09:00
    assert schedule.is_complete(sched, NOW) is False
    assert schedule.is_due(sched, NOW) is True
    assert schedule.is_overdue(sched, NOW) is True


def test_is_complete_is_never_true_for_a_repeating_cadence():
    assert schedule.is_complete(_sched(cadence="daily", last_run=_ran(NOW)), NOW) is False
    assert schedule.is_complete(_once("2026-07-01"), NOW) is False  # never run


# -- "complete" is WINDOW-relative, and a failure is not a finish ------------
#
# The two rules that stop a one-shot advertising an instant it will never honour, and stop it
# going quiet on the one run that most needed the noise.


def test_a_re_dated_one_shot_is_not_complete_on_its_OLD_receipt():
    # Both add paths deliberately carry last_run across an amendment, so a re-dated one-shot
    # arrives holding a receipt that predates the instant it now claims. Under an "any receipt"
    # rule it was born complete: due=False, overdue=False, forever, with nothing saying so.
    ran = _ran(datetime(2026, 8, 18, 8, 0).astimezone())
    redated = _once("2026-12-31", at="09:00", grace_hours=4, last_run=ran)
    fires = datetime(2026, 12, 31, 9, 0).astimezone()
    assert schedule.is_complete(redated, fires) is False
    assert schedule.is_due(redated, fires) is True  # due AT its new instant...
    assert schedule.is_due(redated, fires - timedelta(minutes=1)) is False  # ...and not before
    # ...amber once its grace lapses, and the sweep picks it up.
    later = datetime(2027, 1, 2, 9, 0).astimezone()
    assert schedule.is_overdue(redated, later) is True
    assert [s.notebook for s in schedule.due([redated], later)] == ["notebooks/board.py"]
    # The history is KEPT — the old run just no longer counts as satisfying the new window.
    assert redated.last_run.at == ran.at and redated.last_run.outcome == schedule.OK


def test_a_daily_switched_to_once_is_not_complete_on_its_daily_history():
    # The other amendment shape. Yesterday's daily run is a real receipt, but it says nothing
    # about a one-shot booked for December.
    switched = _once("2026-12-31", at="09:00", last_run=_ran(datetime(2026, 7, 29, 7, 30).astimezone()))
    assert schedule.is_complete(switched, NOW) is False
    assert schedule.is_due(switched, NOW) is False  # not before its instant, either
    assert schedule.is_due(switched, datetime(2026, 12, 31, 9, 0).astimezone()) is True


def test_a_failed_one_shot_goes_overdue_exactly_like_any_other_cadence():
    # The asymmetry this closes: with the same failure, the same grace and the same clock, a
    # FAILED daily reported overdue while a FAILED once reported complete=True, overdue=False.
    # The banner, the rail count and the rail severity dot are all driven by `overdue`, so the
    # loudest alarms went quiet for precisely the run that most needed them.
    broke = _ran(datetime(2026, 7, 30, 0, 35).astimezone(), outcome=schedule.FAILED)
    once = _once("2026-07-30", at="00:30", grace_hours=4, last_run=broke)  # deadline 04:30
    daily = _sched(cadence="daily", at="00:30", grace_hours=4, last_run=broke)
    assert schedule.is_complete(once, NOW) is False
    assert schedule.is_overdue(once, NOW) is True
    assert schedule.is_overdue(daily, NOW) is True  # the behaviour it now matches
    # ...but NOT due: the failed run consumed the only window a one-shot will ever have, so it
    # must never auto-refire on every sweep. `overdue` is what says it out loud instead.
    assert schedule.is_due(once, NOW) is False
    # Inside the grace it is not late yet — same as any other cadence mid-window.
    assert schedule.is_overdue(once, datetime(2026, 7, 30, 3, 0).astimezone()) is False


def test_a_one_shot_the_failure_budget_auto_paused_still_reports_overdue():
    # It is no longer "complete", so it falls through to the paused branch — which is the
    # correct answer: a paused schedule is by definition not being kept fresh.
    broke = _ran(datetime(2026, 7, 30, 0, 35).astimezone(), outcome=schedule.FAILED)
    parked = _once(
        "2026-07-30", at="00:30", paused=True, consecutive_failures=1, last_run=broke
    )
    assert schedule.is_complete(parked, NOW) is False
    assert schedule.is_overdue(parked, NOW) is True
    assert schedule.is_due(parked, NOW) is False  # paused schedules are never due


def test_a_paused_one_shot_booked_for_the_FUTURE_is_not_late_yet():
    # "Paused means not being kept fresh" holds for a cadence whose window has already
    # opened. `once` is the first one whose window can be entirely in the future, and a job
    # booked for December is not late in July merely because it is paused — reporting it
    # overdue turns the board, the rail dot and `mooring doctor` red today for a run nothing
    # was owed yet.
    parked = _once("2026-12-31", at="09:00", paused=True)
    assert schedule.is_overdue(parked, NOW) is False
    assert schedule.is_due(parked, NOW) is False  # paused schedules are never due either
    # ...and the paused rule keeps its full force the moment its window DOES open.
    assert schedule.is_overdue(parked, datetime(2027, 1, 2, 9, 0).astimezone()) is True


def test_pausing_a_repeating_cadence_still_reports_overdue_immediately():
    # The deliberate half of the rule, unweakened: every repeating cadence's window is always
    # already open, so the new guard is a no-op there and a paused daily still goes amber.
    for cadence in ("hourly", "daily", "weekdays", "weekly"):
        sched = _sched(cadence=cadence, at="07:30", paused=True, last_run=_ran(NOW))
        assert schedule.is_overdue(sched, NOW) is True
    # ...as does a paused one-shot whose instant has passed — the case the rule is for.
    assert schedule.is_overdue(_once("2026-07-01", paused=True), NOW) is True


def test_a_one_shot_whose_tie_outs_failed_is_still_complete():
    # CHECKS_FAILED is a DATA problem, not an infrastructure one: the notebook ran, the
    # numbers merely stopped tying out. That is the board's red badge to carry, not the
    # clock's — the same line this module draws in the failure budget.
    ran = _ran(datetime(2026, 7, 1, 8, 0).astimezone(), outcome=schedule.CHECKS_FAILED)
    done = _once("2026-07-01", last_run=ran)
    assert schedule.is_complete(done, NOW) is True
    assert schedule.is_overdue(done, NOW) is False


# -- the clock is TOTAL ------------------------------------------------------


def test_the_clock_answers_for_a_hand_built_out_of_range_date():
    # normalize_date refuses these at the boundary now, but a hand-built Schedule skips it.
    # Every clock call must still ANSWER rather than raise: the hub board asks all four per
    # row, so one raising row would 500 every schedule route — and the hub's sweep runs under
    # contextlib.suppress(Exception), so it would SILENTLY stop background refresh for every
    # other schedule on the machine.
    bad = _once("9999-12-31")
    assert schedule.is_due(bad, NOW) is False
    assert schedule.is_overdue(bad, NOW) is False
    assert schedule.is_complete(bad, NOW) is False
    # Parked a century out rather than fired: an unplaceable date must never mean "run now".
    assert schedule.next_due(bad, NOW) > NOW + timedelta(days=36000)


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00", "not a timestamp", "lunchtime"])
def test_the_clock_answers_for_a_RECEIPT_it_cannot_place(stamp):
    # The same totality rule on the other input. A receipt mooring wrote is always UTC
    # ISO-8601, but a hand-edited (or half-written) one can be anything — and localising a
    # NAIVE stamp is the step that raises, so `_ran_at` has to catch what `_once_start`
    # catches. Note the answers hold whichever way the platform falls: where year 1 IS
    # localisable it simply predates every window, and where it is not the guard reports the
    # same thing. Either way the receipt reads as "no usable run" — the LOUD direction, so a
    # corrupt stamp can never quietly satisfy a window.
    receipt = schedule.LastRun(at=stamp, outcome=schedule.OK)
    bad = _sched(cadence="daily", at="07:30", last_run=receipt)
    assert schedule.ran_this_window(bad, NOW) is False
    assert schedule.is_due(bad, NOW) is True
    assert schedule.is_overdue(bad, NOW.replace(hour=12)) is True
    assert schedule.is_complete(_once("2026-07-01", last_run=receipt), NOW) is False


def test_one_unplaceable_schedule_never_takes_the_others_down():
    bad = _once("9999-12-31", notebook="bad.py")
    good = _sched(cadence="daily", at="07:30", notebook="good.py")
    assert [s.notebook for s in schedule.due([bad, good], NOW)] == ["good.py"]


def test_the_due_guard_leaves_repeating_cadences_alone():
    # window_start for a repeating cadence is always <= now, so the new guard is a no-op there.
    for cadence in ("hourly", "daily", "weekdays", "weekly"):
        assert schedule.is_due(_sched(cadence=cadence, at="07:30"), NOW) is True


def test_a_one_shot_round_trips_through_the_store(tmp_path):
    schedule.put(tmp_path, _once("2026-08-20", at="15:00"))
    loaded = schedule.load(tmp_path)[0]
    assert loaded.cadence == "once" and loaded.date == "2026-08-20" and loaded.at == "15:00"


@pytest.mark.parametrize(
    "entry",
    [
        {},  # no date at all
        {"date": ""},
        {"date": "20 Aug 2026"},
        {"date": "2026-13-40"},
        {"date": 20260820},  # not even a string
    ],
)
def test_a_once_without_a_usable_date_parses_to_None_rather_than_raising(entry):
    # The tolerance rule: an entry that cannot be placed on the clock drops out on its own,
    # it never takes the hub down with it.
    assert schedule.Schedule.from_dict({"notebook": "n.py", "cadence": "once", **entry}) is None


def test_a_broken_once_entry_does_not_hide_the_others(tmp_path):
    path = schedule.schedules_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "schedules": [
                    {"notebook": "good.py", "cadence": "once", "date": "2026-08-20"},
                    {"notebook": "bad.py", "cadence": "once"},  # no date
                ],
            }
        ),
        encoding="utf-8",
    )
    assert [s.notebook for s in schedule.load(tmp_path)] == ["good.py"]


def test_describe_cadence_for_a_one_shot():
    assert _once("2026-08-20", at="15:00").describe_cadence() == "once on 2026-08-20 at 15:00"


def test_the_next_due_wording_names_a_date_only_a_one_shot_needs():
    # A weekday alone is enough for every repeating cadence — its next tick is always inside
    # seven days, so "Fri 07:30" cannot be misread.
    assert schedule.describe_next_due(_sched(cadence="daily", at="07:30"), NOW) == "Fri 07:30"
    # A one-shot four months out is the case that breaks it: "Thu 09:00" reads as THIS
    # Thursday, which is the one thing the wording must not say.
    far = schedule.describe_next_due(_once("2026-12-31", at="09:00"), NOW)
    assert far == "Thu 31 Dec 2026 09:00"


def test_a_finished_one_shot_has_nothing_to_say_about_a_next_run():
    # next_due honestly answers "the instant itself" for a `once`; the WORDING layer is what
    # must not promise it, or a done job advertises a tick that is already behind it. Same
    # line schedule_fmt.nextDue draws for the board.
    done = _once("2026-07-01", last_run=_ran(datetime(2026, 7, 1, 8, 0).astimezone()))
    assert schedule.is_complete(done, NOW) is True
    assert schedule.describe_next_due(done, NOW) == ""


def test_the_failure_budget_still_applies_to_a_one_shot(tmp_path):
    schedule.put(tmp_path, _once("2026-07-30", max_failures=1))
    updated = schedule.record_run(
        tmp_path, "notebooks/board.py", outcome=schedule.FAILED, ran_at="2026-07-30T07:30:00+00:00"
    )
    assert updated.consecutive_failures == 1 and updated.paused is True


# -- the CLI -----------------------------------------------------------------


def test_the_cli_round_trips_a_one_shot(tmp_path, monkeypatch, capsys):
    from mooring import cli, gitsha, paths, verify

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(cli, "_tier_note", lambda alias="": "")  # no schtasks probe in a test
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "board.py").write_text("import marimo\n\napp = marimo.App()\n", encoding="utf-8")
    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")
    monkeypatch.setenv("MOORING_OWNER", "acme")
    monkeypatch.setenv("MOORING_REPO", "nbs")
    monkeypatch.setenv("MOORING_WORKSPACE", str(ws))
    monkeypatch.setenv("MOORING_TRUSTSTORE", "0")
    for var in ("MOORING_TOKEN", "MOORING_ACTIVE_REPO", "MOORING_BRANCH",
                "MOORING_GITHUB_HOST", "MOORING_FORCE_FROZEN"):
        monkeypatch.delenv(var, raising=False)
    verify.record(
        ws,
        "board.py",
        passed=True,
        sha=gitsha.local_blob_sha(ws / "board.py", "board.py"),
        cells_failed=None,
        ran_at="2026-07-30T06:00:00+00:00",
    )
    args = ["schedule", "add", "board.py", "--cadence", "once", "--at", "15:00"]
    assert cli.main([*args, "--date", "2026-08-20"]) == 0
    stored = schedule.get(ws, "board.py")
    assert stored.cadence == "once" and stored.date == "2026-08-20" and stored.at == "15:00"
    out = capsys.readouterr().out
    assert "once on 2026-08-20 at 15:00" in out
    # The confirmation names the DATE. "Next due Thu 15:00" would read as this Thursday for
    # a job four months out — the one thing the CLI must not imply about a one-shot.
    assert "Next due Thu 20 Aug 2026 15:00." in out
    # ...and a `once` with nothing to fire at is refused rather than stored and forgotten.
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert "needs a date" in str(exc.value)
    # ...as is a year this machine's clock cannot represent — a typo must be a clean non-zero
    # exit here, not a stored schedule that makes every later clock call raise.
    with pytest.raises(SystemExit) as exc:
        cli.main([*args, "--date", "9999-12-31"])
    assert "check the year" in str(exc.value)
    assert exc.value.code not in (0, None)
    assert schedule.get(ws, "board.py").date == "2026-08-20"  # the good one is untouched

    # `schedule list` must not tell the user to resume a one-shot that has already had its
    # run. `paused` is how a finished one-shot is STORED (record_run leaves it that way), so
    # a bare `any(s.paused ...)` fires on every one of them — sending the user after a
    # schedule the very next column has just called "done". The two must agree.
    schedule.record_run(
        ws, "board.py", outcome=schedule.OK, ran_at="2026-08-20T15:30:00+00:00"
    )
    schedule.set_paused(ws, "board.py", True)
    capsys.readouterr()
    assert cli.main(["schedule", "list"]) == 0
    listed = capsys.readouterr().out
    assert "done" in listed
    assert "resume" not in listed
    # ...while a genuinely paused REPEATING schedule still gets the tip, or the exclusion
    # would have silenced the case it exists for.
    schedule.put(ws, schedule.Schedule(notebook="board.py", cadence="daily", paused=True))
    assert cli.main(["schedule", "list"]) == 0
    assert "resume" in capsys.readouterr().out


def test_the_cli_state_column_speaks_the_same_once_vocabulary_as_the_board(monkeypatch):
    # The two adapters describe one schedule, so they must not word it two ways. `cfg` is
    # unused by this helper (the state is read entirely off the schedule), hence the None.
    from mooring import cli

    # `_schedule_state` takes no clock, so it reads schedule._now — which makes every
    # overdue-sensitive assertion below a wall-clock lottery unless it is pinned. NOW is a
    # Thursday 09:00, i.e. INSIDE the 4h grace on a 07:30 daily window: that is what makes
    # "not yet late" a fact here rather than a fact about the hour the suite happens to run.
    monkeypatch.setattr(schedule, "_now", lambda: NOW)

    # Never run and months out: the DATE, not a weekday that reads as this week.
    waiting = _once("2026-12-31", at="09:00")
    assert cli._schedule_state(None, waiting) == "never run — due Thu 31 Dec 2026 09:00"
    # Run and finished: "done", the same word (and the same precedence over paused/overdue)
    # the board's badge uses — a one-shot that has had its run has no future to be late for.
    done = _once("2026-07-01", last_run=_ran(datetime(2026, 7, 1, 8, 0).astimezone()))
    assert cli._schedule_state(None, done).startswith("ok   done — last 2026-07-01")
    assert "OVERDUE" not in cli._schedule_state(None, done)
    # ...and "done" never becomes the word that hides a run whose tie-outs failed.
    broke = _ran(datetime(2026, 7, 1, 8, 0).astimezone(), outcome=schedule.CHECKS_FAILED)
    assert cli._schedule_state(None, _once("2026-07-01", last_run=broke)).startswith("FAIL done")
    # A repeating cadence is untouched: a weekday and a time, and no "done".
    daily = _sched(cadence="daily", at="07:30")
    assert cli._schedule_state(None, daily) == "never run — due Fri 07:30"
    # ...and a never-run schedule that IS late says so. This is the half the CLI used to drop:
    # the never-run branch returned before the OVERDUE marker was ever appended, so a schedule
    # counted in the board's overdue tally still read as merely pending on its own row. Same
    # schedule, same clock, one hour past its 4h grace.
    late = NOW.replace(hour=12)
    monkeypatch.setattr(schedule, "_now", lambda: late)
    assert cli._schedule_state(None, daily) == "never run — due Fri 07:30 OVERDUE"
    # A one-shot still ahead of its instant is NOT late, however long it has sat there —
    # nothing can be overdue before it was ever owed.
    assert "OVERDUE" not in cli._schedule_state(None, waiting)
