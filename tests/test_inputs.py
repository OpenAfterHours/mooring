"""Input fingerprints: the injected ``mooring_inputs`` payload + the mooring-side reader.

The payload is loaded from disk exactly as the marimo kernel would (from
``.mooring/pylib``) and driven with a fake ``__file__`` cell global, so these exercise
the real value-free receipt path and the change detection.
"""

from __future__ import annotations

import ast
import importlib.util
import json

import polars as pl

from mooring import inputs, sync

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


def _imported_roots(src: bytes) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src.decode("utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / "notebooks").mkdir(parents=True)
    (ws / "notebooks" / "recon.py").write_text("# notebook\n", "utf-8")
    return ws


def _load_payload(ws):
    """Install and import the payload the way a notebook kernel would."""
    inputs.install_runtime(ws)
    mod_path = inputs.pylib_dir(ws) / "mooring_inputs.py"
    spec = importlib.util.spec_from_file_location("mooring_inputs_under_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call(mi, nb, df, name, path=None):
    """Fingerprint from a fake notebook cell: marimo sets ``__file__`` to the notebook."""
    g = {"mi": mi, "df": df, "__file__": str(nb)}
    if path is not None:
        exec(f"r = mi.fingerprint(df, {name!r}, path={str(path)!r})", g)
    else:
        exec(f"r = mi.fingerprint(df, {name!r})", g)
    return g["r"]


def test_install_runtime_writes_importable_stdlib_only_payload(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    inputs.install_runtime(ws)
    payload = inputs.pylib_dir(ws) / "mooring_inputs.py"
    assert payload.is_file()
    src = payload.read_bytes()
    assert b"def fingerprint" in src
    # Standalone: runs in the notebook kernel where mooring isn't installed — stdlib only.
    assert "mooring" not in _imported_roots(src)
    assert _imported_roots(src) <= {
        "__future__",
        "hashlib",
        "inspect",
        "json",
        "os",
        "tempfile",
        "datetime",
        "pathlib",
    }


def test_inputs_dir_is_structurally_unsyncable():
    # A fingerprint receipt can NEVER ride a push — it lives under .mooring, which sync
    # excludes structurally even against a custom exclude.
    for rel in (".mooring/inputs/notebooks__recon.py.json", ".mooring/inputs/x.json"):
        assert sync.is_synced_path(rel) is False
        assert sync.is_synced_path(rel, exclude=("*.json",)) is False


def test_receipt_is_value_free(tmp_path):
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    csv = ws / "data" / "d.csv"
    csv.parent.mkdir()
    csv.write_text(f"id,customer\n1,{SECRET}\n2,{SECRET}\n", "utf-8")  # sentinel is a VALUE
    _call(mi, ws / "notebooks" / "recon.py", pl.read_csv(csv), "d", path=csv)

    receipt = json.loads((inputs.inputs_dir(ws) / "notebooks__recon.py.json").read_text("utf-8"))
    blob = json.dumps(receipt)
    assert SECRET not in blob  # THE guarantee: no data value reaches the receipt
    entry = receipt["inputs"]["d"]
    assert set(entry) == {"path", "hashed", "sha", "rows", "cols", "schema", "changed", "ts"}
    assert entry["rows"] == 2 and entry["cols"] == 2
    assert entry["schema"] == [["id", "Int64"], ["customer", "String"]]  # names + types only
    assert len(entry["sha"]) == 64  # a sha256 hex digest


def test_categorical_dtype_categories_do_not_leak(tmp_path):
    # Value-blindness: a polars Enum/Categorical dtype STRINGIFIES with its category
    # labels (real data values) — the receipt must record only the type NAME, never them.
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    df = pl.DataFrame(
        {"region": pl.Series(["EMEA", "APAC", "EMEA"], dtype=pl.Enum(["EMEA", "APAC", "LATAM"]))}
    )
    _call(mi, ws / "notebooks" / "recon.py", df, "d")
    blob = (inputs.inputs_dir(ws) / "notebooks__recon.py.json").read_text("utf-8")
    assert "EMEA" not in blob and "APAC" not in blob and "LATAM" not in blob
    entry = json.loads(blob)["inputs"]["d"]
    assert entry["schema"] == [["region", "Enum"]]  # the type name only


def test_detects_new_same_and_changed(tmp_path):
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    nb = ws / "notebooks" / "recon.py"
    csv = ws / "sales.csv"
    csv.write_text("a,b\n1,2\n", "utf-8")

    r1 = _call(mi, nb, pl.read_csv(csv), "sales", path=csv)
    assert bool(r1) and not r1.changed and not r1.seen_before  # NEW counts as unchanged

    r2 = _call(mi, nb, pl.read_csv(csv), "sales", path=csv)
    assert bool(r2) and not r2.changed and r2.seen_before  # SAME

    csv.write_text("a,b\n1,2\n3,4\n", "utf-8")  # a row added
    r3 = _call(mi, nb, pl.read_csv(csv), "sales", path=csv)
    assert not bool(r3) and r3.changed  # CHANGED -> falsy (usable as a reproducibility guard)


def test_detects_change_without_a_path_via_shape(tmp_path):
    # A df-only fingerprint (no file) still flags a moved input via shape/schema.
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    nb = ws / "notebooks" / "recon.py"
    assert bool(_call(mi, nb, pl.DataFrame({"a": [1, 2]}), "t"))  # NEW
    r = _call(mi, nb, pl.DataFrame({"a": [1, 2], "b": [3, 4]}), "t")  # extra column
    assert r.changed


def test_read_results_counts_total_and_changed(tmp_path):
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    nb = ws / "notebooks" / "recon.py"
    csv = ws / "sales.csv"
    csv.write_text("a\n1\n", "utf-8")
    _call(mi, nb, pl.read_csv(csv), "sales", path=csv)
    _call(mi, nb, pl.DataFrame({"x": [1]}), "extra")  # a second, unchanged input
    res = inputs.read_results(ws)
    assert res["notebooks/recon.py"]["total"] == 2
    assert res["notebooks/recon.py"]["changed"] == 0

    csv.write_text("a\n1\n2\n", "utf-8")
    _call(mi, nb, pl.read_csv(csv), "sales", path=csv)  # now changed
    assert inputs.read_results(ws)["notebooks/recon.py"]["changed"] == 1


def test_read_results_drops_a_deleted_notebooks_receipt(tmp_path):
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    _call(mi, ws / "notebooks" / "recon.py", pl.DataFrame({"a": [1]}), "x")
    assert "notebooks/recon.py" in inputs.read_results(ws)
    (ws / "notebooks" / "recon.py").unlink()
    assert inputs.read_results(ws) == {}


def test_read_results_skips_corrupt_and_foreign_files(tmp_path):
    ws = _ws(tmp_path)
    inputs.inputs_dir(ws).mkdir(parents=True)
    (inputs.inputs_dir(ws) / "corrupt.json").write_text("{not json", "utf-8")
    (inputs.inputs_dir(ws) / "foreign.json").write_text(json.dumps(["a", "list"]), "utf-8")
    assert inputs.read_results(ws) == {}


def test_receipts_are_keyed_per_notebook(tmp_path):
    ws = _ws(tmp_path)
    (ws / "notebooks" / "other.py").write_text("# nb\n", "utf-8")
    mi = _load_payload(ws)
    _call(mi, ws / "notebooks" / "recon.py", pl.DataFrame({"a": [1]}), "x")
    _call(mi, ws / "notebooks" / "other.py", pl.DataFrame({"a": [1]}), "x")
    assert set(inputs.read_results(ws)) == {"notebooks/recon.py", "notebooks/other.py"}


def test_failed_hash_fails_closed_not_silently_same(tmp_path):
    # If a path was given but the file can't be hashed this run, we must NOT report the
    # input unchanged (a false "same inputs") — fail closed and flag it changed.
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    nb = ws / "notebooks" / "recon.py"
    csv = ws / "sales.csv"
    csv.write_text("a,b\n1,2\n", "utf-8")
    assert bool(_call(mi, nb, pl.read_csv(csv), "sales", path=csv))  # baseline (hashed)
    # Now fingerprint with a path that can't be read (same df shape) -> must be CHANGED.
    r = _call(mi, nb, pl.read_csv(csv), "sales", path=ws / "gone.csv")
    assert r.changed is True


def test_lazyframe_row_count_is_unknown_not_zero(tmp_path):
    # A polars LazyFrame has no cheap row count; recording 0 would make a real row change
    # compare equal. It must be None (unknown), and the schema still resolves.
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    lazy = pl.LazyFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    _call(mi, ws / "notebooks" / "recon.py", lazy, "big")
    entry = json.loads((inputs.inputs_dir(ws) / "notebooks__recon.py.json").read_text("utf-8"))
    e = entry["inputs"]["big"]
    assert e["rows"] is None  # unknown, NOT 0
    assert e["cols"] == 2
    assert e["schema"] == [["a", "Int64"], ["b", "String"]]


def test_reset_clears_this_notebooks_receipt(tmp_path):
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    nb = ws / "notebooks" / "recon.py"
    _call(mi, nb, pl.DataFrame({"a": [1]}), "x")
    g = {"mi": mi, "__file__": str(nb)}
    exec("mi.reset()", g)
    assert inputs.read_results(ws) == {}


def test_clear_all_and_one(tmp_path):
    ws = _ws(tmp_path)
    (ws / "notebooks" / "other.py").write_text("# nb\n", "utf-8")
    mi = _load_payload(ws)
    _call(mi, ws / "notebooks" / "recon.py", pl.DataFrame({"a": [1]}), "x")
    _call(mi, ws / "notebooks" / "other.py", pl.DataFrame({"a": [1]}), "x")
    assert inputs.clear(ws, "notebooks/recon.py") == 1
    assert set(inputs.read_results(ws)) == {"notebooks/other.py"}
    assert inputs.clear(ws) == 1
    assert inputs.read_results(ws) == {}


def test_copilot_guide_is_value_free_and_names_the_api():
    guide = inputs.copilot_guide()
    assert "mooring_inputs" in guide and "fingerprint" in guide
    assert SECRET not in guide


def test_build_system_context_folds_in_the_inputs_help():
    from mooring.ai import egress

    ctx = egress.build_system_context(
        schema_text="amount: float",
        notebook_source="df = pl.read_csv('x')",
        notebook_rel="nb.py",
        inputs_help=inputs.copilot_guide(),
    )
    assert "mooring_inputs" in ctx and "fingerprint" in ctx
    without = egress.build_system_context(
        schema_text="amount: float", notebook_source="df = 1", notebook_rel="nb.py"
    )
    assert "mooring_inputs" not in without  # omitted unless explicitly provided
