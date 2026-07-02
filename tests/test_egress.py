"""The egress gateway: the single outbound-scrub choke point for the AI.

These tests lock the two guarantees Phase 1 adds:
  1. :func:`build_system_context` ENFORCES value-freedom — every fragment is
     scrubbed at the assembler, so a checksum-validated PII value cannot reach the
     model even when an upstream caller forgot to scrub.
  2. STRUCTURE, not convention — nothing outside ``egress.py`` calls
     ``pii.scrub_columns`` directly, and the system-context assembler is defined
     only in ``egress.py``. (The lightweight stand-in for the Phase 2 import-linter
     contract.)
"""

from __future__ import annotations

import re
from pathlib import Path

from mooring.ai import egress, pii

# Synthetic, checksum-valid identifiers (shared with test_pii): a Luhn-valid card
# NOT on any test-PAN list, a mod-97 IBAN, a mod-11 NHS number.
VALID_CARD = "4012888888881881"
VALID_IBAN = "GB82WEST12345698765432"
VALID_NHS = "9434765919"

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "mooring"


# -- scrub_text: the text-level analogue of scrub_columns -----------------------


def test_scrub_text_drops_checksum_line_keeps_clean():
    text = f"clean header\nleak {VALID_CARD} here\nclean footer"
    scrubbed, findings = egress.scrub_text(text)
    assert VALID_CARD not in scrubbed
    assert "clean header" in scrubbed and "clean footer" in scrubbed
    assert {f.kind for f in findings} == {pii.CARD}
    assert VALID_CARD not in repr(findings)  # value-free finding


def test_scrub_text_drops_iban_and_nhs_lines():
    text = f"a\niban {VALID_IBAN}\nb\nnhs {VALID_NHS}\nc"
    scrubbed, findings = egress.scrub_text(text)
    assert VALID_IBAN not in scrubbed and VALID_NHS not in scrubbed
    assert scrubbed.splitlines() == ["a", "b", "c"]
    assert {f.kind for f in findings} == {pii.IBAN, pii.NHS}


def test_scrub_text_keeps_shape_only_kinds():
    # email / NINO are too low-confidence to silently drop a line (a real contact
    # address or product code) — they are surfaced as a warn-only banner instead.
    text = "mail support@acme.com\nni AB123456C"
    scrubbed, findings = egress.scrub_text(text)
    assert scrubbed == text  # unchanged
    assert findings == []


def test_scrub_text_clean_is_returned_unchanged():
    text = "DATASET SCHEMA:\ncol_a Int64\ncol_b String"
    scrubbed, findings = egress.scrub_text(text)
    assert scrubbed is text  # identity — no whitespace reshape on the common path
    assert findings == []


def test_scrub_text_empty():
    assert egress.scrub_text("") == ("", [])


# -- scrub_columns: a thin, auditable pass-through to pii ------------------------


def test_scrub_columns_delegates_to_pii():
    cols = (("id", "Int64"), (VALID_CARD, "Float64"), ("amt", "Float64"))
    kept, findings = egress.scrub_columns(cols)
    assert [c[0] for c in kept] == ["id", "amt"]
    assert {f.kind for f in findings} == {pii.CARD}
    assert egress.scrub_columns(cols) == pii.scrub_columns(cols)


def test_guard_prompt_routes_through_egress():
    # The outbound-prompt valve is re-exported, so a session calls egress.guard_prompt.
    assert egress.guard_prompt is pii.guard_prompt
    hold, findings, err = egress.guard_prompt(f"x {VALID_CARD}", enabled=True, block=True)
    assert hold is True and findings and err == ""


# -- build_system_context: scrubs every value-bearing fragment ------------------

_BASE = {"schema_text": "DATASET", "notebook_source": "import marimo", "notebook_rel": "nb.py"}


