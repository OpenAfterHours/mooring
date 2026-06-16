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

# Added only when the workspace has a parsed data dictionary. Each is value-free:
# it serves the already five-slot-allowlisted in-memory index, looking up by table
# NAME (never a filesystem path), so it can reach no data file or value.
DICT_TOOL_NAMES = [
    "mooring_list_tables",
    "mooring_describe_table",
    "mooring_search_dictionary",
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
    dictionary=None,
    pii_enabled: bool = False,
) -> list:
    """Build the safe tools, bound to one workspace + target notebook.

    Handlers follow the SDK's ``ToolHandler`` contract: a single ``ToolInvocation``
    argument (``invocation.arguments`` is the parsed args), returning a ToolResult.
    When ``dictionary`` (a :class:`mooring.ai.datadictionary.DictionaryIndex`) is
    non-empty, the three value-free dictionary tools are added. When ``pii_enabled``,
    ``get_schema`` withholds any column whose NAME is itself a PII value (a pivot/
    transpose on a PII key) — the second, dynamic schema egress (besides the system
    context) that the agent can reach at any time.
    """
    from dataclasses import replace

    from copilot.tools import Tool, ToolResult

    from mooring import schema
    from mooring.ai import pii

    def list_datasets(_invocation):
        found = schema.list_datasets(workspace, folders)
        return ToolResult(text_result_for_llm="\n".join(found) or "(no datasets found)")

    def get_schema(invocation):
        rel = str(_args(invocation).get("dataset", "")).strip()
        if not rel:
            return ToolResult(text_result_for_llm="", result_type="error", error="dataset required")
        try:
            target = _safe(workspace, rel)
            ds = schema.extract_schema(target)
            if pii_enabled:
                kept, col_findings = pii.scrub_columns(ds.columns)
                if col_findings:  # a column NAME is itself a PII value — withhold it
                    ds = replace(ds, columns=kept)
            text = schema.format_for_ai(ds, source=rel)
        except (ValueError, OSError) as exc:
            return ToolResult(
                text_result_for_llm="", result_type="error", error=f"cannot read schema: {exc}"
            )
        return ToolResult(text_result_for_llm=text)

    def read_notebook_source(_invocation):
        try:
            text = _safe(workspace, notebook_rel).read_text("utf-8")
        except (ValueError, OSError) as exc:
            return ToolResult(text_result_for_llm="", result_type="error", error=str(exc))
        return ToolResult(text_result_for_llm=text)

    def propose_cell(invocation):
        args = _args(invocation)
        code = str(args.get("code", ""))
        rationale = str(args.get("rationale", ""))
        if not code.strip():
            return ToolResult(text_result_for_llm="", result_type="error", error="code required")
        emit_proposal(code, rationale)
        return ToolResult(
            text_result_for_llm="Proposed the cell to the analyst, who will review and apply it."
        )

    def list_tables(_invocation):
        from mooring.ai.datadictionary import render_listing

        return ToolResult(
            text_result_for_llm=render_listing(dictionary) or "(the data dictionary is empty)"
        )

    def describe_table(invocation):
        from mooring.ai.datadictionary import render_table

        name = str(_args(invocation).get("table", "")).strip()
        if not name:
            return ToolResult(text_result_for_llm="", result_type="error", error="table required")
        table = dictionary.get(name)
        if table is None:
            return ToolResult(text_result_for_llm=f"No table named {name!r} in the data dictionary.")
        return ToolResult(text_result_for_llm=render_table(table))

    def search_dictionary(invocation):
        from mooring.ai.datadictionary import render_table

        query = str(_args(invocation).get("query", "")).strip()
        if not query:
            return ToolResult(text_result_for_llm="", result_type="error", error="query required")
        hits = dictionary.search(query, limit=8)
        if not hits:
            return ToolResult(text_result_for_llm=f"No dictionary tables match {query!r}.")
        return ToolResult(text_result_for_llm="\n\n".join(render_table(t, max_cols=12) for t in hits))

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
            "Use this to suggest code; the analyst reviews and applies it.",
            handler=propose_cell,
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "the Python code for the cell"},
                    "rationale": {"type": "string", "description": "a one-line reason (optional)"},
                },
                "required": ["code"],
            },
            skip_permission=True,  # only surfaces a proposal to the analyst; never injects
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
                        "query": {"type": "string", "description": "a table/column term to search for"}
                    },
                    "required": ["query"],
                },
                skip_permission=True,  # searches the value-minimised in-memory index
            ),
        ]
    return tools
