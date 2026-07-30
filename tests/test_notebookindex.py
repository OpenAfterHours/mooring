"""notebookindex: the repo-wide catalog is value-free BY CONSTRUCTION (not via egress).

The suite-wide SECRET_VALUE_DO_NOT_LEAK sentinel must never survive into a rendered
catalog entry through any Python construct a notebook can hold — a cell body, a computed
string, a filter literal, an output — and extraction must never execute a notebook or
read a ``.mooring/`` run receipt.
"""

from __future__ import annotations

import pytest

from mooring.ai import notebookindex
from mooring.ai.notebookindex import ast_walk, loader

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
CARD = "4012888888881881"  # a valid Luhn card (egress WOULD catch this; the drop is structural)

_HEAD = 'import marimo\n\napp = marimo.App()\n\n'


def _nb(*cells: str) -> str:
    """A syntactically real marimo notebook whose cells hold ``cells``."""
    body = "".join(
        "@app.cell\ndef _():\n" + "".join(f"    {line}\n" for line in cell.splitlines()) + "    return\n\n"
        for cell in cells
    )
    return _HEAD + body + 'if __name__ == "__main__":\n    app.run()\n'


def _entry(source, rel="nb.py"):
    return ast_walk.extract_notebook(source, rel)[0]


def _rendered(source):
    return notebookindex.render_notebook(_entry(source))


# -- structural value-freeness: no slot for a value -----------------------------


@pytest.mark.parametrize(
    "cell",
    [
        'df = pl.read_parquet("data/sales.parquet")\nq = df.filter(pl.col("x") == "SECRET_VALUE_DO_NOT_LEAK")',
        'total = 4012888888881881\nnote = "SECRET_VALUE_DO_NOT_LEAK"',
        'LOOKUP = {"acct": "SECRET_VALUE_DO_NOT_LEAK"}',
        'df  # SECRET_VALUE_DO_NOT_LEAK',
        'out = f"loaded SECRET_VALUE_DO_NOT_LEAK rows"',
        'def helper(default="SECRET_VALUE_DO_NOT_LEAK"):\n    return default',
    ],
)
def test_value_never_leaks_from_a_cell_body(cell):
    rendered = _rendered(_nb('import marimo as mo\nmo.md("""# Sales""")', cell))
    assert SECRET not in rendered
    assert CARD not in rendered
    assert "Sales" in rendered  # the allowlisted title still survives


def test_a_computed_string_has_no_slot_in_any_indexed_call():
    # The three call shapes the walk lifts literals from, each given a COMPUTED argument:
    # an f-string is where a data value would appear, so it is dropped, never captured.
    src = _nb(
        'import marimo as mo\nimport mooring_inputs as mi\nimport mooring_checks as mc',
        'mi.fingerprint(df, f"SECRET_VALUE_DO_NOT_LEAK", path=f"data/{SECRET}.csv")',
        'mc.expect(ok, name=f"SECRET_VALUE_DO_NOT_LEAK")',
        'res = mo.sql(f"select * from {SECRET}")',
    )
    entry = _entry(src)
    rendered = notebookindex.render_notebook(entry)
    assert SECRET not in rendered
    assert entry.datasets[0].name == "" and entry.datasets[0].path == ""
    assert entry.checks[0].name == ""
    assert entry.sql_tables == ()


def test_a_secret_in_the_markdown_summary_is_withheld_whole():
    # The one free-text slot. A high-confidence hit drops the summary entirely rather than
    # trimming around it (the codelib docstring rule).
    src = _nb('import marimo as mo\nmo.md("""# Recon\n\nUse card 4012888888881881 to test.""")')
    entry = _entry(src)
    assert entry.title == "Recon"
    assert entry.summary == ""
    assert CARD not in notebookindex.render_notebook(entry)


def test_extraction_never_executes_the_notebook(tmp_path):
    canary = tmp_path / "ran.txt"
    src = _nb(
        f'from pathlib import Path\nPath({str(canary)!r}).write_text("executed")',
        'import marimo as mo\nmo.md("""# Side effects""")',
    )
    (tmp_path / "nb.py").write_text(src, encoding="utf-8")
    catalog = loader.load_catalog(tmp_path, [])
    assert not catalog.is_empty()
    assert not canary.exists()  # parsed with ast, never imported or run


def test_receipts_are_never_read(tmp_path):
    # .mooring/ holds what a run against REAL data observed. The catalog reports only what
    # the SOURCE declares, so a receipt's contents can never ride this channel.
    receipts = tmp_path / ".mooring" / "inputs"
    receipts.mkdir(parents=True)
    (receipts / "nb.json").write_text(
        '{"notebook": "nb.py", "inputs": {"sales": {"note": "SECRET_VALUE_DO_NOT_LEAK"}}}',
        encoding="utf-8",
    )
    src = _nb('import mooring_inputs as mi\nmi.fingerprint(df, "sales", path="data/sales.csv")')
    (tmp_path / "nb.py").write_text(src, encoding="utf-8")
    catalog = loader.load_catalog(tmp_path, [])
    text = notebookindex.render_notebooks(catalog.notebooks)
    assert "sales" in text and "data/sales.csv" in text  # what the SOURCE says
    assert SECRET not in text  # what the RUN saw


