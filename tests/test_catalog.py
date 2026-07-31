"""Catalog: the hub harvests a notebook's value-free title + content terms for the
listing and its search box (which searches CONTENT, not just the filename)."""

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


# -- content search: the row carries value-free catalog terms ---------------------

RICH = (
    "import marimo\n\napp = marimo.App()\n\n"
    "@app.cell\ndef _():\n"
    "    import marimo as mo\n"
    "    import mooring_inputs as mi\n"
    '    mo.md("""# Sales Reconciliation\n'
    "\n"
    "    Ties the ledger to the GL feed. Top account SECRET_VALUE_DO_NOT_LEAK.\n"
    '    | EMEA | 4,231,999 |""")\n'
    '    mi.fingerprint(df, "ledger", path="data/gl_ledger.parquet")\n'
    '    hush = "SECRET_VALUE_DO_NOT_LEAK"\n'
    "    return\n"
)


def test_notebook_row_carries_value_free_catalog_terms(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(RICH, encoding="utf-8")
    files, _ = hub._files_artifacts(_report("sales.py"), ws)
    row = next(f for f in files if f["path"] == "sales.py")
    terms = row["terms"]
    # The search box can now find this notebook by what it DOES, not just its filename.
    assert "Sales Reconciliation" in terms
    assert "data/gl_ledger.parquet" in terms
    assert "mooring_inputs" in terms
    # The hub row is built from the SAME allowlist the copilot's tools serve, so the
    # markdown PROSE that allowlist refuses is absent here too — a value in a cell body
    # or a pasted result table can never become a search term.
    joined = " ".join(terms)
    assert "SECRET_VALUE_DO_NOT_LEAK" not in joined
    assert "Ties the ledger" not in joined
    assert "4,231,999" not in joined


def test_module_row_has_no_terms(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "helper.py").write_text(MODULE, encoding="utf-8")
    files, _ = hub._files_artifacts(_report("helper.py"), ws)
    assert "terms" not in next(f for f in files if f["path"] == "helper.py")


def test_an_unparseable_notebook_still_lists(tmp_path, monkeypatch):
    # A half-typed notebook must degrade to "no terms", never break the whole listing.
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "wip.py").write_text("import marimo\n\napp = marimo.App()\n\ndef _(:\n", encoding="utf-8")
    files, _ = hub._files_artifacts(_report("wip.py"), ws)
    row = next(f for f in files if f["path"] == "wip.py")
    assert row.get("is_notebook") is True
    assert "terms" not in row


def test_the_sniff_is_cached_by_mtime(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(RICH, encoding="utf-8")
    hub._files_artifacts(_report("sales.py"), ws)
    assert len(hub._notebook_cache) == 1
    cached = next(iter(hub._notebook_cache.values()))
    hub._files_artifacts(_report("sales.py"), ws)  # unchanged -> no re-read, no re-parse
    assert next(iter(hub._notebook_cache.values())) == cached
