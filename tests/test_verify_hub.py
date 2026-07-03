"""Hub wiring for Verify + the value-free trust badge.

The Verify route's run is faked (a real one spawns marimo); these pin the endpoint
contract and that ``/api/state`` surfaces the ``verified`` badge only while the
receipt still matches the notebook's content SHA.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from mooring import config, gitsha, paths, sync, verify
from mooring.app import verify_run
from mooring.hub.server import Hub, create_app

NOTEBOOK = "import marimo\n\napp = marimo.App()\n"


def _hub(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(ws))
    return Hub(config.AppConfig(repos=(spec,), active_alias="ws")), ws


def test_api_verify_reports_a_clean_run(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")

    def _fake(cfg, rel):
        return verify_run.VerifyResult(
            notebook_rel=rel, passed=True, cells_failed=None, ran_at="2026-07-03T09:00:00+00:00"
        )

    monkeypatch.setattr(verify_run, "verify_notebook", _fake)
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/verify", json={"path": "sales.py"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert any("ran clean" in line for line in body["lines"])


def test_api_verify_reports_a_failure(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")

    def _fake(cfg, rel):
        return verify_run.VerifyResult(
            notebook_rel=rel, passed=False, cells_failed=2, ran_at="2026-07-03T09:00:00+00:00"
        )

    monkeypatch.setattr(verify_run, "verify_notebook", _fake)
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/verify", json={"path": "sales.py"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert any("2 cells failed" in line for line in body["lines"])


def test_api_verify_surfaces_a_run_error(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")

    def _boom(cfg, rel):
        raise verify_run.VerifyError("could not run")

    monkeypatch.setattr(verify_run, "verify_notebook", _boom)
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/verify", json={"path": "sales.py"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "could not run"


def test_state_row_carries_the_verified_badge(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    nb = ws / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    sha = gitsha.local_blob_sha(nb, "sales.py")
    verify.record(
        ws, "sales.py", passed=True, sha=sha, cells_failed=None, ran_at="2026-07-03T09:00:00+00:00"
    )
    report = sync.StatusReport(
        head_commit="",
        files=[sync.FileStatus(path="sales.py", state=sync.FileState.NEW_LOCAL, local_sha=sha)],
    )
    files, _ = hub._files_artifacts(report, ws)
    row = next(f for f in files if f["path"] == "sales.py")
    assert row["verified"] == {"passed": True, "cells_failed": None, "ran_at": "2026-07-03T09:00:00+00:00"}


def test_verified_badge_clears_after_an_edit(tmp_path, monkeypatch):
    # The receipt is keyed to the file's SHA; editing the notebook drops the badge.
    hub, ws = _hub(tmp_path, monkeypatch)
    nb = ws / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    sha = gitsha.local_blob_sha(nb, "sales.py")
    verify.record(
        ws, "sales.py", passed=True, sha=sha, cells_failed=None, ran_at="2026-07-03T09:00:00+00:00"
    )
    nb.write_text(NOTEBOOK + "\n# edited\n", encoding="utf-8")
    report = sync.StatusReport(
        head_commit="",
        files=[sync.FileStatus(path="sales.py", state=sync.FileState.NEW_LOCAL, local_sha="x")],
    )
    files, _ = hub._files_artifacts(report, ws)
    row = next(f for f in files if f["path"] == "sales.py")
    assert "verified" not in row
