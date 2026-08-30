"""Static validation of a CANDIDATE notebook (``marimo_rt.validate_notebook_source``).

The gap this fills: ``apply_cell_patch`` only checks that a cell PARSES, so a weak
model's proposal can duplicate a definition, close a dependency cycle, reference a name
no cell defines, or paste whole ``@app.cell`` blocks into a cell body — and mooring
would tell it that it succeeded. These tests pin what the validator catches, and (at
least as importantly) the much longer list of correct notebooks it must stay silent
about: a false positive blocks a good proposal and teaches its reader to ignore every
other diagnostic.

Pure and offline: source in, value-free dataclasses out. Nothing is executed, nothing is
written to disk.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading

import pytest

from mooring import marimo_rt
from mooring.marimo_rt import (
    DIAG_CELL_SYNTAX,
    DIAG_NESTED_CELL,
    DIAG_NOT_A_NOTEBOOK,
    DIAG_TOO_LARGE,
    DIAG_UNRESOLVED_REFERENCE,
    DIAG_VALIDATOR_UNAVAILABLE,
    VALIDATE_LINT_RULES,
    VALIDATE_MAX_CELLS,
    validate_notebook_source,
)

HEAD = 'import marimo\n\n__generated_with = "0.23.9"\napp = marimo.App()\n\n\n'
TAIL = 'if __name__ == "__main__":\n    app.run()\n'

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


def nb(*cells: str) -> str:
    """A marimo notebook whose cells have the given bodies (marimo's own file shape)."""
    parts = [HEAD]
    for code in cells:
        body = "\n".join("    " + line for line in code.splitlines())
        parts.append(f"@app.cell\ndef _():\n{body}\n    return\n\n\n")
    parts.append(TAIL)
    return "".join(parts)


def codes(diagnostics) -> list[str]:
    return [d.code for d in diagnostics]


# --- what it must catch ----------------------------------------------------


def test_a_clean_notebook_has_nothing_to_report():
    source = nb("import polars as pl", "df = pl.DataFrame({'a': [1]})", "rows = df.height")
    assert validate_notebook_source(source) == []


def test_a_variable_defined_in_two_cells_is_marimos_MB002():
    # The realistic weak-model failure: `df = df.filter(...)` where `df` came from
    # another cell. It applies cleanly and then stops the WHOLE notebook.
    source = nb("import polars as pl\ndf = pl.DataFrame()", "df = df.filter(True)")
    found = [d for d in validate_notebook_source(source) if d.code == "MB002"]
    assert len(found) == 1
    assert "df" in found[0].message
    assert len(found[0].lines) == 2  # both defining cells, located in the candidate
    assert found[0].fix  # marimo's own advice is carried through


def test_a_dependency_cycle_is_MB003():
    source = (
        HEAD
        + "@app.cell\ndef _(b):\n    a = b + 1\n    return (a,)\n\n\n"
        + "@app.cell\ndef _(a):\n    b = a + 1\n    return (b,)\n\n\n"
        + TAIL
    )
    assert "MB003" in codes(validate_notebook_source(source))


def test_a_cell_that_does_not_parse_names_the_cell():
    source = nb("x = 1", "def broken(:\n    pass")
    found = validate_notebook_source(source)
    assert codes(found) == [DIAG_CELL_SYNTAX]
    assert "cell 1" in found[0].message
    assert found[0].lines  # located in the candidate source, not inside the cell body


def test_a_cell_syntax_error_is_not_also_reported_by_marimo():
    # marimo's MB001/MB005 restate the same fact per FILE. Saying it twice is exactly
    # the noise that teaches a reader to skim past diagnostics.
    source = nb("x = 1", "def broken(:\n    pass")
    assert "MB001" not in codes(validate_notebook_source(source))


def test_return_at_cell_top_level_is_caught_although_marimo_parses_it():
    # marimo's converter accepts this happily (it becomes a cell that silently
    # disappears from the dependency graph); the per-cell compile is what catches it.
    found = validate_notebook_source(nb("return 5"))
    assert codes(found) == [DIAG_CELL_SYNTAX]
    assert "'return' outside function" in found[0].message


def test_a_name_no_cell_defines_is_reported():
    source = nb("import polars as pl", "out = customer_frame.head()")
    found = [d for d in validate_notebook_source(source) if d.code == DIAG_UNRESOLVED_REFERENCE]
    assert len(found) == 1
    assert "customer_frame" in found[0].message
    assert "cell 1" in found[0].message


def test_app_cell_blocks_pasted_into_a_cell_body():
    # Two @app.cell blocks handed back as ONE cell body: valid Python, nonsense marimo,
    # and marimo's own linter says nothing about it.
    body = (
        "@app.cell\ndef _():\n    x = 1\n    return (x,)\n\n"
        "@app.cell\ndef _(x):\n    y = x + 1\n    return (y,)"
    )
    found = [d for d in validate_notebook_source(nb(body)) if d.code == DIAG_NESTED_CELL]
    assert len(found) == 1
    assert "cell 0" in found[0].message
    assert "one operation per cell" in found[0].fix


def test_a_pasted_app_function_is_caught_by_the_same_check():
    body = "@app.function\ndef helper(x):\n    return x + 1"
    assert DIAG_NESTED_CELL in codes(validate_notebook_source(nb(body)))


def test_a_plain_python_script_is_not_a_notebook():
    found = validate_notebook_source("import os\nprint(os.sep)\n")
    assert codes(found) == [DIAG_NOT_A_NOTEBOOK]


# --- what it must NOT say --------------------------------------------------

# Every entry here is a correct notebook. A diagnostic on any of them is the failure
# mode that matters: it blocks a good proposal.
CORRECT = {
    "builtins": nb("total = len([1, 2, 3])\nprint(total)"),
    "imported-name": nb("import polars as pl", "frame = pl.DataFrame()"),
    "name-from-an-earlier-cell": nb("seed = 1", "total = seed + 1"),
    "name-from-a-later-cell": nb("total = seed + 1", "seed = 1"),
    "underscore-prefixed-cell-local": nb("_tmp = 1\nvalue = _tmp + 1"),
    "comprehension-variable": nb("squares = [n * n for n in range(3)]"),
    "lambda-argument": nb("bump = lambda q: q + 1"),
    "walrus": nb("if (m := 3) > 2:\n    ok = m"),
    "with-as-target": nb("import io", "with io.StringIO() as fh:\n    data = fh.read()"),
    "except-as-target": nb("try:\n    x = 1\nexcept ValueError as err:\n    x = err"),
    "dunder-reference": nb("here = __file__\nwho = __name__"),
    "top-level-await": nb("import asyncio", "res = await asyncio.sleep(0)"),
    "markdown-in-branches": nb(
        "import marimo as mo",
        "ok = True",
        "if ok:\n    mo.md('**passed**')\nelse:\n    mo.md('**failed**')",
    ),
    "match-statement": nb(
        "v = 1", "match v:\n    case 1:\n        out = 'one'\n    case _:\n        out = 'other'"
    ),
    "class-inheritance": nb("class Base:\n    pass", "class Kid(Base):\n    pass"),
    "conditional-import-alias": nb(
        "try:\n    import ujson as J\nexcept ImportError:\n    import json as J",
        "blob = J.dumps({})",
    ),
    "third-party-decorator": nb("import functools", "@functools.cache\ndef g():\n    return 1"),
    "keyword-argument-name": nb("import polars as pl", "d = pl.DataFrame(data={'a': [1]})"),
    "marimo-sql-cell": nb("import marimo as mo", 'q = mo.sql("""SELECT 1""")'),
    "type-annotation": nb("from typing import Any", "def f(x: Any) -> Any:\n    return x"),
    "recursive-function": nb("def fact(n):\n    return 1 if n < 2 else n * fact(n - 1)"),
    "nested-function-local": nb(
        "def outer():\n    z = 1\n\n    def inner():\n        return z\n\n    return inner()"
    ),
    "app-function-at-file-level": (
        HEAD
        + "@app.function\ndef helper(x):\n    return x + 1\n\n\n"
        + "@app.cell\ndef _():\n    y = helper(1)\n    return (y,)\n\n\n"
        + TAIL
    ),
    "setup-cell": (
        'import marimo\n\n__generated_with = "0.23.9"\napp = marimo.App()\n\n'
        "with app.setup:\n    import numpy as np\n\n\n"
        "@app.cell\ndef _():\n    arr = np.array([1])\n    return (arr,)\n\n\n" + TAIL
    ),
    "import-in-the-file-header": (
        'import marimo\nimport os\n\n__generated_with = "0.23.9"\napp = marimo.App()\n\n\n'
        + "@app.cell\ndef _():\n    p = os.sep\n    return (p,)\n\n\n"
        + TAIL
    ),
    "injected-mooring-helper": nb(
        "import mooring_checks as mc", "mc.unique_key(None, ['id'], name='ids')"
    ),
}


@pytest.mark.parametrize("name", sorted(CORRECT))
def test_correct_notebooks_produce_no_diagnostics(name):
    assert validate_notebook_source(CORRECT[name]) == []


def test_a_star_import_never_produces_an_unresolved_reference():
    # marimo drops a star-importing cell from its graph entirely, so the names it binds
    # are invisible — the unresolved check must disqualify itself rather than guess.
    # (marimo separately, and correctly, reports that `import *` is not allowed at all.)
    source = nb("from math import *", "r = sqrt(4)")
    found = validate_notebook_source(source)
    assert DIAG_UNRESOLVED_REFERENCE not in codes(found)
    assert codes(found) == ["MB005"]


def test_an_unparsable_cell_disqualifies_the_unresolved_check():
    # The broken cell's defs are invisible, so `later` would look unresolved.
    source = nb("def broken(:\n    pass", "value = later + 1", "later = 2")
    assert codes(validate_notebook_source(source)) == [DIAG_CELL_SYNTAX]


def test_an_empty_source_reports_nothing():
    assert validate_notebook_source("") == []


# --- mo.sql cells: table names are not Python names ------------------------

# A marimo SQL cell's refs include the TABLES its query reads, because such a cell can
# read a dataframe another cell defines by name. When the table is a real database table
# instead, nothing in the notebook defines it — and it would be reported as an
# unresolved Python name, on a cell mooring's own SQL guide told the model to write.
# marimo only resolves SQL at all once duckdb and sqlglot are importable, so the
# end-to-end tests below skip without them; the unit tests either side of them pin the
# same logic unconditionally.

SQL_NOTEBOOK = nb("import marimo as mo", "res = mo.sql('SELECT * FROM customers')")
SQL_QUALIFIED = nb("import marimo as mo", "res = mo.sql('SELECT * FROM sales.public.orders')")


def inject_sql_refs(monkeypatch, table, *, marimo_itemises_it=True):
    """Make marimo's graph look the way it does once duckdb and sqlglot are importable.

    Neither is a dependency of this repo, so on CI marimo's visitor never reaches its
    SQL branch and a plain end-to-end test would pass for the wrong reason — green on a
    build where the bug is live. This reproduces the one condition that branch creates
    (``marimo/_ast/visitor.py``: ``if has_sqlglot: ... self._add_ref(None, name,
    sql_ref=ref)``) by adding the table to the SQL cell's ``refs``, and recording it in
    ``sql_refs`` exactly as marimo does. ``CellImpl`` is a frozen dataclass, but both
    fields are mutable containers, so no attribute is rebound.

    ``marimo_itemises_it=False`` CLEARS ``sql_refs`` — the negative control, which must
    still produce the diagnostic. Without it these tests could not tell suppression from
    an injection that never fired. Clearing rather than merely not adding matters: where
    duckdb and sqlglot ARE installed, marimo has already itemised the table itself, and
    the control would pass for the wrong reason. Forcing the state makes every
    assertion here mean the same thing on every machine.
    """
    import marimo._lint.context as lint_context

    real = lint_context.LintContext

    class Injecting(real):
        def get_graph(self):
            graph = super().get_graph()
            for cell in graph.cells.values():
                if "mo.sql(" in cell.code:
                    cell.refs.add(table)
                    if marimo_itemises_it:
                        cell.sql_refs[table] = None  # marimo stores an SQLRef here
                    else:
                        cell.sql_refs.clear()
            return graph

    monkeypatch.setattr(lint_context, "LintContext", Injecting)


def test_a_sql_table_name_is_never_an_unresolved_reference(monkeypatch):
    inject_sql_refs(monkeypatch, "customers")
    assert DIAG_UNRESOLVED_REFERENCE not in codes(validate_notebook_source(SQL_NOTEBOOK))


def test_the_injection_really_does_produce_the_diagnostic(monkeypatch):
    # The negative control for the test above: the same table name, not itemised as a
    # SQL ref, IS reported. So the suppression is what silences it — not a fixture that
    # quietly does nothing.
    inject_sql_refs(monkeypatch, "customers", marimo_itemises_it=False)
    found = [d for d in validate_notebook_source(SQL_NOTEBOOK) if d.code == DIAG_UNRESOLVED_REFERENCE]
    assert len(found) == 1
    assert "customers" in found[0].message


def test_a_ref_that_is_not_a_python_identifier_is_never_reported(monkeypatch):
    # The structural backstop, independent of the SQL path: `sales.public.orders` is not
    # a name Python could ever look up, so it cannot be a NameError however it got here.
    # Injected WITHOUT the sql_refs marking, so only the identifier guard can suppress it.
    inject_sql_refs(monkeypatch, "sales.public.orders", marimo_itemises_it=False)
    assert DIAG_UNRESOLVED_REFERENCE not in codes(validate_notebook_source(SQL_QUALIFIED))


@pytest.mark.parametrize("source", [SQL_NOTEBOOK, SQL_QUALIFIED])
def test_sql_table_names_are_never_unresolved_references_for_real(source):
    # The same property against the real marimo visitor, wherever `marimo[sql]` is
    # installed. Skips on this repo's CI, which is why the injected tests above exist.
    pytest.importorskip("duckdb")
    pytest.importorskip("sqlglot")
    assert DIAG_UNRESOLVED_REFERENCE not in codes(validate_notebook_source(source))


def test_a_sql_cells_own_python_refs_are_still_checked():
    # Only the table names are exempt — an f-string interpolating a name that does not
    # exist is still a NameError, and still reported.
    pytest.importorskip("duckdb")
    pytest.importorskip("sqlglot")
    source = nb("import marimo as mo", "res = mo.sql(f'SELECT * FROM t LIMIT {no_such_limit}')")
    found = [d for d in validate_notebook_source(source) if d.code == DIAG_UNRESOLVED_REFERENCE]
    assert len(found) == 1
    assert "no_such_limit" in found[0].message


class _StubCell:
    """Stands in for marimo's CellImpl — only the two attributes the filter reads."""

    def __init__(self, sql_refs=None, language="python"):
        if sql_refs is not None:
            self.sql_refs = sql_refs
        self.language = language


def test_sql_refs_reported_by_marimo_are_subtracted():
    cell = _StubCell(sql_refs={"customers", "sales.public.orders"}, language="sql")
    assert marimo_rt._sql_names(cell, "res = mo.sql('SELECT 1')") == {
        "customers",
        "sales.public.orders",
    }


def test_a_sql_cell_marimo_cannot_itemise_disqualifies_the_whole_cell():
    # A future marimo that drops `sql_refs` must cost the check, not the caller's trust.
    cell = _StubCell(language="sql")
    assert marimo_rt._sql_names(cell, "res = mo.sql('SELECT * FROM customers')") is None
    # ...and the code-shape fallback catches it even if `language` goes too.
    assert marimo_rt._sql_names(_StubCell(), "res = mo.sql('SELECT * FROM t')") is None
    assert marimo_rt._sql_names(_StubCell(), "df = pl.DataFrame()") == set()


@pytest.mark.parametrize(
    "code",
    [
        "res = mo.sql('SELECT 1')",
        "res = marimo.sql('SELECT 1')",
        "res = duckdb.sql('SELECT 1')",
        "res = duckdb.execute('SELECT 1')",
        "res = mo.sql(f'SELECT {n}')",
    ],
)
def test_marimos_sql_call_shapes_are_all_recognised(code):
    # Mirrors marimo's own `valid_sql_calls` list in _ast/visitor.py.
    assert marimo_rt._looks_like_a_sql_cell(code)


@pytest.mark.parametrize(
    "code", ["df = pl.DataFrame()", "total = len([1])", "res = conn.sql('SELECT 1', extra=2)"]
)
def test_ordinary_cells_are_not_mistaken_for_sql(code):
    assert not marimo_rt._looks_like_a_sql_cell(code)


# --- the properties that must hold by construction -------------------------


def test_it_never_executes_the_notebook(tmp_path):
    sentinel = tmp_path / "the-cell-ran"
    source = nb(
        "import pathlib",
        f"pathlib.Path({str(sentinel)!r}).write_text('ran', 'utf-8')",
        "df = 1",
        "df = 2",  # forces a diagnostic, so the checks really do run
    )
    assert validate_notebook_source(source)  # it found something
    assert not sentinel.exists()


def test_it_writes_nothing_beside_the_notebook(tmp_path):
    # marimo's `collect_messages` wants file patterns; the validator drives the rule
    # engine in memory instead, so a candidate never reaches the synced tree (where it
    # would trip the file watcher and the sync engine).
    notebook = tmp_path / "analysis.py"
    notebook.write_text(nb("df = 1", "df = 2"), "utf-8")
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}

    assert validate_notebook_source(notebook.read_text("utf-8"))

    assert {p.name: p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before


def test_diagnostics_carry_no_notebook_content():
    # Two different strengths in one assertion. For mooring's own MOOR codes this is
    # structural — every field is built in marimo_rt from a code, an index, a line
    # number, an identifier or authored text, and `_syntax_detail` reads
    # `SyntaxError.msg` and never `.text`. For marimo's MB codes it is a behavioural
    # check against the pinned marimo: its `message`/`fix` are forwarded verbatim, so
    # this test is what would notice a future rule starting to quote the offending line
    # the way ruff's messages do. A caller sending diagnostics out of the workspace
    # still scrubs them through ai/egress.py; this is not a substitute for that.
    sources = [
        nb(f"token = '{SECRET}'", f"token = '{SECRET}' + '!'"),  # duplicate definition
        nb(f"token = '{SECRET}' +"),  # syntax error on the secret's own line
        nb(f"token = missing_name + '{SECRET}'"),  # unresolved reference
        nb(f"@app.cell\ndef _():\n    token = '{SECRET}'\n    return (token,)"),  # nested
        f"token = '{SECRET}'\n",  # not a notebook at all
    ]
    for source in sources:
        found = validate_notebook_source(source)
        assert found, "expected a diagnostic so the assertion below is meaningful"
        for d in found:
            assert SECRET not in d.code + d.name + d.message + d.fix


def test_the_lint_allowlist_is_pinned_to_marimos_breaking_rules():
    # An allowlist, not "everything at severity X": a marimo upgrade that adds a chatty
    # new rule must not start rejecting valid proposals behind our back. If marimo
    # renames, drops or reclassifies one of these, this test says so.
    from marimo._lint.rules import RULE_CODES

    for code in VALIDATE_LINT_RULES:
        assert code in RULE_CODES, f"marimo no longer has {code}"
        assert RULE_CODES[code]().severity.value == "breaking", f"{code} is no longer breaking"


def test_the_noisy_rules_stay_excluded():
    # MR002 fires on `if ok: mo.md(...) else: mo.md(...)` — ordinary correct code.
    # MF005 cannot fire on this path at all (it needs duckdb plus marimo's own SQL
    # dependency analysis, which a pure IR parse never runs).
    from marimo._lint.rules import RULE_CODES

    for code in ("MR002", "MF005"):
        assert code in RULE_CODES, "the exclusion is only meaningful while marimo has it"
        assert code not in VALIDATE_LINT_RULES


def test_the_result_is_stable_across_runs():
    # marimo runs its rules concurrently, so the raw order varies; a report that
    # reshuffles between calls is unusable for diffing or caching.
    source = nb("import polars as pl\ndf = pl.DataFrame()", "df = df.filter(True)", "n = df.height")
    first = validate_notebook_source(source)
    assert all(validate_notebook_source(source) == first for _ in range(3))


def test_it_works_inside_a_running_event_loop():
    # The hub calls this from an async request handler, where marimo's own
    # `asyncio.run` would raise "cannot be called from a running event loop".
    async def main():
        return validate_notebook_source(nb("df = 1", "df = 2"))

    assert "MB002" in codes(asyncio.run(main()))


# --- degradation: it must never raise --------------------------------------


@pytest.mark.parametrize(
    "source",
    ["", "!!! not python (((", "\x00\x01\x02", "# just a comment\n", '"""a docstring"""\n'],
)
def test_garbage_returns_instead_of_raising(source):
    assert isinstance(validate_notebook_source(source), list)


def test_a_missing_marimo_codegen_api_degrades_to_one_diagnostic(monkeypatch):
    def boom():
        raise marimo_rt.MarimoTransportError("marimo codegen API unavailable: moved")

    monkeypatch.setattr(marimo_rt, "_codegen_api", boom)
    found = validate_notebook_source(nb("x = 1"))
    assert codes(found) == [DIAG_VALIDATOR_UNAVAILABLE]
    # It says the CHECKER failed, not that the notebook is wrong.
    assert "could not statically validate" in found[0].message


def test_a_too_old_marimo_degrades_to_one_diagnostic(monkeypatch):
    def boom():
        raise marimo_rt.MarimoTooOld("mooring requires marimo>=0.23.9, found 0.1.0")

    monkeypatch.setattr(marimo_rt, "_require_marimo_floor", boom)
    found = validate_notebook_source(nb("x = 1"))
    assert codes(found) == [DIAG_VALIDATOR_UNAVAILABLE]
    assert marimo_rt.MARIMO_FLOOR_STR in found[0].fix


def test_concurrent_validations_never_steal_stdout_or_stderr():
    # marimo wraps every cell compile in `contextlib.redirect_stdout`/`redirect_stderr`,
    # which is process-global and not reentrant. Two overlapping passes interleave the
    # save/restore and leave sys.stdout/sys.stderr pointing at leaked StringIO buffers
    # for the rest of the process's life — every print and warning silently goes dead,
    # with nothing raised and nothing logged. Reproduced 100% of the time at four
    # concurrent calls before the validator was serialized.
    before_out, before_err = sys.stdout, sys.stderr
    source = nb("df = 1", "df = 2")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: validate_notebook_source(source), range(8)))

    assert sys.stdout is before_out
    assert sys.stderr is before_err
    assert all(codes(r) == ["MB002"] for r in results), "every caller still gets its answer"


