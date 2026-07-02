"""The three semantic-model tools are value-free and bound to the in-memory
pre-parsed models (the test_ai_dict_tools structure, for the PBIP trio)."""

from __future__ import annotations

import types

from mooring import pbip_model
from mooring.ai.tools import MODEL_TOOL_NAMES, TOOL_NAMES, build_tools

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
VALID_CARD = "4012888888881881"  # Luhn-valid, not on any test-PAN list


def _invocation(**arguments):
    return types.SimpleNamespace(arguments=arguments)


def _model(tmp_path, *, measure_dax="SUM(Sales[Amount])"):
    d = tmp_path / "reports" / "Sales.SemanticModel" / "definition"
    (d / "tables").mkdir(parents=True)
    (d / "roles").mkdir()
    (d / "roles" / "R.tmdl").write_text(f"role R\n\ttablePermission Sales = {SECRET}\n", "utf-8")
    (d / "tables" / "Sales.tmdl").write_text(
        "table Sales\n"
        f"\tmeasure 'Gross Margin %' = {measure_dax}\n"
        "\t\tformatString: 0.00%\n"
        f"\t\tannotation Leak = {SECRET}\n"
        "\tcolumn Amount\n"
        "\t\tdataType: decimal\n"
        "\tpartition Sales = m\n"
        "\t\tsource =\n"
        f'\t\t\t\tSql.Database("{SECRET}", "db")\n',
        "utf-8",
    )
    return pbip_model.extract_model(
        tmp_path / "reports" / "Sales.SemanticModel", key="reports/Sales"
    )


def _tools(tmp_path, **extract_kwargs):
    (tmp_path / "nb.py").write_text("import marimo\n", "utf-8")
    return {
        t.name: t
        for t in build_tools(
            workspace=tmp_path,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda *a, **k: None,
            semantic_models=[_model(tmp_path, **extract_kwargs)],
        )
    }


def test_model_tools_added_when_models_present(tmp_path):
    tools = _tools(tmp_path)
    assert sorted(tools) == sorted(TOOL_NAMES + MODEL_TOOL_NAMES)
    assert all(tools[n].skip_permission for n in MODEL_TOOL_NAMES)


def test_no_model_tools_without_models(tmp_path):
    (tmp_path / "nb.py").write_text("import marimo\n", "utf-8")
    for absent in (None, []):
        tools = build_tools(
            workspace=tmp_path,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda *a, **k: None,
            semantic_models=absent,
        )
        assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)


def test_summary_and_table_are_value_free(tmp_path):
    tools = _tools(tmp_path)
    summary = tools["mooring_get_semantic_model"].handler(_invocation()).text_result_for_llm
    assert "Sales" in summary and "Gross Margin %" in summary
    assert SECRET not in summary
    assert "SUM(" not in summary  # the summary is names-only: no DAX
    described = (
        tools["mooring_describe_model_table"]
        .handler(_invocation(table="Sales"))
        .text_result_for_llm
    )
    assert "Amount: decimal" in described and "SUM(Sales[Amount])" in described
    assert SECRET not in described  # partition M / role / annotation never captured


def test_get_measure_returns_the_dax(tmp_path):
    out = (
        _tools(tmp_path)["mooring_get_measure"]
        .handler(_invocation(measure="gross margin %"))  # case-insensitive name lookup
        .text_result_for_llm
    )
    assert "SUM(Sales[Amount])" in out and "formatString: 0.00%" in out
    assert SECRET not in out


def test_tool_output_gets_the_checksum_scrub(tmp_path):
    # Authored DAX can embed literal values — the tool path must drop the line
    # via egress.scrub_text, exactly like the dictionary tools.
    tools = _tools(tmp_path, measure_dax=f'IF(Sales[card] = "{VALID_CARD}", 1, 0)')
    described = (
        tools["mooring_describe_model_table"]
        .handler(_invocation(table="Sales"))
        .text_result_for_llm
    )
    assert VALID_CARD not in described
    assert "Amount: decimal" in described  # the clean lines still render
    fetched = (
        tools["mooring_get_measure"]
        .handler(_invocation(measure="Gross Margin %"))
        .text_result_for_llm
    )
    assert VALID_CARD not in fetched


def test_name_lookup_cannot_reach_a_path(tmp_path):
    # The tools look up NAMES in the parsed in-memory models; a path argument
    # matches nothing and touches no filesystem.
    tools = _tools(tmp_path)
    res = tools["mooring_describe_model_table"].handler(
        _invocation(table="../../etc/passwd")
    )
    assert "No table" in res.text_result_for_llm
    res = tools["mooring_get_measure"].handler(_invocation(measure="C:/secrets.txt"))
    assert "No measure" in res.text_result_for_llm
    res = tools["mooring_get_semantic_model"].handler(_invocation(model="../outside"))
    assert "No semantic model" in res.text_result_for_llm


def test_missing_args_are_errors(tmp_path):
    tools = _tools(tmp_path)
    assert tools["mooring_describe_model_table"].handler(_invocation()).result_type == "error"
    assert tools["mooring_get_measure"].handler(_invocation()).result_type == "error"
