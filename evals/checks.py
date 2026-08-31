"""The scoring vocabulary. Every check in here is STATIC: it reads the proposal
the model emitted, composes the notebook that proposal would produce, and looks
at the result. Nothing runs a cell, reads a data value, or asks a model to judge
another model.

The heavy lifting is mooring's own :func:`mooring.marimo_rt.validate_notebook_source`
— the same checker the propose gate uses — so a case's verdict and the copilot's
in-loop diagnostics can never disagree about whether a notebook works.

A check returns ``""`` for a pass and a short human reason for a failure. The
reason is the whole product of a failing run: a capability card that says "50%"
without saying WHAT the model got wrong is a number, not an answer.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from mooring import marimo_rt
from mooring.ai.tools import WRITE_TOOL_NAMES

if TYPE_CHECKING:
    from evals.harness import Attempt

# The gate's own classification, mirrored so a case cannot be stricter than the
# copilot is. MOOR000/MOOR005 mean the CHECKER declined (not that the notebook is
# wrong) and MOOR003 is advisory — marimo's codegen writes a cell's refs into its
# signature, so an unresolved name is a note there and must be a note here too.
NON_BLOCKING = frozenset(
    {
        marimo_rt.DIAG_VALIDATOR_UNAVAILABLE,
        marimo_rt.DIAG_TOO_LARGE,
        marimo_rt.DIAG_UNRESOLVED_REFERENCE,
    }
)


@dataclass(frozen=True)
class Check:
    """One named predicate. ``run`` returns "" to pass, else the failure reason."""

    name: str
    run: Callable[["Attempt"], str]


# -- did anything come back at all -------------------------------------------


def proposed() -> Check:
    """A proposal reached the analyst. The floor under every other check: a model
    that answers a code request in prose has produced nothing to apply."""

    # ASCII only, like every other message a check returns: a reason is printed to a
    # console and asserted `.isascii()` by the suite, and a cp1252 terminal turns an
    # em dash into a replacement character. Pinned by
    # `test_every_check_message_is_ascii`, which is a STATIC scan, so this holds for
    # messages no test happens to trigger.
    def run(a: "Attempt") -> str:
        if a.proposals:
            return ""
        if a.refusals:
            return f"no proposal: {a.refusals} write call(s) were refused by the gate"
        # Membership of WRITE_TOOL_NAMES, not a `mooring_propose` prefix: the same tool
        # is registered as `mooring_edit_notebook` in edit mode, and a prefix test
        # misreported an edit-mode run as "answered in prose" — the exact opposite of
        # what happened.
        if any(name in WRITE_TOOL_NAMES for name in a.tool_calls):
            return "no proposal: the write tool was called but emitted nothing"
        return "no proposal: the model answered in prose without calling the write tool"

    return Check("proposed", run)


def answered() -> Check:
    """The model produced SOMETHING — a proposal, or words.

    The floor under every case where declining is an acceptable answer. Without it
    those cases are passed by a model that emits nothing at all, because each of
    their checks is vacuously true over an empty proposal: no invented column, no
    destructive SQL, no dependency cycle. Silence is not a decline, and an eval that
    credits it as one flatters exactly the weak models it exists to identify.

    Kept separate from :func:`proposed` because the two say different things: this
    is "did it engage", that is "did it produce something applicable". A case that
    genuinely has no correct proposal wants the first and not the second.
    """

    def run(a: "Attempt") -> str:
        if a.proposals or any(r.strip() for r in a.replies):
            return ""
        return "produced nothing at all: no proposal and no reply"

    return Check("answered", run)


def declined_explaining(*terms: str) -> Check:
    """When nothing was proposed, the reply must show it engaged with the CONSTRAINT
    — it mentions at least one of ``terms``.

    Gated behind "proposed nothing", and that gate is what makes a keyword test
    defensible here: it can never fail a model that did the work, so its whole blast
    radius is the population where the eval otherwise cannot tell a reasoned decline
    ("that would close a dependency cycle") from a shrug ("here's some code") or a
    model that simply cannot call tools. ``terms`` are the vocabulary of the
    constraint, never of the request, so an answer that ignored the constraint
    cannot match by accident.

    Deliberately generous: any one term, case-insensitive, anywhere in any reply. A
    decline that matches none of several natural phrasings is not a decline this
    eval is willing to credit — but the terms per case are chosen so that is a very
    small set. It is a heuristic, and the one place the eval reads a model's PROSE;
    see the README.
    """
    wanted = tuple(t.lower() for t in terms)

    def run(a: "Attempt") -> str:
        if a.proposals:
            return ""  # it did the work; there is no decline to justify
        blob = " ".join(a.replies).lower()
        if any(term in blob for term in wanted):
            return ""
        return (
            "proposed nothing and the reply does not explain why "
            f"(expected it to mention one of: {', '.join(wanted)})"
        )

    return Check("declined-explaining", run)


def within_turns(limit: int) -> Check:
    """The proposal landed inside ``limit`` analyst turns."""

    def run(a: "Attempt") -> str:
        if not a.proposals:
            return f"nothing proposed in {a.turns_used} turn(s)"
        if a.first_proposal_turn > limit:
            return f"proposed only on turn {a.first_proposal_turn} (limit {limit})"
        return ""

    return Check(f"within-{limit}-turns", run)


# -- is it the shape a marimo cell has ---------------------------------------

_WRAPPER_MARKERS = (
    (re.compile(r"^\s*@app\.cell", re.M), "an '@app.cell' decorator"),
    (re.compile(r"^\s*def _\(", re.M), "a 'def _(...)' wrapper"),
    (re.compile(r"^\s*return\b", re.M), "a top-level 'return'"),
)


def body_only() -> Check:
    """No ``@app.cell`` / ``def _(...)`` / trailing ``return`` survived into the
    cell body the analyst would apply.

    Checked on the NORMALISED code (what mooring actually writes), not the raw
    model output: unwrapping one pasted ``@app.cell`` and stripping one trailing
    return is a documented kindness of ``normalize_cell_code``, so a model that
    leans on it has not made a mistake. What this catches is the leftovers the
    normaliser cannot clean — two stacked cells, a wrapper it declined to unwrap.
    """

    def run(a: "Attempt") -> str:
        for label, code in a.proposed_cells():
            for pattern, what in _WRAPPER_MARKERS:
                if pattern.search(code):
                    return f"{label} still contains {what}"
        return ""

    return Check("body-only", run)


def cells_parse() -> Check:
    """Every proposed cell body compiles as Python."""

    def run(a: "Attempt") -> str:
        for label, code in a.proposed_cells():
            try:
                compile(code, "<cell>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            except SyntaxError as exc:
                return f"{label} does not parse: {exc.msg}"  # .msg, never .text
        return ""

    return Check("cells-parse", run)


# -- does the notebook it produces actually work ------------------------------


def validates_clean() -> Check:
    """The candidate notebook has no BLOCKING diagnostic the base did not already
    have. Pre-existing faults are the analyst's, not the model's — the gate makes
    that distinction and so must the score, or the ``already_broken`` fixture would
    fail every case run against it for a reason no model could fix."""

    def run(a: "Attempt") -> str:
        if a.last_proposal() is None:
            # NOT "the notebook would break". An `expect` refusal emits nothing, so
            # this is now the common path, and dressing "there was nothing to check"
            # up as a diagnostic would put a fault on the model's card that the
            # notebook does not have. `proposed()` owns the real reason.
            return "nothing proposed, so there is no notebook to check"
        introduced = a.introduced_diagnostics()
        if not introduced:
            return ""
        return "the notebook would break: " + _render(introduced)

    return Check("validates-clean", run)


def candidate_clean() -> Check:
    """The candidate notebook has NO blocking diagnostic at all — not merely none
    the base lacked. For the cases whose whole job is to REPAIR a broken notebook,
    where "introduced nothing new" is not the bar."""

    def run(a: "Attempt") -> str:
        if a.last_proposal() is None:
            return "nothing proposed, so the notebook is still as it was"
        candidate = a.candidate()
        if candidate is None:
            return "the proposal could not be applied to the notebook"
        diagnostics = marimo_rt.validate_notebook_source(candidate)
        found = [d for d in diagnostics if d.code not in NON_BLOCKING]
        return "the notebook is still broken: " + _render(found) if found else ""

    return Check("candidate-clean", run)


def no_diagnostic(code: str) -> Check:
    """No NEWLY introduced diagnostic with this rule code (e.g. ``MB002``).

    Vacuously true when nothing was proposed: a change that was never made cannot
    have introduced anything. ``proposed()`` is what fails in that case, and stating
    the same absence twice buries the one line that explains it.
    """

    def run(a: "Attempt") -> str:
        if a.last_proposal() is None:
            return ""
        hits = [d for d in a.introduced_diagnostics() if d.code == code]
        return f"introduced {code}: " + _render(hits) if hits else ""

    return Check(f"no-{code}", run)


def if_proposed(check: Check) -> Check:
    """``check``, but a vacuous PASS when nothing was proposed.

    For the cases where declining is a correct answer — an impossible column, a
    request that would close a dependency cycle, a SQL mutation. Scoring silence as
    a failure there would train the eval to reward a model that always produces
    something, which is the opposite of what these cases are asking.

    **Never the only thing a case checks.** Every predicate in here is vacuously
    true over an empty proposal, so a case built solely from these combinators is
    passed by a model that emits nothing — which is how four cases once credited a
    model with no tool-calling ability at all with four correct declines. Pair it
    with :func:`answered` (something came back) and, where the case is really about
    a decline, :func:`declined_explaining` (the decline is reasoned). Pinned by
    ``test_a_silent_model_passes_nothing``, which drives every case with an empty
    script, so a case that reintroduces the shape fails in CI rather than in a
    capability card.
    """

    def run(a: "Attempt") -> str:
        return "" if not a.proposals else check.run(a)

    return Check(f"if-proposed:{check.name}", run)


def cell_contains(needle: str) -> Check:
    """Some proposed cell contains ``needle`` (a literal substring)."""

    def run(a: "Attempt") -> str:
        if any(needle in code for _, code in a.proposed_cells()):
            return ""
        return f"no proposed cell contains {needle!r}"

    return Check(f"contains-{needle}", run)


def at_least_cells(count: int) -> Check:
    """The proposal writes at least ``count`` cell bodies (a multi-cell request
    answered with one crammed cell is a different thing from what was asked)."""

    def run(a: "Attempt") -> str:
        written = len(a.proposed_cells())
        if written >= count:
            return ""
        return f"proposed {written} cell(s), expected at least {count}"

    return Check(f"at-least-{count}-cells", run)


def _render(diagnostics) -> str:
    return "; ".join(f"[{d.code}] {d.name}" for d in diagnostics[:3])


# -- did it reach for the right tool -----------------------------------------

# There is ONE write tool now (registered under one of the two names in
# :data:`~mooring.ai.tools.WRITE_TOOL_NAMES`, depending on the session's mode), so a
# tool-choice mistake is no longer a wrong TOOL — it is the wrong FIELD of it. The
# proposal payload's shape names the field the model reached for, so that choice is
# read off the proposal rather than sniffed out of the event stream.
_FIELD_FOR_KIND = {
    "append": "'appends' (a new cell)",
    "edit": "'edits'",
    "patch": "a mixed patch",
    "rewrite": "'cells' (a whole-notebook rewrite)",
}


def edits_a_cell() -> Check:
    """The proposal REPLACES an existing cell rather than adding another one.

    The single most useful signal in the whole eval: asked to change something the
    notebook already does, a capable model edits the cell that does it, and a weak
    one appends a near-duplicate — which then either shadows the original or, more
    often, collides with it (MB002) and breaks the notebook.

    Reaching this check at all means the model got its ``expect`` right: an edit
    whose ``expect`` does not match the cell at that index is refused outright and
    emits nothing, so it fails :func:`proposed` first.
    """

    def run(a: "Attempt") -> str:
        last = a.last_proposal()
        if last is None:
            return "nothing proposed"
        if any(op.get("op") == "edit" for op in a.ops_of(last)):
            return ""
        used = _FIELD_FOR_KIND.get(a.kind_of(last), "an unknown shape")
        return f"used {used} instead of editing the cell that already does this"

    return Check("edits-a-cell", run)


def edits_cell(index: int) -> Check:
    """The proposal edits exactly the cell it was asked to."""

    def run(a: "Attempt") -> str:
        last = a.last_proposal()
        if last is None:
            return "nothing proposed"
        edited = [op.get("index") for op in a.ops_of(last) if op.get("op") == "edit"]
        if index in edited:
            return ""
        if not edited:
            return f"edited no cell (asked for cell {index})"
        return f"edited cell(s) {edited} instead of cell {index}"

    return Check(f"edits-cell-{index}", run)


def deletes_cell(index: int) -> Check:
    """The proposal removes exactly the cell it was asked to."""

    def run(a: "Attempt") -> str:
        last = a.last_proposal()
        if last is None:
            return "nothing proposed"
        gone = [op.get("index") for op in a.ops_of(last) if op.get("op") == "delete"]
        if index in gone:
            return ""
        return f"deleted cell(s) {gone or 'none'} instead of cell {index}"

    return Check(f"deletes-cell-{index}", run)


def not_a_rewrite() -> Check:
    """A targeted change did not turn into a whole-notebook rewrite. A rewrite
    re-runs every cell and loses every cell's identity; the tool description says
    to prefer an edit, so reaching for it anyway is a failure to follow the tools."""

    def run(a: "Attempt") -> str:
        last = a.last_proposal()
        if last is not None and a.kind_of(last) == "rewrite":
            return "rewrote the whole notebook for a targeted change"
        return ""

    return Check("not-a-rewrite", run)


def called_tool(name: str) -> Check:
    """The model used a particular read tool at some point in the case."""

    def run(a: "Attempt") -> str:
        return "" if name in a.tool_calls else f"never called {name}"

    return Check(f"called-{name}", run)


# -- does it use the schema it was given --------------------------------------

# Method names whose STRING arguments name columns. Deliberately a small, closed
# set: a string in one of these positions is a column reference in both polars and
# pandas, and a string anywhere else is not assumed to be one. The extractor is
# built to under-report — a missed reference scores a lenient pass, an invented one
# that IS caught is unambiguous.
_COLUMN_METHODS = frozenset(
    {
        "col", "select", "group_by", "groupby", "agg", "sort", "sort_values",
        "drop", "drop_nulls", "unique", "join", "explode", "melt", "unpivot",
        "n_unique", "value_counts", "over", "filter",
        # `pl.sum("amount")` and friends; the bare `.sum()` form takes no argument,
        # so adding them creates no false positives.
        "sum", "mean", "median", "min", "max", "first", "last", "count", "std", "var",
    }
)
# Keyword arguments of those methods that also name columns.
_COLUMN_KWARGS = frozenset({"on", "left_on", "right_on", "by", "subset", "index", "values"})
# Calls that NAME A NEW column rather than referring to one.
_ALIAS_METHODS = frozenset({"alias", "name"})


def _strings(node: ast.AST) -> set[str]:
    """The string literals in ``node``, seeing through a list/tuple/set literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out: set[str] = set()
        for item in node.elts:
            out |= _strings(item)
        return out
    return set()


