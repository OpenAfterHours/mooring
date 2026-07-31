"""The hub's parameterised-run endpoints: start, poll, cancel.

The route layer is thin on purpose (the orchestration is pinned in ``test_param_runs.py``),
so what these tests hold down is the ADAPTER's obligations: a refusal answers a real status
code rather than minting a run that dies, a second start is refused instead of racing the
workspace lock, and the snapshot the page renders from carries counts and curated reasons
only — never marimo's stderr.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mooring import config, params, paths
from mooring.app import notebook_run
from mooring.hub.server import Hub, create_app

REL = "notebooks/board.py"
NOTEBOOK = (
    "import marimo\n\n"
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    import mooring_params\n"
    '    region = mooring_params.get("region", "EMEA")\n'
    "    return\n"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    ws = tmp_path / "ws"
    (ws / REL).parent.mkdir(parents=True, exist_ok=True)
    (ws / REL).write_text(NOTEBOOK, encoding="utf-8")
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(ws))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws"))
    with TestClient(create_app(hub)) as tc:
        yield tc, hub, ws


def _out_of(cmd):
    for i, tok in enumerate(cmd):
        if tok == "-o":
            return Path(cmd[i + 1])
    return None


def _fake_exec(*, fails=(), gate=None, stderr=""):
    def _run(cmd, cwd, env, timeout, cancel=None):
        if gate is not None:
            gate.wait(10)
        raw = (env or {}).get(params.ENV_VAR)
        value = next(iter(json.loads(raw).values())) if raw else None
        out = _out_of(cmd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"<html><body>{value}</body></html>", encoding="utf-8")
        failing = value in fails
        return subprocess.CompletedProcess(cmd, 1 if failing else 0, "", stderr if failing else "")

    return _run


def _await_done(tc, tries=600):
    for _ in range(tries):
        run = tc.get("/api/run/state").json()["run"]
        if run and run["done"]:
            return run
        threading.Event().wait(0.02)
    raise AssertionError("the run never finished")


# -- the happy path ----------------------------------------------------------


def test_start_runs_every_value_and_the_snapshot_reports_each(client, monkeypatch):
    tc, hub, ws = client
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())

    resp = tc.post("/api/run/start", json={"path": REL, "for": "region=EMEA,APAC"})
    assert resp.status_code == 200
    assert resp.json()["run"]["values"] == ["EMEA", "APAC"]

    run = _await_done(tc)
    assert [r["value"] for r in run["runs"]] == ["EMEA", "APAC"]
    assert all(r["outcome"] == "ok" for r in run["runs"])
    assert all(r["artifact"].startswith(".mooring/outbox/") for r in run["runs"])
    assert len({r["artifact"] for r in run["runs"]}) == 2  # never one file for two values


def test_state_is_null_before_anything_has_run(client):
    tc, _hub, _ws = client
    assert tc.get("/api/run/state").json() == {"run": None}


def test_one_failing_value_is_reported_without_marimos_stderr(client, monkeypatch):
    tc, hub, ws = client
    monkeypatch.setattr(
        notebook_run,
        "_exec",
        _fake_exec(fails={"APAC"}, stderr="MarimoExceptionRaisedError: 'SECRET_VALUE_DO_NOT_LEAK'\n"),
    )
    tc.post("/api/run/start", json={"path": REL, "for": "region=EMEA,APAC,AMER"})
    run = _await_done(tc)

    outcomes = {r["value"]: r["outcome"] for r in run["runs"]}
    assert outcomes == {"EMEA": "ok", "APAC": "failed", "AMER": "ok"}
    assert "SECRET" not in json.dumps(run)


# -- refusals ----------------------------------------------------------------


def test_start_requires_a_path_and_a_valid_spec(client):
    tc, _hub, _ws = client
    assert tc.post("/api/run/start", json={"for": "region=EMEA"}).status_code == 400
    bad = tc.post("/api/run/start", json={"path": REL, "for": "region"})
    assert bad.status_code == 400 and "NAME=VALUES" in bad.json()["error"]
    dupe = tc.post("/api/run/start", json={"path": REL, "for": "region=EMEA,emea"})
    assert dupe.status_code == 400  # would collide on one artifact


def test_start_refuses_a_notebook_that_ignores_the_parameter(client):
    tc, _hub, ws = client
    (ws / "plain.py").write_text(
        "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n",
        encoding="utf-8",
    )
    resp = tc.post("/api/run/start", json={"path": "plain.py", "for": "region=EMEA,APAC"})
    assert resp.status_code == 409
    assert "never reads a parameter" in resp.json()["error"]
    assert tc.get("/api/run/state").json()["run"] is None  # no doomed run was minted


def test_start_rejects_an_escaping_path(client):
    tc, _hub, _ws = client
    assert tc.post("/api/run/start", json={"path": "../x.py", "for": "n=1"}).status_code == 400


def test_a_second_start_is_refused_while_one_is_going(client, monkeypatch):
    tc, hub, ws = client
    gate = threading.Event()
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(gate=gate))
    try:
        assert tc.post("/api/run/start", json={"path": REL, "for": "region=EMEA,APAC"}).status_code == 200
        second = tc.post("/api/run/start", json={"path": REL, "for": "region=AMER"})
        assert second.status_code == 409 and "already going" in second.json()["error"]
    finally:
        hub.param_run.cancel.set()
        gate.set()
        _await_done(tc)


# -- cancel ------------------------------------------------------------------


def test_cancel_stops_the_run_and_the_remaining_values_are_reported_not_dropped(
    client, monkeypatch
):
    tc, hub, ws = client
    gate = threading.Event()
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(gate=gate))
    tc.post("/api/run/start", json={"path": REL, "for": "region=EMEA,APAC,AMER"})

    resp = tc.post("/api/run/cancel", json={})
    assert resp.status_code == 200
    gate.set()
    run = _await_done(tc)

    assert len(run["runs"]) == 3  # every declared value is accounted for
    assert {r["outcome"] for r in run["runs"]} <= {"ok", "cancelled", "skipped"}
    assert any(r["outcome"] == "skipped" for r in run["runs"])


def test_cancel_with_nothing_running_is_a_404(client):
    tc, _hub, _ws = client
    assert tc.post("/api/run/cancel", json={}).status_code == 404


def test_switching_repos_cancels_an_in_flight_run(client, monkeypatch):
    # A fan-out is bound to the workspace it started in; its remaining values must not
    # execute against the OLD workspace while the page shows the new one.
    tc, hub, ws = client
    gate = threading.Event()
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(gate=gate))
    tc.post("/api/run/start", json={"path": REL, "for": "region=EMEA,APAC"})
    handle = hub.param_run
    hub._cancel_param_run()
    gate.set()
    assert handle.cancel.is_set()
    assert hub.param_run is None
    for _ in range(600):
        if handle.snapshot()["done"]:
            break
        threading.Event().wait(0.02)
    assert handle.snapshot()["done"] is True
