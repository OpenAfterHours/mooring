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


def _run_probe(namespace: dict, tmp_path, names=()) -> dict:
    """Exec the real kernel snippet against ``namespace`` (its globals) and read
    back what it wrote — faithfully simulating /api/kernel/run."""
    out = tmp_path / "schema.json"
    src = introspect.probe_source(out, names)
    exec(src, namespace)  # noqa: S102  # the frozen probe, our own source
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
            {
                "name": "df",
                "columns": [["a", "Int64"], ["b", "String"]],
                "n_rows": 3,
                "preview": [[SECRET]],
                "sample": SECRET,
            },  # extra fields ignored
            {"name": "bad", "columns": "not-a-list"},  # dropped: columns wrong type
            {"columns": [["a", "Int64"]]},  # dropped: no name
            "not-a-dict",  # dropped
            {
                "name": "empty",
                "columns": [["a", 123]],
            },  # dtype not str -> col dropped -> frame dropped
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


# --- the name probe: "is this name bound, and what is it?" -----------------
#
# The second question the probe answers, so the copilot can learn whether the cell
# it just wrote actually ran. The answer per name is a bool plus the runtime CLASS
# name — the value itself must never be reachable through it.


def test_a_bound_secret_yields_its_type_name_and_nothing_else(tmp_path):
    ns = {"token": SECRET, "count": 7, "df": pl.DataFrame({"a": [1]})}
    data = _run_probe(ns, tmp_path, ["token", "count", "df", "never_defined"])

    assert SECRET not in json.dumps(data)
    assert data["names"] == [
        {"name": "token", "present": True, "type": "str"},
        {"name": "count", "present": True, "type": "int"},
        {"name": "df", "present": True, "type": "DataFrame"},
        {"name": "never_defined", "present": False, "type": None},
    ]
    present, missing, types = introspect._parse_names(data, ["token", "count", "df", "never_defined"])
    assert present == ("count", "df", "token")
    assert missing == ("never_defined",)
    assert dict(types)["token"] == "str"
    assert SECRET not in repr((present, missing, types))


def test_the_probe_never_calls_repr_or_str_on_the_object(tmp_path):
    # The single most likely way a value could leak out of a "what type is it?"
    # answer: an object whose repr/str IS the value. The probe must not touch them.
    class Leaky:
        def __repr__(self):
            return SECRET

        def __str__(self):
            return SECRET

        def __len__(self):
            return 42

    data = _run_probe({"obj": Leaky()}, tmp_path, ["obj"])

    assert SECRET not in json.dumps(data)
    assert data["names"] == [{"name": "obj", "present": True, "type": "Leaky"}]


def test_the_reported_type_is_the_class_name_verbatim(tmp_path):
    # The BOUNDARY of the guarantee, stated so it is a decision rather than an
    # accident: what comes back is `type(obj).__name__`, which is authored text — a
    # class statement, or the name handed to namedtuple — of exactly the kind the
    # model already reads in the notebook source. It is not read out of the data.
    # A class deliberately named after a data value would therefore be reported, and
    # the source that named it would already be visible; nothing else about the
    # object (its repr, length, contents or attributes) is ever touched.
    from collections import namedtuple

    data = _run_probe({"row": namedtuple("RowShape", ["a"])(1)}, tmp_path, ["row"])
    assert data["names"] == [{"name": "row", "present": True, "type": "RowShape"}]


def test_the_probe_is_only_ever_asked_about_bindable_names(tmp_path):
    # `_`-prefixed names are marimo CELL-LOCALS: absent from the kernel globals
    # however well the cell ran, so asking would manufacture a false "missing".
    src = introspect.probe_source(tmp_path / "x.json", ["good", "_local", "not a name", 7, "good"])
    assert "('good',)" in src
    assert "_local" not in src.split("_mooring_probe(")[-1]

    data = _run_probe({"good": 1, "_local": 2}, tmp_path, ["good", "_local"])
    assert [e["name"] for e in data["names"]] == ["good"]


def test_the_names_section_does_not_disturb_the_frames_section(tmp_path):
    # live_dataset_schemas' path must behave exactly as it did before.
    ns = {"df": pl.DataFrame({"region": ["EU"], "note": [SECRET]})}
    data = _run_probe(ns, tmp_path, ["df"])

    (f,) = introspect._parse_frames(data)
    assert f.name == "df" and [c[0] for c in f.columns] == ["region", "note"]
    assert f.n_rows == 1
    assert SECRET not in json.dumps(data)


def test_parse_names_is_fail_closed():
    asked = ["a", "b", "c", "d", "e"]
    data = {
        "names": [
            {"name": "a", "present": True, "type": "DataFrame", "preview": [[SECRET]]},
            {"name": "b", "present": 1, "type": "int"},  # present must be a real bool
            {"name": "c", "present": True, "type": f"str: {SECRET}"},  # not a class name
            {"name": "d", "present": False, "type": None},
            {"name": "not an identifier", "present": True, "type": "int"},
            {"name": "smuggled", "present": True, "type": "int"},  # never asked about
            "not-a-dict",
            {"present": True, "type": "int"},  # no name
        ]
    }
    present, missing, types = introspect._parse_names(data, asked)

    assert present == ("a", "c")  # b dropped entirely: its `present` was not a bool
    assert missing == ("d",)
    # the suspect type is dropped, but the FACT that `c` is bound survives
    assert types == (("a", "DataFrame"),)
    assert SECRET not in repr((present, missing, types))


def test_parse_names_rejects_a_malformed_payload():
    for junk in (None, [1, 2, 3], {"names": "nope"}, {"names": {"a": True}}, {}):
        assert introspect._parse_names(junk) == ((), (), ())


def test_parse_names_without_an_ask_still_only_accepts_the_exact_shape():
    data = {"names": [{"name": "a", "present": True, "type": "int"}]}
    assert introspect._parse_names(data) == (("a",), (), (("a", "int"),))


def test_live_dataset_schemas_no_editor_is_empty():
    assert introspect.live_dataset_schemas(None, "nb.py") == []

    class NotRunning:
        running = False
        port = None
        token = "t"

    assert introspect.live_dataset_schemas(NotRunning(), "nb.py") == []
