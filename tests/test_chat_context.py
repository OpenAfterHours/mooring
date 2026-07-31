"""build_system_context stays the single assembler; team context is additive."""

from __future__ import annotations

import pytest

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


def test_helpers_text_is_byte_identical_when_empty():
    base = build_system_context(**BASE)
    assert build_system_context(**BASE, helpers_text="") == base
    assert "RELEVANT HELPER MODULES" not in base
    with_helpers = build_system_context(**BASE, helpers_text="Module `utils.helpers`\n  def clean()")
    assert "RELEVANT HELPER MODULES" in with_helpers and "utils.helpers" in with_helpers


def test_build_context_seeds_and_returns_the_code_library(tmp_path):
    service, app_cfg, ws = _service_setup(tmp_path, env={"MOORING_AI_CODE_INDEX": "1"})
    (ws / "utils").mkdir()
    (ws / "utils" / "helpers.py").write_text(
        "def clean_dates(df, cols: list):\n"
        '    """Normalize dates."""\n'
        '    key = "SECRET_VALUE_DO_NOT_LEAK"\n'
        "    return key\n",
        "utf-8",
    )
    (ws / "nb.py").write_text("import utils.helpers\n", "utf-8")  # references the helper
    context, _i, _b, _l, _m, code_index, _cat = service.build_context(
        app_cfg, ws, "nb.py", "", folders=("utils",)
    )
    assert code_index is not None and not code_index.is_empty()
    assert "RELEVANT HELPER MODULES" in context
    assert "clean_dates(df, cols: list)" in context
    assert "from utils.helpers import" in context
    assert "SECRET_VALUE_DO_NOT_LEAK" not in context  # the body literal is dropped


def test_build_context_no_code_library_when_flag_off(tmp_path):
    service, app_cfg, ws = _service_setup(tmp_path)  # [ai] code_index defaults OFF
    (ws / "utils").mkdir()
    (ws / "utils" / "helpers.py").write_text("def h(): pass\n", "utf-8")
    (ws / "nb.py").write_text("import utils.helpers\n", "utf-8")
    context, _i, _b, _l, _m, code_index, _cat = service.build_context(
        app_cfg, ws, "nb.py", "", folders=("utils",)
    )
    assert code_index is None
    assert "RELEVANT HELPER MODULES" not in context


# -- ChatService.build_context: the repo-wide notebook catalog --------------------

_CATALOG_ON = {"MOORING_AI_NOTEBOOK_CATALOG": "1"}
_CATALOG_NB = (
    "import marimo\n\napp = marimo.App()\n\n"
    "@app.cell\ndef _():\n"
    "    import marimo as mo\n"
    '    mo.md("""# Month End Recon\n'
    "\n"
    "    Ties out the ledger. Top account SECRET_VALUE_DO_NOT_LEAK.\"\"\")\n"
    '    key = "SECRET_VALUE_DO_NOT_LEAK"\n'
    "    return\n"
)


def test_build_context_returns_the_catalog_but_never_bloats_the_context(tmp_path):
    # The catalog is deliberately tool-only: nothing about it enters the system context,
    # because a repo-wide listing would be paid on every turn even when never asked for.
    service, app_cfg, ws = _service_setup(tmp_path, env=_CATALOG_ON)
    (ws / "recon.py").write_text(_CATALOG_NB, "utf-8")
    context, *_rest, catalog = service.build_context(app_cfg, ws, "nb.py", "", folders=("",))
    assert catalog is not None and not catalog.is_empty()
    assert catalog.get("recon.py").title == "Month End Recon"
    assert "Month End Recon" not in context
    assert "SECRET_VALUE_DO_NOT_LEAK" not in context


def test_build_context_no_catalog_by_default(tmp_path):
    # OPT-IN: the catalog widens the model's view from one notebook to the whole repo and
    # its title slot is authored prose, so it ships off like context/code_index — NOT on
    # like semantic_model, whose extractor has no free-prose field at all.
    from mooring.ai_config import AiConfig

    service, app_cfg, ws = _service_setup(tmp_path)
    (ws / "recon.py").write_text(_CATALOG_NB, "utf-8")
    assert AiConfig().notebook_catalog is False  # the dataclass default...
    assert app_cfg.ai_notebook_catalog is False  # ...and what config_default.toml ships
    assert service.build_context(app_cfg, ws, "nb.py", "", folders=("",))[6] is None


def test_build_context_skips_the_catalog_when_no_folders_are_passed(tmp_path):
    # The batch planner's build_context deliberately passes no folders and discards
    # everything past [:2]; building a whole-repo index per job would be pure cost.
    service, app_cfg, ws = _service_setup(tmp_path, env=_CATALOG_ON)
    (ws / "recon.py").write_text(_CATALOG_NB, "utf-8")
    assert service.build_context(app_cfg, ws, "nb.py", "")[6] is None


def test_build_context_drops_notebooks_the_team_turned_ai_off_for(tmp_path):
    # The per-notebook opt-out means "don't let AI touch this" — so it must remove the
    # notebook from the searchable catalog too, not just refuse to open a chat on it.
    from mooring import workspace_config

    service, app_cfg, ws = _service_setup(tmp_path, env=_CATALOG_ON)
    (ws / "recon.py").write_text(_CATALOG_NB, "utf-8")
    workspace_config.set_ai_disabled(ws, "recon.py", True)
    catalog = service.build_context(app_cfg, ws, "nb.py", "", folders=("",))[6]
    assert catalog.get("recon.py") is None
    assert catalog.search("recon") == []


