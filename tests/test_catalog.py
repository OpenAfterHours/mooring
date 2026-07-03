"""Catalog: the hub harvests a notebook's value-free title for the listing + search."""

from __future__ import annotations

from mooring import config, paths, sync
from mooring.hub.server import Hub

NOTEBOOK = (
    "import marimo\n\napp = marimo.App()\n\n"
    '@app.cell\ndef _(mo):\n    mo.md(r"""# Sales Reconciliation""")\n    return\n'
)
MODULE = "def helper():\n    return 1\n"


def _hub(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(ws))
    return Hub(config.AppConfig(repos=(spec,), active_alias="ws")), ws


def _report(*rels):
    return sync.StatusReport(
        head_commit="",
        files=[sync.FileStatus(path=r, state=sync.FileState.NEW_LOCAL, local_sha="x") for r in rels],
    )


def test_notebook_row_carries_its_title(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")
    files, _ = hub._files_artifacts(_report("sales.py"), ws)
    row = next(f for f in files if f["path"] == "sales.py")
    assert row.get("is_notebook") is True
    assert row["title"] == "Sales Reconciliation"


def test_module_row_has_no_title(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "helper.py").write_text(MODULE, encoding="utf-8")
    files, _ = hub._files_artifacts(_report("helper.py"), ws)
    row = next(f for f in files if f["path"] == "helper.py")
    assert "title" not in row
    assert row.get("is_module") is True


def test_titleless_notebook_has_no_title_field(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "bare.py").write_text("import marimo\n\napp = marimo.App()\n", encoding="utf-8")
    files, _ = hub._files_artifacts(_report("bare.py"), ws)
    row = next(f for f in files if f["path"] == "bare.py")
    assert "title" not in row
