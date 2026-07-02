"""The mooring-mediated safe toolset given to the Copilot agent.

Every tool here is **value-free by construction** — it returns only a dataset's
SCHEMA (names + dtypes, via the trusted ``schema`` module), the notebook's
SOURCE code, or a list of dataset paths. None can reach a data value, a cell
output, or the kernel. ``propose_cell`` does NOT write the notebook; it surfaces
a proposal to the chat UI, and the analyst Applies it.

Combined with ``available_tools`` allowlisting exactly these names (so the SDK's
built-in file/shell tools are dropped) and a deny-all permission backstop, the
agent has no path to data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

# The always-on tools. The session's ``available_tools`` allowlist is derived
# from the tools actually built (these plus the dictionary tools when a data
# dictionary is present), so it stays in lock-step with what is registered.
TOOL_NAMES = [
    "mooring_list_datasets",
    "mooring_get_schema",
    "mooring_read_notebook_source",
    "mooring_propose_cell",
]

# Added when the caller supplies an ``emit_proposal_patch`` callback (the real chat
# session always does). These let the model propose EDITING an existing cell or
# rewriting the notebook — each is still propose-only (the analyst Applies it) and
# value-free (it emits source code to the local UI, never reads a data value).
EDIT_TOOL_NAMES = [
    "mooring_propose_cell_edit",
    "mooring_propose_notebook_edit",
    "mooring_propose_notebook_rewrite",
]

# Cell-source format reminder for every propose tool. The displayed file source shows
# marimo's wrapper (`@app.cell` / `def _()` / a trailing `return (...)`); a cell's
# source is the BODY ONLY — mooring regenerates the wrapper and the return.
_RATIONALE_DESC = "a one-line reason (optional)"

_CELL_FORMAT = (
    " Each cell is the BODY ONLY (top-level statements) — do NOT include '@app.cell', "
    "'def _():', or a trailing 'return (...)'; those are added automatically."
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


def _safe(workspace: Path, rel: str) -> Path:
    target = (workspace / rel).resolve()
    target.relative_to(workspace.resolve())  # raises ValueError on escape
    return target


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


def build_tools(
    *,
    workspace: Path,
    folders: tuple[str, ...],
    notebook_rel: str,
    emit_proposal: Callable[[str, str], None],
    emit_proposal_patch: Callable[[dict], None] | None = None,
    dictionary=None,
    semantic_models=None,
    pii_enabled: bool = False,
) -> list:
    """Build the safe tools, bound to one workspace + target notebook.

    Handlers follow the SDK's ``ToolHandler`` contract: a single ``ToolInvocation``
    argument (``invocation.arguments`` is the parsed args), returning a ToolResult.
    When ``dictionary`` (a :class:`mooring.ai.datadictionary.DictionaryIndex`) is
    non-empty, the three value-free dictionary tools are added. When
    ``semantic_models`` (pre-parsed :class:`mooring.pbip_model.SemanticModel`
    objects — the caller has already applied the config gate and the synced
    per-model opt-out) is non-empty, the three model tools are added. When
    ``pii_enabled``, ``get_schema`` withholds any column whose NAME is itself a PII
    value (a pivot/transpose on a PII key) — the second, dynamic schema egress
    (besides the system context) that the agent can reach at any time.

    ``emit_proposal_patch`` (supplied by the real chat session) enables the
    edit/rewrite tools: each captures the target cell's current source as an
    ``anchor`` and emits a structured proposal ``{kind, ops, diffs}`` to the local
    UI for the analyst to review and Apply (never an autonomous write).
    """
    from dataclasses import replace

    from copilot.tools import Tool

    from mooring import marimo_rt, pbip_model, schema
    from mooring.ai import egress

    def _err(msg: str):
        # egress mints (and scrubs) the error channel — exception text can quote
        # user input, so it gets the same checksum-PII floor as the text channel.
        return egress.to_error_result(msg)

    def list_datasets(_invocation):
        found = schema.list_datasets(workspace, folders)
        return egress.to_tool_result("\n".join(found) or "(no datasets found)")

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
        return egress.to_tool_result(text)

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
        # bypass it). On any parse trouble, fall back to the scrubbed raw source.
        try:
            raw = _safe(workspace, notebook_rel).read_text("utf-8")
        except (ValueError, OSError) as exc:
            return _err(str(exc))
        try:
            cells = marimo_rt.read_cells(raw)
        except _NB_READ_ERRORS:
            cells = []
        if cells:
            body = "\n\n".join(f"# === cell {i} ===\n{code}" for i, code in cells)
            rendered = (
                f"The notebook has {len(cells)} cell(s); each is shown with its index "
                "(use mooring_propose_cell_edit to change one):\n\n" + body
            )
        else:  # not a parseable marimo notebook — show the raw source instead
            rendered = raw
        scrubbed, _ = egress.scrub_text(rendered)
        return egress.to_tool_result(scrubbed)

    def propose_cell(invocation):
        args = _args(invocation)
        code = marimo_rt.normalize_cell_code(str(args.get("code", "")))
        rationale = str(args.get("rationale", ""))
        if not code.strip():
            return _err("code required")
        emit_proposal(code, rationale)
        return egress.to_tool_result(
            "Proposed the cell to the analyst, who will review and apply it."
        )

    def _coerce_index(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def propose_cell_edit(invocation):
        args = _args(invocation)
        code = marimo_rt.normalize_cell_code(str(args.get("code", "")))
        rationale = str(args.get("rationale", ""))
        if not code.strip():
            return _err("code required")
        idx = _coerce_index(args.get("index"))
        if idx is None:
            return _err("index (the integer cell number to edit) is required")
        try:
            cells = _current_cells()
        except _NB_READ_ERRORS as exc:
            return _err(f"cannot read the notebook cells: {exc}")
        if not 0 <= idx < len(cells):
            return _err(f"index must be 0..{len(cells) - 1} (the notebook has {len(cells)} cells)")
        anchor = cells[idx][1]
        assert emit_proposal_patch is not None  # tool only registered when the callback exists
        emit_proposal_patch(
            {
                "kind": "edit",
                "rationale": rationale,
                "ops": [{"op": "edit", "index": idx, "anchor": anchor, "code": code}],
                "diffs": [{"label": f"cell {idx}", "before": anchor, "after": code}],
            }
        )
        return egress.to_tool_result(
            f"Proposed an edit to cell {idx} for the analyst to review and apply."
        )

    def propose_notebook_edit(invocation):
        args = _args(invocation)
        rationale = str(args.get("rationale", ""))
        try:
            cells = _current_cells()
        except _NB_READ_ERRORS as exc:
            return _err(f"cannot read the notebook cells: {exc}")
        n = len(cells)
        ops: list[dict] = []
        diffs: list[dict] = []
        targeted: set[int] = set()
        for edit in args.get("edits") or []:
            if not isinstance(edit, dict):
                return _err("each entry in 'edits' must be an object {index, code}")
            idx = _coerce_index(edit.get("index"))
            code = marimo_rt.normalize_cell_code(str(edit.get("code", "")))
            if idx is None:
                return _err("each edit needs an integer 'index'")
            if not code.strip():
                return _err(f"the edit for cell {idx} has no code")
            if not 0 <= idx < n:
                return _err(f"edit index {idx} is out of range 0..{n - 1}")
            if idx in targeted:
                return _err(f"cell {idx} is targeted more than once")
            targeted.add(idx)
            anchor = cells[idx][1]
            ops.append({"op": "edit", "index": idx, "anchor": anchor, "code": code})
            diffs.append({"label": f"cell {idx}", "before": anchor, "after": code})
        for raw in args.get("deletes") or []:
            idx = _coerce_index(raw)
            if idx is None:
                return _err("each entry in 'deletes' must be an integer cell index")
            if not 0 <= idx < n:
                return _err(f"delete index {idx} is out of range 0..{n - 1}")
            if idx in targeted:
                return _err(f"cell {idx} is targeted more than once")
            targeted.add(idx)
            anchor = cells[idx][1]
            ops.append({"op": "delete", "index": idx, "anchor": anchor})
            diffs.append({"label": f"cell {idx} (deleted)", "before": anchor, "after": ""})
        for raw in args.get("appends") or []:
            code = marimo_rt.normalize_cell_code(
                str(raw.get("code", "") if isinstance(raw, dict) else raw)
            )
            if not code.strip():
                return _err("an appended cell has no code")
            ops.append({"op": "append", "code": code})
            diffs.append({"label": "new cell", "before": "", "after": code})
        if not ops:
            return _err("provide at least one of edits, appends, or deletes")
        assert emit_proposal_patch is not None  # tool only registered when the callback exists
        emit_proposal_patch({"kind": "patch", "rationale": rationale, "ops": ops, "diffs": diffs})
        return egress.to_tool_result(
            f"Proposed {len(ops)} change(s) to the notebook for the analyst to review and apply."
        )

    def propose_notebook_rewrite(invocation):
        args = _args(invocation)
        rationale = str(args.get("rationale", ""))
        new_cells = [
            marimo_rt.normalize_cell_code(str(c.get("code", "") if isinstance(c, dict) else c))
            for c in (args.get("cells") or [])
        ]
        new_cells = [c for c in new_cells if c.strip()]
        if not new_cells:
            return _err("a rewrite needs a non-empty 'cells' list of cell source strings")
        try:
            before = "\n\n".join(code for _, code in _current_cells())
        except _NB_READ_ERRORS:
            before = ""  # still allow the rewrite; the diff just reads as all-additions
        assert emit_proposal_patch is not None  # tool only registered when the callback exists
        emit_proposal_patch(
            {
                "kind": "rewrite",
                "rationale": rationale,
                "ops": [{"op": "replace_all", "cells": new_cells}],
                "diffs": [
                    {"label": "whole notebook", "before": before, "after": "\n\n".join(new_cells)}
                ],
            }
        )
        return egress.to_tool_result(
            f"Proposed a full rewrite ({len(new_cells)} cells) for the analyst to review and apply."
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
        return egress.to_tool_result(listing or "(the data dictionary is empty)")

    def describe_table(invocation):
        from mooring.ai.datadictionary import render_table

        name = str(_args(invocation).get("table", "")).strip()
        if not name:
            return _err("table required")
        assert dictionary is not None  # dictionary tools only registered when it is present
        table = dictionary.get(name)
        if table is None:
            return egress.to_tool_result(f"No table named {name!r} in the data dictionary.")
        rendered, _ = egress.scrub_text(render_table(table))
        return egress.to_tool_result(rendered)

    def search_dictionary(invocation):
        from mooring.ai.datadictionary import render_table

        query = str(_args(invocation).get("query", "")).strip()
        if not query:
            return _err("query required")
        assert dictionary is not None  # dictionary tools only registered when it is present
        hits = dictionary.search(query, limit=8)
        if not hits:
            return egress.to_tool_result(f"No dictionary tables match {query!r}.")
        rendered, _ = egress.scrub_text("\n\n".join(render_table(t, max_cols=12) for t in hits))
        return egress.to_tool_result(rendered)

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
                return egress.to_tool_result(
                    f"No semantic model named {name!r} in this workspace."
                )
            picked = [model]
        else:
            picked = models
        rendered, _ = egress.scrub_text(
            "\n\n".join(pbip_model.render_summary(m) for m in picked)
        )
        return egress.to_tool_result(rendered)

    def describe_model_table(invocation):
        args = _args(invocation)
        table_name = str(args.get("table", "")).strip()
        if not table_name:
            return _err("table required")
        model_name = str(args.get("model", "")).strip()
        if model_name:
            model = _find_model(model_name)
            if model is None:
                return egress.to_tool_result(
                    f"No semantic model named {model_name!r} in this workspace."
                )
            search = [model]
        else:
            search = models
        for m in search:
            table = m.get_table(table_name)
            if table is not None:
                rendered, _ = egress.scrub_text(pbip_model.render_table(m, table))
                return egress.to_tool_result(rendered)
        return egress.to_tool_result(f"No table named {table_name!r} in the semantic model.")

    def get_measure(invocation):
        args = _args(invocation)
        measure_name = str(args.get("measure", "")).strip()
        if not measure_name:
            return _err("measure required")
        model_name = str(args.get("model", "")).strip()
        if model_name:
            model = _find_model(model_name)
            if model is None:
                return egress.to_tool_result(
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
                return egress.to_tool_result(rendered)
        return egress.to_tool_result(f"No measure named {measure_name!r} in the semantic model.")

    tools = [
        Tool(
            "mooring_list_datasets",
            "List the dataset files (parquet/csv/xlsx) available in this workspace.",
            handler=list_datasets,
            parameters={"type": "object", "properties": {}},
            skip_permission=True,  # value-free by construction; no prompt needed
        ),
        Tool(
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
        Tool(
            "mooring_read_notebook_source",
            "Read the current marimo notebook's Python source code (no data values).",
            handler=read_notebook_source,
            parameters={"type": "object", "properties": {}},
            skip_permission=True,  # source only — value-free
        ),
        Tool(
            "mooring_propose_cell",
            "Propose a Python cell for the analyst to apply into the notebook. "
            "Use this to suggest code; the analyst reviews and applies it." + _CELL_FORMAT,
            handler=propose_cell,
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "the cell BODY (no @app.cell/def/return)",
                    },
                    "rationale": {"type": "string", "description": _RATIONALE_DESC},
                },
                "required": ["code"],
            },
            skip_permission=True,  # only surfaces a proposal to the analyst; never injects
        ),
    ]

    if emit_proposal_patch is not None:
        tools += [
            Tool(
                "mooring_propose_cell_edit",
                "Propose REPLACING an existing cell's code. Read the notebook first "
                "(mooring_read_notebook_source) to get cell indices. The analyst sees a "
                "diff and applies it; only that cell (and its dependents) re-runs." + _CELL_FORMAT,
                handler=propose_cell_edit,
                parameters={
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "the cell number to edit (0-based)",
                        },
                        "code": {
                            "type": "string",
                            "description": "the new cell BODY (no @app.cell/def/return)",
                        },
                        "rationale": {"type": "string", "description": _RATIONALE_DESC},
                    },
                    "required": ["index", "code"],
                },
                skip_permission=True,  # surfaces a proposal only; the analyst applies it
            ),
            Tool(
                "mooring_propose_notebook_edit",
                "Propose SEVERAL cell changes at once — edits, appends, and deletes — "
                "as one reviewable patch (prefer this for a multi-cell change). Indices "
                "are 0-based against the current notebook. The analyst reviews and applies."
                + _CELL_FORMAT,
                handler=propose_notebook_edit,
                parameters={
                    "type": "object",
                    "properties": {
                        "edits": {
                            "type": "array",
                            "description": "cells to replace",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "integer"},
                                    "code": {"type": "string"},
                                },
                                "required": ["index", "code"],
                            },
                        },
                        "appends": {
                            "type": "array",
                            "description": "source of new cells to add at the end",
                            "items": {"type": "string"},
                        },
                        "deletes": {
                            "type": "array",
                            "description": "indices of cells to remove",
                            "items": {"type": "integer"},
                        },
                        "rationale": {"type": "string", "description": _RATIONALE_DESC},
                    },
                },
                skip_permission=True,  # surfaces a proposal only; the analyst applies it
            ),
            Tool(
                "mooring_propose_notebook_rewrite",
                "Propose REPLACING THE WHOLE notebook with a new ordered list of cells. "
                "Heavier than an edit (every changed cell re-runs and loses its identity) — "
                "PREFER mooring_propose_notebook_edit for targeted changes; use this only for "
                "a wholesale rewrite. The analyst reviews a full diff and applies." + _CELL_FORMAT,
                handler=propose_notebook_rewrite,
                parameters={
                    "type": "object",
                    "properties": {
                        "cells": {
                            "type": "array",
                            "description": "the full ordered list of cell BODIES (each: no @app.cell/def/return)",
                            "items": {"type": "string"},
                        },
                        "rationale": {"type": "string", "description": _RATIONALE_DESC},
                    },
                    "required": ["cells"],
                },
                skip_permission=True,  # surfaces a proposal only; the analyst applies it
            ),
        ]

    if dictionary is not None and not dictionary.is_empty():
        tools += [
            Tool(
                "mooring_list_tables",
                "List the tables in the team data dictionary (grouped by domain). "
                "Returns table names, column counts, and descriptions — never any data value.",
                handler=list_tables,
                parameters={"type": "object", "properties": {}},
                skip_permission=True,  # serves the value-minimised in-memory index
            ),
            Tool(
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
            Tool(
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

    if models:
        _MODEL_ARG = {
            "type": "string",
            "description": "the semantic model's name (only needed when several exist)",
        }
        tools += [
            Tool(
                "mooring_get_semantic_model",
                "Summarise the workspace's Power BI semantic model(s): table names, "
                "column counts, measure NAMES, and relationships — no DAX (cheap to "
                "read; fetch detail per table or measure).",
                handler=get_semantic_model,
                parameters={"type": "object", "properties": {"model": _MODEL_ARG}},
                skip_permission=True,  # names only, from the pre-parsed in-memory model
            ),
            Tool(
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
            Tool(
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
    return tools