def column_names(code: str) -> tuple[set[str], set[str]]:
    """``(referenced, created)`` column names in ``code``, best-effort.

    ``referenced`` must exist in the data; ``created`` is what the cell itself
    names into being (``alias("net")``, ``with_columns(net=...)``, a rename map's
    values), which is why the two are separated rather than netted off here.
    """
    referenced: set[str] = set()
    created: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return referenced, created
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            referenced |= _strings(node.slice)
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr in _ALIAS_METHODS:
            for arg in node.args:
                created |= _strings(arg)
            continue
        if attr == "rename":
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    for key, value in zip(arg.keys, arg.values):
                        referenced |= _strings(key) if key is not None else set()
                        created |= _strings(value)
            continue
        if attr == "with_columns":
            created |= {kw.arg for kw in node.keywords if kw.arg}
        if attr not in _COLUMN_METHODS:
            continue
        for arg in node.args:
            referenced |= _strings(arg)
        for kw in node.keywords:
            if kw.arg in _COLUMN_KWARGS:
                referenced |= _strings(kw.value)
            elif kw.arg:
                created.add(kw.arg)
    return referenced, created


def columns_in_schema() -> Check:
    """Every column the proposal names exists — in a dataset's schema, in the
    notebook it is editing, or in the same cell that just created it.

    This is the hallucination check. It is the only check here that is a heuristic
    rather than a proof (see :func:`column_names`), and it is tuned to under-report:
    a string it cannot confidently place as a column reference is ignored.
    """

    def run(a: "Attempt") -> str:
        allowed = set(a.known_columns)
        referenced: set[str] = set()
        for _, code in a.proposed_cells():
            refs, created = column_names(code)
            referenced |= refs
            allowed |= created
        invented = sorted(referenced - allowed)
        if not invented:
            return ""
        return "columns not in the schema: " + ", ".join(invented[:5])

    return Check("columns-in-schema", run)


