"""build_system_context stays the single assembler; team context is additive."""

from __future__ import annotations

from mooring.ai.chat import build_system_context

BASE = dict(schema_text="DATASET", notebook_source="import marimo", notebook_rel="nb.py")
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
