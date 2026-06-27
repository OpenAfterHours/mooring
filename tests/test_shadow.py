"""Tests for the notebook-name shadow guard (mooring.shadow).

Pure detector, so no FakeClient/responses — only tmp_path for the on-disk reads.
The ambient interpreter's loaded-stdlib set is monkeypatched where an assertion
depends on it, so the tests pin the BOUNDARY, not this machine's import state.
"""

import pytest

from mooring import shadow


def _w(ws, rel, text="import marimo\n"):
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")
    return rel


# -- the shadowable oracle (built-in/frozen/missing can't be shadowed) --------


def test_shadowable_oracle():
    assert shadow._shadowable("sys") is False  # built-in: precedes the path finder
    assert shadow._shadowable("json") is True  # a real .py in the stdlib
    assert shadow._shadowable("definitely_not_a_real_module_zzz") is False


# -- the danger set: flagged unconditionally, exact-case, no ambient ----------


@pytest.mark.parametrize(
    "name",
    ["polars", "pandas", "numpy", "sklearn", "plotly", "pyarrow", "duckdb", "marimo", "PIL", "cv2"],
)
def test_danger_modules_flagged(tmp_path, name):
    rel = _w(tmp_path, f"notebooks/{name}.py")
    assert shadow.scan([rel], workspace=tmp_path) == {rel: name}


def test_danger_flagged_even_when_package_absent(tmp_path):
    # pandas need not be importable in THIS process — the danger set matches the name.
    rel = _w(tmp_path, "pandas.py", "x = 1\n")
    assert shadow.scan([rel], workspace=tmp_path) == {rel: "pandas"}


# -- the user's real innocent filenames are never flagged (the load-bearing one)


def test_innocent_names_not_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "_loaded_stdlib", lambda: {"json", "string", "re"})
    rels = [
        _w(tmp_path, "notebooks/test.py"),  # 'test' not in the danger set; no sibling imports it
        _w(tmp_path, "notebooks/test_book.py"),
        _w(tmp_path, "notebooks/second-test.py"),  # not a valid identifier
        _w(tmp_path, "notebooks/rwa-check.py"),  # not a valid identifier
        _w(tmp_path, "notebooks/analysis.py"),
        _w(tmp_path, "notebooks/utils.py"),  # a local helper, not stdlib/danger
    ]
    assert shadow.scan(rels, workspace=tmp_path) == {}


# -- stdlib names: flagged only when actually loaded or sibling-imported ------


def test_stdlib_name_flagged_when_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "_loaded_stdlib", lambda: {"json"})
    rel = _w(tmp_path, "notebooks/json.py")
    assert shadow.scan([rel], workspace=tmp_path) == {rel: "json"}


def test_dormant_stdlib_name_not_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "_loaded_stdlib", lambda: set())  # csv not loaded, no sibling
    rel = _w(tmp_path, "notebooks/csv.py")
    assert shadow.scan([rel], workspace=tmp_path) == {}


def test_stdlib_name_flagged_when_sibling_imports_it(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "_loaded_stdlib", lambda: set())
    rel = _w(tmp_path, "notebooks/csv.py")
    # A sibling notebook genuinely imports csv — and the import lives INSIDE a marimo
    # cell function, so this also pins that _top_level_imports recurses (ast.walk).
    _w(
        tmp_path,
        "notebooks/report.py",
        "import marimo\napp = marimo.App()\n@app.cell\ndef _():\n    import csv\n    return\n",
    )
    findings = shadow.scan([rel, "notebooks/report.py"], workspace=tmp_path)
    assert findings == {rel: "csv"}


# -- case sensitivity: Polars.py does NOT shadow polars -----------------------


def test_mixed_case_not_flagged(tmp_path):
    rel = _w(tmp_path, "Polars.py")
    assert shadow.scan([rel], workspace=tmp_path) == {}


def test_exact_case_flagged(tmp_path):
    rel = _w(tmp_path, "polars.py")
    assert shadow.scan([rel], workspace=tmp_path) == {rel: "polars"}


# -- extra (the repo's declared packages) ------------------------------------


def test_extra_names_flagged(tmp_path):
    rel = _w(tmp_path, "notebooks/myteampkg.py")
    assert shadow.scan([rel], workspace=tmp_path, extra=frozenset({"myteampkg"})) == {
        rel: "myteampkg"
    }
    assert shadow.scan([rel], workspace=tmp_path) == {}  # silent without extra


# -- the ignore list ----------------------------------------------------------


def test_ignore_suppresses(tmp_path):
    rel = _w(tmp_path, "notebooks/polars.py")
    assert shadow.scan([rel], workspace=tmp_path, ignore=frozenset({rel})) == {}


# -- skips: non-.py, dunder, per-directory grouping --------------------------


def test_non_py_and_dunder_skipped(tmp_path):
    _w(tmp_path, "notebooks/polars.csv", "a\n")
    _w(tmp_path, "notebooks/__init__.py", "\n")
    assert shadow.scan(["notebooks/polars.csv", "notebooks/__init__.py"], workspace=tmp_path) == {}


def test_per_directory_grouping(tmp_path):
    a = _w(tmp_path, "a/polars.py")
    _w(tmp_path, "b/clean.py")
    assert shadow.scan([a, "b/clean.py"], workspace=tmp_path) == {a: "polars"}


def test_root_level_notebook(tmp_path):
    rel = _w(tmp_path, "polars.py")
    assert shadow.scan([rel], workspace=tmp_path) == {rel: "polars"}


# -- folder_shadows: open-time, reads disk, warns on innocent siblings -------


def test_folder_shadows_warns_when_opening_innocent_sibling(tmp_path):
    _w(tmp_path, "notebooks/polars.py")
    _w(tmp_path, "notebooks/analysis.py")
    assert shadow.folder_shadows("notebooks/analysis.py", workspace=tmp_path) == {
        "notebooks/polars.py": "polars"
    }


def test_folder_shadows_clean_folder_is_empty(tmp_path):
    _w(tmp_path, "notebooks/analysis.py")
    assert shadow.folder_shadows("notebooks/analysis.py", workspace=tmp_path) == {}


def test_folder_shadows_missing_dir_is_empty(tmp_path):
    assert shadow.folder_shadows("ghost/x.py", workspace=tmp_path) == {}


# -- warning_lines formatting (the _missing_deps_lines house style) ----------


def test_warning_lines_formatting():
    lines = shadow.warning_lines({"notebooks/polars.py": "polars"})
    assert lines[0].startswith("Warning:")
    assert any("notebooks/polars.py" in line and "polars" in line for line in lines)
    assert any("mooring shadow ignore" in line for line in lines)
    assert shadow.warning_lines({}) == []


# -- _top_level_imports internals --------------------------------------------


def test_top_level_imports_recurses_into_cell_bodies():
    src = (
        "import marimo\n"
        "@marimo.cell\n"
        "def _():\n"
        "    import polars as pl\n"
        "    from os import path\n"
        "    return\n"
    )
    roots = shadow._top_level_imports(src)
    assert {"marimo", "polars", "os"} <= roots


def test_top_level_imports_skips_relative_and_unparseable():
    assert shadow._top_level_imports("from . import sibling\n") == set()
    assert shadow._top_level_imports("def (:\n  not valid") == set()
