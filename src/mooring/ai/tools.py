"""The mooring-mediated safe toolset for the AI copilot.

:func:`build_tool_specs` builds the tools as provider-neutral :class:`ToolSpec`
objects whose handlers return a value-free :class:`mooring.ai.egress.ToolOutput`;
:func:`build_tools` is the GitHub Copilot adapter that wraps them in the SDK's
``Tool`` type. Sharing one set of handlers lets a second backend (which runs its
own tool-calling loop) reuse the exact value-free logic instead of re-implementing
tool serialisation.

Every tool here is **value-free by construction** — it returns only a dataset's
SCHEMA (names + dtypes, via the trusted ``schema`` module), the notebook's
SOURCE code, or a list of dataset paths. None can reach a data value, a cell
output, or the kernel. ``propose_notebook_edit`` does NOT write the notebook; it
surfaces a proposal to the chat UI, and the analyst Applies it.

Before it surfaces anything, the propose tool composes the notebook its change
would produce IN MEMORY and statically validates it (see the propose gate below,
on ``marimo_rt.validate_notebook_source``): a proposal that would break the
notebook comes back to the model as the diagnostics, not as a success. An
edit/delete is checked once more before that — against what the model SAYS is at
the index it is targeting (``expect``; see :func:`_expect_matches`) — so a stale
or forged index is refused instead of writing to a cell the model never read.

Combined with ``available_tools`` allowlisting exactly these names (so the SDK's
built-in file/shell tools are dropped) and a deny-all permission backstop, the
agent has no path to data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mooring.ai.egress import ToolOutput

# The always-on tools. The session's ``available_tools`` allowlist is derived
# from the tools actually built (these plus the dictionary tools when a data
# dictionary is present), so it stays in lock-step with what is registered.
#
# ``mooring_propose_notebook_edit`` is the ONE write surface, and it is only on when the
# caller wires a proposal callback — a read-only investigate sub-agent gets the three
# reads and nothing else (see the gate on the spec below).
#
# There used to be FOUR propose tools — ``mooring_propose_cell`` (append),
# ``mooring_propose_cell_edit`` (edit one), this one (the general patch) and
# ``mooring_propose_notebook_rewrite`` (replace everything) — three of which expressed a
# change this one already covers. Tool SELECTION is one of the sharpest capability
# gradients between model tiers, and here a mis-selection was not graceful: a model asked
# to FIX cell 3 that reached for the append tool wrote a SECOND definition of the same
# name, which stops both cells and everything downstream. They were merged rather than
# aliased: nothing outside this process ever calls a tool by name (no persisted
# conversation — ``enable_session_store`` is False and the OpenAI message list is
# in-memory — no HTTP or CLI surface takes a tool name), and on the copilot path
# ``available_tools`` IS the advertised list, so a "hidden alias" cannot exist there. An
# alias would therefore have been a fourth tool for the model to choose between, which is
# the thing being removed.
TOOL_NAMES = [
    "mooring_list_datasets",
    "mooring_get_schema",
    "mooring_read_notebook_source",
    "mooring_propose_notebook_edit",
]

# Cell-source format reminder for every propose tool: a cell's source is the BODY ONLY
# — mooring regenerates marimo's wrapper (`@app.cell` / `def _()` / a trailing
# `return (...)`). The model is now SHOWN that same body-only form (egress.
# render_notebook_for_model), so this states a rule it can already see obeyed rather
# than asking it to translate out of the wrapped file on disk.
_RATIONALE_DESC = "a one-line reason (optional)"

_CELL_FORMAT = (
    " Each cell is the BODY ONLY (top-level statements) — do NOT include '@app.cell', "
    "'def _():', or a trailing 'return (...)'; those are added automatically."
)

# What the model's ``expect`` claim is, and what checking it does and does not promise.
#
# ``expect`` is the model saying WHICH cell it believes index N holds. The server-captured
# ``anchor`` cannot say that: it is read live at the index the model supplied, so it always
# matches whatever is there and guards only the propose->apply race. A stale index (the
# system context's cell view is built once per session and never refreshed) or a forged one
# sails straight past it, into a cell the model never read.
#
# Two conditions, and BOTH are needed (see `_matching_cells`): the claim must fit the cell
# at that index, and it must fit no OTHER cell whose source differs. The second is not a
# nicety — marimo's codegen gives every markdown cell the same opening line, so without it
# a one-line claim about `mo.md("""` is satisfied by any markdown cell in the notebook and
# the check is theatre for the commonest cell shape mooring itself writes.
#
# What it does NOT promise: that the model READ the cell. A claim that fits exactly one
# cell only shows the model knows what is there — which is the property the write needs,
# and all a static check can establish.
#
# Comparison is on non-blank lines with each line's whitespace collapsed, so indentation
# drift, a re-wrapped copy and the render's own defused cell-boundary marker
# (``egress._DEFUSED_BOUNDARY_MARK``, which wedges one space into a comment) all still
# match. Deliberately tolerant on FORM and strict on identity: a false PASS is the
# behaviour this replaces, a false FAIL blocks correct work.


def sql_cell_guide() -> str:
    """A value-free capability note telling the copilot it can author marimo SQL cells.

    Threaded into the system context as ``sql_help`` (mirrors
    :func:`mooring.checks.copilot_guide`) so the model knows the ``mo.sql`` idiom and can
    PROPOSE a SQL cell from the schema + source it already sees. It reads no data value —
    SQL is authored code and marimo runs it locally; the model never sees the result, so
    this opens no new egress channel. Deliberately terse (a few lines) to stay cheap on
    every turn; the fuller instruction rides the on-demand ``/sql`` command.

    A marimo SQL cell is just a normal Python cell whose body is
    ``name = mo.sql(...)`` — marimo detects the SQL and runs it with DuckDB — so it
    round-trips through the same value-free codegen as any proposed cell (no new path).

    The READ-ONLY rule is a safety one, not a style one: marimo runs an applied cell
    immediately, and the analyst's undo restores the notebook TEXT only — a `DROP` or an
    unfiltered `DELETE` would already have reached the database. The Apply gate holds a
    write for an explicit confirm; this keeps the guide from teaching one in the first place.

    The no-PIVOT caveat is a value-blindness rule, not a style one: a pivot/crosstab
    names the output columns after the row VALUES it pivots on, and the live-kernel schema
    probe reports column NAMES back to the model — so a value→header pivot would smuggle
    data values into the schema the model sees. The value-blind contract holds only if the
    copilot never generates one."""
    return (
        "SQL CELLS (value-free): propose a marimo SQL cell that runs on DuckDB via "
        '`result = mo.sql("""<query>""")` (marimo detects the SQL). It requires '
        "`import marimo as mo` in the notebook — add it if the source you see lacks it — and "
        "the `duckdb` package in the notebook's environment; if duckdb may be missing, say so "
        "(the analyst can add it with `mooring deps add duckdb`). The query must be "
        "READ-ONLY: SELECT / WITH ... SELECT (SHOW, DESCRIBE and EXPLAIN are fine too) — "
        "never DROP, TRUNCATE, DELETE, INSERT, UPDATE, ALTER or MERGE; an applied cell runs "
        "at once and undo restores only the notebook text. Query any dataframe already "
        "in scope BY ITS VARIABLE NAME and refer to columns by the names in the schema — never "
        "inline a data value, and prefer an explicit column list over SELECT *. Do NOT pivot or "
        "crosstab row VALUES into column headers (e.g. DuckDB PIVOT): the resulting column names "
        "would BE data values. Assign the result to a well-named dataframe variable so later "
        "cells can use it, and propose it with mooring_propose_notebook_edit's `appends` "
        "(the BODY only)."
    )

# Added only when the workspace has a parsed data dictionary. Each is value-free:
# it serves the already five-slot-allowlisted in-memory index, looking up by table
# NAME (never a filesystem path), so it can reach no data file or value.
DICT_TOOL_NAMES = [
    "mooring_list_tables",
    "mooring_describe_table",
    "mooring_search_dictionary",
]

# Added only when the workspace has a parsed Power BI semantic model (and the
# feature is on — the caller applies the gates). Same shape as the dictionary
# trio: name lookups in the pre-parsed in-memory SemanticModel objects (never a
# caller path), serving the allowlist skeleton from mooring.pbip_model — tables,
# columns+dataTypes, relationships, and measure DAX (authored code; every result
# still passes egress.scrub_text). Partition M, RLS roles, annotations, and
# translations were never parsed, so no tool can reach them.
MODEL_TOOL_NAMES = [
    "mooring_get_semantic_model",
    "mooring_describe_model_table",
    "mooring_get_measure",
]

# Added only when the workspace's offered folders hold importable .py modules (and the
# feature is on — the caller applies the gate). Same shape as the dictionary/model trios:
# name lookups in the pre-parsed in-memory CodeIndex (never a caller path), serving the
# value-free API skeleton from mooring.ai.codelib — module import paths, structurally
# value-free signatures, sanitised type hints, and best-effort-scanned docstrings. Function
# bodies, literals, and constant values were never parsed, so no tool can reach them. There
# is deliberately NO get_source tool: real bodies are a value channel no floor makes safe.
HELPER_TOOL_NAMES = [
    "mooring_list_helpers",
    "mooring_describe_helper",
    "mooring_search_helpers",
]

# Added only when the workspace holds catalogued marimo notebooks (and the feature is on
# — the caller applies the gate and drops the team's AI-disabled notebooks). Same shape as
# the trios above: name lookups in the pre-parsed in-memory Catalog (never a caller path),
# serving the value-free entries from mooring.ai.notebookindex — path, title, the scanned
# first-markdown-cell summary, imports, and the inputs/checks/SQL tables the SOURCE
# declares. Cell bodies, outputs, and .mooring/ receipts were never read, so no tool can
# reach them. There is deliberately NO tool that returns another notebook's SOURCE: the
# current notebook's is already in the system context, and serving a second one would be
# a new, unreviewed egress of full authored code.
CATALOG_TOOL_NAMES = [
    "mooring_list_notebooks",
    "mooring_search_notebooks",
    "mooring_describe_notebook",
]


def _safe(workspace: Path, rel: str) -> Path:
    target = (workspace / rel).resolve()
    target.relative_to(workspace.resolve())  # raises ValueError on escape
    return target


def _norm_lines(code: str, limit: int | None = None) -> list[str]:
    """``code``'s non-blank lines, each whitespace-collapsed — the normalised form both
    sides of an ``expect`` check take. ``limit`` stops after that many, which bounds the
    work to the length of the CLAIM: every edit is now checked against every cell (see
    :func:`_matching_cells`), so a long cell must not cost more than a short one."""
    out: list[str] = []
    for line in code.splitlines():
        if not line.strip():
            continue
        out.append(" ".join(line.split()))
        if limit is not None and len(out) >= limit:
            break
    return out


def _expect_matches(expect: str, actual: str) -> bool:
    """Whether ``actual`` starts the way the model said it does.

    A PREFIX comparison on normalised lines, of exactly the length the model supplied:
    one line is cheap and is what the tool asks for first, and every further line is a
    STRONGER claim that still passes. Uncapped on purpose — when one line turns out to
    name several cells (see :func:`_matching_cells`) the model is told to send more, and
    a ceiling would leave it nothing further to say. An empty ``expect`` never matches;
    a claim has to be made to be checked.
    """
    want = _norm_lines(expect)
    if not want:
        return False
    return _norm_lines(actual, len(want)) == want


def _matching_cells(expect: str, cells) -> list[int]:
    """Every index ``expect`` describes — NOT just the one the model aimed at.

    Matching the target alone is not enough to know the model read the target. marimo's
    own codegen is what makes that bite: every markdown cell in a mooring-written
    notebook opens with the identical line ``mo.md(\"\"\"`` (escaped only so it does not
    close this docstring), so a model that read one and aims at another passes a
    first-line check trivially and writes over a cell it never looked at — the exact
    stale-index case ``expect`` exists to catch, with no attacker involved. So a claim
    that fits several DIFFERENT cells identifies none of them.

    Also the corrective half of a mis-target: when the claim fits exactly one OTHER cell,
    the refusal can say where that cell is now. That points the model at what it actually
    meant and — unlike quoting back what sits at the index it got wrong — cannot be
    copied back to succeed at writing over a cell it never read.
    """
    return [i for i, code in cells if _expect_matches(expect, code)]


def _as_list(value) -> list:
    """A tool argument that should be a list, as one. ``None`` is empty; a lone scalar
    becomes a one-item list, so a model that sends ``appends: "code"`` instead of
    ``appends: ["code"]`` is answered rather than failed on a shape slip."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _args(invocation) -> dict:
    """The tool's arguments as a dict (the SDK passes a dict; tolerate a JSON string)."""
    import json

    raw = getattr(invocation, "arguments", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


@dataclass(frozen=True)
class ToolSpec:
    """A provider-neutral tool descriptor produced by :func:`build_tool_specs`.

    Adapted per backend: :func:`build_tools` wraps each in a ``copilot.tools.Tool``;
    an OpenAI backend emits ``{"type": "function", "function": {...}}`` from
    ``name`` / ``description`` / ``parameters`` (already plain JSON-Schema, reusable
    verbatim) and dispatches ``handler`` by name. ``handler`` takes the SDK
    invocation (anything exposing ``.arguments``) and returns a value-free
    :class:`mooring.ai.egress.ToolOutput` — never a provider-specific result type.
    """

    name: str
    description: str
    parameters: dict
    handler: Callable[[object], "ToolOutput"]
    skip_permission: bool = True
    # ``blocking`` marks a handler that can run for many seconds (the investigate fan-out
    # drives N sub-sessions to completion). The Copilot SDK calls tool handlers ON its
    # session's asyncio loop thread and awaits an awaitable result, so a blocking handler
    # there would wedge the loop — stalling close()/teardown for the whole fan-out. The
    # copilot adapter therefore offloads a blocking handler to a worker thread. The OpenAI
    # session runs its loop on its own worker thread, so blocking is harmless there.
    blocking: bool = False


# A cell ORDINAL inside a diagnostic's message. mooring writes one into its own MOOR001 /
# MOOR002 / MOOR003 messages ("cell 2 is not valid Python: …"), and the propose gate
# compares a diagnostic found on the CANDIDATE against the same fault on the notebook as
# it was — where a delete has shifted every ordinal below it. Normalised out of the
# comparison key only (see `_diag_key`); what the model is shown keeps the real number.
_DIAG_ORDINAL = re.compile(r"\bcell \d+\b")


def build_tool_specs(
    *,
    workspace: Path,
    folders: tuple[str, ...],
    notebook_rel: str,
    emit_proposal: Callable[[str, str], None] | None = None,
    emit_proposal_patch: Callable[[dict], None] | None = None,
    dictionary=None,
    semantic_models=None,
    code_index=None,
    catalog=None,
    run_investigation: Callable[..., str] | None = None,
    emit_tool_progress: Callable[[str], None] | None = None,
    pii_enabled: bool = False,
    allow_read_tools: bool = True,
    trusted_customer_data: bool = False,
    output_guard: Callable[[str], bool] | None = None,
) -> list["ToolSpec"]:
    """Build the safe tools as provider-neutral :class:`ToolSpec`s, bound to one
    workspace + target notebook.

    Handlers take a single invocation argument (anything exposing ``.arguments`` —
    the parsed args; a JSON string is tolerated) and return a value-free
    :class:`mooring.ai.egress.ToolOutput`. When ``dictionary`` (a
    :class:`mooring.ai.datadictionary.DictionaryIndex`) is non-empty, the three
    value-free dictionary tools are added. When ``semantic_models`` (pre-parsed
    :class:`mooring.pbip_model.SemanticModel` objects — the caller has already
    applied the config gate and the synced per-model opt-out) is non-empty, the
    three model tools are added. When ``catalog`` (a
    :class:`mooring.ai.notebookindex.Catalog` — the caller has already applied the
    config gate and dropped the team's AI-disabled notebooks) is non-empty, the three
    repo-wide notebook-catalog tools are added. When ``pii_enabled``, ``get_schema``
    withholds any column whose NAME is itself a PII value (a pivot/transpose on a PII
    key) — the second, dynamic schema egress (besides the system context) that the
    agent can reach at any time.

    Either proposal callback enables the ONE write tool,
    ``mooring_propose_notebook_edit``: it captures each targeted cell's current source
    as an ``anchor`` and emits a proposal to the local UI for the analyst to review and
    Apply (never an autonomous write). ``emit_proposal_patch`` (the real chat session
    supplies both) carries the structured ``{kind, ops, diffs}`` payload; a proposal
    that is exactly one appended cell still goes out on ``emit_proposal`` as
    ``{code, rationale}``, the shape the chat UI renders as an additive block.

    :func:`build_tools` adapts these to the copilot SDK; a second backend reuses the
    same handlers and only re-expresses the spec and result shapes.
    """
    from collections import Counter
    from dataclasses import replace

    from mooring import marimo_rt, pbip_model, schema
    from mooring.ai import egress

    def _ok(text: str) -> "ToolOutput":
        return egress.ToolOutput(text=text)

    def _err(msg: str) -> "ToolOutput":
        # A value-free error output. The message carries the RAW text and is scrubbed
        # at the mint (egress.to_error_result / egress.to_openai_tool_message both
        # apply egress.scrub_error_text), so no egress channel sees it unscrubbed.
        return egress.ToolOutput(text=msg, is_error=True)

    def list_datasets(_invocation):
        found = schema.list_datasets(workspace, folders)
        return _ok("\n".join(found) or "(no datasets found)")

    def get_schema(invocation):
        rel = str(_args(invocation).get("dataset", "")).strip()
        if not rel:
            return _err("dataset required")
        try:
            target = _safe(workspace, rel)
            ds = schema.extract_schema(target)
            if pii_enabled:
                kept, col_findings = egress.scrub_columns(ds.columns)
                if col_findings:  # a column NAME is itself a PII value — withhold it
                    ds = replace(ds, columns=kept)
            text = schema.format_for_ai(ds, source=rel)
        except (ValueError, OSError) as exc:
            return _err(f"cannot read schema: {exc}")
        return _ok(text)

    _NB_READ_ERRORS = (
        ValueError,
        OSError,
        SyntaxError,
        marimo_rt.MarimoTooOld,
        marimo_rt.MarimoTransportError,
    )

    def _current_cells() -> list[tuple[int, str]]:
        """The notebook's cells as ``(index, code)`` — the model's view for editing,
        and the source of the ``anchor`` captured per edit/delete."""
        src = _safe(workspace, notebook_rel).read_text("utf-8")
        return marimo_rt.read_cells(src)

    def read_notebook_source(_invocation):
        # Enumerate the cells WITH their indices so the model can target one for an
        # edit, and route the result through the egress scrubber — the same value-free
        # treatment build_system_context gives the notebook source (this tool used to
        # bypass it). The rendering itself lives in egress (ONE renderer, shared with
        # the system context, so a re-read never disagrees with what the model was
        # already shown); it falls back to the raw source on any parse trouble.
        try:
            raw = _safe(workspace, notebook_rel).read_text("utf-8")
        except (ValueError, OSError) as exc:
            return _err(str(exc))
        rendered = egress.render_notebook_for_model(raw)
        if not trusted_customer_data:
            rendered, _ = egress.scrub_text(rendered)
        return _ok(rendered)

    def _coerce_index(value):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            # OverflowError is not hypothetical: a JSON number too large for a float
            # parses as `inf`, and `int(inf)` raises it — so without it a model sending
            # `index: 1e400` takes down the tool call with an unhandled exception
            # instead of being told its index is not a cell number.
            return None

    # --- the propose gate: check the notebook a proposal WOULD produce ---------
    #
    # Every propose handler runs its ops through `_gate` before it emits. Until this
    # existed they checked only that the code string was non-empty and then told the
    # model it had succeeded — so a cell that redefines a name another cell already
    # defines, closes a dependency cycle, or pastes whole `@app.cell` blocks into a
    # cell BODY was reported as a success, and the analyst found out by Applying it.
    # A strong model re-reads its own work; a weaker one believes the environment,
    # and mooring's environment congratulated it either way.
    #
    # The candidate is built the way Apply itself builds it — the same wire op dicts,
    # through the same cellwrite converter, into the same `marimo_rt.apply_cell_patch`
    # — so what is checked is the file that would land, not a re-derivation of it.
    # `apply_cell_patch` is PURE (source in, source out), so nothing is written here.

    # A refusal only helps while the model can still act on it. After this many failed
    # validations IN A ROW the gate stops handing back diagnostics it has already
    # proved it cannot act on, and tells it to take the problem to the analyst: the
    # copilot SDK drives its own tool loop with no mooring-side iteration bound at all,
    # and the OpenAI loop's bound (`openai_session._MAX_TOOL_ITERS`, 12) covers the
    # WHOLE turn — so a stuck model must not be able to spend the lot re-proposing one
    # cell. Any accepted proposal resets it: the budget measures "stuck", not "has been
    # wrong before".
    _VALIDATION_BUDGET = 3
    _consecutive_failures = [0]  # a one-slot list: a closure may mutate, not rebind

    # Diagnostics that do NOT refuse a proposal, and why.
    #
    #   MOOR000 / MOOR005 — the CHECKER was unavailable or declined (marimo too old, a
    #     pass that overran, a notebook past the ceilings). Nothing is known to be wrong
    #     with the proposal, and refusing would turn a checker outage into a dead
    #     copilot — strictly worse than the behaviour this gate replaced. Fail OPEN, but
    #     SAY so: "checked and clean" and "not checked" must never read the same.
    #   MOOR003 — a name no cell defines. The one diagnostic whose correctness depends
    #     on what happens NEXT: a model may legitimately propose a cell using a name it
    #     will define in the following proposal, and refusing would break that plan.
    #     It also breaks one cell, visibly, when it runs — where a duplicate definition
    #     or a cycle stops those cells and everything downstream of them. So it rides
    #     along as a note. (It is also inert on this path today — marimo's codegen writes
    #     each cell's refs into its `def _(name):` signature, which the validator's own
    #     `_bound_names` backstop then reads as a binding. See
    #     `test_an_unresolved_name_is_a_note_not_a_refusal`.)
    #
    # Everything else refuses, INCLUDING a code on neither list: marimo's rule allowlist
    # (`marimo_rt.VALIDATE_LINT_RULES`) is curated and every entry is `breaking`, so an
    # unrecognised code is far likelier to be a new breaking rule than a new advisory.
    # A maintainer who adds one classifies it here.
    _UNCHECKED_CODES = frozenset(
        {marimo_rt.DIAG_VALIDATOR_UNAVAILABLE, marimo_rt.DIAG_TOO_LARGE}
    )
    _ADVISORY_CODES = frozenset({marimo_rt.DIAG_UNRESOLVED_REFERENCE})
    _NON_BLOCKING_CODES = _UNCHECKED_CODES | _ADVISORY_CODES

    # Enough for the model to see the pattern, few enough that it reads them. A wall of
    # diagnostics is as useless as none.
    _MAX_REPORTED = 3

    # The three headers a proposal can come back under, kept apart on purpose: each makes
    # a DIFFERENT claim about who caused what, and only one of them may ever say "not
    # you". Telling a model it did not cause a fault it did cause is worse than telling it
    # nothing, so "could not tell" has its own wording rather than borrowing either.
    _PRE_EXISTING = (
        "The notebook ALREADY had these problems before your change — it neither caused "
        "nor fixed them:"
    )
    _UNATTRIBUTABLE = (
        "mooring could not check the notebook as it was BEFORE this change, so it cannot "
        "tell whether these are yours or were already there — but the result has them:"
    )
    _WORTH_KNOWING = "Not blocking, but worth knowing:"

    def _scrubbed(text: str, fallback: str) -> str:
        """``text`` through the egress floor, or ``fallback`` if the scrub empties it.

        Diagnostics are the one tool result here NOT authored in mooring: the validator
        forwards marimo's `message`/`fix` verbatim and `MOOR000` embeds `str(exc)` from
        a marimo internal, so they get the same `egress.scrub_text` every other result in
        this module gets. No marimo rule quotes notebook text today, but nothing
        structurally stops one starting — which is exactly what ruff's messages do.
        """
        out, _ = egress.scrub_text(text)
        return out.strip() or fallback

    def _render_diagnostics(diagnostics) -> str:
        """The diagnostics as short, actionable, value-free lines."""
        lines = []
        for d in diagnostics[:_MAX_REPORTED]:
            plural = "s" if len(d.lines) > 1 else ""
            where = f" (line{plural} {', '.join(str(n) for n in d.lines)})" if d.lines else ""
            lines.append(f"- [{d.code}] {d.name}{where}: {d.message}")
            if d.fix:
                lines.append(f"  fix: {d.fix}")
        extra = len(diagnostics) - _MAX_REPORTED
        if extra > 0:
            lines.append(f"- (and {extra} more)")
        # The fallback keeps the rule CODES: fixed identifiers authored in mooring and
        # marimo's rule registry, never anything read out of the notebook.
        return _scrubbed(
            "\n".join(lines),
            "\n".join(f"- [{d.code}] {d.name}" for d in diagnostics[:_MAX_REPORTED]),
        )

    def _spent_the_budget() -> "ToolOutput | None":
        """Count one rejection, and return the give-up result once the budget is gone.

        Shared by BOTH ways a proposal comes back refused — the static check and the
        ``expect`` mis-target check — because they are the same problem from the model's
        side: it has been told no, repeatedly, and is not converging. A model that cannot
        aim at the right cell after three tries is exactly as stuck as one that cannot
        write a cell the notebook accepts.
        """
        _consecutive_failures[0] += 1
        if _consecutive_failures[0] > _VALIDATION_BUDGET:
            return _err(
                f"NOT proposed. {_consecutive_failures[0]} proposals in a row have been "
                "rejected, so re-proposing is not working. Stop calling the propose tools "
                "for this change: tell the analyst in your reply what you were trying to "
                "write and what the notebook does not allow, and let them decide."
            )
        return None

    def _refused(detail: str) -> "ToolOutput":
        """The result for a proposal the gate is holding back. Nothing was emitted."""
        spent = _spent_the_budget()
        if spent is not None:
            return spent
        return _err(
            "NOT proposed — the analyst was shown nothing. mooring built the notebook this "
            "change would produce and checked it statically; it would not work:\n"
            f"{detail}\n"
            "Fix the cause and call the tool again."
        )

    def _mistargeted(detail: str) -> "ToolOutput":
        """The result for a change aimed at a cell the model has not actually seen.

        Deliberately says NOTHING about what is really at that index. Handing over the
        real first line would let a model that just wants the call to succeed paste it
        back and write over a cell it never read — which is the exact outcome this check
        exists to prevent. It re-points instead: read the notebook, then aim again.
        """
        spent = _spent_the_budget()
        if spent is not None:
            return spent
        return _err(
            "NOT proposed — the analyst was shown nothing. What you said is at that index "
            "is not what is there, so mooring did not write to a cell you have not read:\n"
            f"{detail}\n"
            "Call mooring_read_notebook_source for the notebook as it is NOW, then propose "
            "again against the current cells."
        )

    def _gate(op_dicts) -> tuple["ToolOutput | None", str]:
        """Statically check the notebook ``op_dicts`` would produce.

        Returns ``(refusal, note)``. A non-None ``refusal`` is the tool result to return
        INSTEAD of emitting anything; otherwise emit as before and append ``note``
        (usually empty) to the success message.
        """
        try:
            return _gate_inner(op_dicts)
        except Exception:  # noqa: BLE001  # a gate that can break a turn is worse than none
            return None, ""

    def _gate_inner(op_dicts) -> tuple["ToolOutput | None", str]:
        from mooring.ai import cellwrite

        try:
            base = _safe(workspace, notebook_rel).read_text("utf-8")
            marimo_rt.read_cells(base)
        except _NB_READ_ERRORS:
            # There is no readable notebook to build a candidate ON. A missing or
            # unparseable file is the analyst's, not something the model can fix, and it
            # leaves nothing to judge the proposal against — so behave exactly as this
            # tool did before the gate existed.
            return None, ""
        try:
            # `cellwrite._ops_from_wire` on purpose: the Apply endpoint converts these
            # same dicts with it (`cellwrite.apply_wire_patch`), so the candidate checked
            # here is the file that would land.
            candidate = marimo_rt.apply_cell_patch(base, cellwrite._ops_from_wire(op_dicts))
        except (ValueError, SyntaxError, cellwrite.CellWriteError) as exc:
            # The base parsed a moment ago, so the PATCH is at fault: a cell that does not
            # compile, an op that would empty the notebook, a stale index or anchor.
            # (`CellPatchConflict` is a ValueError.) Apply would have failed the same way.
            return _refused(
                _scrubbed(
                    f"- the change could not be applied to the notebook: {exc}",
                    "- the change could not be applied to the notebook",
                )
            ), ""
        diagnostics = marimo_rt.validate_notebook_source(candidate)
        blocking = [d for d in diagnostics if d.code not in _NON_BLOCKING_CODES]
        notes = [d for d in diagnostics if d.code in _NON_BLOCKING_CODES]
        if not blocking:
            _consecutive_failures[0] = 0
            return None, _note([(_WORTH_KNOWING, notes)])
        # Only now is the base worth checking, so the clean path pays for one validation
        # pass and not two.
        base_faults = _already_broken(base)
        if base_faults is None:
            # THE one place an unattributable result is decided. The base could not be
            # checked, so nothing here can be shown to be the change's doing — and a gate
            # that refuses on what it cannot show is the failure mode this whole design
            # rejects (see `_already_broken`). Report, never refuse.
            _consecutive_failures[0] = 0
            return None, _note([(_UNATTRIBUTABLE, blocking), (_WORTH_KNOWING, notes)])
        introduced, pre_existing = _split_by_blame(blocking, base_faults)
        if introduced:
            return _refused(_render_diagnostics(introduced)), ""
        _consecutive_failures[0] = 0
        return None, _note([(_PRE_EXISTING, pre_existing), (_WORTH_KNOWING, notes)])

    def _note(sections) -> str:
        """The block appended to a success message: each ``(header, diagnostics)`` that
        has anything to report, or ``""`` when none do."""
        parts = [f"{header}\n{_render_diagnostics(found)}" for header, found in sections if found]
        return ("\n\n" + "\n\n".join(parts)) if parts else ""

    def _diag_key(d) -> tuple:
        """The identity a diagnostic is compared BY when deciding who caused it.

        Three things go into it, and each earns its place:

        * the **code**;
        * the **message with cell ordinals normalised out** (:data:`_DIAG_ORDINAL`).
          mooring writes the ordinal into its own MOOR001/MOOR002/MOOR003 messages, and a
          DELETE renumbers every cell below it — so the identical pre-existing fault
          reads as "cell 2" on the base and "cell 1" on the candidate. Same reason line
          numbers are excluded; the ordinal was just line numbers wearing a hat;
        * how MANY lines the finding names — NOT which. marimo reports one MB002 per
          duplicated NAME, so a third definition of an already-duplicated name carries
          the same code and the same message as the second, and the count of findings
          does not change either. What changes is that its `lines` grow from 2 to 3,
          which is precisely the change in the fault.
        """
        return (d.code, _DIAG_ORDINAL.sub("cell _", d.message), len(d.lines))

    def _split_by_blame(blocking, existing) -> tuple[list, list]:
        """Split ``blocking`` into ``(introduced, pre_existing)`` against the base.

        By COUNT, not membership. Three of the five allowlisted marimo rules carry a
        CONSTANT message — MB001 "Notebook contains unparsable code", MB003 "Cell is
        part of a circular dependency", MB004 "Setup cell cannot have dependencies" — so
        a set test would let ONE pre-existing instance whitelist every new one: a model
        could add a whole new cycle to a notebook that already had one and be told the
        notebook "already had" it. Counting also stays correct when a change REMOVES one
        of several instances (the candidate's count is lower, so nothing reads as new).

        Only ever reached with a real ``Counter``: an unattributable base is decided by
        the caller, at the one branch above.
        """
        seen: Counter = Counter()
        introduced: list = []
        pre_existing: list = []
        for d in blocking:
            key = _diag_key(d)
            seen[key] += 1
            (introduced if seen[key] > existing[key] else pre_existing).append(d)
        return introduced, pre_existing

    def _already_broken(base_source: str):
        """What is wrong with the notebook BEFORE the change, as a ``Counter`` of
        :func:`_diag_key` — or ``None`` when the base could not be checked at all.

        An analyst opens the copilot on a broken notebook more often than on a healthy
        one — a duplicate definition is among the commonest ways a marimo notebook stops
        — and a fault that was already there is not the model's to fix before it may
        propose anything else. Without this, one pre-existing MB002 would refuse EVERY
        proposal, spend the retry budget, and end with the model told to give up: the
        gate would be at its most obstructive in exactly the situation the copilot is
        most often opened for. (A proposal that FIXES the fault still passes either way,
        because the candidate is then clean.)

        The ``None`` matters as much as the count. ``MOOR000``/``MOOR005`` mean the base
        was NOT checked — not that it was found clean — so folding them in as if they
        were findings would make every pre-existing fault read as newly introduced. There
        are three ways in, and none is a corner case:

        * the base is over ``VALIDATE_MAX_CELLS`` while the candidate is not — a 151-cell
          notebook and a proposal DELETING a cell;
        * the same across ``VALIDATE_MAX_BYTES``, for a delete that crosses 512 KB
          downward;
        * an orphaned validator thread. ``marimo_rt`` reports ``MOOR000`` for as long as
          an overrun pass is still alive, so ONE timeout poisons every base check that
          follows it — session-wide, not for one call.

        Each of those would have refused a correct change three times and then hit the
        retry bound and declared the propose tools dead, on the shape most likely to need
        one: large, already broken. So the rule that governs an unavailable checker
        everywhere else in this gate governs it here — what could not be checked cannot
        refuse — and it is enforced at ONE branch in the caller rather than at each
        symptom.

        Run only when the candidate has something blocking to explain, so the clean path
        still pays for one validation pass, not two.
        """
        found = marimo_rt.validate_notebook_source(base_source)
        if any(d.code in _UNCHECKED_CODES for d in found):
            return None
        return Counter(_diag_key(d) for d in found)

    # --- the ONE write tool ----------------------------------------------------
    #
    # Every kind of change the copilot can make to the notebook arrives here: append,
    # edit, delete, and the wholesale rewrite. What it EMITS is unchanged per op shape,
    # so the analyst's card and both Apply routes see exactly the payloads they saw when
    # four tools produced them — a lone append still goes out as the legacy
    # `{code, rationale}`, a lone edit as `kind: "edit"`, a `cells` rewrite as
    # `kind: "rewrite"`, anything else as `kind: "patch"`.

    def _verify_target(idx: int, expect, cells) -> "ToolOutput | None":
        """Check the model's claim about what is at ``idx``. ``None`` means it holds."""
        text = str(expect or "").strip()
        if not text:
            return _mistargeted(
                f"- cell {idx}: no 'expect' was given. Every edit and delete must say what "
                "you believe is at that index — the first line of that cell, as you last "
                "saw it — because the cell view you were given is a SNAPSHOT and anything "
                "applied since has renumbered it. mooring will not tell you what is there; "
                "read it."
            )
        hits = _matching_cells(text, cells)
        if idx not in hits:
            where = (
                f" What you described is at index {hits[0]} in the notebook as it is now."
                if len(hits) == 1
                else ""
            )
            return _mistargeted(
                f"- cell {idx} does not begin the way your 'expect' says it does, so it is "
                f"not the cell you meant to change.{where}"
            )
        # The claim fits the target — but if it fits other cells whose source DIFFERS, it
        # does not show WHICH of them the model read, so it shows nothing. Cells that are
        # byte-identical are exempt: the model read exactly this content, and there is no
        # unseen cell for the write to land on. Without that carve-out two identical
        # separator cells would be permanently uneditable.
        rival = {cells[i][1] for i in hits}
        if len(rival) > 1:
            return _mistargeted(
                f"- cell {idx}: your 'expect' describes {len(hits)} of this notebook's "
                "cells, so it does not show which one you read — marimo writes every "
                "markdown cell with the same opening line, for instance. Send MORE of the "
                "cell (the next line or two, as you saw them) so it identifies exactly one."
            )
        return None

    def propose_notebook_edit(invocation):
        args = _args(invocation)
        rationale = str(args.get("rationale", ""))
        edits = _as_list(args.get("edits"))
        appends = _as_list(args.get("appends"))
        deletes = _as_list(args.get("deletes"))
        raw_cells = args.get("cells")

        # Shape tolerance for the flat form the retired tools took (`code`, or `code` +
        # `index`): a model that reaches for it is answered rather than failed, and the
        # change still goes through every check below — including `expect`, which an
        # `index` here does not get to skip. One tool, one schema; this only stops a
        # near-miss costing a whole round-trip.
        #
        # A tolerance may only decide how an argument is READ, never which OPERATION runs.
        # An `index` that will not parse is therefore an error: reading it as "no index"
        # would turn a model's EDIT into an APPEND — silently producing the second
        # definition of a name that stops both cells, which is the exact slip this
        # consolidation exists to remove. An explicitly null `index` is absence, not a
        # malformed value: models routinely emit null for a parameter they are omitting.
        top_code = args.get("code")
        if isinstance(top_code, str) and top_code.strip():
            wants_edit = args.get("index") is not None
            top_index = _coerce_index(args.get("index"))
            if wants_edit and top_index is None:
                return _err(
                    "'index' must be an integer cell number. It is not clear whether you "
                    "meant to edit that cell or to add a new one, and mooring will not "
                    "guess: adding a cell that redefines a name an existing cell defines "
                    "stops both. Re-send with an integer 'index' (plus 'expect') to edit, "
                    "or with 'appends' to add a new cell."
                )
            if not wants_edit:
                appends = [*appends, top_code]
            else:
                edits = [
                    *edits,
                    {"index": top_index, "code": top_code, "expect": args.get("expect", "")},
                ]

        if raw_cells is not None:
            return _propose_rewrite(raw_cells, args, rationale, edits, appends, deletes)
        return _propose_patch(edits, appends, deletes, rationale)

    def _propose_rewrite(raw_cells, args, rationale, edits, appends, deletes):
        """The ``cells`` variant: replace every cell in the notebook.

        Kept as a field on this tool rather than a separate one because it is the same
        act — proposing the notebook's next state — and because forcing a rewrite through
        N edits + M deletes would make the model enumerate indices it may well get wrong,
        pushing work onto exactly the weak model this consolidation is for. It is the
        most destructive shape the tool has, so it is EXCLUSIVE (a model that meant to
        append cannot half-fill it) and it carries its own claim, ``expect_cells``.
        """
        if edits or appends or deletes:
            return _err(
                "'cells' REPLACES the whole notebook, so it cannot be combined with edits, "
                "appends or deletes. Send either 'cells' on its own for a wholesale "
                "rewrite, or the targeted changes."
            )
        if not isinstance(raw_cells, (list, tuple)):
            return _err(
                "'cells' must be a LIST of cell bodies (the full ordered notebook). A "
                "single string would rewrite the notebook as one cell."
            )
        new_cells = [
            marimo_rt.normalize_cell_code(str(c.get("code", "") if isinstance(c, dict) else c))
            for c in raw_cells
        ]
        new_cells = [c for c in new_cells if c.strip()]
        if not new_cells:
            return _err("a rewrite needs a non-empty 'cells' list of cell source strings")
        try:
            current = _current_cells()
        except _NB_READ_ERRORS:
            # Nothing to verify the claim against, and nothing to diff against either. The
            # house rule everywhere in this module: what cannot be checked cannot refuse.
            current = None
        if current is not None:
            claimed = _coerce_index(args.get("expect_cells"))
            if claimed is None:
                return _mistargeted(
                    "- a rewrite discards every existing cell, so 'expect_cells' is "
                    "required: how many cells you believe the notebook has right now."
                )
            if claimed != len(current):
                return _mistargeted(
                    f"- you said the notebook has {claimed} cell(s). It does not — so a "
                    "rewrite built from that view would delete cells you have never seen."
                )
        before = "\n\n".join(code for _, code in current) if current is not None else ""
        # A rewrite replaces every cell, so its candidate is these cells wholesale —
        # the path where a weak model's output is LEAST constrained by existing code,
        # and the one where an unchecked proposal costs the whole notebook.
        ops = [{"op": "replace_all", "cells": new_cells}]
        refusal, note = _gate(ops)
        if refusal is not None:
            return refusal
        if emit_proposal_patch is None:
            return _err("this session can only propose appended cells")
        emit_proposal_patch(
            {
                "kind": "rewrite",
                "rationale": rationale,
                "ops": ops,
                "diffs": [
                    {"label": "whole notebook", "before": before, "after": "\n\n".join(new_cells)}
                ],
            }
        )
        return _ok(
            f"Proposed a full rewrite ({len(new_cells)} cells) for the analyst to review and apply."
            + note
        )

    def _propose_patch(edits, appends, deletes, rationale):
        """The targeted form: any mix of edits, deletes and appends as ONE patch."""
        cells = None
        try:
            cells = _current_cells()
        except _NB_READ_ERRORS as exc:
            if edits or deletes:
                # An index has nothing to mean without cells to index into.
                return _err(f"cannot read the notebook cells: {exc}")
        n = len(cells) if cells is not None else 0
        ops: list[dict] = []
        diffs: list[dict] = []
        targeted: set[int] = set()

        def _claim(idx, where: str) -> "ToolOutput | None":
            if idx is None:
                return _err(f"each entry in '{where}' needs an integer 'index'")
            if not 0 <= idx < n:
                return _err(f"{where} index {idx} is out of range 0..{n - 1}")
            if idx in targeted:
                return _err(f"cell {idx} is targeted more than once")
            targeted.add(idx)
            return None

        for edit in edits:
            if not isinstance(edit, dict):
                return _err("each entry in 'edits' must be an object {index, expect, code}")
            idx = _coerce_index(edit.get("index"))
            code = marimo_rt.normalize_cell_code(str(edit.get("code", "")))
            if idx is not None and not code.strip():
                return _err(f"the edit for cell {idx} has no code")
            bad = _claim(idx, "edits")
            if bad is not None:
                return bad
            bad = _verify_target(idx, edit.get("expect"), cells)
            if bad is not None:
                return bad
            anchor = cells[idx][1]
            ops.append({"op": "edit", "index": idx, "anchor": anchor, "code": code})
            diffs.append({"label": f"cell {idx}", "before": anchor, "after": code})
        for raw in deletes:
            # A bare integer is accepted only so the model gets the real answer — that a
            # delete needs an 'expect' too — instead of a shape complaint. Deleting the
            # wrong cell is worse than editing it, not better.
            entry = raw if isinstance(raw, dict) else {"index": raw}
            idx = _coerce_index(entry.get("index"))
            bad = _claim(idx, "deletes")
            if bad is not None:
                return bad
            bad = _verify_target(idx, entry.get("expect"), cells)
            if bad is not None:
                return bad
            anchor = cells[idx][1]
            ops.append({"op": "delete", "index": idx, "anchor": anchor})
            diffs.append({"label": f"cell {idx} (deleted)", "before": anchor, "after": ""})
        for raw in appends:
            code = marimo_rt.normalize_cell_code(
                str(raw.get("code", "") if isinstance(raw, dict) else raw)
            )
            if not code.strip():
                return _err("an appended cell has no code")
            ops.append({"op": "append", "code": code})
            diffs.append({"label": "new cell", "before": "", "after": code})
        if not ops:
            return _err("provide at least one of edits, appends, deletes, or cells (a rewrite)")
        refusal, note = _gate(ops)
        if refusal is not None:
            return refusal

        # One appended cell keeps the legacy `{code, rationale}` event: the chat renders it
        # as an additive block rather than a diff against nothing, which is the better card
        # for the commonest change the copilot makes.
        if len(ops) == 1 and ops[0]["op"] == "append" and emit_proposal is not None:
            emit_proposal(ops[0]["code"], rationale)
            return _ok("Proposed the cell to the analyst, who will review and apply it." + note)
        if emit_proposal_patch is None:
            return _err("this session can only propose appended cells")
        if len(ops) == 1 and ops[0]["op"] == "edit":
            emit_proposal_patch(
                {"kind": "edit", "rationale": rationale, "ops": ops, "diffs": diffs}
            )
            return _ok(
                f"Proposed an edit to cell {ops[0]['index']} for the analyst to review and apply."
                + note
            )
        emit_proposal_patch({"kind": "patch", "rationale": rationale, "ops": ops, "diffs": diffs})
        return _ok(
            f"Proposed {len(ops)} change(s) to the notebook for the analyst to review and apply."
            + note
        )

    # The dictionary tools render TEAM-AUTHORED content (already value-minimised by
    # the five-slot allowlist and secret-scanned at sync), so scrubbing here is
    # defence-in-depth: the rendered slice gets the same checksum-PII floor
    # build_system_context gives the dictionary fragment, closing the one tool
    # channel that used to reach the model without an egress scrub.

    def list_tables(_invocation):
        from mooring.ai.datadictionary import render_listing

        assert dictionary is not None  # dictionary tools only registered when it is present
        listing, _ = egress.scrub_text(render_listing(dictionary))
        return _ok(listing or "(the data dictionary is empty)")

    def describe_table(invocation):
        from mooring.ai.datadictionary import render_table

        name = str(_args(invocation).get("table", "")).strip()
        if not name:
            return _err("table required")
        assert dictionary is not None  # dictionary tools only registered when it is present
        table = dictionary.get(name)
        if table is None:
            return _ok(f"No table named {name!r} in the data dictionary.")
        rendered, _ = egress.scrub_text(render_table(table))
        return _ok(rendered)

    def search_dictionary(invocation):
        from mooring.ai.datadictionary import render_table

        query = str(_args(invocation).get("query", "")).strip()
        if not query:
            return _err("query required")
        assert dictionary is not None  # dictionary tools only registered when it is present
        hits = dictionary.search(query, limit=8)
        if not hits:
            return _ok(f"No dictionary tables match {query!r}.")
        rendered, _ = egress.scrub_text("\n\n".join(render_table(t, max_cols=12) for t in hits))
        return _ok(rendered)

    # The code-library tools serve the PRE-PARSED API skeleton (mooring.ai.codelib):
    # module import paths, value-free signatures, sanitised type hints, best-effort-scanned
    # docstrings. Lookups are by NAME in the in-memory CodeIndex — never a caller path — and
    # every rendered string still passes egress.scrub_text (defence in depth; the real
    # value-blindness is the structural allowlist, since egress is only a checksum floor).
    # There is no source-body tool by design.

    def list_helpers(_invocation):
        from mooring.ai import codelib

        assert code_index is not None  # helper tools only registered when it is present
        listing, _ = egress.scrub_text(codelib.render_listing(code_index))
        return _ok(listing or "(the team code library is empty)")

    def describe_helper(invocation):
        from mooring.ai import codelib

        name = str(_args(invocation).get("name", "")).strip()
        if not name:
            return _err("name required")
        assert code_index is not None
        rendered = codelib.render_lookup(code_index, name)
        if not rendered:
            return _ok(f"No helper named {name!r} in the team code library.")
        out, _ = egress.scrub_text(rendered)
        return _ok(out)

    def search_helpers(invocation):
        from mooring.ai import codelib

        query = str(_args(invocation).get("query", "")).strip()
        if not query:
            return _err("query required")
        assert code_index is not None
        hits = code_index.search(query, limit=8)
        if not hits:
            return _ok(f"No helpers match {query!r}.")
        out, _ = egress.scrub_text(codelib.render_modules(hits, max_methods=12))
        return _ok(out)

    # The notebook-catalog tools serve the PRE-PARSED repo-wide entries
    # (mooring.ai.notebookindex): path, title, scanned summary, imports, and the inputs /
    # checks / SQL tables the SOURCE declares. Lookups are by NAME in the in-memory
    # Catalog — never a caller path, so a path-like argument that names no catalogued
    # notebook simply finds nothing — and every rendered string still passes
    # egress.scrub_text (defence in depth; the real value-blindness is the structural
    # allowlist, since egress is only a checksum floor). No tool returns cell source.

    def list_notebooks(_invocation):
        from mooring.ai import notebookindex

        assert catalog is not None  # catalog tools only registered when it is present
        listing, _ = egress.scrub_text(notebookindex.render_listing(catalog))
        return _ok(listing or "(the notebook catalog is empty)")

    def search_notebooks(invocation):
        from mooring.ai import notebookindex

        query = str(_args(invocation).get("query", "")).strip()
        if not query:
            return _err("query required")
        assert catalog is not None
        hits = catalog.search(query, limit=8)
        if not hits:
            return _ok(f"No notebooks match {query!r}.")
        out, _ = egress.scrub_text(notebookindex.render_notebooks(hits))
        return _ok(out)

    def describe_notebook(invocation):
        from mooring.ai import notebookindex

        name = str(_args(invocation).get("notebook", "")).strip()
        if not name:
            return _err("notebook required")
        assert catalog is not None
        entry = catalog.get(name)
        if entry is None:
            return _ok(f"No notebook named {name!r} in this workspace's catalog.")
        out, _ = egress.scrub_text(notebookindex.render_notebook(entry))
        return _ok(out)

    # The semantic-model tools serve the PRE-PARSED allowlist skeleton (tables,
    # columns+dataTypes, relationships, measure DAX — mooring.pbip_model; partition
    # M, roles, annotations, and translations were never parsed, so no tool can
    # reach them). Lookups are by NAME in the in-memory objects — an argument is
    # never treated as a filesystem path — and every rendered string passes the
    # egress scrub, because authored DAX can embed literal values.

    models = list(semantic_models or [])

    def _find_model(name: str):
        """By model name or artifact key, case-insensitive (in memory only)."""
        key = name.strip().strip("'\"").lower()
        for m in models:
            if key in (m.name.lower(), m.key.lower()):
                return m
        return None

    def get_semantic_model(invocation):
        name = str(_args(invocation).get("model", "")).strip()
        if name:
            model = _find_model(name)
            if model is None:
                return _ok(
                    f"No semantic model named {name!r} in this workspace."
                )
            picked = [model]
        else:
            picked = models
        rendered, _ = egress.scrub_text(
            "\n\n".join(pbip_model.render_summary(m) for m in picked)
        )
        return _ok(rendered)

    def describe_model_table(invocation):
        args = _args(invocation)
        table_name = str(args.get("table", "")).strip()
        if not table_name:
            return _err("table required")
        model_name = str(args.get("model", "")).strip()
        if model_name:
            model = _find_model(model_name)
            if model is None:
                return _ok(
                    f"No semantic model named {model_name!r} in this workspace."
                )
            search = [model]
        else:
            search = models
        for m in search:
            table = m.get_table(table_name)
            if table is not None:
                rendered, _ = egress.scrub_text(pbip_model.render_table(m, table))
                return _ok(rendered)
        return _ok(f"No table named {table_name!r} in the semantic model.")

    def get_measure(invocation):
        args = _args(invocation)
        measure_name = str(args.get("measure", "")).strip()
        if not measure_name:
            return _err("measure required")
        model_name = str(args.get("model", "")).strip()
        if model_name:
            model = _find_model(model_name)
            if model is None:
                return _ok(
                    f"No semantic model named {model_name!r} in this workspace."
                )
            search = [model]
        else:
            search = models
        for m in search:
            hit = m.find_measure(measure_name)
            if hit is not None:
                table, measure = hit
                rendered, _ = egress.scrub_text(pbip_model.render_measure(m, table, measure))
                return _ok(rendered)
        return _ok(f"No measure named {measure_name!r} in the semantic model.")

    specs = [
        ToolSpec(
            "mooring_list_datasets",
            "List the dataset files (parquet/csv/xlsx) available in this workspace.",
            handler=list_datasets,
            parameters={"type": "object", "properties": {}},
            skip_permission=True,  # value-free by construction; no prompt needed
        ),
        ToolSpec(
            "mooring_get_schema",
            "Get a dataset's schema: column names, dtypes, and row count. "
            "Returns ONLY the schema — never any data value.",
            handler=get_schema,
            parameters={
                "type": "object",
                "properties": {
                    "dataset": {
                        "type": "string",
                        "description": "workspace-relative path to a parquet/csv/xlsx file",
                    }
                },
                "required": ["dataset"],
            },
            skip_permission=True,  # returns schema only — value-free
        ),
        ToolSpec(
            "mooring_read_notebook_source",
            "Read the current marimo notebook's Python source code (no data values).",
            handler=read_notebook_source,
            parameters={"type": "object", "properties": {}},
            skip_permission=True,  # source only — value-free
        ),
    ] if allow_read_tools else []

    # The propose tool is the WRITE surface, and there is exactly ONE of it. It is gated
    # on a proposal callback, so a READ-ONLY session — an investigate sub-agent, built
    # with emit_proposal=None and emit_proposal_patch=None — registers NO way to write:
    # only the value-free read tools above (plus dictionary/model/helper reads). This gate
    # is a LOAD-BEARING privacy invariant: a sub-agent's finding is trusted because the
    # sub-agent is structurally value-blind (docs/admins/ai-privacy.md), which holds only
    # if no write/value-returning tool is ever added to a read-only session.
    #
    # It is `blocking`: it builds the candidate notebook and runs
    # `marimo_rt.validate_notebook_source` on it — measured end to end at ~50 ms for a
    # 12-cell notebook, ~170 ms at 48 cells and ~370 ms at 100, with the validator's own
    # 5 s cap under it — and that validator is not safe to call from a coroutine: it joins
    # its own worker thread, and it serializes on a module-wide lock, so a propose in one
    # chat session can queue behind another's. On the copilot path handlers run ON the
    # SDK's event-loop thread, which must stay free for streaming and teardown, so the
    # adapter hands a blocking handler to a worker thread. (The OpenAI session already
    # runs its loop on its own thread, where the flag is a no-op.)
    if emit_proposal is not None or emit_proposal_patch is not None:
        specs.append(
            ToolSpec(
                "mooring_propose_notebook_edit",
                "THE one tool that changes the notebook: propose new cells, edits to "
                "existing cells, deletions, or a wholesale rewrite — any mix, as ONE "
                "reviewable patch. You never write the file; the analyst sees a diff and "
                "applies it. To CHANGE what the notebook already does, EDIT the cell that "
                "does it: appending a second cell that defines the same name stops both "
                "cells and everything downstream. Indices are 0-based against the notebook "
                "AS IT IS NOW — call mooring_read_notebook_source first if anything has "
                "been applied since the cell view you were given. Every 'edits' and "
                "'deletes' entry MUST carry 'expect' (the first line of the cell you "
                "believe is at that index, plus the next line or two when that line is not "
                "unique to one cell); mooring checks it against the real cell and refuses "
                "the whole change unless it matches that cell and no other, which is what "
                "stops a stale index writing over a cell you never read." + _CELL_FORMAT,
                handler=propose_notebook_edit,
                parameters={
                    "type": "object",
                    "properties": {
                        "edits": {
                            "type": "array",
                            "description": "cells to replace with new source",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {
                                        "type": "integer",
                                        "description": "0-based index of the cell to replace",
                                    },
                                    "expect": {
                                        "type": "string",
                                        "description": (
                                            "the FIRST LINE of the cell you believe is at "
                                            "that index, copied from the cell view you were "
                                            "shown. Checked: the edit is refused if it does "
                                            "not match, and also if that line is not unique "
                                            "to one cell (every markdown cell opens with "
                                            "the same line) — then send the next line or "
                                            "two as well"
                                        ),
                                    },
                                    "code": {
                                        "type": "string",
                                        "description": "the new cell BODY (no @app.cell/def/return)",
                                    },
                                },
                                "required": ["index", "expect", "code"],
                            },
                        },
                        "appends": {
                            "type": "array",
                            "description": "BODIES of brand-new cells to add at the end",
                            "items": {"type": "string"},
                        },
                        "deletes": {
                            "type": "array",
                            "description": "cells to remove",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {
                                        "type": "integer",
                                        "description": "0-based index of the cell to remove",
                                    },
                                    "expect": {
                                        "type": "string",
                                        "description": (
                                            "the FIRST LINE of the cell you believe is at "
                                            "that index — plus the next line or two when "
                                            "that line is not unique to one cell (checked, "
                                            "as for an edit)"
                                        ),
                                    },
                                },
                                "required": ["index", "expect"],
                            },
                        },
                        "cells": {
                            "type": "array",
                            "description": (
                                "REWRITE: the full ordered list of cell BODIES that REPLACES "
                                "every existing cell. Heavier than an edit (every cell loses "
                                "its identity and re-runs) and it discards anything you did "
                                "not carry over — use it only for a wholesale rewrite, never "
                                "to add a cell, and never together with edits/appends/"
                                "deletes. Requires expect_cells."
                            ),
                            "items": {"type": "string"},
                        },
                        "expect_cells": {
                            "type": "integer",
                            "description": (
                                "with 'cells' only: how many cells you believe the notebook "
                                "has right now (checked; the rewrite is refused if it does "
                                "not match, because one built from a stale view would delete "
                                "cells you have not seen)"
                            ),
                        },
                        "rationale": {"type": "string", "description": _RATIONALE_DESC},
                    },
                },
                skip_permission=True,  # surfaces a proposal only; the analyst applies it
                blocking=True,  # static check off the loop (see the note above)
            )
        )

    if allow_read_tools and dictionary is not None and not dictionary.is_empty():
        specs += [
            ToolSpec(
                "mooring_list_tables",
                "List the tables in the team data dictionary (grouped by domain). "
                "Returns table names, column counts, and descriptions — never any data value.",
                handler=list_tables,
                parameters={"type": "object", "properties": {}},
                skip_permission=True,  # serves the value-minimised in-memory index
            ),
            ToolSpec(
                "mooring_describe_table",
                "Describe one data-dictionary table: its columns' names, types, "
                "nullability, foreign keys, and descriptions. Never any data value.",
                handler=describe_table,
                parameters={
                    "type": "object",
                    "properties": {
                        "table": {
                            "type": "string",
                            "description": "a table name (optionally domain-qualified, e.g. credit.fact_loans)",
                        }
                    },
                    "required": ["table"],
                },
                skip_permission=True,  # name lookup in-memory; never a path, never a value
            ),
            ToolSpec(
                "mooring_search_dictionary",
                "Search the data dictionary for tables/columns matching a query "
                "(use before writing a JOIN). Returns matching schemas — never any value.",
                handler=search_dictionary,
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "a table/column term to search for",
                        }
                    },
                    "required": ["query"],
                },
                skip_permission=True,  # searches the value-minimised in-memory index
            ),
        ]

    if allow_read_tools and code_index is not None and not code_index.is_empty():
        specs += [
            ToolSpec(
                "mooring_list_helpers",
                "List the team's reusable helper modules with each function/class NAME and "
                "signature (so you can reuse them instead of re-implementing) — never a body "
                "or any data value.",
                handler=list_helpers,
                parameters={"type": "object", "properties": {}},
                skip_permission=True,  # serves the value-free in-memory code index
            ),
            ToolSpec(
                "mooring_describe_helper",
                "Describe one helper (a module, function, class, or Class.method): its "
                "signature, type hints, docstring, and the exact `from ... import ...` line "
                "to reuse it. Never a function body or any data value.",
                handler=describe_helper,
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "a module import path, or a function/class/Class.method name",
                        }
                    },
                    "required": ["name"],
                },
                skip_permission=True,  # name lookup in-memory; never a path, never a value
            ),
            ToolSpec(
                "mooring_search_helpers",
                "Search the team's helper library for functions/classes matching a query "
                "(use before writing a helper yourself). Returns signatures — never a body.",
                handler=search_helpers,
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "a helper name/term to search for"}
                    },
                    "required": ["query"],
                },
                skip_permission=True,  # searches the value-free in-memory code index
            ),
        ]

    # Registered OUTSIDE the emit_proposal gate on purpose: these are read tools, so a
    # read-only investigate sub-agent gets them too — "which notebook already does this?"
    # is exactly the kind of independent sub-question a branch is spawned to answer.
    if allow_read_tools and catalog is not None and not catalog.is_empty():
        specs += [
            ToolSpec(
                "mooring_list_notebooks",
                "List every marimo notebook in this workspace with its title and a count of "
                "the inputs/checks it pins — the repo-wide catalog. Paths and titles only; "
                "never a cell output or any data value.",
                handler=list_notebooks,
                parameters={"type": "object", "properties": {}},
                skip_permission=True,  # serves the value-free in-memory catalog
            ),
            ToolSpec(
                "mooring_search_notebooks",
                "Search every notebook in this workspace by term — ALWAYS do this before "
                "writing a new analysis, to find whether a teammate already built it (search "
                "the metric, the dataset, or the table name). Matches titles, the notebooks' "
                "own descriptions, their imports, and the inputs/checks/SQL tables their "
                "source declares. Never a cell output or any data value.",
                handler=search_notebooks,
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "terms to search for (all must match), e.g. 'month end recon'",
                        }
                    },
                    "required": ["query"],
                },
                skip_permission=True,  # searches the value-free in-memory catalog
            ),
            ToolSpec(
                "mooring_describe_notebook",
                "Describe one catalogued notebook: its title, what it says it does, what it "
                "imports, the inputs it fingerprints, the checks it asserts, and the SQL "
                "tables it queries. Metadata only — it does NOT return that notebook's code "
                "(only the currently open notebook's source is readable).",
                handler=describe_notebook,
                parameters={
                    "type": "object",
                    "properties": {
                        "notebook": {
                            "type": "string",
                            "description": "a workspace-relative notebook path, file name, or title",
                        }
                    },
                    "required": ["notebook"],
                },
                skip_permission=True,  # name lookup in-memory; never a path, never a value
            ),
        ]

    if allow_read_tools and models:
        _MODEL_ARG = {
            "type": "string",
            "description": "the semantic model's name (only needed when several exist)",
        }
        specs += [
            ToolSpec(
                "mooring_get_semantic_model",
                "Summarise the workspace's Power BI semantic model(s): table names, "
                "column counts, measure NAMES, and relationships — no DAX (cheap to "
                "read; fetch detail per table or measure).",
                handler=get_semantic_model,
                parameters={"type": "object", "properties": {"model": _MODEL_ARG}},
                skip_permission=True,  # names only, from the pre-parsed in-memory model
            ),
            ToolSpec(
                "mooring_describe_model_table",
                "Describe one semantic-model table: columns with dataTypes, "
                "calculated-column DAX, that table's measures with DAX, and its "
                "relationships. Authored expressions only — never any data value.",
                handler=describe_model_table,
                parameters={
                    "type": "object",
                    "properties": {
                        "table": {"type": "string", "description": "a table name"},
                        "model": _MODEL_ARG,
                    },
                    "required": ["table"],
                },
                skip_permission=True,  # name lookup in-memory; never a path, never a value
            ),
            ToolSpec(
                "mooring_get_measure",
                "Fetch one measure's full DAX expression (plus format string and "
                "display folder) from the semantic model, by measure name.",
                handler=get_measure,
                parameters={
                    "type": "object",
                    "properties": {
                        "measure": {"type": "string", "description": "a measure name"},
                        "model": _MODEL_ARG,
                    },
                    "required": ["measure"],
                },
                skip_permission=True,  # name lookup in-memory; never a path, never a value
            ),
        ]

    # The fan-out tool. Registered ONLY when the caller injects ``run_investigation``
    # (the interactive parent session, never a read-only sub-agent — so an investigation
    # cannot recurse). Its handler runs the injected coordinator, which spawns read-only
    # value-blind sub-agents and returns their scrubbed, merged findings — value-free text
    # the model reads back as this tool's result (through the one egress mint), then turns
    # into ONE proposal the analyst Applies.
    if allow_read_tools and run_investigation is not None:

        def _progress(event: dict) -> None:
            """Render a value-free in-flight cue for the analyst. Carries COUNTS and
            statuses only — never a sub-question or a finding — so the progress channel
            opens no egress path (it goes to the local UI, not the model)."""
            if emit_tool_progress is None:
                return
            phase = event.get("phase")
            total = int(event.get("total", 0) or 0)
            done = int(event.get("done", 0) or 0)
            if not total:
                return
            if phase == "start":
                emit_tool_progress(f"researching {total} questions in parallel…")
            elif phase == "branch":
                emit_tool_progress(f"researched {done} of {total}…")
            elif phase == "done":
                found = int(event.get("found", 0) or 0)
                emit_tool_progress(f"merging findings from {found} of {total} branches…")

        def investigate(invocation):
            raw = _args(invocation).get("branches") or []
            branches = [
                b
                for b in raw
                if isinstance(b, dict) and str(b.get("question", "")).strip()
            ]
            if not branches:
                return _err(
                    "provide a non-empty 'branches' list of {question, notebook?, dataset?} objects"
                )
            try:
                merged = run_investigation(branches, on_progress=_progress)
            except Exception as exc:  # noqa: BLE001 - a coordinator error still yields a clean turn
                return _err(f"the investigation could not run: {exc}")
            return _ok(merged or "(the investigation produced no findings)")

        specs.append(
            ToolSpec(
                "mooring_investigate",
                "Research SEVERAL INDEPENDENT sub-questions IN PARALLEL before you propose. "
                "Each branch is answered by a separate read-only assistant that can inspect "
                "schemas, notebook source, the data dictionary, and semantic models — but "
                "CANNOT write. Use this when a task splits into independent parts (understand "
                "several notebooks, map several tables/models, or plan a join across datasets); "
                "the findings come back merged so you can then propose ONE change. Prefer it "
                "over asking many read questions yourself in series. Do NOT put any data value "
                "in a question — only names/paths and plain-English asks.",
                handler=investigate,
                parameters={
                    "type": "object",
                    "properties": {
                        "branches": {
                            "type": "array",
                            "description": "the independent sub-questions to research in parallel",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "question": {
                                        "type": "string",
                                        "description": "a value-free, plain-English sub-question",
                                    },
                                    "notebook": {
                                        "type": "string",
                                        "description": "optional workspace-relative notebook path to focus this branch on",
                                    },
                                    "dataset": {
                                        "type": "string",
                                        "description": "optional workspace-relative dataset path to give this branch its schema",
                                    },
                                },
                                "required": ["question"],
                            },
                        }
                    },
                    "required": ["branches"],
                },
                skip_permission=True,  # spawns only read-only value-free sub-agents; returns merged value-free findings
                blocking=True,  # drives N sub-sessions; must not run on an event loop
            )
        )
    if output_guard is None:
        return specs

    # Wrap the provider-neutral handler itself so every adapter gets the same final
    # egress gate. This covers successes, ordinary `_err` results, and unexpected
    # exceptions before either SDK can mint or transmit a tool result.
    def guarded(spec: ToolSpec) -> ToolSpec:
        raw_handler = spec.handler

        def handler(invocation):
            try:
                output = raw_handler(invocation)
            except Exception as exc:  # noqa: BLE001 - inspect/withhold exception text
                output = egress.ToolOutput(
                    text=f"tool {spec.name} failed: {exc}", is_error=True
                )
            try:
                allowed = output_guard(output.text)
            except Exception:  # noqa: BLE001 - a guard fault must withhold the output
                allowed = False
            if not allowed:
                return egress.ToolOutput(
                    text="tool output withheld by the approved data policy", is_error=True
                )
            return output

        return replace(spec, handler=handler)

    return [guarded(spec) for spec in specs]


def build_tools(
    *,
    workspace: Path,
    folders: tuple[str, ...],
    notebook_rel: str,
    emit_proposal: Callable[[str, str], None] | None = None,
    emit_proposal_patch: Callable[[dict], None] | None = None,
    dictionary=None,
    semantic_models=None,
    code_index=None,
    catalog=None,
    run_investigation: Callable[..., str] | None = None,
    emit_tool_progress: Callable[[str], None] | None = None,
    pii_enabled: bool = False,
    allow_read_tools: bool = True,
    trusted_customer_data: bool = False,
    output_guard: Callable[[str], bool] | None = None,
) -> list:
    """The GitHub Copilot adapter over :func:`build_tool_specs`.

    Wraps each provider-neutral :class:`ToolSpec` in a ``copilot.tools.Tool`` whose
    handler maps the spec's value-free :class:`~mooring.ai.egress.ToolOutput` onto a
    copilot ``ToolResult`` via the egress minters — so a ``ToolResult`` is still
    constructed ONLY inside egress (pinned by ``tests/test_egress.py``), and the
    copilot session (``available_tools=[t.name for t in tools]``) is unchanged.

    Kept as the SAME public entry point the copilot session and the tool tests use:
    it still returns ``copilot.tools.Tool`` objects with the same ``name`` /
    ``parameters`` / ``skip_permission`` and handlers that return a ``ToolResult``.
    The SDK import stays function-local (``copilot`` is the optional extra).
    """
    from copilot.tools import Tool

    from mooring.ai import egress

    specs = build_tool_specs(
        workspace=workspace,
        folders=folders,
        notebook_rel=notebook_rel,
        emit_proposal=emit_proposal,
        emit_proposal_patch=emit_proposal_patch,
        dictionary=dictionary,
        semantic_models=semantic_models,
        code_index=code_index,
        catalog=catalog,
        run_investigation=run_investigation,
        emit_tool_progress=emit_tool_progress,
        pii_enabled=pii_enabled,
        allow_read_tools=allow_read_tools,
        trusted_customer_data=trusted_customer_data,
        output_guard=output_guard,
    )

    def _to_tool(spec: ToolSpec):
        def run(invocation):
            out = spec.handler(invocation)
            if out.is_error:
                return egress.to_error_result(out.text)
            return egress.to_tool_result(out.text)

        # The SDK invokes handlers ON the session's asyncio loop thread and awaits an
        # awaitable result. A long, blocking handler (the investigate fan-out) would wedge
        # that loop — close() schedules its disconnect with run_coroutine_threadsafe and
        # would never get to run. Hand such a handler to a worker thread and await it, so
        # the loop stays free to service teardown and events while the fan-out runs.
        if spec.blocking:

            async def handler(invocation):
                import asyncio

                return await asyncio.to_thread(run, invocation)
        else:
            handler = run

        return Tool(
            spec.name,
            spec.description,
            handler=handler,
            parameters=spec.parameters,
            skip_permission=spec.skip_permission,
        )

    return [_to_tool(spec) for spec in specs]


def build_openai_tools(
    *,
    workspace: Path,
    folders: tuple[str, ...],
    notebook_rel: str,
    emit_proposal: Callable[[str, str], None] | None = None,
    emit_proposal_patch: Callable[[dict], None] | None = None,
    dictionary=None,
    semantic_models=None,
    code_index=None,
    catalog=None,
    run_investigation: Callable[..., str] | None = None,
    emit_tool_progress: Callable[[str], None] | None = None,
    pii_enabled: bool = False,
    allow_read_tools: bool = True,
    trusted_customer_data: bool = False,
    output_guard: Callable[[str], bool] | None = None,
) -> tuple[list[dict], dict[str, Callable[[object], "ToolOutput"]]]:
    """The OpenAI adapter over :func:`build_tool_specs`.

    Returns ``(tool_specs, dispatch)``: ``tool_specs`` is the OpenAI function-tool
    schema list — ``[{"type": "function", "function": {name, description,
    parameters}}]`` — passed verbatim as the ``tools=`` argument (the ``parameters``
    dicts are already plain JSON-Schema, reusable as-is); ``dispatch`` maps each tool
    name to its value-free handler, which the session's own tool-calling loop invokes
    and whose :class:`~mooring.ai.egress.ToolOutput` it mints through
    :func:`mooring.ai.egress.to_openai_tool_message`.

    This adapter is SDK-free by design (it only builds dicts) — the same value-free
    handlers as the copilot path, re-expressed as function specs. Only mooring's own
    tools are ever produced; a backend that runs this NEVER registers a hosted tool
    (web_search / file_search / code_interpreter), which is how value-blindness stays
    structural for a self-driven loop.
    """
    specs = build_tool_specs(
        workspace=workspace,
        folders=folders,
        notebook_rel=notebook_rel,
        emit_proposal=emit_proposal,
        emit_proposal_patch=emit_proposal_patch,
        dictionary=dictionary,
        semantic_models=semantic_models,
        code_index=code_index,
        catalog=catalog,
        run_investigation=run_investigation,
        emit_tool_progress=emit_tool_progress,
        pii_enabled=pii_enabled,
        allow_read_tools=allow_read_tools,
        trusted_customer_data=trusted_customer_data,
        output_guard=output_guard,
    )
    tool_specs = [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in specs
    ]
    dispatch = {spec.name: spec.handler for spec in specs}
    return tool_specs, dispatch
