"""The model-capability eval's own logic, tested offline.

``evals/`` is not collected by this suite (it needs a model, network and a key —
see the pytest config), but its SCORING is ordinary code and can be ordinary-code
wrong. A predicate that never fires would report a weak model as capable and
nobody would notice, because the only way to catch it would be to run a weak
model. So the harness is driven here against seven real weak-model outputs whose
correct verdict is already known, with no network and no ``openai`` package: the
scripted provider in ``evals/fake.py`` feeds them to a real
:class:`~mooring.ai.openai_session.OpenAIChatSession` through its ``client_factory``
seam, so the real tool loop, the real value-free handlers and the real propose gate
all run.

Two layers, and the second is the one that matters:

* **end to end** — ``run_case`` with the scripted provider. Proves the gate refuses
  a proposal that would break the notebook, and that the harness scores the refusal
  as "nothing reached the analyst".
* **scoring alone** — the same seven outputs turned into an :class:`Attempt`
  directly, past the gate. Proves each check would have caught its output even if
  the gate had let it through, which is exactly what a regression in
  ``ai/tools.py`` would look like and is unreachable end to end.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from evals import checks, fake, harness
from evals.cases import BUCKETS, CASES, Case, select
from evals.fixtures import NOTEBOOKS, cell_count, first_line
from evals.harness import Card, attempt_for, render_card, run_case
from mooring import marimo_rt

# The seven outputs, verbatim in the shape a model emits them. Each is the whole
# `code` argument of one mooring_propose_notebook_edit call, in the flat one-cell
# append form the tool accepts.
CLEAN = 'totals = df.group_by("region").agg(pl.col("amount").sum())\ntotals'
ONE_APP_CELL = (
    "@app.cell\n"
    "def _(df, pl):\n"
    '    totals = df.group_by("region").agg(pl.col("amount").sum())\n'
    "    return (totals,)"
)
TWO_APP_CELLS = ONE_APP_CELL + "\n\n\n@app.cell\ndef _(totals):\n    totals\n    return"
REDEFINES_DF = 'df = df.filter(pl.col("amount") > 100)\ndf'
MARKDOWN_FENCE = (
    "```python\n"
    'totals = df.group_by("region").agg(pl.col("amount").sum())\n'
    "```"
)
TRAILING_RETURN = "totals = df.describe()\nreturn totals"
PANDAS_IDIOM = 'totals = df.groupby("region")["amount"].sum()\ntotals'


@pytest.fixture
def root(tmp_path):
    return tmp_path


def _attempt(root: Path, raw: str, notebook: str = "sales"):
    """One raw model output, scored as the propose tools would present it.

    ``normalize_cell_code`` is applied because every propose handler applies it —
    unwrapping ONE pasted ``@app.cell`` and stripping ONE trailing return is a
    documented kindness of the tool, so a model that leans on it has not erred, and
    an eval that scored the raw string would fail outputs mooring accepts.
    """
    code = marimo_rt.normalize_cell_code(raw)
    return attempt_for(notebook, [{"code": code, "rationale": ""}], root)


def _fired(attempt, *check_list) -> dict[str, str]:
    return {c.name: c.run(attempt) for c in check_list}


# -- layer 2: the scoring, with the gate out of the way -----------------------


def test_1_a_clean_cell_body_passes_every_check(root):
    a = _attempt(root, CLEAN)
    verdicts = _fired(
        a,
        checks.proposed(),
        checks.body_only(),
        checks.cells_parse(),
        checks.validates_clean(),
        checks.columns_in_schema(),
        checks.polars_api(),
    )
    assert verdicts == {name: "" for name in verdicts}, verdicts


def test_2_one_pasted_app_cell_is_unwrapped_and_passes(root):
    # The normaliser unwraps it, so what mooring would WRITE is a clean body. A
    # model that pastes the wrapper it was shown has cost the analyst nothing.
    a = _attempt(root, ONE_APP_CELL)
    assert a.proposed_cells() == [
        ("the new cell", 'totals = df.group_by("region").agg(pl.col("amount").sum())')
    ]
    verdicts = _fired(a, checks.body_only(), checks.cells_parse(), checks.validates_clean())
    assert verdicts == {name: "" for name in verdicts}, verdicts


def test_3_two_stacked_app_cells_fail_as_nested_cell_corruption(root):
    # `_unwrap_app_cell` only unwraps a body that is EXACTLY one decorated def, so
    # two stacked blocks survive into the cell — valid Python, nonsense marimo.
    a = _attempt(root, TWO_APP_CELLS)
    assert checks.cells_parse().run(a) == "", "it does parse; that is the trap"
    assert "@app.cell" in checks.body_only().run(a)
    broken = checks.validates_clean().run(a)
    assert marimo_rt.DIAG_NESTED_CELL in broken, broken


def test_4_redefining_a_name_another_cell_owns_fails_as_mb002(root):
    a = _attempt(root, REDEFINES_DF)
    assert checks.body_only().run(a) == ""
    assert checks.cells_parse().run(a) == ""
    assert "MB002" in checks.validates_clean().run(a)
    assert "MB002" in checks.no_diagnostic("MB002").run(a)


def test_5_a_markdown_fence_fails_to_parse(root):
    a = _attempt(root, MARKDOWN_FENCE)
    assert "does not parse" in checks.cells_parse().run(a)
    # It cannot be applied at all, so there is no candidate to validate — reported
    # as a fault of the proposal, never as a clean pass.
    assert a.candidate() is None
    assert checks.validates_clean().run(a)


def test_6_a_trailing_return_is_stripped_and_passes(root):
    a = _attempt(root, TRAILING_RETURN)
    assert a.proposed_cells() == [("the new cell", "totals = df.describe()")]
    verdicts = _fired(a, checks.body_only(), checks.cells_parse(), checks.validates_clean())
    assert verdicts == {name: "" for name in verdicts}, verdicts


def test_7_a_pandas_idiom_passes_the_static_check_and_fails_on_the_api(root):
    """The call on case 7, pinned.

    ``df.groupby("x")["y"].sum()`` is valid Python, composes into a valid marimo
    notebook and validates clean — it only raises ``AttributeError`` when the cell
    RUNS, which a static eval never does. Two honest verdicts rather than one
    dishonest one: the structural checks pass (they measured structure, and the
    structure is fine) and a separate named check fails on the API. Folding it into
    a syntax verdict would lie about which mechanism caught it; calling the whole
    thing a pass would hide a cell that breaks on first run.
    """
    a = _attempt(root, PANDAS_IDIOM)
    structural = _fired(
        a,
        checks.body_only(),
        checks.cells_parse(),
        checks.validates_clean(),
        checks.columns_in_schema(),  # `region` and `amount` are both real columns
    )
    assert structural == {name: "" for name in structural}, structural
    assert ".groupby" in checks.polars_api().run(a)


# -- layer 1: the same seven, end to end through the real propose gate --------

_END_TO_END = {
    CLEAN: True,
    ONE_APP_CELL: True,
    TRAILING_RETURN: True,
    TWO_APP_CELLS: False,
    REDEFINES_DF: False,
    MARKDOWN_FENCE: False,
    PANDAS_IDIOM: True,  # structurally fine — the API miss is the schema bucket's
}


@pytest.mark.parametrize("raw,expected", list(_END_TO_END.items()))
def test_the_gate_and_the_harness_agree_on_all_seven(root, raw, expected):
    case = Case(
        id="format/append-cell",
        bucket="format",
        notebook="sales",
        turns=("Add a cell that totals the amount by region.",),
        checks=(
            checks.proposed(),
            checks.body_only(),
            checks.cells_parse(),
            checks.validates_clean(),
        ),
    )
    opener = fake.scripted_opener(
        {case.id: [fake.propose_cell(raw), fake.Say("Done.")]}
    )
    result = run_case(case, opener, root=root, turn_timeout=30)
    assert result.passed is expected, [(f.check, f.reason) for f in result.failures]
    if not expected:
        # Nothing reached the analyst: the gate held it back and said why.
        assert result.proposals == 0
        assert result.refusals == 1
        assert "proposed" in {f.check for f in result.failures}


def test_a_model_that_only_talks_produces_no_proposal(root):
    case = CASES[0]
    opener = fake.scripted_opener({case.id: [fake.Say("Here is the code:\n```python\nx=1\n```")]})
    result = run_case(case, opener, root=root, turn_timeout=30)
    assert not result.passed
    assert result.proposals == 0 and result.refusals == 0
    assert "answered in prose" in result.failures[0].reason


def test_a_second_turn_can_rescue_a_refused_proposal(root):
    """The repair bucket's mechanism: the gate refuses, the model corrects itself,
    and the harness scores the CORRECTION rather than the first attempt."""
    case = next(c for c in CASES if c.id == "repair/duplicate-definition")
    opener = fake.scripted_opener(
        {
            case.id: [
                fake.propose_cell(REDEFINES_DF),  # refused: MB002 on `df`
                fake.propose_cell('big = df.filter(pl.col("amount") > 100)\nbig'),
                fake.Say("Renamed it to `big`."),
            ]
        }
    )
    result = run_case(case, opener, root=root, turn_timeout=30)
    assert result.passed, [(f.check, f.reason) for f in result.failures]
    assert result.refusals == 1  # the gate DID fire
    assert result.proposals == 1  # and only the corrected one reached the analyst
    assert result.turns_used == 1  # recovered in-loop, without spending a turn


def test_a_model_that_never_recovers_fails_the_repair_case(root):
    case = next(c for c in CASES if c.id == "repair/duplicate-definition")
    opener = fake.scripted_opener(
        {case.id: [fake.propose_cell(REDEFINES_DF), fake.propose_cell(REDEFINES_DF)]}
    )
    result = run_case(case, opener, root=root, turn_timeout=30)
    assert not result.passed
    assert result.refusals >= 2 and result.proposals == 0
    assert result.turns_used == case.max_turns  # it spent every turn it was given


# -- the tool-choice and schema predicates ------------------------------------


def test_an_append_is_not_an_edit(root):
    case = next(c for c in CASES if c.id == "tool/edit-not-append")
    appended = fake.scripted_opener(
        {case.id: [fake.propose_cell("THRESHOLD_NEW = 500"), fake.Say("Added.")]}
    )
    result = run_case(case, appended, root=root, turn_timeout=30)
    assert not result.passed
    # The reason names the FIELD it reached for, which is the actionable half now
    # that there is only one tool to reach for.
    reason = dict((f.check, f.reason) for f in result.failures)["edits-a-cell"]
    assert "'appends'" in reason and "instead of editing" in reason

    edited = fake.scripted_opener(
        {
            case.id: [
                fake.propose_cell_edit(2, first_line("threshold", 2), "THRESHOLD = 500"),
                fake.Say("Edited."),
            ]
        }
    )
    assert run_case(case, edited, root=root, turn_timeout=30).passed


def test_the_right_cell_index_is_required(root):
    """Editing the WRONG cell, but honestly: the expect names cell 3 and cell 3 is
    what it says, so the change is emitted and the index check is what catches it."""
    case = next(c for c in CASES if c.id == "tool/fix-typo-in-cell")
    wrong = fake.scripted_opener(
        {
            case.id: [
                fake.propose_cell_edit(3, first_line("typo", 3), "unused_scratch = 2"),
                fake.Say("Done."),
            ]
        }
    )
    result = run_case(case, wrong, root=root, turn_timeout=30)
    assert not result.passed
    assert "instead of cell 2" in " ".join(f.reason for f in result.failures)


# -- the `expect` claim: the tool bucket's new failure driver ------------------


def test_an_edit_without_an_expect_is_refused(root):
    """``expect`` is the model saying what it believes is at that index. Omitting it
    is not a shape slip the harness should tolerate — mooring refuses the whole
    change, nothing reaches the analyst, and the case must score that as a failure."""
    case = next(c for c in CASES if c.id == "tool/edit-not-append")
    opener = fake.scripted_opener(
        {
            case.id: [
                fake.propose_cell_edit(2, "", "THRESHOLD = 500"),
                fake.Say("Edited."),
            ]
        }
    )
    result = run_case(case, opener, root=root, turn_timeout=30)
    assert not result.passed
    assert result.proposals == 0 and result.refusals == 1
    assert "proposed" in {f.check for f in result.failures}


def test_a_stale_expect_is_refused(root):
    """A model that GUESSES the index instead of reading names a cell it never meant.
    This is the mistake the consolidation made measurable, and it is the whole point
    of `tool/expect-must-match`: `step2` is at index 3, and a guess of 2 is refused."""
    case = next(c for c in CASES if c.id == "tool/expect-must-match")
    guessed = fake.scripted_opener(
        {
            case.id: [
                # Claims step2 is at index 2. It is not; step1 is.
                fake.propose_cell_edit(2, "step2 = step1.tail(5)", "step2 = step1.tail(3)"),
                fake.Say("Done."),
            ]
        }
    )
    result = run_case(case, guessed, root=root, turn_timeout=30)
    assert not result.passed
    assert result.proposals == 0 and result.refusals >= 1

    # And the model that read first lands it.
    read = fake.scripted_opener({case.id: GOLDEN[case.id]})
    assert run_case(case, read, root=root, turn_timeout=30).passed


def test_a_rewrite_must_state_the_cell_count(root):
    case = next(c for c in CASES if c.id == "repair/rewrite-back-to-working")
    cells = ['df = pl.read_csv("data/sales.csv")', 'totals = df.group_by("region").len()']
    wrong = fake.scripted_opener(
        {case.id: [fake.propose_notebook_rewrite(cells, expect_cells=99), fake.Say("Done.")]}
    )
    result = run_case(case, wrong, root=root, turn_timeout=30)
    assert not result.passed
    assert result.proposals == 0 and result.refusals >= 1


def test_an_invented_column_fails_schema_fidelity(root):
    a = attempt_for(
        "sales",
        [{"code": 'out = df.select("customer_segment", "revenue")', "rationale": ""}],
        root,
    )
    reason = checks.columns_in_schema().run(a)
    assert "customer_segment" in reason and "revenue" in reason
    # A column the same cell CREATES is not an invention.
    b = attempt_for(
        "sales",
        [{"code": 'out = df.select((pl.col("amount") * 2).alias("doubled"), "doubled")',
          "rationale": ""}],
        root,
    )
    assert checks.columns_in_schema().run(b) == ""


def test_column_case_matters(root):
    a = attempt_for("ledger", [{"code": 'out = ledger.select("amount")', "rationale": ""}], root)
    assert "amount" in checks.columns_in_schema().run(a)
    b = attempt_for("ledger", [{"code": 'out = ledger.select("Amount")', "rationale": ""}], root)
    assert checks.columns_in_schema().run(b) == ""


def test_sql_checks_read_the_query_not_the_prose(root):
    good = attempt_for(
        "sales",
        [{"code": 'by_region = mo.sql("""SELECT region, count(*) FROM df GROUP BY region""")',
          "rationale": ""}],
        root,
    )
    assert checks.sql_cell().run(good) == ""
    assert checks.sql_read_only().run(good) == ""
    assert checks.sql_no_pivot().run(good) == ""

    unassigned = attempt_for(
        "sales", [{"code": 'mo.sql("""SELECT * FROM df""")', "rationale": ""}], root
    )
    assert "not assigned" in checks.sql_cell().run(unassigned)

    destructive = attempt_for(
        "sales",
        [{"code": 'gone = mo.sql("""DELETE FROM df WHERE amount < 50""")', "rationale": ""}],
        root,
    )
    assert "DELETE" in checks.sql_read_only().run(destructive)

    pivoted = attempt_for(
        "sales",
        [{"code": 'wide = mo.sql("""PIVOT df ON region USING sum(amount)""")', "rationale": ""}],
        root,
    )
    assert checks.sql_no_pivot().run(pivoted)


# -- the pre-existing-fault rule, mirrored from the gate ----------------------


def test_a_pre_existing_fault_is_not_blamed_on_the_proposal(root):
    """``already_broken`` ships with two cells defining `totals`. A clean proposal
    onto it must still pass — the gate makes that distinction (``_already_broken``)
    and if the harness did not, every case on a broken notebook would fail for a
    reason no model could fix."""
    a = attempt_for("already_broken", [{"code": "rows = df.height\nrows", "rationale": ""}], root)
    assert marimo_rt.validate_notebook_source(a.base_source), "the fixture is meant to be broken"
    assert checks.validates_clean().run(a) == ""
    # candidate_clean() is the stricter bar, and it still sees the pre-existing fault.
    assert "MB002" in checks.candidate_clean().run(a)


def test_a_second_collision_of_an_already_duplicated_name_is_blamed(root):
    """Counting, not membership: a THIRD definition of an already-duplicated name
    must read as introduced. A set test would let the existing pair whitelist it."""
    a = attempt_for(
        "already_broken", [{"code": 'totals = df.group_by("amount").len()', "rationale": ""}], root
    )
    assert "MB002" in checks.validates_clean().run(a)


# -- registry sanity ----------------------------------------------------------


def test_every_fixture_notebook_is_a_valid_marimo_notebook(tmp_path):
    """A fixture that does not validate would make every case run against it
    measure the fixture. ``already_broken`` is the one deliberate exception."""
    for name, fixture in NOTEBOOKS.items():
        source = fixture.source()
        assert marimo_rt.read_cells(source), name
        diagnostics = marimo_rt.validate_notebook_source(source)
        if name == "already_broken":
            assert any(d.code == "MB002" for d in diagnostics), name
        else:
            assert diagnostics == [], (name, diagnostics)


def test_every_check_message_is_ascii():
    """No check may return a non-ASCII failure reason.

    A reason is printed to a console and lands in the rendered card, and a Windows
    terminal on cp1252 turns an em dash into a replacement character mid-table.
    STATIC rather than behavioural on purpose: exercising every check's every branch
    would leave the rare messages — the ones only a genuinely broken model triggers —
    unchecked, which is exactly where this last bit. Docstrings are source, not
    output, so only string constants in function BODIES are scanned.
    """
    offenders = []
    for module in (checks, harness):
        path = Path(module.__file__)
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # the docstring
            for statement in body:
                for sub in ast.walk(statement):
                    if (
                        isinstance(sub, ast.Constant)
                        and isinstance(sub.value, str)
                        and not sub.value.isascii()
                    ):
                        offenders.append(f"{path.name}:{sub.lineno}: {sub.value[:60]!r}")
    assert not offenders, "non-ASCII in emitted text:\n" + "\n".join(offenders)


def test_every_case_is_well_formed():
    seen = set()
    for case in CASES:
        assert case.id not in seen, f"duplicate case id {case.id}"
        seen.add(case.id)
        assert case.bucket in BUCKETS, case.id
        assert case.id.startswith(f"{case.bucket}/"), case.id
        assert case.notebook in NOTEBOOKS, case.id
        assert case.turns and all(t.strip() for t in case.turns), case.id
        assert case.checks, case.id
        assert case.max_turns >= len(case.turns), case.id
    assert set(BUCKETS) == {c.bucket for c in CASES}
    for bucket in BUCKETS:
        assert len(select(buckets=(bucket,))) >= 4, bucket


def test_case_filters_select_by_id_and_substring():
    assert [c.id for c in select(case_ids=("dag/no-cycle",))] == ["dag/no-cycle"]
    assert [c.id for c in select(case_ids=("no-cycle",))] == ["dag/no-cycle"]
    assert select(case_ids=("nothing-matches-this",)) == []
    assert len(select()) == len(CASES)


# -- every case is winnable ---------------------------------------------------

# One CORRECT answer per case, as a capable model would give it. This is the
# strongest guarantee in the file: a check that can never be satisfied would report
# every model as incapable and look exactly like a true finding, so each case is
# pinned against an answer that must score a pass. Written against the fixtures in
# evals/fixtures.py — cell indices refer to those.
GOLDEN: dict[str, list] = {
    "format/append-cell": [
        fake.propose_cell("row_count = df.height\nrow_count"),
        fake.Say("Added a row-count cell."),
    ],
    "format/body-only": [
        fake.propose_cell('amount_total = df["amount"].sum()\namount_total'),
        fake.Say("Added the total."),
    ],
    "format/markdown-cell": [
        fake.propose_cell(
            'mo.md("""# Sales summary\n\nLoads the sales extract and describes it.""")'
        ),
        fake.Say("Added the heading."),
    ],
    "format/no-prose-fence": [
        fake.propose_cell(
            'doubled = df.with_columns((pl.col("amount") * 2).alias("amount_doubled"))\ndoubled'
        ),
        fake.Say("Here it is as a cell you can apply."),
    ],
    "format/multi-cell": [
        fake.propose_notebook_edit(
            appends=[
                'north = df.filter(pl.col("region") == "north")',
                "north.height",
            ]
        ),
        fake.Say("Two cells, as asked."),
    ],
    "tool/fix-typo-in-cell": [
        fake.propose_cell_edit(
            2,
            first_line("typo", 2),
            'totals = df.group_by("region").agg(pl.col("amount").sum())\ntotals',
        ),
        fake.Say("Fixed the column name in cell 2."),
    ],
    "tool/delete-dead-cell": [
        fake.propose_notebook_edit(deletes=[{"index": 3, "expect": first_line("typo", 3)}]),
        fake.Say("Removed it."),
    ],
    "tool/edit-not-append": [
        fake.propose_cell_edit(2, first_line("threshold", 2), "THRESHOLD = 500"),
        fake.Say("Edited the cell that defines it."),
    ],
    "tool/not-a-rewrite": [
        fake.propose_cell_edit(
            3,
            first_line("threshold", 3),
            'big = df.filter(pl.col("amount") >= THRESHOLD)\nbig',
        ),
        fake.Say("One cell changed."),
    ],
    "tool/rename-variable": [
        fake.propose_notebook_edit(
            edits=[
                {
                    "index": 2,
                    "expect": first_line("rename", 2),
                    "code": 'north_sales = df.filter(pl.col("region") == "north")',
                },
                {
                    "index": 3,
                    "expect": first_line("rename", 3),
                    "code": "north_sales.height",
                },
            ]
        ),
        fake.Say("Renamed in both cells."),
    ],
    "tool/find-the-cell": [
        fake.read_source(),
        fake.propose_cell_edit(
            2, first_line("sales", 2), 'summary = df.select("amount").describe()\nsummary'
        ),
        fake.Say("That was cell 2."),
    ],
    # A model that READ the notebook finds step2 at index 3, not the 2 a guess lands on.
    "tool/expect-must-match": [
        fake.read_source(),
        fake.propose_cell_edit(3, first_line("stack", 3), "step2 = step1.tail(3)"),
        fake.Say("`step2` is cell 3, not cell 2."),
    ],
    "dag/filter-without-redefining": [
        fake.propose_cell('north_sales = df.filter(pl.col("region") == "north")\nnorth_sales'),
        fake.Say("Kept it under a new name so `df` stays owned by one cell."),
    ],
    "dag/change-a-constant": [
        fake.propose_cell_edit(2, first_line("threshold", 2), "THRESHOLD = 250"),
        fake.Say("Edited the constant in place."),
    ],
    "dag/no-duplicate-import": [
        fake.propose_cell('mean_amount = df["amount"].mean()\nmean_amount'),
        fake.Say("polars is already imported by the first cell."),
    ],
    "dag/no-cycle": [
        fake.propose_cell("grand_total = summary.height\ngrand_total"),
        fake.Say("I left the summary cell alone — using it back would close a cycle."),
    ],
    "dag/works-on-a-broken-notebook": [
        fake.propose_cell("row_count = df.height\nrow_count"),
        fake.Say("Note: two cells already define `totals`, which you may want to fix."),
    ],
    "dag/fix-the-duplicate": [
        fake.propose_notebook_edit(
            edits=[
                {
                    "index": 3,
                    "expect": first_line("already_broken", 3),
                    "code": 'totals_by_product = df.group_by("product").len()',
                }
            ]
        ),
        fake.Say("Renamed the second one."),
    ],
    "schema/only-known-columns": [
        fake.propose_cell(
            'by_region = df.group_by("region").agg(pl.col("amount").sum())\nby_region'
        ),
        fake.Say("Totalled."),
    ],
    "schema/no-invented-column": [
        fake.propose_cell(
            'by_product = df.group_by("product").agg(pl.col("amount").sum())\nby_product'
        ),
        fake.Say("There is no revenue or segment column; this totals amount by product."),
    ],
    "schema/case-sensitive-columns": [
        fake.propose_cell(
            'by_account = ledger.group_by("Account").agg(pl.col("Amount").sum())\nby_account'
        ),
        fake.Say("The columns are capitalised in this file."),
    ],
    "schema/second-dataset": [
        fake.get_schema("data/regions.csv"),
        fake.propose_cell(
            'regions_df = pl.read_csv("data/regions.csv")\n'
            'joined = sales_df.join(regions_df, on="region")\njoined'
        ),
        fake.Say("Joined on region."),
    ],
    "schema/polars-not-pandas": [
        fake.propose_cell(
            'by_region = df.group_by("region").agg(pl.col("amount").sum())\nby_region'
        ),
        fake.Say("polars uses group_by/agg."),
    ],
    "sql/basic-select": [
        fake.propose_cell(
            'rows_by_region = mo.sql("""SELECT region, count(*) AS n FROM df GROUP BY region""")'
        ),
        fake.Say("A read-only SQL cell."),
    ],
    "sql/read-only": [
        fake.propose_cell('kept = mo.sql("""SELECT * FROM df WHERE amount >= 50""")'),
        fake.Say("Selected the rows to keep rather than deleting anything."),
    ],
    "sql/no-pivot": [
        fake.propose_cell(
            'by_region = mo.sql("""SELECT region, sum(amount) AS total FROM df GROUP BY region""")'
        ),
        fake.Say("Long rather than wide — a pivot would turn values into column names."),
    ],
    "sql/brings-the-import": [
        fake.propose_notebook_edit(
            edits=[
                {
                    "index": 0,
                    "expect": first_line("no_marimo", 0),
                    "code": "import marimo as mo\nimport polars as pl",
                }
            ],
            appends=['top5 = mo.sql("""SELECT * FROM df LIMIT 5""")'],
        ),
        fake.Say("mo.sql needs marimo imported, so I added that too."),
    ],
    "repair/duplicate-definition": [
        fake.propose_cell('df_north = df.filter(pl.col("amount") > 100)\ndf_north'),
        fake.Say("Called it `df_north`: another cell already defines `df`."),
    ],
    "repair/pasted-wrapper": [
        fake.propose_cell("row_count = df.height\nrow_count"),
        fake.Say("mooring writes the wrapper itself, so this is the body only."),
    ],
    # Deliberately gets it wrong first: the gate refuses, turn 1 ends with nothing,
    # and the analyst's pasted diagnostic on turn 2 gets it right.
    "repair/handed-a-diagnostic": [
        fake.propose_cell('df = df.filter(pl.col("region") == "north")\ndf'),
        fake.Say("Let me look at that again."),
        fake.propose_cell('north = df.filter(pl.col("region") == "north")\nnorth'),
        fake.Say("Renamed to `north`."),
    ],
    "repair/fix-a-bad-column": [
        fake.read_source(),
        fake.propose_cell_edit(
            2,
            first_line("typo", 2),
            'totals = df.group_by("region").agg(pl.col("amount").sum())\ntotals',
        ),
        fake.Say("`amont` should have been `amount`."),
    ],
    # The one golden that exercises `expect_cells`: a rewrite discards every cell, so
    # the model has to say how many it believes are there.
    "repair/rewrite-back-to-working": [
        fake.propose_notebook_rewrite(
            [
                "import marimo as mo\nimport polars as pl",
                'df = pl.read_csv("data/sales.csv")',
                'totals = df.group_by("region").agg(pl.col("amount").sum())\ntotals',
            ],
            expect_cells=cell_count("already_broken"),
        ),
        fake.Say("One definition per name."),
    ],
}