def test_build_system_context_scrubs_every_fragment():
    out = egress.build_system_context(
        schema_text=f"good_col Int64\nbad {VALID_CARD} thing",
        notebook_source=f"import marimo\nx = {VALID_CARD}\nprint('ok')",
        notebook_rel="nb.py",
        live_schemas_text=f"live_col Int64\nfr {VALID_IBAN} col",
        instructions_text=f"Report in GBP.\nleak {VALID_NHS}",
        dictionary_text=f"Table credit.loans\nrow {VALID_CARD}",
        semantic_models_text=f"- `Sales` (reports/Sales)\n- `Bad {VALID_IBAN}`",
    )
    # No checksum-validated value survives, from ANY fragment.
    for value in (VALID_CARD, VALID_IBAN, VALID_NHS):
        assert value not in out
    # The clean content around each leak is preserved.
    for marker in (
        "good_col",
        "import marimo",
        "print('ok')",
        "live_col",
        "GBP",
        "credit.loans",
        "reports/Sales",
    ):
        assert marker in out


def test_build_system_context_semantic_models_section():
    out = egress.build_system_context(**_BASE, semantic_models_text="- `Sales` (reports/Sales)")
    assert "POWER BI SEMANTIC MODELS" in out and "reports/Sales" in out
    # Absent when the hint is empty (the section never renders as a dangling header).
    assert "POWER BI SEMANTIC MODELS" not in egress.build_system_context(**_BASE)


def test_build_system_context_clean_assembly_unchanged():
    out = egress.build_system_context(**_BASE)
    assert "DATASET SCHEMA:" in out and "CURRENT NOTEBOOK (nb.py)" in out
    assert "STRICT PRIVACY RULES:" in out


def test_build_system_context_reexported_from_chat_for_backcompat():
    # The assembler moved to egress; chat re-exports the SAME object so existing
    # importers (and test_chat_context) keep working.
    from mooring.ai.chat import build_system_context as via_chat

    assert via_chat is egress.build_system_context


# -- the ToolResult mint gateway -------------------------------------------------


def test_to_tool_result_mints_without_reshaping():
    # Mints only — no re-scrub: each channel owns its scrub semantics (get_schema's
    # column withholding is gated on the PII setting; re-scrubbing here would
    # silently change that contract).
    res = egress.to_tool_result("col_a: Int64\ncol_b: Utf8")
    assert res.text_result_for_llm == "col_a: Int64\ncol_b: Utf8"
    assert res.error is None


def test_to_error_result_scrubs_the_error_channel():
    # Exception text can quote user input; the error field crosses to the model
    # too, so it gets the same checksum-PII floor as the text channel. A typical
    # exception message is ONE line, and the scrub drops whole lines — so the
    # withheld case must still EXPLAIN itself, never hand back an empty error
    # the model would silently retry.
    res = egress.to_error_result(f"cannot read schema: bad value {VALID_CARD} in header")
    assert VALID_CARD not in (res.error or "")
    assert res.error and "withheld" in res.error
    assert res.text_result_for_llm == ""
    assert res.result_type == "error"


def test_to_error_result_keeps_clean_lines_of_a_multiline_message():
    res = egress.to_error_result(f"cannot read schema\nbad value {VALID_CARD}\nin row 3")
    assert VALID_CARD not in (res.error or "")
    assert "cannot read schema" in res.error and "in row 3" in res.error


def test_to_error_result_clean_message_unchanged():
    res = egress.to_error_result("dataset required")
    assert res.error == "dataset required"


def test_sanitize_traceback_gateway_rewrites_value_safe():
    # The gateway wraps the sanitiser: detection + fail-closed rewrite + the
    # known-token rescue built from raw session text. Behavioural depth lives in
    # test_traceback.py; this pins the gateway's contract shape.
    secret = "SECRET_VALUE_DO_NOT_LEAK"
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        f"KeyError: '{secret}'"
    )
    result = egress.sanitize_traceback(text, workspace=None)
    assert result.detected is True
    assert secret not in result.text
    assert "KeyError: <redacted:" in result.text
    assert secret not in repr(result.findings)  # value-free findings
    # known_text rescues an in-channel token (schema column, notebook source, …).
    rescued = egress.sanitize_traceback(
        text.replace(f"'{secret}'", "'revenue'"), workspace=None, known_text="revenue Int64"
    )
    assert "KeyError: 'revenue'" in rescued.text


