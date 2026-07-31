"""notebookindex: the repo-wide catalog is value-free BY CONSTRUCTION (not via egress).

The suite-wide SECRET_VALUE_DO_NOT_LEAK sentinel must never survive into a rendered
catalog entry through any construct a notebook can hold — a cell body, a computed string,
a filter literal, a MARKDOWN PARAGRAPH, an output, or a SQL narrative — and extraction
must never execute a notebook or read a ``.mooring/`` run receipt.

The one deliberate exception is the H1 ``title``, the single authored-prose slot: it is
scanned and withheld whole on a hit, but that is best-effort, not structural, and the
tests below say so explicitly rather than implying a guarantee that does not hold.
"""

from __future__ import annotations

import pytest

from mooring.ai import notebookindex
from mooring.ai.notebookindex import ast_walk, loader

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
CARD = "4012888888881881"  # a valid Luhn card — what the title scanner actually catches

_HEAD = 'import marimo\n\napp = marimo.App()\n\n'


def _nb(*cells: str) -> str:
    """A syntactically real marimo notebook whose cells hold ``cells``."""
    body = "".join(
        "@app.cell\ndef _():\n"
        + "".join(f"    {line}\n" for line in cell.splitlines())
        + "    return\n\n"
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


@pytest.mark.parametrize(
    "markdown",
    [
        # The exact shapes the reviewer confirmed leaking from the earlier `summary` field:
        # a pasted result table and a closing-balance note. Neither the structured scanner
        # nor the egress floor catches a bare account name, so the ONLY defence is that
        # markdown prose has no slot at all.
        '# Q3 revenue\n\n    Numbers as at close. Top account SECRET_VALUE_DO_NOT_LEAK.',
        '# Q3 revenue\n\n    | Region | Top customer |\n    | EMEA | SECRET_VALUE_DO_NOT_LEAK |',
        '# Q3 revenue\n\n    Signed off by SECRET_VALUE_DO_NOT_LEAK on the 4th.',
        # ...and with no heading at all, so there is no title to fall back through either.
        '    Top account SECRET_VALUE_DO_NOT_LEAK. Balance 4,231,999.',
    ],
)
def test_markdown_prose_has_no_slot(markdown):
    # THE regression test for the free-prose channel: paragraphs beneath (or instead of) a
    # heading are read for their H1 and otherwise discarded — structurally, not by scanning.
    entry = _entry(_nb(f'import marimo as mo\nmo.md("""{markdown}""")'))
    rendered = notebookindex.render_notebook(entry)
    assert SECRET not in rendered
    assert "4,231,999" not in rendered
    assert SECRET not in " ".join(entry.terms())
    assert not any("Top account" in t for t in entry.terms())


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


# -- the SQL reduction cannot lift a value out of a quoted literal ---------------


@pytest.mark.parametrize(
    "sql, expected",
    [
        # The reviewer's case: a narrative that legitimately contains "from <name>".
        (
            "select acct_id from gl_ledger\n"
            "  where narrative like '%transfer from ACME_Holdings_Ltd%'\n"
            "     or narrative = 'settlement from Northwind_Traders_88213'",
            ("gl_ledger",),
        ),
        # A doubled quote escapes within a SQL string and must not end it early.
        ("select * from gl_ledger where n = 'paid from O''Brien_Ltd'", ("gl_ledger",)),
        ("select * from gl_ledger -- reconciled from ACME_Holdings_Ltd", ("gl_ledger",)),
        ("select * from gl_ledger /* pulled from ACME_Holdings_Ltd */", ("gl_ledger",)),
        ("select * from gl_ledger where n = $$paid from ACME_Holdings_Ltd$$", ("gl_ledger",)),
        # A double-quoted identifier is dropped WITH the literals: losing a table name is
        # the fail-closed trade against reading a quoted value as one.
        ('select * from gl_ledger, "from ACME_Holdings_Ltd"', ("gl_ledger",)),
        # An UNBALANCED quote must not leave the tail scannable.
        ("select * from gl_ledger where n = 'paid from ACME_Holdings_Ltd", ("gl_ledger",)),
        # The plain case still works, including a real JOIN.
        ("select id from gl_feed join dim_date using (dt)", ("gl_feed", "dim_date")),
    ],
)
def test_sql_tables_are_read_only_from_unquoted_sql(sql, expected):
    # !r so a query containing quotes still forms a valid Python literal — a fixture that
    # silently failed to parse would vacuously "pass" every assertion below.
    entry = _entry(_nb("import marimo as mo", f"res = mo.sql({sql!r})"))
    assert entry.n_cells == 2, "the fixture notebook must actually parse"
    assert entry.sql_tables == expected
    assert "ACME_Holdings_Ltd" not in " ".join(entry.terms())
    assert "Northwind_Traders_88213" not in " ".join(entry.terms())


# -- the one authored-prose slot: the H1 title ----------------------------------


def test_title_is_an_h1_only_and_never_falls_back_to_pasted_prose():
    # The hub's DISPLAY title falls back to the first non-empty line, which for a pasted
    # table is a row of data. The catalog egresses, so it takes the H1 or nothing.
    from mooring import notebook_template

    src = _nb(
        'import marimo as mo',
        'mo.md("""| Region | Revenue | Top account |\n    | EMEA | 4,231,999 | Contoso |""")',
    )
    assert notebook_template.notebook_title(src).startswith("| Region")  # the hub's fallback
    entry = _entry(src)
    assert entry.title == ""  # the catalog refuses it
    assert "Region" not in notebookindex.render_notebook(entry)


def test_a_scannable_value_in_the_title_is_withheld_whole():
    # BEST-EFFORT, not structural: this catches what the scanner catches (a checksum-valid
    # card here). A bare name in a heading can still survive — which is exactly why the
    # whole feature is opt-in. The entry keeps its PATH so it is never anonymised.
    entry = _entry(_nb(f'import marimo as mo\nmo.md("""# Recon for card {CARD}""")'))
    assert entry.title == ""
    rendered = notebookindex.render_notebook(entry)
    assert CARD not in rendered
    assert "nb.py" in rendered


def test_the_title_scanner_is_injectable_so_the_egressing_path_can_strengthen_it():
    # chat_service injects the operator's full NER scanner; the default is structured-only.
    src = _nb('import marimo as mo\nmo.md("""# Jane Smith quarterly""")')
    assert ast_walk.extract_notebook(src, "nb.py")[0].title == "Jane Smith quarterly"
    entry, report = ast_walk.extract_notebook(
        src, "nb.py", scan=lambda text: "person name" if "Jane" in text else None
    )
    assert entry.title == ""
    assert ("title", 1) in report.dropped_nodes


def test_title_is_capped():
    entry = _entry(_nb(f'import marimo as mo\nmo.md("""# {"x" * 500}""")'))
    assert len(entry.title) <= 120  # a literal bound: a widened TITLE_CAP must fail here


def test_first_h1_wins_regardless_of_walk_order():
    # ast.walk is breadth-first, so the source LINE decides which cell is "first".
    src = _nb('import marimo as mo', 'mo.md("""# The real title""")', 'mo.md("""# A later note""")')
    assert _entry(src).title == "The real title"


def test_a_notebook_with_no_heading_has_no_title():
    entry = _entry(_nb('import marimo as mo\nmo.md("""just some prose""")'))
    assert entry.title == ""
    assert "nb.py" in notebookindex.render_notebook(entry)


# -- never executed, never a receipt ---------------------------------------------


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


def test_receipts_and_undo_snapshots_under_dot_mooring_are_never_read(tmp_path):
    # .mooring/ holds what a run against REAL data observed (receipts) AND undo snapshots
    # of real notebooks. Neither may enter the catalog. The snapshot is a .py, so this
    # fails if the directory guard goes — the json alone would not exercise it, since the
    # loader only globs *.py.
    receipts = tmp_path / ".mooring" / "inputs"
    receipts.mkdir(parents=True)
    (receipts / "nb.json").write_text(
        '{"notebook": "nb.py", "inputs": {"sales": {"note": "SECRET_VALUE_DO_NOT_LEAK"}}}',
        encoding="utf-8",
    )
    (tmp_path / ".mooring" / "undo").mkdir()
    (tmp_path / ".mooring" / "undo" / "snap.py").write_text(
        _nb('import marimo as mo\nmo.md("""# SECRET_VALUE_DO_NOT_LEAK snapshot""")'), "utf-8"
    )
    src = _nb('import mooring_inputs as mi\nmi.fingerprint(df, "sales", path="data/sales.csv")')
    (tmp_path / "nb.py").write_text(src, encoding="utf-8")

    catalog = loader.load_catalog(tmp_path, [".mooring", ".mooring/undo"])
    text = notebookindex.render_notebooks(catalog.notebooks)
    assert [nb.path for nb in catalog.notebooks] == ["nb.py"]
    assert "sales" in text and "data/sales.csv" in text  # what the SOURCE says
    assert SECRET not in text  # what the RUN saw, and the snapshot


# -- what the allowlist does keep ------------------------------------------------


def test_indexes_title_imports_inputs_checks_and_sql():
    src = _nb(
        'import marimo as mo\nimport polars as pl\nimport mooring_inputs as mi\n'
        'import mooring_checks as mc\nfrom utils.dates import to_utc',
        'mo.md("""# Month End Recon\n\n    Ties the ledger to the GL feed.""")',
        'mi.fingerprint(ledger, "ledger", path="data/ledger.parquet")',
        'mc.unique_key(ledger, "id")\nmc.reconciles(a, b, name="gl_tieout")',
        'res = mo.sql("""select id from gl_feed join dim_date on 1=1""")',
    )
    entry = _entry(src, "reports/recon.py")
    assert entry.title == "Month End Recon"
    assert "polars" in entry.imports and "utils.dates.to_utc" in entry.imports
    assert entry.datasets == (notebookindex.Dataset(name="ledger", path="data/ledger.parquet"),)
    assert {c.kind for c in entry.checks} == {"unique_key", "reconciles"}
    assert "gl_tieout" in {c.name for c in entry.checks}
    assert set(entry.sql_tables) == {"gl_feed", "dim_date"}
    assert entry.n_cells == 5
    # The prose under the heading is NOT carried, even though it is innocuous here.
    assert "Ties the ledger" not in notebookindex.render_notebook(entry)


def test_from_import_call_form_resolves_too():
    src = _nb(
        'from mooring_checks import unique_key\nfrom mooring_inputs import fingerprint',
        'fingerprint(df, "sales", path="data/s.csv")\nunique_key(df, "id")',
    )
    entry = _entry(src)
    assert entry.datasets[0].name == "sales"
    assert entry.checks[0].kind == "unique_key"


def test_the_path_gets_a_line_to_itself_so_the_egress_floor_cannot_anonymise_an_entry():
    # egress.scrub_text drops a whole LINE on a checksum-PII hit. A title sharing the
    # path's line would take the notebook's identity down with it.
    from mooring.ai import egress

    entry = notebookindex.Notebook(path="reports/recon.py", title=f"Recon {CARD}")
    for rendered in (
        notebookindex.render_notebook(entry),
        notebookindex.render_lines([entry]),
    ):
        scrubbed, findings = egress.scrub_text(rendered)
        assert findings and CARD not in scrubbed
        assert "reports/recon.py" in scrubbed


# -- robustness: never fatal ------------------------------------------------------


def test_a_broken_notebook_is_skipped_not_fatal(tmp_path):
    (tmp_path / "good.py").write_text(_nb('import marimo as mo\nmo.md("""# Good""")'), "utf-8")
    (tmp_path / "broken.py").write_text(_HEAD + "@app.cell\ndef _(:\n  oops\n", "utf-8")
    catalog = loader.load_catalog(tmp_path, [])
    assert [nb.path for nb in catalog.notebooks] == ["good.py"]
    broken = next(r for r in catalog.reports if r.path == "broken.py")
    assert broken.error.startswith("SyntaxError@")


def test_a_parse_error_never_carries_the_offending_source_line():
    # str(SyntaxError) embeds the OFFENDING line + a caret. The sentinel is ON that line
    # here, so reporting str(exc) instead of the type+lineno would leak it.
    src = _HEAD + '@app.cell\ndef _():\n    x = "SECRET_VALUE_DO_NOT_LEAK" ===\n'
    entry, report = ast_walk.extract_notebook(src, "nb.py")
    assert report.error.startswith("SyntaxError@")
    assert SECRET not in report.error
    assert SECRET not in notebookindex.render_notebook(entry)


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


def test_discovers_folders_and_the_loose_root(tmp_path):
    _write(tmp_path, "root.py", _nb('import marimo as mo\nmo.md("""# Root""")'))
    _write(tmp_path, "reports/deep/q3.py", _nb('import marimo as mo\nmo.md("""# Q3""")'))
    catalog = loader.load_catalog(tmp_path, ["reports"])
    assert sorted(nb.path for nb in catalog.notebooks) == ["reports/deep/q3.py", "root.py"]


@pytest.mark.parametrize("ignored", ["node_modules", ".hidden"])
def test_ignored_trees_are_never_scanned(tmp_path, ignored):
    # Two independent rules: the _IGNORE_DIRS set (node_modules is not dot-prefixed, so
    # only the set catches it) and the dot-prefix rule (.hidden is in no set).
    _write(tmp_path, "root.py", _nb('import marimo as mo\nmo.md("""# Root""")'))
    _write(tmp_path, f"{ignored}/vendored.py", _nb('import marimo as mo\nmo.md("""# Vendor""")'))
    catalog = loader.load_catalog(tmp_path, [ignored])
    assert [nb.path for nb in catalog.notebooks] == ["root.py"]


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


def test_search_ands_terms_and_ranks_title_hits_first(tmp_path):
    _write(tmp_path, "recon.py", _nb('import marimo as mo\nmo.md("""# Month End Recon""")'))
    _write(
        tmp_path,
        "other.py",
        _nb('import mooring_checks as mc', 'mc.expect(ok, name="recon_variance")'),
    )
    catalog = loader.load_catalog(tmp_path, [])
    assert [nb.path for nb in catalog.search("recon")] == ["recon.py", "other.py"]
    assert [nb.path for nb in catalog.search("month end recon")] == ["recon.py"]
    assert catalog.search("recon payroll") == []  # every term must match
    assert catalog.search("") == []


def test_search_finds_a_notebook_by_the_dataset_and_check_it_declares(tmp_path):
    _write(
        tmp_path,
        "nb.py",
        _nb(
            'import mooring_inputs as mi\nimport mooring_checks as mc',
            'mi.fingerprint(df, "ledger", path="data/gl_ledger.parquet")\n'
            'mc.expect(ok, name="gl_tieout")',
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
        _nb('import marimo as mo\nimport polars as pl', 'mo.md("""# Recon""")'),
        "reports/recon.py",
    )
    terms = entry.terms()
    assert len(terms) == len(set(terms))
    assert "reports/recon.py" in terms and "Recon" in terms and "polars" in terms
