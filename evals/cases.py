"""The case registry — six buckets, each isolating one known failure driver.

A bucket is not a topic, it is a CAUSE. The point of splitting them is that
"the copilot is bad on this model" is useless and "this model emits valid cells
but redefines names other cells own" is actionable — the analyst can keep using
it for appends and stop asking it to edit. So each case is written to fail for
ONE reason, and cases that would fail for two reasons at once were split.

The buckets:

``format``  Does a proposal come back at all, in the body-only shape mooring
            writes? The floor: a model that answers a code request with a prose
            code fence has produced nothing the analyst can apply.
``tool``    There is ONE write tool, so this is no longer "did it pick the right
            tool". It is two things the consolidation made the real question: does
            the change go in the right FIELD (``edits`` rather than ``appends``),
            and can the model state what it believes is at the index it is aiming
            at (``expect``)? A wrong or missing ``expect`` is refused outright, so a
            model that guesses indices instead of reading emits nothing at all.
``dag``     marimo is a dataflow graph, not a script. Asked to change a value a
            cell already defines, does it edit that cell, or redefine the name and
            break the notebook (``MB002 multiple-definitions``)?
``schema``  Does it use the columns it was actually shown — right names, right
            case — and the dataframe library it was told about?
``sql``     Can it author a valid, READ-ONLY ``mo.sql`` cell?
``repair``  Handed a diagnostic, does it fix the problem within two turns? This
            bucket measures the propose gate itself: everything else scores what
            the model produces, this scores whether telling it what is wrong helps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from evals.checks import (
    Check,
    at_least_cells,
    binds_name,
    body_only,
    called_tool,
    candidate_clean,
    cell_contains,
    cells_parse,
    columns_in_schema,
    deletes_cell,
    edits_a_cell,
    edits_cell,
    if_proposed,
    no_diagnostic,
    not_a_rewrite,
    polars_api,
    proposed,
    sql_cell,
    sql_no_pivot,
    sql_read_only,
    validates_clean,
    within_turns,
)


@dataclass(frozen=True)
class Case:
    """One prompt against one fixture, with the checks that decide the verdict."""

    id: str
    bucket: str
    notebook: str  # a key in evals.fixtures.NOTEBOOKS
    turns: tuple[str, ...]
    checks: tuple[Check, ...]
    # Turns the runner MAY spend. Extra turns are filled with the neutral nudge in
    # evals.harness.RETRY_TURN; a run stops early the moment a proposal validates.
    max_turns: int = 1
    # Per-case AiConfig overrides, for a case that needs a non-default feature on.
    ai_config: dict = field(default_factory=dict)


# The shape every proposal must have, whatever it was asked for: something came
# back, it is a cell body and not a pasted wrapper, it parses, and the notebook it
# produces still works.
WELL_FORMED: tuple[Check, ...] = (proposed(), body_only(), cells_parse(), validates_clean())


CASES: tuple[Case, ...] = (
    # -- format ---------------------------------------------------------------
    Case(
        id="format/append-cell",
        bucket="format",
        notebook="sales",
        turns=("Add a cell that shows how many rows are in `df`.",),
        checks=WELL_FORMED,
    ),
    Case(
        id="format/body-only",
        bucket="format",
        notebook="sales",
        turns=(
            "Add a cell that computes the total of the amount column and displays it.",
        ),
        checks=WELL_FORMED + (columns_in_schema(),),
    ),
    Case(
        id="format/markdown-cell",
        bucket="format",
        notebook="sales",
        turns=(
            "Add a markdown cell with the heading 'Sales summary' and one sentence "
            "describing what this notebook does.",
        ),
        checks=WELL_FORMED + (cell_contains("mo.md"),),
    ),
    # Phrased as a question, which is what tempts a weaker model into answering in
    # prose with a ```python fence instead of calling a propose tool.
    Case(
        id="format/no-prose-fence",
        bucket="format",
        notebook="sales",
        turns=("How would I add a column holding the amount doubled? Show me the code.",),
        checks=WELL_FORMED,
    ),
    Case(
        id="format/multi-cell",
        bucket="format",
        notebook="sales",
        turns=(
            "Add two new cells: one that filters `df` to the north region, and one "
            "that shows the row count of that filtered frame.",
        ),
        checks=WELL_FORMED + (at_least_cells(2), columns_in_schema()),
    ),
    # -- tool choice ----------------------------------------------------------
    Case(
        id="tool/fix-typo-in-cell",
        bucket="tool",
        notebook="typo",
        turns=(
            "Cell 2 has a typo: it aggregates a column called `amont`, but the real "
            "column is `amount`. Fix that cell.",
        ),
        checks=WELL_FORMED + (edits_cell(2), columns_in_schema()),
    ),
    Case(
        id="tool/delete-dead-cell",
        bucket="tool",
        notebook="typo",
        turns=("Cell 3 defines `unused_scratch`, which nothing uses. Remove it.",),
        checks=(proposed(), deletes_cell(3), validates_clean()),
    ),
    Case(
        id="tool/edit-not-append",
        bucket="tool",
        notebook="threshold",
        turns=("The notebook filters on THRESHOLD. Make the threshold 500 instead.",),
        checks=WELL_FORMED + (edits_a_cell(), no_diagnostic("MB002")),
    ),
    Case(
        id="tool/not-a-rewrite",
        bucket="tool",
        notebook="threshold",
        turns=("Change the filter so it uses >= instead of >.",),
        checks=WELL_FORMED + (not_a_rewrite(), edits_a_cell()),
    ),
    Case(
        id="tool/rename-variable",
        bucket="tool",
        notebook="rename",
        turns=("`df2` is a bad name. Rename it to `north_sales` everywhere it is used.",),
        checks=WELL_FORMED + (edits_a_cell(), binds_name("north_sales")),
    ),
    # No cell index in the prompt: the model has to find the cell itself, which is
    # what mooring_read_notebook_source is for.
    Case(
        id="tool/find-the-cell",
        bucket="tool",
        notebook="sales",
        turns=(
            "Update the cell that builds `summary` so it describes only the amount "
            "column instead of the whole frame.",
        ),
        checks=WELL_FORMED + (edits_a_cell(), columns_in_schema()),
    ),
    # The `expect` discipline, isolated. Five near-identical pipeline steps, and the
    # prompt names the variable rather than the index — so a model that GUESSES which
    # cell holds `step2` writes an `expect` that names a cell it never meant, and the
    # change is refused before anything reaches the analyst. Nothing here can be got
    # right by luck: an off-by-one fails exactly as a wild guess does.
    Case(
        id="tool/expect-must-match",
        bucket="tool",
        notebook="stack",
        turns=(
            "Change the cell that defines `step2` so it takes the last 3 rows "
            "instead of the last 5.",
        ),
        checks=WELL_FORMED + (edits_cell(3),),
        max_turns=2,
    ),
    # -- DAG hygiene ----------------------------------------------------------
    # The single commonest way a copilot breaks a marimo notebook: `df = df.filter(...)`
    # in a NEW cell, when another cell already defines `df`.
    Case(
        id="dag/filter-without-redefining",
        bucket="dag",
        notebook="sales",
        turns=("Filter `df` down to the north region and keep the result.",),
        checks=WELL_FORMED + (no_diagnostic("MB002"), columns_in_schema()),
    ),
    Case(
        id="dag/change-a-constant",
        bucket="dag",
        notebook="threshold",
        turns=("THRESHOLD should be 250, not 100.",),
        checks=WELL_FORMED + (edits_a_cell(), no_diagnostic("MB002")),
    ),
    Case(
        id="dag/no-duplicate-import",
        bucket="dag",
        notebook="sales",
        turns=("Add a cell that uses polars to compute the mean amount.",),
        checks=WELL_FORMED + (no_diagnostic("MB002"), columns_in_schema()),
    ),
    # Asks for something that WOULD be a cycle. Declining is a correct answer, so
    # the checks are conditional — what fails is proposing the cycle anyway.
    Case(
        id="dag/no-cycle",
        bucket="dag",
        notebook="sales",
        turns=(
            "Add a cell that computes `grand_total` from `summary`, and change the "
            "`summary` cell so it also uses `grand_total`.",
        ),
        checks=(if_proposed(no_diagnostic("MB003")), if_proposed(validates_clean())),
    ),
    # The notebook is ALREADY broken (two cells define `totals`). A pre-existing
    # fault is not the model's to fix before it may propose anything else, so this
    # case must pass — it is the regression net for the gate's `_already_broken`.
    Case(
        id="dag/works-on-a-broken-notebook",
        bucket="dag",
        notebook="already_broken",
        turns=("Add a cell that shows how many rows are in `df`.",),
        checks=WELL_FORMED,
    ),
    Case(
        id="dag/fix-the-duplicate",
        bucket="dag",
        notebook="already_broken",
        turns=(
            "Two cells both define `totals`, which breaks this notebook. Fix it so "
            "each name is defined exactly once.",
        ),
        checks=(proposed(), body_only(), cells_parse(), candidate_clean()),
        max_turns=2,
    ),
    # -- schema fidelity ------------------------------------------------------
    Case(
        id="schema/only-known-columns",
        bucket="schema",
        notebook="sales",
        turns=("Total the amount by region.",),
        checks=WELL_FORMED + (columns_in_schema(),),
    ),
    # Neither `revenue` nor `segment` exists. Saying so is a correct answer; making
    # them up is not.
    Case(
        id="schema/no-invented-column",
        bucket="schema",
        notebook="sales",
        turns=("Show me total revenue per customer segment.",),
        checks=(if_proposed(columns_in_schema()), if_proposed(validates_clean())),
    ),
    # The prompt says "amount"; the schema says `Amount`. Only reading the schema
    # gets this right — no amount of care in the prompt does.
    Case(
        id="schema/case-sensitive-columns",
        bucket="schema",
        notebook="ledger",
        turns=("Sum the amount column by account.",),
        checks=WELL_FORMED + (columns_in_schema(),),
    ),
    # The second dataset is not loaded in the notebook, so its columns are reachable
    # only through mooring_get_schema.
    Case(
        id="schema/second-dataset",
        bucket="schema",
        notebook="join",
        turns=(
            "Join `sales_df` to the regions dataset in this workspace and show the "
            "result.",
        ),
        checks=WELL_FORMED + (called_tool("mooring_get_schema"), columns_in_schema()),
    ),
    Case(
        id="schema/polars-not-pandas",
        bucket="schema",
        notebook="sales",
        turns=("Group the data by region and sum the amount.",),
        checks=WELL_FORMED + (polars_api(), columns_in_schema()),
    ),
    # -- sql cells ------------------------------------------------------------
    Case(
        id="sql/basic-select",
        bucket="sql",
        notebook="sales",
        turns=("Use a SQL cell to count the rows in `df` by region.",),
        checks=WELL_FORMED + (sql_cell(), sql_read_only()),
    ),
    # Phrased as a mutation. A SELECT that keeps the rows is right; a DELETE is not.
    Case(
        id="sql/read-only",
        bucket="sql",
        notebook="sales",
        turns=("Use SQL to get rid of the rows where amount is below 50.",),
        checks=(if_proposed(sql_read_only()), if_proposed(validates_clean())),
    ),
    # A pivot would name output columns after row VALUES — the one SQL construct
    # that can smuggle data into the value-blind schema channel.
    Case(
        id="sql/no-pivot",
        bucket="sql",
        notebook="sales",
        turns=("Use SQL to show the amounts with one column per region.",),
        checks=(if_proposed(sql_no_pivot()), if_proposed(validates_clean())),
    ),
    # This notebook has no `import marimo as mo`, which mo.sql needs.
    Case(
        id="sql/brings-the-import",
        bucket="sql",
        notebook="no_marimo",
        turns=("Add a SQL cell that selects the first 5 rows of `df`.",),
        checks=WELL_FORMED + (sql_cell(), binds_name("mo")),
    ),
    # -- repair ---------------------------------------------------------------
    # Asks for the colliding name on purpose. The gate refuses the first proposal
    # with MB002; the question is whether the model can act on that.
    Case(
        id="repair/duplicate-definition",
        bucket="repair",
        notebook="sales",
        turns=("Filter `df` to rows where amount is over 100. Call the result `df`.",),
        checks=WELL_FORMED + (within_turns(2), columns_in_schema()),
        max_turns=2,
    ),
    # Invites the nested-cell corruption (MOOR002): two stacked @app.cell blocks
    # pasted into one cell body.
    Case(
        id="repair/pasted-wrapper",
        bucket="repair",
        notebook="sales",
        turns=(
            "Add a cell that shows the row count. Include the full marimo cell — the "
            "decorator, the def and the return — exactly as it appears in the file.",
        ),
        checks=WELL_FORMED + (within_turns(2),),
        max_turns=2,
    ),
    # The analyst pastes a diagnostic back by hand. Value-free text: a rule code, a
    # rule name and a fix line, exactly as mooring renders one.
    Case(
        id="repair/handed-a-diagnostic",
        bucket="repair",
        notebook="sales",
        turns=(
            "Add a cell that filters `df` to the north region and calls it `df`.",
            "mooring rejected that: [MB002] multiple-definitions — the name `df` is "
            "defined by more than one cell. fix: rename the variable, or edit the cell "
            "that already defines it. Please propose something that works.",
        ),
        checks=WELL_FORMED + (within_turns(2), no_diagnostic("MB002")),
        max_turns=2,
    ),
    Case(
        id="repair/fix-a-bad-column",
        bucket="repair",
        notebook="typo",
        turns=(
            "This notebook is broken: one cell aggregates a column that does not "
            "exist in the data. Fix it.",
        ),
        checks=WELL_FORMED + (within_turns(2), columns_in_schema()),
        max_turns=2,
    ),
    Case(
        id="repair/rewrite-back-to-working",
        bucket="repair",
        notebook="already_broken",
        turns=(
            "Rewrite this notebook so it loads the sales data and produces one "
            "summary per region, with every name defined exactly once.",
        ),
        checks=(proposed(), body_only(), cells_parse(), candidate_clean(), within_turns(2)),
        max_turns=2,
    ),
)


BUCKETS: tuple[str, ...] = ("format", "tool", "dag", "schema", "sql", "repair")


def select(case_ids: tuple[str, ...] = (), buckets: tuple[str, ...] = ()) -> list[Case]:
    """The cases matching the filters (empty filters select everything).

    A ``case_ids`` entry matches a full id (``dag/no-cycle``) or a substring of one
    (``no-cycle``), so a case can be named without its bucket prefix.
    """
    picked = list(CASES)
    if buckets:
        wanted = {b.strip().lower() for b in buckets if b.strip()}
        picked = [c for c in picked if c.bucket in wanted]
    if case_ids:
        terms = [t.strip().lower() for t in case_ids if t.strip()]
        picked = [c for c in picked if any(t == c.id or t in c.id for t in terms)]
    return picked
