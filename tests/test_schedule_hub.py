"""Hub wiring for scheduled refresh: the board, the preflight gate, and running one now.

The actual notebook run is faked (a real one spawns marimo); these pin the endpoint
contracts — most importantly that an UNVERIFIED notebook cannot be scheduled at all (the
409 that closes the "it never worked in the first place" support class), and that the board
is value-free.
"""

from __future__ import annotations

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
        "notebook", "cadence", "cadence_text", "at", "day", "deliver", "pull", "paused",
        "verified", "due", "overdue", "next_due", "consecutive_failures", "last_run", "auto",
    }


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