def test_a_pass_that_overruns_is_abandoned_rather_than_waited_on(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(source):
        started.set()
        release.wait(30)
        return []

    monkeypatch.setattr(marimo_rt, "_validate_notebook_source", slow)
    monkeypatch.setattr(marimo_rt, "VALIDATE_TIMEOUT_SECONDS", 0.05)

    found = validate_notebook_source(nb("x = 1"))
    assert codes(found) == [DIAG_VALIDATOR_UNAVAILABLE]
    assert "did not finish" in found[0].message
    assert started.is_set()

    # The abandoned pass still owns marimo's output redirect, so until it finishes
    # nobody else may start one — that is what stops the timeout reintroducing the
    # interleaving the lock was added to prevent.
    blocked = validate_notebook_source(nb("x = 1"))
    assert codes(blocked) == [DIAG_VALIDATOR_UNAVAILABLE]
    assert "still running" in blocked[0].message

    release.set()
    orphan = marimo_rt._VALIDATE_ORPHAN
    assert orphan is not None
    orphan.join(10)
    assert not orphan.is_alive(), "the abandoned pass finishes on its own"

    monkeypatch.undo()
    assert "MB002" in codes(validate_notebook_source(nb("df = 1", "df = 2"))), "recovers fully"


def test_a_notebook_over_the_cell_ceiling_is_declined_not_cleared():
    # Declined, never silently "clean": marimo's multiple-definitions rule is quadratic
    # in colliding definitions, so an unbounded pass is a hang waiting to happen.
    source = nb(*[f"v{i} = {i}" for i in range(VALIDATE_MAX_CELLS + 1)])
    found = validate_notebook_source(source)
    assert codes(found) == [DIAG_TOO_LARGE]
    assert "has NOT been checked" in found[0].message


def test_a_notebook_at_the_cell_ceiling_is_still_checked():
    source = nb(*[f"v{i} = {i}" for i in range(VALIDATE_MAX_CELLS)])
    assert validate_notebook_source(source) == []


def test_a_notebook_over_the_byte_ceiling_is_declined():
    found = validate_notebook_source(nb("x = 1", "y = '" + "a" * 600_000 + "'"))
    assert codes(found) == [DIAG_TOO_LARGE]


def test_a_broken_graph_costs_only_the_unresolved_check(monkeypatch):
    # The unresolved check leans hardest on marimo internals. If they move, the other
    # three checks must survive — losing an extra is not losing the validator.
    import marimo._lint.context as lint_context

    class Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("LintContext moved")

    monkeypatch.setattr(lint_context, "LintContext", Boom)
    source = nb("import polars as pl\ndf = pl.DataFrame()", "df = df.filter(True)")
    assert codes(validate_notebook_source(source)) == ["MB002"]
