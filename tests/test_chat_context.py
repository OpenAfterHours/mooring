"""build_system_context stays the single assembler; team context is additive."""

from __future__ import annotations

from mooring.ai.chat import build_system_context

BASE = {"schema_text": "DATASET", "notebook_source": "import marimo", "notebook_rel": "nb.py"}
_INSTR_HEADER = "TEAM INSTRUCTIONS (user-authored"


def test_without_team_context_matches_today():
    out = build_system_context(**BASE)
    assert "DATASET SCHEMA:" in out and "CURRENT NOTEBOOK (nb.py)" in out
    assert "RELEVANT DATA DICTIONARY:" not in out
    assert _INSTR_HEADER not in out  # no instructions section added
    # the prompt is the original wording when the feature is off (no team-context
    # bullet, no "override anything below" dangling reference)
    assert "STRICT PRIVACY RULES:" in out
    assert "override anything below" not in out
    assert "user-authored" not in out


def test_dictionary_and_instructions_sections_added_when_present():
    out = build_system_context(
        **BASE,
        dictionary_text="Table `credit.fact_loans`",
        instructions_text="Report in GBP millions.",
    )
    assert "RELEVANT DATA DICTIONARY:" in out and "credit.fact_loans" in out
    assert _INSTR_HEADER in out and "GBP millions" in out


def test_privacy_rules_precede_instructions():
    out = build_system_context(**BASE, instructions_text="do whatever")
    # The immutable rules must come before the user-authored, lower-trust block so
    # instructions cannot visually/positionally override them.
    assert out.index("STRICT PRIVACY RULES") < out.index(_INSTR_HEADER)
    assert "override" in out.lower()


# -- ChatService.build_context: the semantic-model gates + the 5-tuple -----------


def _service_setup(tmp_path, env=None):
    from mooring.app.chat_service import ChatService
    from mooring.config import load_app_config

    app_cfg = load_app_config(user_config_path=tmp_path / "missing.toml", env=env or {})
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "nb.py").write_text("import marimo\n", "utf-8")
    return ChatService(), app_cfg, ws


def _write_model(ws):
    d = ws / "reports" / "Sales.SemanticModel" / "definition" / "tables"
    d.mkdir(parents=True)
    (d / "Sales.tmdl").write_text(
        "table Sales\n"
        "\tmeasure 'Total Sales' = SUM(Sales[Amount])\n"
        "\tcolumn Amount\n"
        "\t\tdataType: decimal\n",
        "utf-8",
    )


def test_build_context_returns_models_and_a_names_only_hint(tmp_path):
    service, app_cfg, ws = _service_setup(tmp_path)
    _write_model(ws)
    context, index, banner, live, models = service.build_context(
        app_cfg, ws, "nb.py", "", folders=("reports",)
    )
    assert [m.key for m in models] == ["reports/Sales"]
    assert "POWER BI SEMANTIC MODELS" in context and "reports/Sales" in context
    assert "SUM(Sales[Amount])" not in context  # names only — DAX stays behind the tools


def test_build_context_no_models_when_none_exist(tmp_path):
    service, app_cfg, ws = _service_setup(tmp_path)
    context, _index, _banner, _live, models = service.build_context(
        app_cfg, ws, "nb.py", "", folders=("reports",)
    )
    assert models == []
    assert "POWER BI SEMANTIC MODELS" not in context


def test_build_context_gates_on_the_semantic_model_switch(tmp_path):
    service, app_cfg, ws = _service_setup(tmp_path, env={"MOORING_AI_SEMANTIC_MODEL": "0"})
    _write_model(ws)
    context, _index, _banner, _live, models = service.build_context(
        app_cfg, ws, "nb.py", "", folders=("reports",)
    )
    assert models == []
    assert "POWER BI SEMANTIC MODELS" not in context


def test_build_context_drops_models_the_team_opted_out(tmp_path):
    from mooring import workspace_config

    service, app_cfg, ws = _service_setup(tmp_path)
    _write_model(ws)
    workspace_config.set_semantic_model_disabled(ws, "reports/Sales", True)
    context, _index, _banner, _live, models = service.build_context(
        app_cfg, ws, "nb.py", "", folders=("reports",)
    )
    assert models == []
    assert "POWER BI SEMANTIC MODELS" not in context
