"""Hub wiring for scheduled refresh: the board, the preflight gate, and running one now.

The actual notebook run is faked (a real one spawns marimo); these pin the endpoint
contracts — most importantly that an UNVERIFIED notebook cannot be scheduled at all (the
409 that closes the "it never worked in the first place" support class), and that the board
is value-free.
"""

from __future__ import annotations

import json
from datetime import datetime

from starlette.testclient import TestClient

from mooring import config, gitsha, paths, schedule, verify
from mooring.app import refresh
from mooring.hub.server import Hub, create_app

NOTEBOOK = "import marimo\n\napp = marimo.App()\n"
REL = "board.py"


def _hub(tmp_path, monkeypatch, *, verified=True):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / REL).write_text(NOTEBOOK, encoding="utf-8")
    if verified:
        verify.record(
            ws,
            REL,
            passed=True,
            sha=gitsha.local_blob_sha(ws / REL, REL),
            cells_failed=None,
            ran_at="2026-07-30T06:00:00+00:00",
        )
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(ws))
    return Hub(config.AppConfig(repos=(spec,), active_alias="ws")), ws


def _freeze(monkeypatch, moment: datetime) -> None:
    """Pin the schedule module's clock for a test that asserts due / overdue / complete.

    The board is a ROUTE: it computes those per row with no injected clock, so a test that
    books a schedule for a fixed date is only telling the truth while that date is still in
    the future. Left unfrozen such a test passes today and then fails FOREVER on the day it
    names — a dated CI break, and one nobody can reproduce by re-running it. Freezing here
    is what makes "booked for the future" a property of the test rather than of the calendar
    the suite happens to run on. (Everything the routes ask goes through this one function:
    is_due / is_overdue / is_complete / next_due all default their ``now`` to it.)"""
    monkeypatch.setattr(schedule, "_now", lambda: moment)