# pandas spellings with a DIFFERENT polars spelling. Every entry is a name that
# does not exist on a polars DataFrame, so a hit is a real API error rather than a
# style opinion — which is the only reason a check this blunt is allowed in here.
_PANDAS_ONLY = {
    "groupby": "group_by",
    "iloc": "row()/slice",
    "loc": "filter()",
    "astype": "cast()",
    "reset_index": "(polars has no index)",
    "set_index": "(polars has no index)",
    "sort_values": "sort()",
    "isnull": "is_null()",
    "notnull": "is_not_null()",
    "fillna": "fill_null()",
    "dropna": "drop_nulls()",
    "nunique": "n_unique()",
}


def polars_api() -> Check:
    """The proposal uses polars, not pandas-on-a-polars-frame.

    The one failure class the static validator structurally cannot see:
    ``df.groupby("x")["y"].sum()`` is valid Python, composes into a valid marimo
    notebook, and validates clean — it just raises ``AttributeError`` the moment
    the cell runs. Scored as its own named check rather than folded into a syntax
    verdict, because "structurally correct marimo, wrong dataframe library" is a
    genuinely different thing to tell a user about a model than "it emits garbage".
    """

    def run(a: "Attempt") -> str:
        for label, code in a.proposed_cells():
            try:
                tree = ast.parse(code)
            except SyntaxError:
                continue  # cells_parse() owns that verdict
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in _PANDAS_ONLY:
                    instead = _PANDAS_ONLY[node.attr]
                    return f"{label} uses pandas '.{node.attr}' on a polars frame (use {instead})"
        return ""

    return Check("polars-api", run)


