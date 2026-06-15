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

# The tool names, used as the create_session ``available_tools`` allowlist.
TOOL_NAMES = [
    "mooring_list_datasets",
    "mooring_get_schema",
    "mooring_read_notebook_source",
    "mooring_propose_cell",
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
) -> list:
    """Build the safe tools, bound to one workspace + target notebook.

    Handlers follow the SDK's ``ToolHandler`` contract: a single ``ToolInvocation``
    argument (``invocation.arguments`` is the parsed args), returning a ToolResult.
    """
    from copilot.tools import Tool, ToolResult
    from mooring import schema

    def list_datasets(_invocation):
        found = schema.list_datasets(workspace, folders)
        return ToolResult(text_result_for_llm="\n".join(found) or "(no datasets found)")

    def get_schema(invocation):
        rel = str(_args(invocation).get("dataset", "")).strip()
        if not rel:
            return ToolResult(text_result_for_llm="", result_type="error", error="dataset required")
        try:
            target = _safe(workspace, rel)
            text = schema.format_for_ai(schema.extract_schema(target), source=rel)
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

    return [
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