# -- what the allowlist does keep ------------------------------------------------


def test_indexes_title_summary_imports_inputs_checks_and_sql():
    src = _nb(
        'import marimo as mo\nimport polars as pl\nimport mooring_inputs as mi\n'
        'import mooring_checks as mc\nfrom utils.dates import to_utc',
        'mo.md("""# Month End Recon\n\nTies the ledger to the GL feed.""")',
        'mi.fingerprint(ledger, "ledger", path="data/ledger.parquet")',
        'mc.unique_key(ledger, "id")\nmc.reconciles(a, b, name="gl_tieout")',
        'res = mo.sql("""select id from gl_feed join dim_date on 1=1""")',
    )
    entry = _entry(src, "reports/recon.py")
    assert entry.title == "Month End Recon"
    assert entry.summary == "Ties the ledger to the GL feed."
    assert "polars" in entry.imports and "utils.dates.to_utc" in entry.imports
    assert entry.datasets == (notebookindex.Dataset(name="ledger", path="data/ledger.parquet"),)
    assert {c.kind for c in entry.checks} == {"unique_key", "reconciles"}
    assert "gl_tieout" in {c.name for c in entry.checks}
    assert set(entry.sql_tables) == {"gl_feed", "dim_date"}
    assert entry.n_cells == 5


def test_from_import_call_form_resolves_too():
    src = _nb(
        'from mooring_checks import unique_key\nfrom mooring_inputs import fingerprint',
        'fingerprint(df, "sales", path="data/s.csv")\nunique_key(df, "id")',
    )
    entry = _entry(src)
    assert entry.datasets[0].name == "sales"
    assert entry.checks[0].kind == "unique_key"


def test_summary_that_only_restates_the_title_is_dropped():
    entry = _entry(_nb('import marimo as mo\nmo.md("""# Sales""")'))
    assert entry.title == "Sales" and entry.summary == ""


def test_summary_is_capped():
    long = "x" * (notebookindex.SUMMARY_CAP + 200)
    entry = _entry(_nb(f'import marimo as mo\nmo.md("""# T\n\n{long}""")'))
    assert len(entry.summary) < notebookindex.SUMMARY_CAP + 40
    assert entry.summary.endswith("...[trimmed]")


def test_first_markdown_cell_wins_regardless_of_walk_order():
    # ast.walk is breadth-first, so the source LINE decides which cell is "first".
    src = _nb(
        'import marimo as mo',
        'mo.md("""# T\n\nThe real summary.""")',
        'mo.md("""A later note.""")',
    )
    assert _entry(src).summary == "The real summary."


def test_sql_extraction_cannot_capture_a_quoted_literal():
    src = _nb(
        'import marimo as mo',
        'res = mo.sql("""select * from ledger where name = \'SECRET_VALUE_DO_NOT_LEAK\'""")',
    )
    entry = _entry(src)
    assert entry.sql_tables == ("ledger",)
    assert SECRET not in notebookindex.render_notebook(entry)


# -- robustness: never fatal ------------------------------------------------------


def test_a_broken_notebook_is_skipped_not_fatal(tmp_path):
    (tmp_path / "good.py").write_text(_nb('import marimo as mo\nmo.md("""# Good""")'), "utf-8")
    (tmp_path / "broken.py").write_text(_HEAD + "@app.cell\ndef _(:\n  oops\n", "utf-8")
    catalog = loader.load_catalog(tmp_path, [])
    assert [nb.path for nb in catalog.notebooks] == ["good.py"]
    broken = next(r for r in catalog.reports if r.path == "broken.py")
    assert broken.error.startswith("SyntaxError@")


def test_a_parse_error_never_carries_the_offending_source_line():
    # str(SyntaxError) embeds the offending line + caret, which is value-bearing.
    src = _HEAD + '@app.cell\ndef _(:\n    x = "SECRET_VALUE_DO_NOT_LEAK"\n'
    entry, report = ast_walk.extract_notebook(src, "nb.py")
    assert SECRET not in report.error and SECRET not in notebookindex.render_notebook(entry)
    assert report.error.startswith("SyntaxError@")


def test_a_plain_module_is_not_catalogued(tmp_path):
    (tmp_path / "helpers.py").write_text("def clean(df):\n    return df\n", "utf-8")
    catalog = loader.load_catalog(tmp_path, [])
    assert catalog.is_empty()
    assert not next(r for r in catalog.reports if r.path == "helpers.py").is_notebook


def test_an_oversized_file_is_skipped(tmp_path):
    (tmp_path / "big.py").write_text(_nb("x = 1") + "# pad\n" * 500, "utf-8")
    catalog = loader.load_catalog(tmp_path, [], max_file_bytes=100)
    assert catalog.is_empty()
    assert next(r for r in catalog.reports if r.path == "big.py").error.startswith("TooLarge@")


