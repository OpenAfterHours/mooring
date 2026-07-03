"""Tie-out checks: the injected ``mooring_checks`` payload + the mooring-side installer.

The payload is loaded from disk exactly as the marimo kernel would (from
``.mooring/pylib``), so these exercise the real value-free receipt path.
"""

from __future__ import annotations

import ast
import importlib.util
import json

import pytest

from mooring import checks


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
    checks.install_runtime(ws)
    mod_path = checks.pylib_dir(ws) / "mooring_checks.py"
    spec = importlib.util.spec_from_file_location("mooring_checks_under_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_install_runtime_writes_importable_payload(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    checks.install_runtime(ws)
    payload = checks.pylib_dir(ws) / "mooring_checks.py"
    assert payload.is_file()
    src = payload.read_bytes()
    assert b"def unique_key" in src and b"def reconciles" in src
    # Standalone: it runs in the notebook kernel where mooring isn't installed, so it
    # must import only the standard library (never the mooring package).
    assert "mooring" not in _imported_roots(src)
    assert _imported_roots(src) <= {
        "__future__",
        "inspect",
        "json",
        "os",
        "tempfile",
        "datetime",
        "pathlib",
    }


def test_install_runtime_is_idempotent(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    checks.install_runtime(ws)
    payload = checks.pylib_dir(ws) / "mooring_checks.py"
    before = payload.stat().st_mtime_ns
    checks.install_runtime(ws)  # unchanged bytes -> no rewrite
    assert payload.stat().st_mtime_ns == before


def test_check_logic_with_polars(tmp_path):
    pl = pytest.importorskip("polars")
    ws = tmp_path / "ws"
    ws.mkdir()
    mc = _load_payload(ws)
    df = pl.DataFrame({"id": [1, 2, 3, 3], "v": [1.0, 2.0, 3.0, 4.0], "n": [1, 2, None, 4]})
    assert bool(mc.reconciles(10.0, 10.0)) is True
    assert bool(mc.reconciles(10.0, 11.0, tol=0.5)) is False
    assert bool(mc.unique_key(df, "id")) is False
    assert bool(mc.unique_key(pl.DataFrame({"id": [1, 2, 3]}), "id")) is True
    assert bool(mc.no_fanout(df, pl.DataFrame({"k": [1, 2]}), on="k")) is True
    assert bool(mc.no_fanout(df, pl.DataFrame({"k": [1, 1]}), on="k")) is False
    assert bool(mc.row_delta(df, 4)) is True
    assert bool(mc.row_delta(df, 2)) is False
    assert bool(mc.not_null(df, "n")) is False
    assert bool(mc.expect(1 + 1 == 2, name="sanity")) is True


def test_receipt_is_value_free(tmp_path):
    pl = pytest.importorskip("polars")
    ws = tmp_path / "ws"
    ws.mkdir()
    mc = _load_payload(ws)
    df = pl.DataFrame({"id": [1, 1], "secret": [424242.0, 999999.0]})
    mc.unique_key(df, "id")
    (receipt,) = list((ws / ".mooring" / "checks").glob("*.json"))
    blob = receipt.read_text("utf-8")
    for value in ("424242", "999999"):  # no raw data value may reach the receipt
        assert value not in blob
    data = json.loads(blob)
    entry = next(iter(data["checks"].values()))
    assert set(entry) == {"kind", "passed", "note", "ts"}
    assert entry["passed"] is False


def test_reconciles_note_carries_no_data_magnitude(tmp_path):
    # A reconciliation difference is a derived DATA value — it must never land in the
    # value-free receipt (only "within/outside tolerance").
    ws = tmp_path / "ws"
    ws.mkdir()
    mc = _load_payload(ws)
    mc.reconciles(1_000_000.0, 987_654.0, tol=1.0, name="board_total")
    (receipt,) = list((ws / ".mooring" / "checks").glob("*.json"))
    blob = receipt.read_text("utf-8")
    assert "12346" not in blob and "12345" not in blob  # |1_000_000 - 987_654| = 12346
    note = json.loads(blob)["checks"]["board_total"]["note"]
    assert note == "outside tolerance"


def test_receipt_filename_is_injective(tmp_path):
    # 'a/b' and 'a__b' must NOT collide onto one receipt file.
    ws = tmp_path / "ws"
    ws.mkdir()
    mc = _load_payload(ws)
    nb1 = ws / "a" / "b.py"
    nb1.parent.mkdir(parents=True)
    nb1.write_text("x = 1\n", encoding="utf-8")
    nb2 = ws / "a__b.py"
    nb2.write_text("x = 1\n", encoding="utf-8")
    exec("mc.expect(True, name='c1')", {"mc": mc, "__file__": str(nb1)})
    exec("mc.expect(True, name='c2')", {"mc": mc, "__file__": str(nb2)})
    results = checks.read_results(ws)
    assert "a/b.py" in results and "a__b.py" in results  # two distinct receipts
    assert len(list((ws / ".mooring" / "checks").glob("*.json"))) == 2


def test_clear_and_read_results_drops_deleted_notebooks(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    mc = _load_payload(ws)
    nb = ws / "gone.py"
    nb.write_text("x = 1\n", encoding="utf-8")
    exec("mc.expect(False, name='c')", {"mc": mc, "__file__": str(nb)})
    assert "gone.py" in checks.read_results(ws)
    nb.unlink()  # the notebook is deleted — its stale receipt must not badge
    assert checks.read_results(ws) == {}
    # clear() removes the receipt file entirely
    assert checks.clear(ws) == 1
    assert list((ws / ".mooring" / "checks").glob("*.json")) == []


def test_read_results_ignores_a_corrupt_entry(tmp_path):
    ws = tmp_path / "ws"
    d = checks.checks_dir(ws)
    d.mkdir(parents=True)
    (ws / "nb.py").write_text("x = 1\n", encoding="utf-8")
    (d / "nb.py.json").write_text(
        json.dumps(
            {
                "notebook": "nb.py",
                "checks": {"ok": {"passed": True}, "broken": "not-a-dict"},
            }
        ),
        encoding="utf-8",
    )
    # The malformed entry is IGNORED (not silently counted as passing).
    assert checks.read_results(ws)["nb.py"] == {
        "total": 1,
        "failed": 0,
        "passed": 1,
        "updated": "",
    }


def test_receipt_is_keyed_to_the_calling_notebook(tmp_path):
    # The payload discovers WHICH notebook called it from the caller frame's
    # __file__ global — marimo sets that to the notebook .py (its cell filename is a
    # temp compiled path, verified against real marimo). Simulate that by exec-ing a
    # call with __file__ set to the notebook path in the caller's globals.
    ws = tmp_path / "ws"
    (ws / "notebooks").mkdir(parents=True)
    mc = _load_payload(ws)
    nb = ws / "notebooks" / "recon.py"
    nb.write_text("x = 1\n", encoding="utf-8")
    exec("mc.expect(True, name='ties')", {"mc": mc, "__file__": str(nb)})
    assert (ws / ".mooring" / "checks" / "notebooks__recon.py.json").is_file()
    results = checks.read_results(ws)
    assert results["notebooks/recon.py"] == {
        "total": 1,
        "failed": 0,
        "passed": 1,
        "updated": results["notebooks/recon.py"]["updated"],
    }


def test_read_results_counts_and_reset(tmp_path):
    pl = pytest.importorskip("polars")
    ws = tmp_path / "ws"
    ws.mkdir()
    mc = _load_payload(ws)
    mc.unique_key(pl.DataFrame({"id": [1, 1]}), "id")  # fails
    mc.expect(True, name="ok")  # passes
    results = checks.read_results(ws)
    assert results["_notebook"]["total"] == 2
    assert results["_notebook"]["failed"] == 1
    mc.reset()
    assert checks.read_results(ws) == {}


def test_read_results_skips_corrupt_and_foreign_files(tmp_path):
    ws = tmp_path / "ws"
    d = checks.checks_dir(ws)
    d.mkdir(parents=True)
    (d / "corrupt.json").write_text("{not json", encoding="utf-8")
    (d / "foreign.json").write_text(json.dumps({"unrelated": 1}), encoding="utf-8")
    assert checks.read_results(ws) == {}  # nothing usable, no crash


def test_copilot_guide_is_value_free_and_names_the_api():
    guide = checks.copilot_guide()
    assert "mooring_checks" in guide
    for fn in ("reconciles", "unique_key", "no_fanout", "row_delta", "not_null"):
        assert fn in guide


def test_build_system_context_folds_in_the_checks_help():
    # The copilot-authoring wiring: the guide reaches the model through the ONE context
    # choke point, and only when passed (default omits it). No data value is introduced.
    from mooring.ai import egress

    ctx = egress.build_system_context(
        schema_text="amount: float",
        notebook_source="SECRET_VALUE_DO_NOT_LEAK = 1\ndf = pl.read_csv('x')",
        notebook_rel="nb.py",
        checks_help=checks.copilot_guide(),
    )
    assert "mooring_checks" in ctx and "unique_key" in ctx
    assert "SECRET_VALUE_DO_NOT_LEAK" in ctx  # (source is authored code, as before)

    without = egress.build_system_context(
        schema_text="amount: float", notebook_source="df = 1", notebook_rel="nb.py"
    )
    assert "mooring_checks" not in without  # omitted unless explicitly provided
