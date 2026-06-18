"""The agent's safe tools must be value-free by construction."""

from __future__ import annotations

import types

import polars as pl
import pytest

from mooring.ai.tools import EDIT_TOOL_NAMES, TOOL_NAMES, build_tools

SECRET = "SECRET_VALUE_DO_NOT_LEAK"

# A valid 2-cell marimo notebook for the edit/rewrite tools (which read real cells).
_REAL_NB = (
    "import marimo\n\n"
    '__generated_with = "0.23.9"\n'
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    seed = 1\n"
    "    return (seed,)\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    x = seed + 1\n"
    "    return (x,)\n\n\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)


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


def _edit_tools(ws, patches, proposals=None):
    """The full toolset incl. the propose-edit/rewrite tools (patch callback wired)."""
    return {
        t.name: t
        for t in build_tools(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda code, rationale="": (proposals if proposals is not None else []).append(
                (code, rationale)
            ),
            emit_proposal_patch=patches.append,
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


def test_get_schema_withholds_pii_column_name_when_enabled(tmp_path):
    # A pivot/transpose on a PII key promotes a data VALUE to a column NAME.
    card = "4012888888881881"
    (tmp_path / "data").mkdir()
    pl.DataFrame({"id": [1], card: [2.0], "amount": [3]}).write_parquet(
        tmp_path / "data" / "wide.parquet"
    )
    tools = {
        t.name: t
        for t in build_tools(
            workspace=tmp_path,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda *a, **k: None,
            pii_enabled=True,
        )
    }
    out = tools["mooring_get_schema"].handler(_invocation(dataset="data/wide.parquet")).text_result_for_llm
    assert "id" in out and "amount" in out  # clean columns kept
    assert card not in out  # the PII-valued column NAME is withheld


def test_read_notebook_source_returns_code(ws):
    # A non-notebook script can't be enumerated, so the tool falls back to the raw
    # (scrubbed) source — the model still sees the code.
    tools = _tools(ws, [])
    out = tools["mooring_read_notebook_source"].handler(_invocation()).text_result_for_llm
    assert "import marimo" in out and "# notebook code" in out


def test_read_notebook_source_enumerates_real_cells(ws):
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    out = _tools(ws, [])["mooring_read_notebook_source"].handler(_invocation()).text_result_for_llm
    assert "=== cell 0 ===" in out and "=== cell 1 ===" in out
    assert "seed = 1" in out and "x = seed + 1" in out


def test_read_notebook_source_scrubs_checksum_pii(ws):
    # Closing the historical gap: the tool output now routes through the egress
    # scrubber, so a checksum-valid value in the source can't reach the model.
    card = "4012888888881881"  # Luhn-valid (shared with test_egress)
    (ws / "nb.py").write_text(_REAL_NB.replace("seed = 1", f"acct = {card}"), "utf-8")
    out = _tools(ws, [])["mooring_read_notebook_source"].handler(_invocation()).text_result_for_llm
    assert card not in out


def test_propose_cell_emits_and_does_not_inject(ws):
    proposals = []
    tools = _tools(ws, proposals)
    res = tools["mooring_propose_cell"].handler(
        _invocation(code="x = 1 + 1", rationale="demo")
    )
    assert proposals == [("x = 1 + 1", "demo")]  # surfaced to the analyst
    assert "apply" in res.text_result_for_llm.lower()  # the agent did not inject


def test_edit_tools_added_only_with_patch_callback(ws):
    base = _tools(ws, [])
    assert all(name not in base for name in EDIT_TOOL_NAMES)  # off without the callback
    full = _edit_tools(ws, [])
    assert sorted(full) == sorted(TOOL_NAMES + EDIT_TOOL_NAMES)
    for tool in full.values():
        assert tool.skip_permission is True  # still value-free by construction


def test_propose_cell_edit_captures_anchor_and_does_not_write(ws):
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    before = (ws / "nb.py").read_text("utf-8")
    patches = []
    res = _edit_tools(ws, patches)["mooring_propose_cell_edit"].handler(
        _invocation(index=1, code="x = seed + 99", rationale="bump")
    )
    assert "apply" in res.text_result_for_llm.lower()
    [payload] = patches
    assert payload["kind"] == "edit"
    assert payload["ops"][0] == {
        "op": "edit", "index": 1, "anchor": "x = seed + 1", "code": "x = seed + 99",
    }
    assert payload["diffs"][0]["before"] == "x = seed + 1"  # diff view gets the old code
    assert (ws / "nb.py").read_text("utf-8") == before  # propose-only; the analyst applies


def test_propose_cell_edit_out_of_range_errors(ws):
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    res = _edit_tools(ws, [])["mooring_propose_cell_edit"].handler(_invocation(index=9, code="z = 0"))
    assert res.result_type == "error"


def test_propose_notebook_edit_builds_combined_ops(ws):
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    patches = []
    _edit_tools(ws, patches)["mooring_propose_notebook_edit"].handler(
        _invocation(edits=[{"index": 0, "code": "seed = 2"}], appends=["extra = 1"], deletes=[1])
    )
    [payload] = patches
    assert payload["kind"] == "patch"
    assert [o["op"] for o in payload["ops"]] == ["edit", "delete", "append"]
    assert payload["ops"][0]["anchor"] == "seed = 1"  # server-captured, not retyped


def test_propose_notebook_rewrite_replaces_all(ws):
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    patches = []
    _edit_tools(ws, patches)["mooring_propose_notebook_rewrite"].handler(
        _invocation(cells=["a = 1", "b = a + 1"])
    )
    [payload] = patches
    assert payload["kind"] == "rewrite"
    assert payload["ops"][0] == {"op": "replace_all", "cells": ["a = 1", "b = a + 1"]}


def test_propose_tools_normalize_returns_in_cell_bodies(ws):
    # The model copies marimo's auto-generated `return` back from the file source; the
    # proposal (ops AND the diff preview) must show the cleaned BODY, not the return.
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    patches = []
    tools = _edit_tools(ws, patches)
    tools["mooring_propose_notebook_rewrite"].handler(
        _invocation(cells=["import marimo as mo\nreturn (mo,)", "z = 1\nreturn (z,)"])
    )
    assert patches[-1]["ops"][0]["cells"] == ["import marimo as mo", "z = 1"]
    assert "return" not in patches[-1]["diffs"][0]["after"]  # the preview matches the result

    tools["mooring_propose_cell_edit"].handler(
        _invocation(index=0, code="seed = 5\nreturn (seed,)")
    )
    assert patches[-1]["ops"][0]["code"] == "seed = 5"