def test_a_utf8_bom_does_not_break_extraction(tmp_path):
    (tmp_path / "bom.py").write_bytes(
        b"\xef\xbb\xbf" + _nb('import marimo as mo\nmo.md("""# BOM""")').encode("utf-8")
    )
    catalog = loader.load_catalog(tmp_path, [])
    assert [nb.title for nb in catalog.notebooks] == ["BOM"]


# -- discovery + search ----------------------------------------------------------


def _write(ws, rel, source):
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def test_discovers_folders_and_the_loose_root_but_not_ignored_trees(tmp_path):
    _write(tmp_path, "root.py", _nb('import marimo as mo\nmo.md("""# Root""")'))
    _write(tmp_path, "reports/deep/q3.py", _nb('import marimo as mo\nmo.md("""# Q3""")'))
    _write(tmp_path, ".venv/lib/vendor.py", _nb('import marimo as mo\nmo.md("""# Vendor""")'))
    _write(tmp_path, ".mooring/undo/old.py", _nb('import marimo as mo\nmo.md("""# Old""")'))
    catalog = loader.load_catalog(tmp_path, ["reports", ".venv"])
    assert sorted(nb.path for nb in catalog.notebooks) == ["reports/deep/q3.py", "root.py"]


def test_excluded_notebooks_never_enter_the_catalog(tmp_path):
    # The caller passes the team's synced per-notebook AI opt-out here.
    _write(tmp_path, "open.py", _nb('import marimo as mo\nmo.md("""# Open""")'))
    _write(tmp_path, "secret.py", _nb('import marimo as mo\nmo.md("""# Fenced off""")'))
    catalog = loader.load_catalog(tmp_path, [], exclude=["secret.py"])
    assert [nb.path for nb in catalog.notebooks] == ["open.py"]
    assert "Fenced off" not in notebookindex.render_listing(catalog)


def test_helpers_are_the_imports_that_resolve_inside_the_workspace(tmp_path):
    _write(tmp_path, "utils/dates.py", "def to_utc(x):\n    return x\n")
    _write(tmp_path, "utils/__init__.py", "")
    _write(
        tmp_path,
        "nb.py",
        _nb('import polars as pl\nfrom utils.dates import to_utc\nimport marimo as mo'),
    )
    (nb,) = loader.load_catalog(tmp_path, ["utils"]).notebooks
    assert nb.helpers == ("utils.dates",)  # polars/marimo are third-party, not ours


def test_search_ands_terms_and_ranks_path_hits_first(tmp_path):
    _write(tmp_path, "recon.py", _nb('import marimo as mo\nmo.md("""# Month End Recon""")'))
    _write(
        tmp_path,
        "other.py",
        _nb('import marimo as mo\nmo.md("""# Ad hoc\n\nMentions the month end recon run.""")'),
    )
    catalog = loader.load_catalog(tmp_path, [])
    assert [nb.path for nb in catalog.search("recon")] == ["recon.py", "other.py"]
    assert [nb.path for nb in catalog.search("month end recon")] == ["recon.py", "other.py"]
    assert catalog.search("recon payroll") == []  # every term must match
    assert catalog.search("") == []


def test_search_finds_a_notebook_by_the_dataset_and_check_it_declares(tmp_path):
    _write(
        tmp_path,
        "nb.py",
        _nb(
            'import mooring_inputs as mi\nimport mooring_checks as mc',
            'mi.fingerprint(df, "ledger", path="data/gl_ledger.parquet")\nmc.expect(ok, name="gl_tieout")',
        ),
    )
    catalog = loader.load_catalog(tmp_path, [])
    assert [nb.path for nb in catalog.search("gl_ledger")] == ["nb.py"]
    assert [nb.path for nb in catalog.search("gl_tieout")] == ["nb.py"]


def test_get_matches_path_stem_title_and_basename_but_never_the_filesystem(tmp_path):
    _write(tmp_path, "reports/recon.py", _nb('import marimo as mo\nmo.md("""# Month End""")'))
    _write(tmp_path, "secret.py", "TOKEN = 1\n")  # not a notebook, so not catalogued
    catalog = loader.load_catalog(tmp_path, ["reports"])
    for name in ("reports/recon.py", "recon", "recon.py", "Month End"):
        assert catalog.get(name) is not None, name
    # A path-like argument is a NAME lookup in memory: it cannot reach a file.
    assert catalog.get("../secret.py") is None
    assert catalog.get("secret.py") is None
    assert catalog.get("") is None


def test_terms_are_deduped_value_free_strings(tmp_path):
    entry = _entry(
        _nb(
            'import marimo as mo\nimport polars as pl',
            'mo.md("""# Recon\n\nTies out the ledger.""")',
        ),
        "reports/recon.py",
    )
    terms = entry.terms()
    assert len(terms) == len(set(terms))
    assert "reports/recon.py" in terms and "Recon" in terms and "polars" in terms