# -- SQL cells ----------------------------------------------------------------

_SQL_WRITES = re.compile(
    r"\b(drop|delete|insert|update|alter|truncate|merge|create|replace)\b", re.I
)
_SQL_PIVOT = re.compile(r"\bpivot\b", re.I)


def _sql_queries(a: "Attempt") -> list[str]:
    """The string arguments of every ``mo.sql(...)`` call in the proposal."""
    out: list[str] = []
    for _, code in a.proposed_cells():
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sql"
            ):
                for arg in node.args:
                    out += sorted(_strings(arg))
    return out


def sql_cell() -> Check:
    """The proposal is a marimo SQL cell — a ``mo.sql(...)`` call, assigned to a
    name so later cells can use the frame (the guide asks for both)."""

    def run(a: "Attempt") -> str:
        if not _sql_queries(a):
            return "no mo.sql(...) call in the proposal"
        for _, code in a.proposed_cells():
            try:
                tree = ast.parse(code)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and _is_sql_call(node.value):
                    return ""
        return "the mo.sql(...) result is not assigned to a variable"

    return Check("sql-cell", run)


def _is_sql_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "sql"
    )


def sql_read_only() -> Check:
    """No SQL write verb. A safety check, not a style one: an applied cell runs at
    once and Undo restores the notebook TEXT — a DROP has already reached the
    database by the time anyone can undo it."""

    def run(a: "Attempt") -> str:
        for query in _sql_queries(a):
            hit = _SQL_WRITES.search(query)
            if hit:
                return f"the query is not read-only ('{hit.group(0).upper()}')"
        return ""

    return Check("sql-read-only", run)


def sql_no_pivot() -> Check:
    """No PIVOT. A pivot names the output columns after the row VALUES it pivots
    on, and the live-schema probe reports column names back to the model — so a
    pivot is the one SQL construct that can smuggle data values into a value-blind
    channel."""

    def run(a: "Attempt") -> str:
        for query in _sql_queries(a):
            if _SQL_PIVOT.search(query):
                return "the query pivots row values into column headers"
        return ""

    return Check("sql-no-pivot", run)


def binds_name(name: str) -> Check:
    """The candidate notebook binds ``name`` somewhere — e.g. a SQL cell proposed
    into a notebook that lacked ``import marimo as mo`` must bring the import."""

    def run(a: "Attempt") -> str:
        candidate = a.candidate()
        if candidate is None:
            return "nothing proposed"
        try:
            cells = marimo_rt.read_cells(candidate)
        except (ValueError, SyntaxError, marimo_rt.MarimoTooOld) as exc:
            return f"cannot read the candidate: {exc}"
        for _, code in cells:
            if name in _bound(code):
                return ""
        return f"nothing in the notebook binds '{name}'"

    return Check(f"binds-{name}", run)


def _bound(code: str) -> set[str]:
    """Top-level names ``code`` binds (assignments and imports)."""
    names: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names
