"""Live-kernel schema introspection must expose names + dtypes — never a value.

The probe runs in the analyst's kernel, where real data lives, so its
value-blindness is the guarantee (there is no structural "mooring only reads a
header" here). These tests pin that: we build frames full of secret values,
run the exact source the kernel runs, and prove the readback carries the schema
but none of the values. They also pin the fail-closed readback parser.
"""

from __future__ import annotations

import json

import polars as pl
import pytest

from mooring.ai import introspect
from mooring.schema import DatasetSchema

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


def _run_probe(namespace: dict, tmp_path) -> dict:
    """Exec the real kernel snippet against ``namespace`` (its globals) and read
    back what it wrote — faithfully simulating /api/kernel/run."""
    out = tmp_path / "schema.json"
    src = introspect.probe_source(out)
    exec(src, namespace)  # noqa: S102 - the frozen probe, our own source
    assert out.exists(), "probe did not write the sidecar file"
    return json.loads(out.read_text("utf-8"))


def test_polars_dataframe_schema_no_values(tmp_path):
    ns = {
        "df": pl.DataFrame(
            {"region": ["EU", "US"], "amount": [1, 2], "note": [SECRET, SECRET + "_2"]}
        )
    }
    data = _run_probe(ns, tmp_path)
    blob = json.dumps(data)
    assert SECRET not in blob and "EU" not in blob and "US" not in blob
    frames = introspect._parse_frames(data)
    assert len(frames) == 1
    f = frames[0]
    assert f.name == "df"
    assert [c[0] for c in f.columns] == ["region", "amount", "note"]
    assert dict(f.columns)["amount"] == "Int64"
    assert f.n_rows == 2


def test_polars_lazyframe_schema_no_rowcount(tmp_path):
    ns = {"lazy": pl.LazyFrame({"id": [1], "secret_col": [SECRET]})}
    data = _run_probe(ns, tmp_path)
    assert SECRET not in json.dumps(data)
    (f,) = introspect._parse_frames(data)
    assert f.name == "lazy"
    assert [c[0] for c in f.columns] == ["id", "secret_col"]  # name is fine
    assert f.n_rows is None  # never collected


def test_polars_enum_categories_do_not_leak(tmp_path):
    # Enum embeds its category strings in str(dtype) — the one dtype that could
    # carry author values. The probe must reduce it to the bare type name.
    ns = {"e": pl.DataFrame({"flag": pl.Series([SECRET], dtype=pl.Enum([SECRET]))})}
    data = _run_probe(ns, tmp_path)
    assert SECRET not in json.dumps(data)
    (f,) = introspect._parse_frames(data)
    assert dict(f.columns)["flag"] == "Enum"


def test_pandas_dataframe_schema_no_values(tmp_path):
    pd = pytest.importorskip("pandas")
    ns = {"pdf": pd.DataFrame({"region": ["EU"], "note": [SECRET]})}
    data = _run_probe(ns, tmp_path)
    assert SECRET not in json.dumps(data) and "EU" not in json.dumps(data)
    (f,) = introspect._parse_frames(data)
    assert f.name == "pdf"
    assert [c[0] for c in f.columns] == ["region", "note"]
    assert f.n_rows == 1


def test_non_dataframe_and_underscore_vars_ignored(tmp_path):
    ns = {
        "x": 123,
        "name": "Alice",  # a plain str must never be reported
        "_hidden": pl.DataFrame({"a": [1]}),  # underscore = cell-local, skipped
        "df": pl.DataFrame({"a": [1]}),
    }
    data = _run_probe(ns, tmp_path)
    frames = introspect._parse_frames(data)
    assert [f.name for f in frames] == ["df"]
    assert "Alice" not in json.dumps(data)


def test_parse_frames_is_fail_closed():
    # Junk keys, wrong types, and a sneaky value-bearing field are all dropped.
    data = {
        "frames": [
            {"name": "df", "columns": [["a", "Int64"], ["b", "String"]], "n_rows": 3,
             "preview": [[SECRET]], "sample": SECRET},  # extra fields ignored
            {"name": "bad", "columns": "not-a-list"},     # dropped: columns wrong type
            {"columns": [["a", "Int64"]]},                # dropped: no name
            "not-a-dict",                                  # dropped
            {"name": "empty", "columns": [["a", 123]]},   # dtype not str -> col dropped -> frame dropped
        ]
    }
    frames = introspect._parse_frames(data)
    assert [f.name for f in frames] == ["df"]
    f = frames[0]
    assert f.columns == (("a", "Int64"), ("b", "String"))
    assert f.n_rows == 3
    # nothing the parser produced can carry the secret
    assert SECRET not in repr(frames)


def test_parse_frames_rejects_non_dict():
    assert introspect._parse_frames(None) == []
    assert introspect._parse_frames([1, 2, 3]) == []
    assert introspect._parse_frames({"frames": "nope"}) == []


def test_n_rows_bool_is_not_an_int():
    # bool is a subclass of int — make sure True doesn't masquerade as a row count.
    data = {"frames": [{"name": "df", "columns": [["a", "Int64"]], "n_rows": True}]}
    (f,) = introspect._parse_frames(data)
    assert f.n_rows is None


def test_format_live_schemas_renders_names_and_dtypes():
    frames = [
        DatasetSchema(name="df", columns=(("region", "String"), ("amount", "Int64")), n_rows=1500),
        DatasetSchema(name="lazy", columns=(("id", "Int64"),), n_rows=None),
    ]
    text = introspect.format_live_schemas(frames)
    assert "`df` (1,500 rows):" in text
    assert "- region: String" in text
    assert "`lazy`:" in text  # no row count rendered
    assert introspect.format_live_schemas([]) == ""


def test_extract_server_token_reads_marimo_element():
    # marimo (>=0.23) serves the skew token in a dedicated element; the hub must
    # read it or /api/kernel/run 401s (regression: an older JS-blob regex missed it).
    html = '<head><marimo-server-token data-token="aPe8U7NA3tUhyUfmeCF1mQ" hidden></marimo-server-token></head>'
    assert introspect._extract_server_token(html) == "aPe8U7NA3tUhyUfmeCF1mQ"
    # JS-blob fallback still works for other builds.
    assert introspect._extract_server_token('{"serverToken": "xyz123"}') == "xyz123"
    assert introspect._extract_server_token("<html>no token here</html>") == ""


def test_live_dataset_schemas_no_editor_is_empty():
    assert introspect.live_dataset_schemas(None, "nb.py") == []

    class NotRunning:
        running = False
        port = None
        token = "t"

    assert introspect.live_dataset_schemas(NotRunning(), "nb.py") == []
