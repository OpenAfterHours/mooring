"""Excel delivery: the injected ``mooring_deliver`` payload + the mooring-side wiring.

The payload is loaded from disk exactly as the marimo kernel would (from
``.mooring/pylib``) and driven with a fake ``__file__`` cell global, so these exercise
the real workbook-writing path. The end-to-end tests fake ``notebook_run._exec``, the
one seam where the marimo subprocess would be, and have the fake DRIVE the real payload
— so a delivery runs the actual runtime, not a stand-in for it.

The load-bearing guarantees pinned here: the workbook (which holds real data VALUES —
that is the point) can only ever land somewhere sync excludes, it never claims a commit
for a never-pushed notebook, and an environment with no Excel writer gets an actionable
message instead of a broken run.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from mooring import sync, workbook
from mooring.app import deliver, notebook_run
from mooring.config import Config

NOTEBOOK = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"


def _cfg(tmp_path):
    return Config(client_id="cid", owner="acme", repo="nbs", workspace_path=str(tmp_path / "ws"))


def _imported_roots(src: bytes) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src.decode("utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _load_payload(ws):
    """Install and import the payload the way a notebook kernel would."""
    workbook.install_runtime(ws)
    mod_path = workbook.pylib_dir(ws) / "mooring_deliver.py"
    spec = importlib.util.spec_from_file_location("mooring_deliver_under_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call(md, notebook_path, data, name):
    """Register a table from a fake notebook cell: marimo sets ``__file__`` to the
    notebook, which is how the runtime attributes a receipt to the right file."""
    scope = {"md": md, "data": data, "__file__": str(notebook_path)}
    exec(f"r = md.table(data, {name!r})", scope)
    return scope["r"]


def _reset(md, notebook_path):
    """``md.reset()`` from a fake cell — it too attributes itself by ``__file__``."""
    exec("md.reset()", {"md": md, "__file__": str(notebook_path)})


def _sheets(path):
    book = openpyxl.load_workbook(path)
    try:
        return book.sheetnames
    finally:
        book.close()


def _rows(path, sheet):
    book = openpyxl.load_workbook(path)
    try:
        return [list(row) for row in book[sheet].iter_rows(values_only=True)]
    finally:
        book.close()


# -- the structural guarantee: a workbook of real values can never be pushed ------


def test_every_workbook_path_is_structurally_unsyncable():
    # THE load-bearing guarantee. Unlike the checks/inputs receipts, this artifact holds
    # real data values, so being unable to ride a push is the whole safety story.
    for rel in (
        ".mooring/outbox/sales/sales-20260731.xlsx",
        ".mooring/outbox/reports__q3/q3-20260731.xlsx",
        ".mooring/workbooks/notebooks__sales.py.json",
        ".mooring/workbooks/notebooks__sales.py.html",
        ".mooring/pylib/mooring_deliver.py",
    ):
        assert sync.is_synced_path(rel) is False
        # ...even if a team deliberately widened what syncs.
        assert sync.is_synced_path(rel, exclude=("*.html",)) is False
        assert sync.is_synced_path(rel, exclude=()) is False


def test_target_paths_all_live_under_the_excluded_state_dir(tmp_path):
    ws = tmp_path / "ws"
    for path in (
        deliver.outbox_target(ws, "notebooks/sales.py", ext=".xlsx"),
        workbook.render_target(ws, "notebooks/sales.py"),
        workbook.workbooks_dir(ws),
        workbook.pylib_dir(ws),
    ):
        rel = path.relative_to(ws).as_posix()
        assert rel.startswith(".mooring/")
        assert sync.is_synced_path(rel) is False


def test_a_rogue_target_env_var_cannot_move_the_workbook_out_of_the_state_dir(
    tmp_path, monkeypatch
):
    # The target arrives from OUTSIDE the kernel, so the runtime re-checks it: an env var
    # must not be able to drop real values into a synced folder.
    ws = tmp_path / "ws"
    (ws / "notebooks").mkdir(parents=True)
    nb = ws / "notebooks" / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    md = _load_payload(ws)
    escaped = ws / "notebooks" / "leaked.xlsx"
    monkeypatch.setenv("MOORING_DELIVER_XLSX", str(escaped))
    monkeypatch.setenv("MOORING_DELIVER_NOTEBOOK", "notebooks/sales.py")

    result = _call(md, nb, {"a": [1]}, "Summary")

    assert not escaped.exists()  # refused
    assert bool(result) is True  # ...but the delivery still happened, in the outbox
    landed = Path(result.path).relative_to(ws).as_posix()
    assert landed.startswith(".mooring/outbox/")
    assert sync.is_synced_path(landed) is False


# -- the injected payload --------------------------------------------------------


def test_install_runtime_writes_an_importable_standalone_payload(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    workbook.install_runtime(ws)
    payload = workbook.pylib_dir(ws) / "mooring_deliver.py"
    assert payload.is_file()
    src = payload.read_bytes()
    assert b"def table" in src and b"def reset" in src
    # Standalone: it runs in the notebook kernel where mooring isn't installed, so it
    # must import only the standard library — plus the two Excel engines it reaches for
    # at call time, which are the repo's dependencies, never mooring's.
    assert "mooring" not in _imported_roots(src)
    assert _imported_roots(src) <= {
        "__future__",
        "importlib",
        "inspect",
        "json",
        "os",
        "tempfile",
        "datetime",
        "decimal",
        "pathlib",
        "xlsxwriter",
        "openpyxl",
    }


def test_install_runtime_is_idempotent(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    workbook.install_runtime(ws)
    payload = workbook.pylib_dir(ws) / "mooring_deliver.py"
    before = payload.stat().st_mtime_ns
    workbook.install_runtime(ws)  # unchanged bytes -> no rewrite
    assert payload.stat().st_mtime_ns == before


def test_editor_installs_the_runtime_beside_its_siblings(tmp_path):
    from mooring import editor

    ws = tmp_path / "ws"
    ws.mkdir()
    editor.ensure_runtime_config(ws)
    assert (workbook.pylib_dir(ws) / "mooring_deliver.py").is_file()
    assert (workbook.pylib_dir(ws) / "mooring_checks.py").is_file()


def test_tables_become_sheets_in_order_with_a_provenance_sheet(tmp_path):
    ws = tmp_path / "ws"
    (ws / "notebooks").mkdir(parents=True)
    nb = ws / "notebooks" / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    md = _load_payload(ws)

    _call(md, nb, [{"region": "EMEA", "amount": 10}], "Summary")
    result = _call(md, nb, {"region": ["APAC"], "amount": [4]}, "By region")

    assert _sheets(result.path) == ["Summary", "By region", "Provenance"]
    assert _rows(result.path, "Summary") == [["region", "amount"], ["EMEA", 10]]


def test_a_repeated_sheet_name_replaces_rather_than_duplicates(tmp_path):
    # marimo re-executes cells freely; a second copy of a sheet would be a silently
    # wrong deliverable.
    ws = tmp_path / "ws"
    ws.mkdir()
    nb = ws / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    md = _load_payload(ws)

    _call(md, nb, {"n": [1]}, "Summary")
    result = _call(md, nb, {"n": [2]}, "Summary")

    assert _sheets(result.path) == ["Summary", "Provenance"]
    assert _rows(result.path, "Summary") == [["n"], [2]]


def test_reset_clears_the_sheets_and_removes_yesterdays_workbook(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    nb = ws / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    md = _load_payload(ws)
    first = Path(_call(md, nb, {"n": [1]}, "Old").path)
    assert first.is_file()

    _reset(md, nb)

    # A stale workbook in the outbox looks exactly like a fresh one to whoever emails it.
    assert not first.exists()
    assert workbook.read_receipt(ws, "sales.py")["workbook"] == ""


def test_polars_frames_deliver_their_rows(tmp_path):
    pl = pytest.importorskip("polars")
    ws = tmp_path / "ws"
    ws.mkdir()
    nb = ws / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    md = _load_payload(ws)
    frame = pl.DataFrame({"region": ["EMEA", "APAC"], "amount": [10, 4]})

    eager = _call(md, nb, frame, "Eager")
    lazy = _call(md, nb, frame.lazy(), "Lazy")

    assert bool(eager) and bool(lazy)
    assert _rows(lazy.path, "Eager") == [["region", "amount"], ["EMEA", 10], ["APAC", 4]]
    assert _rows(lazy.path, "Lazy") == [["region", "amount"], ["EMEA", 10], ["APAC", 4]]


def test_decimals_and_aware_datetimes_survive_the_engine(tmp_path):
    # Both are rejected outright by the engines; a finance notebook produces both.
    ws = tmp_path / "ws"
    ws.mkdir()
    nb = ws / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    md = _load_payload(ws)
    when = datetime(2026, 7, 31, 9, 30, tzinfo=timezone.utc)

    result = _call(md, nb, {"amount": [Decimal("12.50")], "at": [when]}, "Summary")

    assert bool(result) is True
    assert _rows(result.path, "Summary")[1] == [12.5, datetime(2026, 7, 31, 9, 30)]


def test_illegal_and_colliding_sheet_names_are_made_legal(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    nb = ws / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    md = _load_payload(ws)

    _call(md, nb, {"n": [1]}, "P&L: 2026/Q3 [draft]")  # Excel rejects : / [ ]
    _call(md, nb, {"n": [1]}, "A" * 40)  # ...and anything over 31 chars
    result = _call(md, nb, {"n": [1]}, "Provenance")  # ...and our reserved name

    names = _sheets(result.path)
    assert names[0] == "P&L  2026 Q3  draft"
    assert names[1] == "A" * 31
    # mooring's own record keeps the predictable name; the data sheet is the one moved.
    assert names[2] == "Provenance (2)"
    assert names[3] == "Provenance"


def test_a_bad_table_reports_and_never_raises(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    nb = ws / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    md = _load_payload(ws)

    result = _call(md, nb, object(), "Summary")

    assert bool(result) is False
    assert "unsupported table" in result.note
    # The receipt records a mooring-authored classification, never an engine's words.
    assert workbook.read_receipt(ws, "sales.py")["reason"] == "could not read the table"


def test_a_later_failure_keeps_the_record_of_the_sheets_already_delivered(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    nb = ws / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    md = _load_payload(ws)
    _call(md, nb, {"n": [1]}, "Summary")

    _call(md, nb, object(), "Broken")

    receipt = workbook.read_receipt(ws, "sales.py")
    assert receipt["sheets"] == ["Summary"]  # not erased by the later failure
    assert receipt["reason"] == "could not read the table"


# -- no Excel writer: actionable, and never fatal --------------------------------


def test_a_missing_excel_writer_is_actionable_and_does_not_break_the_run(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    nb = ws / "sales.py"
    nb.write_text(NOTEBOOK, encoding="utf-8")
    md = _load_payload(ws)
    monkeypatch.setattr(md, "_engine", lambda: "")  # neither xlsxwriter nor openpyxl

    # The cell keeps running: table() returns falsy rather than raising, and the
    # statement after it still executes.
    scope = {"md": md, "__file__": str(nb)}
    exec("r = md.table({'n': [1]}, 'Summary')\nafter = 'still running'", scope)

    assert scope["after"] == "still running"
    assert bool(scope["r"]) is False
    assert "mooring deps add openpyxl" in scope["r"].note
    assert "mooring deps add openpyxl" in workbook.read_receipt(ws, "sales.py")["reason"]


def test_the_missing_writer_hint_names_a_command_that_exists():
    # A message telling an analyst to pip install into a synced repo would be wrong;
    # the package has to reach the repo's pyproject.toml, which is what deps add does.
    from mooring import _workbook_runtime

    assert "mooring deps add" in _workbook_runtime.NO_WRITER_HINT


# -- delivery end to end (the fake kernel drives the REAL payload) ----------------


def _out_of(cmd):
    for i, tok in enumerate(cmd):
        if tok == "-o":
            return Path(cmd[i + 1])
    return None


def _kernel(tables, returncode=0, stderr="", produce=True):
    """Stand in for ``notebook_run._exec``: write the HTML render marimo would write,
    then run the real ``mooring_deliver`` payload against the env mooring passed, exactly
    as a notebook cell would."""

    def _run(cmd, cwd, env, timeout):
        if produce:
            out = _out_of(cmd)
            out.parent.mkdir(parents=True, exist_ok=True)
            # The real render embeds data values; plant one to prove it is deleted.
            out.write_text("<html>SECRET_VALUE_DO_NOT_LEAK</html>", encoding="utf-8")
        ws = Path(cwd)
        passed = {k: v for k, v in (env or {}).items() if k.startswith("MOORING_DELIVER_")}
        before = dict(os.environ)
        os.environ.update(passed)
        try:
            md = _load_payload(ws)
            md.reset()
            for label, data in tables:
                md.table(data, label)
        finally:
            os.environ.clear()
            os.environ.update(before)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return _run


def _mk(tmp_path, rel="notebooks/sales.py"):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    (ws / rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / rel).write_text(NOTEBOOK, encoding="utf-8")
    return cfg, ws


def test_deliver_excel_writes_the_expected_sheets_to_the_outbox(tmp_path, monkeypatch):
    cfg, ws = _mk(tmp_path)
    tables = [("Summary", {"region": ["EMEA"], "amount": [10]}), ("By region", {"n": [1, 2]})]
    monkeypatch.setattr(notebook_run, "_exec", _kernel(tables))

    result = deliver.deliver_excel(cfg, "notebooks/sales.py")

    assert result.out_path.is_file()
    assert result.out_rel.startswith(".mooring/outbox/")
    assert result.out_path.name.startswith("sales-") and result.out_path.suffix == ".xlsx"
    assert result.sheets == ("Summary", "By region")
    assert _sheets(result.out_path) == ["Summary", "By region", "Provenance"]
    assert _rows(result.out_path, "Summary") == [["region", "amount"], ["EMEA", 10]]
    # The value-bearing HTML render is a by-product of executing the notebook; it must
    # not survive, and the workbook must not be syncable.
    assert not workbook.render_target(ws, "notebooks/sales.py").is_file()
    assert sync.is_synced_path(result.out_rel) is False


def test_provenance_never_claims_a_commit_for_a_never_pushed_notebook(tmp_path, monkeypatch):
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _kernel([("Summary", {"n": [1]})]))

    result = deliver.deliver_excel(cfg, "notebooks/sales.py")

    rows = dict(_rows(result.out_path, "Provenance")[1:])
    assert rows["Generated by"] == "mooring"
    assert rows["Source"] == "acme/nbs (this notebook is not yet pushed)"
    assert rows["Notebook"] == "notebooks/sales.py"
    assert rows["Sheets"] == "Summary"
    # A blob link would 404 and an @commit would be a false claim, so neither appears.
    assert "View on GitHub" not in rows
    assert "@" not in rows["Source"]


def test_provenance_links_to_github_only_when_the_notebook_is_synced(tmp_path, monkeypatch):
    cfg, ws = _mk(tmp_path)

    class _Mft:  # tracked on the remote at this head commit
        head_commit = "abcdef1234567890"
        files = {"notebooks/sales.py": "blobsha"}

    monkeypatch.setattr(deliver.manifest, "load", lambda ws: _Mft())
    monkeypatch.setattr(notebook_run, "_exec", _kernel([("Summary", {"n": [1]})]))

    result = deliver.deliver_excel(cfg, "notebooks/sales.py")

    rows = dict(_rows(result.out_path, "Provenance")[1:])
    assert rows["Source"] == "acme/nbs@abcdef1"
    assert "/blob/" in rows["View on GitHub"] and "notebooks/sales.py" in rows["View on GitHub"]


def test_provenance_says_local_when_no_repo_is_configured(tmp_path, monkeypatch):
    cfg = Config(client_id="", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")
    monkeypatch.setattr(notebook_run, "_exec", _kernel([("Summary", {"n": [1]})]))

    result = deliver.deliver_excel(cfg, "sales.py")

    rows = dict(_rows(result.out_path, "Provenance")[1:])
    assert rows["Source"] == "a local workspace"
    assert "View on GitHub" not in rows


def test_deliver_excel_records_a_value_free_activity_entry(tmp_path, monkeypatch):
    from mooring import activity

    cfg, ws = _mk(tmp_path, "sales.py")
    monkeypatch.setattr(notebook_run, "_exec", _kernel([("Summary", {"n": [1]})]))

    deliver.deliver_excel(cfg, "sales.py")

    entries = activity.read(ws)
    assert entries and entries[0]["op"] == "deliver"
    assert entries[0]["path"] == "sales.py"


def _payload_without_engines() -> bytes:
    """The real payload with its engine probe pointed at a package that does not exist —
    the only honest way to simulate an analyst environment with no Excel writer, since
    the test environment has openpyxl installed."""
    src = (Path(workbook.__file__).with_name("_workbook_runtime.py")).read_text("utf-8")
    marker = 'for name in ("xlsxwriter", "openpyxl"):'
    assert marker in src, "the engine probe moved — update this fake"
    return src.replace(marker, 'for name in ("no_such_excel_engine",):').encode("utf-8")


def test_deliver_excel_surfaces_the_missing_writer_hint(tmp_path, monkeypatch):
    # The end-to-end version of the actionable message: the run completes, the notebook
    # is fine, and the reason reaches the user instead of a bare "no workbook produced".
    cfg, ws = _mk(tmp_path, "sales.py")
    # install_runtime is what puts the payload on the kernel path, so swapping its source
    # is what an engine-less environment looks like from mooring's side.
    monkeypatch.setattr(workbook, "_payload_source", _payload_without_engines)
    monkeypatch.setattr(notebook_run, "_exec", _kernel([("Summary", {"n": [1]})]))

    with pytest.raises(deliver.DeliverError) as excinfo:
        deliver.deliver_excel(cfg, "sales.py")
    assert "mooring deps add openpyxl" in str(excinfo.value)


def test_deliver_excel_refuses_a_notebook_that_named_no_tables(tmp_path, monkeypatch):
    cfg, ws = _mk(tmp_path, "sales.py")
    monkeypatch.setattr(notebook_run, "_exec", _kernel([]))

    with pytest.raises(deliver.DeliverError) as excinfo:
        deliver.deliver_excel(cfg, "sales.py")
    assert "mooring_deliver" in str(excinfo.value)


def test_deliver_excel_never_ships_a_stale_workbook(tmp_path, monkeypatch):
    # Yesterday's workbook must not be handed over as though this run produced it.
    cfg, ws = _mk(tmp_path, "sales.py")
    stale = deliver.outbox_target(ws, "sales.py", ext=".xlsx")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"yesterday")
    monkeypatch.setattr(notebook_run, "_exec", _kernel([]))

    with pytest.raises(deliver.DeliverError):
        deliver.deliver_excel(cfg, "sales.py")
    assert not stale.exists()


def test_deliver_excel_refuses_a_run_with_a_failed_cell(tmp_path, monkeypatch):
    # A failed cell means the numbers may be wrong, and a wrong workbook forwarded to a
    # stakeholder is the worst outcome this feature has.
    cfg, ws = _mk(tmp_path, "sales.py")
    stderr = "MarimoExceptionRaisedError: division by zero\n"
    monkeypatch.setattr(
        notebook_run, "_exec", _kernel([("Summary", {"n": [1]})], returncode=1, stderr=stderr)
    )

    with pytest.raises(deliver.DeliverError) as excinfo:
        deliver.deliver_excel(cfg, "sales.py")
    assert "1 cell failed" in str(excinfo.value)
    # The cells that DID run wrote their sheets. A workbook mooring refused must leave
    # nothing behind: half the numbers looks exactly like all of them once forwarded.
    assert not deliver.outbox_target(ws, "sales.py", ext=".xlsx").exists()


def test_deliver_excel_blames_the_environment_when_the_notebook_never_ran(tmp_path, monkeypatch):
    cfg, ws = _mk(tmp_path, "sales.py")
    monkeypatch.setattr(notebook_run, "_exec", _kernel([], returncode=1, produce=False))

    with pytest.raises(deliver.DeliverError) as excinfo:
        deliver.deliver_excel(cfg, "sales.py")
    assert "dependencies" in str(excinfo.value)


def test_deliver_excel_refuses_a_plain_module(tmp_path):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "helpers.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    with pytest.raises(deliver.DeliverError):
        deliver.deliver_excel(cfg, "helpers.py")


def test_deliver_excel_rejects_a_path_escaping_the_workspace(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.workspace().mkdir(parents=True)
    with pytest.raises(ValueError):
        deliver.deliver_excel(cfg, "../secret.py")


# -- the receipt mooring reads back ----------------------------------------------


def test_read_receipt_is_empty_for_a_missing_corrupt_or_foreign_receipt(tmp_path):
    ws = tmp_path / "ws"
    directory = workbook.workbooks_dir(ws)
    directory.mkdir(parents=True)
    assert workbook.read_receipt(ws, "sales.py") == {}

    (directory / "sales.py.json").write_text("{not json", encoding="utf-8")
    assert workbook.read_receipt(ws, "sales.py") == {}

    # A receipt naming a different notebook is never surfaced under this one.
    (directory / "sales.py.json").write_text(
        json.dumps({"notebook": "other.py", "sheets": ["Summary"]}), encoding="utf-8"
    )
    assert workbook.read_receipt(ws, "sales.py") == {}


def test_receipt_slug_is_injective(tmp_path):
    # a/b.py and a__b.py must not collide on one receipt.
    assert workbook.slug("a/b.py") != workbook.slug("a__b.py")


# -- the hub endpoint -------------------------------------------------------------


def _hub(tmp_path, monkeypatch):
    from mooring import config, paths
    from mooring.hub.server import Hub

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(ws))
    return Hub(config.AppConfig(repos=(spec,), active_alias="ws")), ws


def test_api_deliver_excel_reports_the_workbook_and_its_sheets(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from mooring import reveal
    from mooring.hub.server import create_app

    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")
    out = ws / ".mooring" / "outbox" / "sales" / "sales-20260731.xlsx"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"workbook")

    def _fake(cfg, rel):
        return deliver.DeliverResult(
            notebook_rel=rel,
            out_path=out,
            out_rel=out.relative_to(ws).as_posix(),
            commit="abc1234",
            sheets=("Summary", "By region"),
        )

    monkeypatch.setattr(deliver, "deliver_excel", _fake)
    monkeypatch.setattr(reveal, "reveal", lambda p: None)  # no real Explorer window

    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/deliver/excel", json={"path": "sales.py"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["out"].startswith(".mooring/outbox/")
    assert body["sheets"] == ["Summary", "By region"]
    assert any("Delivered" in line for line in body["lines"])


def test_api_deliver_excel_surfaces_the_reason_it_could_not(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from mooring.hub.server import create_app

    hub, ws = _hub(tmp_path, monkeypatch)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")

    def _boom(cfg, rel):
        raise deliver.DeliverError("no Excel writer: mooring deps add openpyxl")

    monkeypatch.setattr(deliver, "deliver_excel", _boom)
    with TestClient(create_app(hub)) as client:
        resp = client.post("/api/deliver/excel", json={"path": "sales.py"})
    assert resp.status_code == 502
    assert "mooring deps add openpyxl" in resp.json()["error"]


# -- no channel to the AI ---------------------------------------------------------


def test_the_feature_has_no_import_path_to_ai(tmp_path):
    # Structural, not a promise: the workbook holds real values, so nothing in this
    # feature may reach the AI layer. The import-linter's frozen-core-is-lean contract
    # enforces the same for marimo/Copilot/spaCy — this pins the ai/ edge here.
    for name in ("workbook.py", "_workbook_runtime.py"):
        src = (Path(workbook.__file__).with_name(name)).read_bytes()
        modules = {
            alias.name
            for node in ast.walk(ast.parse(src.decode("utf-8")))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        modules |= {
            node.module
            for node in ast.walk(ast.parse(src.decode("utf-8")))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not any(m.startswith("mooring.ai") for m in modules)
    # The lean-leaf contract must list it, or a future edit could quietly pull marimo in.
    contract = (Path(workbook.__file__).parents[2] / ".importlinter").read_text("utf-8")
    assert "mooring.workbook" in contract


def test_the_copilot_guide_is_value_free_api_text_only():
    guide = workbook.copilot_guide()
    assert "mooring_deliver" in guide and "md.table" in guide
    assert "never request data values" in guide
    # It describes the API; it must not carry a path, a value, or a receipt.
    assert ".mooring/" not in guide


def test_build_system_context_folds_in_the_workbook_help():
    from mooring.ai import egress

    guide = workbook.copilot_guide()
    ctx = egress.build_system_context(
        schema_text="a: Int64",
        notebook_source="import marimo",
        notebook_rel="nb.py",
        workbook_help=guide,
    )
    assert guide in ctx
    without = egress.build_system_context(
        schema_text="a: Int64", notebook_source="import marimo", notebook_rel="nb.py"
    )
    assert "mooring_deliver" not in without