def test_build_context_gives_the_title_the_operators_full_scanner(tmp_path, monkeypatch):
    # S3: the title is the one authored-prose slot, and this is the egressing path, so it
    # must get the NER-capable pii.scan_prose with the operator's own [ai.pii] settings —
    # not the structured-only default the local hub listing uses (which would never catch
    # a person's name in a heading). Asserted at the seam, so no NER extra is needed.
    from mooring.ai.notebookindex import prosescan

    service, app_cfg, ws = _service_setup(
        tmp_path,
        env={
            **_CATALOG_ON,
            "MOORING_AI_PII": "1",
            "MOORING_AI_PII_NAMES": "1",
            "MOORING_AI_PII_NAME_THRESHOLD": "0.9",
        },
    )
    (ws / "recon.py").write_text(
        _CATALOG_NB.replace("Month End Recon", "Jane Smith quarterly"), "utf-8"
    )
    built: list[dict] = []

    def fake_make_scanner(**kw):
        built.append(kw)
        return lambda text: "person name" if "Jane" in text else None

    monkeypatch.setattr(prosescan, "make_scanner", fake_make_scanner)
    catalog = service.build_context(app_cfg, ws, "nb.py", "", folders=("",))[6]

    assert built and built[0]["names"] is True  # the name pass was armed...
    assert built[0]["threshold"] == 0.9  # ...with the operator's own settings
    assert catalog.get("recon.py").title == ""  # and the flagged title was withheld


def test_build_context_leaves_the_title_scanner_structured_when_the_pii_guard_is_off(
    tmp_path, monkeypatch
):
    from mooring.ai.notebookindex import prosescan

    service, app_cfg, ws = _service_setup(tmp_path, env=_CATALOG_ON)  # [ai.pii] off
    (ws / "recon.py").write_text(_CATALOG_NB, "utf-8")
    monkeypatch.setattr(
        prosescan, "make_scanner", lambda **kw: pytest.fail("must not build a NER scanner")
    )
    catalog = service.build_context(app_cfg, ws, "nb.py", "", folders=("",))[6]
    assert catalog.get("recon.py").title == "Month End Recon"


# -- ChatService.build_context: the semantic-model gates + the returned tuple ----


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
    context, index, banner, live, models, _code, _cat = service.build_context(
        app_cfg, ws, "nb.py", "", folders=("reports",)
    )
    assert [m.key for m in models] == ["reports/Sales"]
    assert "POWER BI SEMANTIC MODELS" in context and "reports/Sales" in context
    assert "SUM(Sales[Amount])" not in context  # names only — DAX stays behind the tools


def test_build_context_no_models_when_none_exist(tmp_path):
    service, app_cfg, ws = _service_setup(tmp_path)
    context, _index, _banner, _live, models, _code, _cat = service.build_context(
        app_cfg, ws, "nb.py", "", folders=("reports",)
    )
    assert models == []
    assert "POWER BI SEMANTIC MODELS" not in context


def test_build_context_gates_on_the_semantic_model_switch(tmp_path):
    service, app_cfg, ws = _service_setup(tmp_path, env={"MOORING_AI_SEMANTIC_MODEL": "0"})
    _write_model(ws)
    context, _index, _banner, _live, models, _code, _cat = service.build_context(
        app_cfg, ws, "nb.py", "", folders=("reports",)
    )
    assert models == []
    assert "POWER BI SEMANTIC MODELS" not in context


def test_build_context_drops_models_the_team_opted_out(tmp_path):
    from mooring import workspace_config

    service, app_cfg, ws = _service_setup(tmp_path)
    _write_model(ws)
    workspace_config.set_semantic_model_disabled(ws, "reports/Sales", True)
    context, _index, _banner, _live, models, _code, _cat = service.build_context(
        app_cfg, ws, "nb.py", "", folders=("reports",)
    )
    assert models == []
    assert "POWER BI SEMANTIC MODELS" not in context


def test_build_context_merges_multiple_offered_context_folders(tmp_path):
    # End-to-end: two OFFERED folders' instructions.md merge into one TEAM INSTRUCTIONS
    # block, in stable sorted-folder order, still below the immutable privacy rules.
    from mooring import workspace_config

    service, app_cfg, ws = _service_setup(tmp_path, env={"MOORING_AI_CONTEXT": "1"})
    workspace_config.set_context_folder(ws, "ctx_b", True)
    workspace_config.set_context_folder(ws, "ctx_a", True)
    (ws / "ctx_a").mkdir()
    (ws / "ctx_a" / "instructions.md").write_text("Report amounts in GBP.", "utf-8")
    (ws / "ctx_b").mkdir()
    (ws / "ctx_b" / "instructions.md").write_text("Fiscal year starts in April.", "utf-8")

    context, _index, _banner, _live, _models, _code, _cat = service.build_context(
        app_cfg, ws, "nb.py", ""
    )

    assert _INSTR_HEADER in context
    assert "Report amounts in GBP." in context and "Fiscal year starts in April." in context
    # sorted-folder order (ctx_a before ctx_b), each behind its value-free banner
    assert context.index("ctx_a/instructions.md") < context.index("ctx_b/instructions.md")
    assert context.index("Report amounts in GBP.") < context.index("Fiscal year starts in April.")
    # the immutable rules still precede the merged user-authored block
    assert context.index("STRICT PRIVACY RULES") < context.index(_INSTR_HEADER)
