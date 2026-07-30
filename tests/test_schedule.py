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
        "notebook", "cadence", "at", "day", "deliver", "pull", "grace_hours",
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


def test_freshness_note_carries_the_cadence_and_the_next_due_date():
    # This clause is stamped into the delivered artifact, so a stakeholder holding the
    # emailed HTML can see it is overdue with no access to mooring at all.
    note = schedule.freshness_note(_sched(cadence="weekdays", at="07:30"), NOW)
    assert "scheduled every weekday at 07:30" in note
    assert "next refresh due 2026-07-31" in note
