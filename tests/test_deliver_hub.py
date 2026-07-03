"""Hub wiring for Deliver + the tie-out checks badge.

The Deliver route's render is faked (a real one spawns marimo and opens a browser);
these pin the endpoint contract and that ``/api/state`` surfaces the value-free
checks badge.
"""

from __future__ import annotations

import json
import webbrowser

from starlette.testclient import TestClient

from mooring import config, paths, reveal, sync
from mooring.app import deliver
from mooring.hub.server import Hub, create_app

NOTEBOOK = "import marimo\n\napp = marimo.App()\n"


def _hub(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(ws))
    return Hub(config.AppConfig(repos=(spec,), active_alias="ws")), ws


def test_api_deliver_renders_and_reports(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")

    out = ws / ".mooring" / "outbox" / "sales" / "sales-20260703.html"
    out.parent.mkdir(parents=True)
    out.write_text("<html></html>", encoding="utf-8")

    def _fake(cfg, rel):
        return deliver.DeliverResult(
            notebook_rel=rel, out_path=out, out_rel=out.relative_to(ws).as_posix(), commit="abc1234"
        )

    monkeypatch.setattr(deliver, "deliver_html", _fake)
    # No real Explorer window / browser tab during the test.
    monkeypatch.setattr(reveal, "reveal", lambda p: None)
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)

    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/deliver", json={"path": "sales.py"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["out"].startswith(".mooring/outbox/")
    assert any("Delivered" in line for line in body["lines"])


def test_api_deliver_surfaces_a_render_error(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")

    def _boom(cfg, rel):
        raise deliver.DeliverError("could not render")

    monkeypatch.setattr(deliver, "deliver_html", _boom)
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/deliver", json={"path": "sales.py"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "could not render"


def test_state_row_carries_the_checks_badge(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")
    receipts = ws / ".mooring" / "checks"
    receipts.mkdir(parents=True)
    (receipts / "sales.py.json").write_text(
        json.dumps(
            {
                "notebook": "sales.py",
                "updated": "2026-07-03T00:00:00+00:00",
                "checks": {
                    "unique_key(id)": {"kind": "unique_key", "passed": False, "note": "1 dup"},
                    "not_null(amt)": {"kind": "not_null", "passed": True, "note": "no nulls"},
                },
            }
        ),
        encoding="utf-8",
    )
    report = sync.StatusReport(
        head_commit="",
        files=[sync.FileStatus(path="sales.py", state=sync.FileState.NEW_LOCAL, local_sha="x")],
    )
    files, _ = hub._files_artifacts(report, ws)
    row = next(f for f in files if f["path"] == "sales.py")
    assert row["checks"] == {
        "total": 2,
        "failed": 1,
        "passed": 1,
        "updated": "2026-07-03T00:00:00+00:00",
    }


def test_state_row_has_no_checks_field_without_receipts(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")
    report = sync.StatusReport(
        head_commit="",
        files=[sync.FileStatus(path="sales.py", state=sync.FileState.NEW_LOCAL, local_sha="x")],
    )
    files, _ = hub._files_artifacts(report, ws)
    row = next(f for f in files if f["path"] == "sales.py")
    assert "checks" not in row
