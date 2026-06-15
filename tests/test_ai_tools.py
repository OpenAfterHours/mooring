"""The agent's safe tools must be value-free by construction."""

from __future__ import annotations

import types

import polars as pl
import pytest

from mooring.ai.tools import TOOL_NAMES, build_tools

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


def _invocation(**arguments):
    return types.SimpleNamespace(
        session_id="s", tool_call_id="t", tool_name="x", arguments=arguments
    )


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "data").mkdir()
    pl.DataFrame({"region": [SECRET], "amount": [123456]}).write_parquet(
        tmp_path / "data" / "sales.parquet"
    )
    (tmp_path / "nb.py").write_text("import marimo\n# notebook code\n", "utf-8")
    return tmp_path


def _tools(ws, proposals):
    return {
        t.name: t
        for t in build_tools(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda code, rationale="": proposals.append((code, rationale)),
        )
    }


def test_tool_names_match_built_tools(ws):
    tools = _tools(ws, [])
    assert sorted(tools) == sorted(TOOL_NAMES)


def test_all_tools_skip_permission(ws):
    # The tools are value-free, so they bypass the deny-all backstop (which would
    # otherwise block them); deny-all + the available_tools allowlist still guard
    # against any built-in tool.
    for tool in _tools(ws, []).values():
        assert tool.skip_permission is True


def test_list_datasets_returns_paths_only(ws):
    tools = _tools(ws, [])
    out = tools["mooring_list_datasets"].handler(_invocation()).text_result_for_llm
    assert "data/sales.parquet" in out
    assert SECRET not in out


def test_get_schema_is_value_free(ws):
    tools = _tools(ws, [])
    res = tools["mooring_get_schema"].handler(_invocation(dataset="data/sales.parquet"))
    out = res.text_result_for_llm
    assert "region" in out and "amount" in out  # column names present
    assert SECRET not in out and "123456" not in out  # values never


def test_get_schema_rejects_traversal(ws):
    tools = _tools(ws, [])
    res = tools["mooring_get_schema"].handler(_invocation(dataset="../escape.parquet"))
    assert res.result_type == "error"


def test_read_notebook_source_returns_code(ws):
    tools = _tools(ws, [])
    out = tools["mooring_read_notebook_source"].handler(_invocation()).text_result_for_llm
    assert "import marimo" in out and "# notebook code" in out


def test_propose_cell_emits_and_does_not_inject(ws):
    proposals = []
    tools = _tools(ws, proposals)
    res = tools["mooring_propose_cell"].handler(
        _invocation(code="x = 1 + 1", rationale="demo")
    )
    assert proposals == [("x = 1 + 1", "demo")]  # surfaced to the analyst
    assert "apply" in res.text_result_for_llm.lower()  # the agent did not inject