def test_the_board_is_empty_until_something_is_scheduled(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    with TestClient(create_app(hub)) as client:
        body = client.get("/api/schedules").json()
    assert body["schedules"] == [] and body["overdue"] == 0 and body["due"] == 0
    # The board always states WHICH clock is running — the tier is the freshness guarantee,
    # so it is never left to assumption.
    assert set(body["background"]) == {"tier", "tier_text", "offer", "reason"}


def test_scheduling_an_unverified_notebook_is_refused(tmp_path, monkeypatch):
    # THE preflight gate. Editing a scheduled notebook lapses its verification for free
    # (verify receipts are SHA-keyed), so this is also what stops a broken notebook being
    # re-scheduled after an edit.
    hub, ws = _hub(tmp_path, monkeypatch, verified=False)
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/schedule/add", json={"path": REL, "cadence": "daily"})
    assert resp.status_code == 409
    assert "Verify this notebook first" in resp.json()["error"]
    assert schedule.load(ws) == []


def test_add_then_list_round_trips(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    with TestClient(create_app(hub)) as client:
        added = client.post(
            "/api/schedule/add",
            json={"path": REL, "cadence": "weekly", "at": "18:05", "day": "thu", "deliver": False},
        )
        assert added.status_code == 200
        body = client.get("/api/schedules").json()
    row = body["schedules"][0]
    assert row["notebook"] == REL
    assert row["cadence"] == "weekly" and row["at"] == "18:05" and row["day"] == 3
    assert row["deliver"] is False
    assert row["cadence_text"] == "every Thu at 18:05"
    assert row["verified"] is True and row["auto"] is True


def test_the_board_is_value_free(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    schedule.put(ws, schedule.Schedule(notebook=REL))
    with TestClient(create_app(hub)) as client:
        row = client.get("/api/schedules").json()["schedules"][0]
    # Settings, booleans, counts, timestamps and curated strings — nothing else.
    assert set(row) == {
        "notebook", "cadence", "cadence_text", "at", "day", "date", "deliver", "pull",
        "paused", "verified", "due", "overdue", "complete", "next_due",
        "consecutive_failures", "last_run", "auto",
    }


def test_a_one_shot_round_trips_with_its_date(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    # 15:00 on the 20th is only "the future" from before it; frozen, it always is.
    _freeze(monkeypatch, datetime(2026, 8, 19, 9, 0).astimezone())
    with TestClient(create_app(hub)) as client:
        added = client.post(
            "/api/schedule/add",
            json={"path": REL, "cadence": "once", "date": "2026-08-20", "at": "15:00"},
        )
        assert added.status_code == 200
        row = client.get("/api/schedules").json()["schedules"][0]
    assert row["cadence"] == "once" and row["date"] == "2026-08-20" and row["at"] == "15:00"
    assert row["cadence_text"] == "once on 2026-08-20 at 15:00"
    # Booked for the future: not due yet, not late, and not finished.
    assert row["due"] is False and row["overdue"] is False and row["complete"] is False
    assert row["next_due"].startswith("2026-08-20T15:00")


def test_a_run_one_shot_reads_as_complete(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    schedule.put(ws, schedule.Schedule(notebook=REL, cadence="once", date="2026-07-01"))
    # "Complete" is WINDOW-relative, so the receipt has to land after the one-shot's instant
    # (2026-07-01 07:30 LOCAL) in whichever zone the test machine is in — 20:00Z clears it
    # everywhere from UTC-12 to UTC+14, which keeps this assertion off the timezone lottery.
    schedule.record_run(ws, REL, outcome=schedule.OK, ran_at="2026-07-01T20:00:00+00:00")
    with TestClient(create_app(hub)) as client:
        row = client.get("/api/schedules").json()["schedules"][0]
    # Done, not "overdue by three weeks" — a one-shot has no next window to be late for.
    assert row["complete"] is True and row["due"] is False and row["overdue"] is False


def test_a_paused_one_shot_booked_for_the_future_is_not_flagged_overdue(tmp_path, monkeypatch):
    # The board's `overdue` count is what drives the banner, the rail's severity dot and
    # `mooring doctor`. "Paused means not being kept fresh" is right for a cadence whose
    # window has opened — but a one-shot booked for December is not late in August, and
    # flagging it turns the whole hub red for a run nothing is owed yet.
    hub, ws = _hub(tmp_path, monkeypatch)
    _freeze(monkeypatch, datetime(2026, 8, 19, 9, 0).astimezone())
    sched = schedule.Schedule(notebook=REL, cadence="once", date="2026-12-31", paused=True)
    schedule.put(ws, sched)
    with TestClient(create_app(hub)) as client:
        body = client.get("/api/schedules").json()
    assert body["overdue"] == 0 and body["schedules"][0]["overdue"] is False
    assert body["due"] == 0 and body["schedules"][0]["paused"] is True


def test_a_once_without_a_valid_date_is_a_400(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    with TestClient(create_app(hub)) as client:
        missing = client.post("/api/schedule/add", json={"path": REL, "cadence": "once"})
        malformed = client.post(
            "/api/schedule/add", json={"path": REL, "cadence": "once", "date": "20 Aug 2026"}
        )
    assert missing.status_code == 400 and "needs a date" in missing.json()["error"]
    assert malformed.status_code == 400 and "YYYY-MM-DD" in malformed.json()["error"]
    assert schedule.load(ws) == []


def test_a_once_dated_outside_the_clock_range_is_a_400(tmp_path, monkeypatch):
    # A mistyped year in the date picker must bounce here rather than persist: on Windows a
    # date outside ~1970..3000 cannot be converted to local time at all, so a stored one makes
    # every later clock call raise — 500ing this route and silently killing the sweep.
    hub, ws = _hub(tmp_path, monkeypatch)
    with TestClient(create_app(hub)) as client:
        resp = client.post(
            "/api/schedule/add", json={"path": REL, "cadence": "once", "date": "9999-12-31"}
        )
    assert resp.status_code == 400 and "check the year" in resp.json()["error"]
    assert schedule.load(ws) == []


def test_a_hand_written_out_of_range_entry_never_takes_the_board_down(tmp_path, monkeypatch):
    # Defence in depth for a file a previous version already wrote (or a hand edit). The board
    # asks is_due/is_overdue/next_due PER ROW, so one raising row would 500 the whole route.
    hub, ws = _hub(tmp_path, monkeypatch)
    path = schedule.schedules_file(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "schedules": [
                    {"notebook": "bad.py", "cadence": "once", "date": "9999-12-31"},
                    {"notebook": REL, "cadence": "daily", "at": "07:30"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with TestClient(create_app(hub)) as client:
        resp = client.get("/api/schedules")
    assert resp.status_code == 200
    # The unplaceable row drops out on its own; the good one is served untouched.
    assert [r["notebook"] for r in resp.json()["schedules"]] == [REL]


def test_an_unverified_notebook_cannot_be_scheduled_once_either(tmp_path, monkeypatch):
    # The verify-first gate is unchanged by the new cadence: it still fires FIRST, so the
    # 409 the UI chains "Verify & schedule…" onto is reachable for a one-shot too.
    hub, ws = _hub(tmp_path, monkeypatch, verified=False)
    with TestClient(create_app(hub)) as client:
        resp = client.post(
            "/api/schedule/add",
            json={"path": REL, "cadence": "once", "date": "2026-08-20"},
        )
    assert resp.status_code == 409 and "Verify this notebook first" in resp.json()["error"]


def test_a_bad_time_is_a_400(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    with TestClient(create_app(hub)) as client:
        out_of_range = client.post("/api/schedule/add", json={"path": REL, "at": "25:99"})
        malformed = client.post("/api/schedule/add", json={"path": REL, "at": "half seven"})
    assert out_of_range.status_code == 400
    assert "between 00:00 and 23:59" in out_of_range.json()["error"]
    assert malformed.status_code == 400
    assert "HH:MM" in malformed.json()["error"]


def test_pause_resume_and_remove(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    schedule.put(ws, schedule.Schedule(notebook=REL))
    with TestClient(create_app(hub)) as client:
        paused = client.post("/api/schedule/pause", json={"path": REL, "paused": True}).json()
        assert paused["schedules"][0]["paused"] is True
        # A paused schedule never fires by itself, and the board says so.
        assert paused["schedules"][0]["auto"] is False
        client.post("/api/schedule/pause", json={"path": REL, "paused": False})
        removed = client.post("/api/schedule/remove", json={"path": REL})
        assert removed.status_code == 200 and removed.json()["schedules"] == []
        assert client.post("/api/schedule/remove", json={"path": REL}).status_code == 404


def test_run_now_reports_the_outcome_and_echoes_the_board(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    schedule.put(ws, schedule.Schedule(notebook=REL, deliver=False, pull=False))

    def _fake(cfg, rel, *, sched=None, pull=None, do_deliver=None):
        return refresh.RefreshResult(
            notebook=rel, outcome=schedule.CHECKS_FAILED, ran=True,
            checks_failed=2, checks_total=5, reason="2 of 5 tie-out check(s) failing",
        )

    monkeypatch.setattr(refresh, "refresh_notebook", _fake)
    with TestClient(create_app(hub)) as client:
        body = client.post("/api/refresh", json={"path": REL}).json()
    assert body["ok"] is False
    assert body["outcome"] == schedule.CHECKS_FAILED
    assert any("tie-out check" in line for line in body["lines"])
    assert body["schedules"][0]["notebook"] == REL  # the board rides the response


def test_run_due_with_no_path_sweeps(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    monkeypatch.setattr(refresh, "run_due", lambda cfg: [])
    with TestClient(create_app(hub)) as client:
        body = client.post("/api/refresh", json={}).json()
    assert body["lines"] == ["Nothing due."]


def test_refusing_a_non_notebook_is_a_409(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "helpers.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    with TestClient(create_app(hub)) as client:
        assert client.post("/api/refresh", json={"path": "helpers.py"}).status_code == 409
        # ...and it can't be scheduled in the first place.
        assert client.post("/api/schedule/add", json={"path": "helpers.py"}).status_code == 400


def test_creating_the_app_never_starts_the_sweep(tmp_path, monkeypatch):
    # The background clock is started by run_hub ONLY. If create_app started it, the test
    # suite (and any embedded use) would execute notebooks — the same posture as the
    # editor pre-warm.
    import threading

    hub, ws = _hub(tmp_path, monkeypatch)
    schedule.put(ws, schedule.Schedule(notebook=REL, cadence="daily", at="00:01"))
    with TestClient(create_app(hub)):
        pass
    assert not any(t.name == "refresh-sweep" for t in threading.enumerate())