def test_sanitize_traceback_gateway_is_a_noop_on_prose():
    result = egress.sanitize_traceback("group revenue by month?", workspace=None)
    assert result.detected is False and result.text == "group revenue by month?"


def test_egress_imports_without_the_copilot_sdk():
    """``copilot`` is the optional ``mooring[copilot]`` extra, and egress is
    imported on non-AI paths (the guard_prompt / Finding re-exports) — so its SDK
    import must stay function-local. Run in a subprocess so this test env's own
    imports can't mask an accidental module-level import."""
    import subprocess
    import sys

    code = "import sys; import mooring.ai.egress; sys.exit(1 if 'copilot' in sys.modules else 0)"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()


# -- structural guard: the choke point cannot be bypassed -----------------------


def test_only_egress_constructs_the_sdk_tool_result():
    """The mint gateway: nothing outside egress.py constructs a ``ToolResult`` or
    sets its ``text_result_for_llm`` field, so every tool's outbound text passes
    through egress BY CONSTRUCTION — a new tool cannot hand the SDK a bare string
    without a review-visible call into egress.py."""
    offenders = []
    for path in _SRC_ROOT.rglob("*.py"):
        if path.name == "egress.py":
            continue
        text = path.read_text("utf-8")
        if re.search(r"\bToolResult\s*\(", text) or re.search(r"\btext_result_for_llm\s*=", text):
            offenders.append(path.relative_to(_SRC_ROOT).as_posix())
    assert offenders == [], f"ToolResult minted outside egress.py: {offenders}"


def test_no_module_bypasses_the_egress_scrub():
    """Only egress.py may call pii.scrub_columns; the assembler is defined only here.

    This is the structural enforcement that converts the privacy guarantee from
    convention into a checked invariant (until the Phase 2 import-linter contract
    supersedes it). A new egress path that forgets to scrub must edit egress.py —
    a review-visible change — rather than leaking quietly from a new call site.
    """
    bypass = []
    assembler_defs = []
    for path in _SRC_ROOT.rglob("*.py"):
        text = path.read_text("utf-8")
        if path.name != "egress.py" and re.search(r"\bpii\.scrub_columns\s*\(", text):
            bypass.append(path.relative_to(_SRC_ROOT).as_posix())
        if re.search(r"^def build_system_context\b", text, re.MULTILINE):
            assembler_defs.append(path.relative_to(_SRC_ROOT).as_posix())
    assert bypass == [], f"scrub_columns called outside egress.py: {bypass}"
    assert assembler_defs == ["ai/egress.py"], assembler_defs


def test_only_egress_imports_the_traceback_sanitizer():
    """The traceback sanitiser has ONE gateway: ``egress.sanitize_traceback``.

    Nothing else in the source tree may import the ``ai/traceback`` module (by
    either import form), so a new caller that would bypass the gateway — and with
    it the known-token construction and the value-free result contract — is a
    review-visible change to egress.py, exactly like the scrub_columns rule above.
    """
    import_forms = re.compile(
        r"from\s+mooring\.ai\s+import\s+[^\n]*\btraceback\b|mooring\.ai\.traceback"
    )
    offenders = []
    for path in _SRC_ROOT.rglob("*.py"):
        if path.name == "egress.py" or path == _SRC_ROOT / "ai" / "traceback.py":
            continue
        if import_forms.search(path.read_text("utf-8")):
            offenders.append(path.relative_to(_SRC_ROOT).as_posix())
    assert offenders == [], f"ai/traceback imported outside egress.py: {offenders}"
