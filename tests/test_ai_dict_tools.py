"""The three dictionary tools are value-free and bound to the in-memory index."""

from __future__ import annotations

import types

from mooring.ai.datadictionary import load_index
from mooring.ai.tools import DICT_TOOL_NAMES, TOOL_NAMES, build_tools

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


def _invocation(**arguments):
    return types.SimpleNamespace(arguments=arguments)


def _index(tmp_path):
    d = tmp_path / "context" / "dictionaries"
    d.mkdir(parents=True)
    (d / "credit.yaml").write_text(
        f"""
models:
  - name: fact_loans
    description: the book
    columns:
      - name: status
        data_type: varchar
        data_tests:
          - accepted_values: {{values: ['open', '{SECRET}']}}
""",
        "utf-8",
    )
    return load_index(tmp_path, "context")


def _tools(tmp_path):
    (tmp_path / "nb.py").write_text("import marimo\n", "utf-8")
    return {
        t.name: t
        for t in build_tools(
            workspace=tmp_path,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda *a, **k: None,
            dictionary=_index(tmp_path),
        )
    }


def test_dictionary_tools_added_when_index_present(tmp_path):
    tools = _tools(tmp_path)
    assert sorted(tools) == sorted(TOOL_NAMES + DICT_TOOL_NAMES)
    assert all(tools[n].skip_permission for n in DICT_TOOL_NAMES)


def test_no_dictionary_tools_without_index(tmp_path):
    (tmp_path / "nb.py").write_text("import marimo\n", "utf-8")
    tools = build_tools(
        workspace=tmp_path,
        folders=("data",),
        notebook_rel="nb.py",
        emit_proposal=lambda *a, **k: None,
    )
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)


def test_list_and_describe_are_value_free(tmp_path):
    tools = _tools(tmp_path)
    listing = tools["mooring_list_tables"].handler(_invocation()).text_result_for_llm
    assert "fact_loans" in listing and SECRET not in listing
    described = (
        tools["mooring_describe_table"].handler(_invocation(table="fact_loans")).text_result_for_llm
    )
    assert "status" in described and SECRET not in described  # accepted_values dropped


def test_search_is_value_free(tmp_path):
    out = (
        _tools(tmp_path)["mooring_search_dictionary"]
        .handler(_invocation(query="status"))
        .text_result_for_llm
    )
    assert "fact_loans" in out and SECRET not in out


def test_describe_path_like_name_finds_nothing(tmp_path):
    # The tool looks up a NAME in the parsed index; a path argument matches nothing
    # and touches no filesystem.
    res = _tools(tmp_path)["mooring_describe_table"].handler(_invocation(table="../../etc/passwd"))
    assert "No table" in res.text_result_for_llm