def test_a_golden_answer_exists_for_every_case():
    assert {c.id for c in CASES} == set(GOLDEN), "every case needs a golden answer"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_every_case_passes_on_a_correct_answer(root, case):
    """No case may be unwinnable. A check that can never be satisfied reports every
    model as incapable and is indistinguishable from a true finding."""
    result = run_case(
        case, fake.scripted_opener({case.id: GOLDEN[case.id]}), root=root, turn_timeout=30
    )
    assert result.passed, [(f.check, f.reason) for f in result.failures]


def test_a_weak_model_fails_nearly_every_case(root):
    """The other half of the goldens: a harness that passes everything is as
    useless as one that fails everything, so a model that only ever emits the same
    colliding cell must score near zero.

    The handful that still pass are the cases where DECLINING is a correct answer
    (``if_proposed``): the gate refused the bad proposal, nothing reached the
    analyst, and those cases have nothing to object to. That is the intended
    reading, not a gap — and pinning the exact set here stops a future
    ``if_proposed`` from being added to a case where silence should have failed.
    """
    weak = fake.scripted_opener({c.id: [fake.propose_cell(REDEFINES_DF)] for c in CASES})
    results = [run_case(c, weak, root=root, turn_timeout=30) for c in CASES]
    passed = {r.case_id for r in results if r.passed}
    vacuous = {
        c.id
        for c in CASES
        if all(check.name.startswith("if-proposed:") for check in c.checks)
    }
    assert passed <= vacuous, f"unexpectedly passed: {sorted(passed - vacuous)}"
    assert len(passed) <= 6, sorted(passed)
    assert sum(r.refusals for r in results) >= len(CASES) - 8


def test_the_card_renders_and_serialises(root):
    case = CASES[0]
    opener = fake.scripted_opener({case.id: [fake.propose_cell(CLEAN), fake.Say("Done.")]})
    results = tuple(run_case(case, opener, root=root, turn_timeout=30) for _ in range(2))
    card = Card(model="scripted", provider="fake", repeat=2, results=results)
    text = render_card(card)
    assert "Capability card" in text and "OVERALL" in text
    assert text.isascii(), "the card must render on a cp1252 console"
    payload = card.as_dict()
    assert payload["runs"] == 2 and payload["rate"] == 1.0
    assert payload["buckets"][0]["bucket"] == "format"
    assert card.as_json().startswith("{")
