"""Hub wiring for the reviewer inbox: the list / detail / submit endpoints.

The GitHub calls are faked at the service layer (a real one hits the PR API); these pin
the endpoint contract, the not-configured empty inbox, and the note validation.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from mooring import auth, config, paths
from mooring.app import reviews as rv
from mooring.hub.server import Hub, create_app


def _hub(tmp_path, monkeypatch, *, owner="acme", repo="nbs", client_id="cid"):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = config.RepoSpec(alias="ws", owner=owner, repo=repo, workspace_path=str(ws))
    hub = Hub(config.AppConfig(client_id=client_id, repos=(spec,), active_alias="ws"))
    return hub, ws


def test_api_reviews_empty_when_not_configured(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch, owner="", repo="", client_id="")  # local, no repo
    with TestClient(create_app(hub)) as client:
        resp = client.get("/api/reviews")
    assert resp.status_code == 200
    assert resp.json() == {"reviews": []}


def test_api_reviews_lists_proposals(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    monkeypatch.setattr(auth, "get_token", lambda host=None: "tok")
    monkeypatch.setattr(hub, "username", lambda: "me")
    monkeypatch.setattr(
        rv,
        "list_reviews",
        lambda client, me: [
            rv.Review(7, "recon fix", "alice", "mooring/alice/x", "main", "https://gh/pull/7", "2026-07-05")
        ],
    )
    with TestClient(create_app(hub)) as client:
        resp = client.get("/api/reviews")
    assert resp.status_code == 200
    row = resp.json()["reviews"][0]
    assert row["number"] == 7 and row["author"] == "alice" and row["title"] == "recon fix"


def test_api_review_detail(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    monkeypatch.setattr(auth, "get_token", lambda host=None: "tok")
    monkeypatch.setattr(
        rv,
        "review_detail",
        lambda client, number: {"number": number, "title": "t", "author": "alice", "url": "u",
                                "files": [{"path": "nb.py", "status": "modified", "diff": {"kind": "cells", "cells": []}}]},
    )
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/reviews/detail", json={"number": 7})
    assert resp.status_code == 200
    assert resp.json()["files"][0]["path"] == "nb.py"


def test_api_review_detail_rejects_no_number(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/reviews/detail", json={})
    assert resp.status_code == 400


def test_api_review_submit_approve(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    monkeypatch.setattr(auth, "get_token", lambda host=None: "tok")
    calls = []
    monkeypatch.setattr(rv, "submit", lambda client, number, event, body: calls.append((number, event, body)))
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/reviews/submit", json={"number": 5, "event": "APPROVE", "body": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and "approved" in body["lines"][0]
    assert calls == [(5, "APPROVE", "")]


def test_api_review_submit_missing_note_is_400(tmp_path, monkeypatch):
    # The real service validates: REQUEST_CHANGES with no note -> ValueError -> 400,
    # before any GitHub call. A token is set so hub.client() constructs without network.
    hub, ws = _hub(tmp_path, monkeypatch)
    monkeypatch.setattr(auth, "get_token", lambda host=None: "tok")
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/reviews/submit", json={"number": 5, "event": "REQUEST_CHANGES", "body": ""})
    assert resp.status_code == 400
    assert "note" in resp.json()["error"].lower()
