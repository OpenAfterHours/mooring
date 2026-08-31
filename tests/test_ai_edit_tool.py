"""The write tool's EDIT mode: it applies its own change, and reports what happened.

The copilot's write tool has two modes, chosen by whether the caller injects an
``apply_edit`` callback:

* **propose** — the historical behaviour. It emits a proposal card and the analyst
  clicks Apply. Pinned in ``test_ai_tools.py``, and re-pinned here from the other
  side: with no applier wired, NOTHING about it changes.
* **edit** — the write happens inside the tool call and the tool returns a value-free
  OBSERVATION of what happened, so the model can check its own work and correct it in
  the same turn without another round-trip through the analyst.

Three properties are load-bearing and every test here exists to hold one of them:

1. **The gate runs BEFORE any write.** Static validation was cheap insurance when the
   worst case was a bad card; it is the last thing standing between a weak model's
   output and the analyst's open, auto-running notebook. A refused change must never
   reach the applier.
2. **The outcome is DUCK-TYPED.** ``ai/`` is L3 and ``app/`` is L3.5, so the concrete
   outcome class may not be imported here (``lint-imports`` fails the build if it is).
   These tests hand the tool a plain object with ``.status`` / ``.text`` / ``.is_error``
   and nothing else, which is exactly the contract.
3. **Cancel is answered at the tool boundary.** The Copilot SDK owns its tool loop and
   cannot be interrupted from mooring's side, so the analyst's Cancel is enforced by
   what every tool call ANSWERS — reads included.
"""

from __future__ import annotations

import types

import polars as pl
import pytest

from mooring.ai.tools import (
    EDIT_TOOL_NAME,
    PROPOSE_TOOL_NAME,
    WRITE_TOOL_NAMES,
    build_openai_tools,
    build_tool_specs,
    build_tools,
)

SECRET = "SECRET_VALUE_DO_NOT_LEAK"

# A valid 2-cell marimo notebook: `seed = 1`, then `x = seed + 1`.
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

_OBSERVATION = "cell 2 ran in 0.1s. new dataframe `totals`: region str, amount i64"


def _invocation(**arguments):
    return types.SimpleNamespace(
        session_id="s", tool_call_id="t", tool_name="x", arguments=arguments
    )


class _Outcome:
    """A stand-in for ``app/apply``'s outcome — deliberately NOT the real class.

    ``ai/`` may not import ``app/``, so the tool reads ``.status`` / ``.text`` /
    ``.is_error`` off whatever object it is handed. Using a local shim here is the
    test of that: if the tool ever grows an ``isinstance`` check or an import, these
    stop passing.
    """

    def __init__(self, status: str, text: str = "", is_error: bool = False):
        self.status = status
        self.text = text
        self.is_error = is_error


class _Applier:
    """Records every call and answers with a scripted outcome (or a list of them)."""

    def __init__(self, *outcomes: _Outcome):
        self.calls: list[tuple[list[dict], str]] = []
        self._outcomes = list(outcomes) or [_Outcome("applied", _OBSERVATION)]

    def __call__(self, ops, rationale):
        self.calls.append((ops, rationale))
        return self._outcomes[min(len(self.calls) - 1, len(self._outcomes) - 1)]


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "data").mkdir()
    pl.DataFrame({"region": [SECRET], "amount": [123456]}).write_parquet(
        tmp_path / "data" / "sales.parquet"
    )
    (tmp_path / "nb.py").write_text(_REAL_NB, "utf-8")
    return tmp_path


def _specs(ws, *, proposals=None, patches=None, **kw):
    """The provider-neutral specs by name, with both proposal callbacks wired.

    Both are ALWAYS wired, exactly as the real chat session wires them, so every
    "nothing was proposed" assertion below is a real one: the card had somewhere to go
    and still did not go there.
    """
    return {
        s.name: s
        for s in build_tool_specs(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda code, rationale="": (
                proposals if proposals is not None else []
            ).append((code, rationale)),
            emit_proposal_patch=(patches if patches is not None else []).append,
            **kw,
        )
    }


def _write_tool(specs):
    """The one write tool in ``specs``, whatever mode named it."""
    [name] = [n for n in specs if n in WRITE_TOOL_NAMES]
    return specs[name]


# --- the name is instruction ------------------------------------------------


def test_the_write_tool_is_named_for_what_it_actually_does(ws):
    # A tool called "propose" teaches the model to sign off with "I've proposed this,
    # let me know" — which is the wrong move when the cell is already running in front
    # of the analyst. Same handler, same JSON schema; the NAME follows the mode.
    proposing = _specs(ws)
    editing = _specs(ws, apply_edit=_Applier())

    assert PROPOSE_TOOL_NAME in proposing and EDIT_TOOL_NAME not in proposing
    assert EDIT_TOOL_NAME in editing and PROPOSE_TOOL_NAME not in editing
    assert set(WRITE_TOOL_NAMES) == {PROPOSE_TOOL_NAME, EDIT_TOOL_NAME}

    # One tool, one schema: only the name and the description may differ, because a
    # different schema between modes would be a second tool wearing the first one's face.
    assert proposing[PROPOSE_TOOL_NAME].parameters == editing[EDIT_TOOL_NAME].parameters
    assert proposing[PROPOSE_TOOL_NAME].blocking == editing[EDIT_TOOL_NAME].blocking is True


def test_an_applier_alone_is_enough_to_register_and_run_the_write_tool(ws):
    # A session may wire the applier and nothing else. Every op shape has to work from
    # there — the propose-only guard ("this session can only propose appended cells")
    # must not surface on a path that is not proposing anything.
    applier = _Applier()
    specs = {
        s.name: s
        for s in build_tool_specs(
            workspace=ws, folders=("data",), notebook_rel="nb.py", apply_edit=applier
        )
    }
    assert EDIT_TOOL_NAME in specs
    tool = specs[EDIT_TOOL_NAME]
    assert not tool.handler(_invocation(code="y = x * 2")).is_error
    assert not tool.handler(
        _invocation(edits=[{"index": 1, "expect": "x = seed + 1", "code": "x = seed + 9"}])
    ).is_error
    assert not tool.handler(
        _invocation(cells=["a = 1", "b = a + 1"], expect_cells=2)
    ).is_error
    assert [op[0]["op"] for op, _ in applier.calls] == ["append", "edit", "replace_all"]


def test_the_edit_mode_description_says_it_writes_and_runs(ws):
    # The description is the model's only account of what the call DOES, and edit mode
    # changes the answer completely: it writes, it runs, and the result is evidence to
    # check its own work against.
    editing = _specs(ws, apply_edit=_Applier())[EDIT_TOOL_NAME].description
    proposing = _specs(ws)[PROPOSE_TOOL_NAME].description

    assert "WRITES to the analyst's open notebook" in editing
    assert "RUNS the change immediately" in editing
    assert "no Apply click" in editing
    assert "OBSERVATION" in editing and "call this tool again to correct" in editing
    # ...and propose mode still says the opposite, unchanged.
    assert "You never write the file; the analyst sees a diff and applies it." in proposing
    assert "RUNS the change immediately" not in proposing
    # The rules that stop a weak model breaking the notebook are shared, not duplicated.
    for rule in ("EDIT the cell that does it", "MUST carry 'expect'", "BODY ONLY"):
        assert rule in editing and rule in proposing


def test_the_sql_guide_names_the_tool_this_session_actually_has():
    # Naming a tool the model has not been given is worse than naming none.
    from mooring.ai import tools

    assert PROPOSE_TOOL_NAME in tools.sql_cell_guide()  # the default is propose mode
    assert EDIT_TOOL_NAME in tools.sql_cell_guide(EDIT_TOOL_NAME)
    assert PROPOSE_TOOL_NAME not in tools.sql_cell_guide(EDIT_TOOL_NAME)


# --- propose mode is untouched ----------------------------------------------


def test_without_an_applier_the_propose_path_is_unchanged(ws):
    # The `[ai] auto_apply = false` path AND the policy escape hatch. It has to genuinely
    # still work, not merely still exist.
    proposals, patches = [], []
    specs = _specs(ws, proposals=proposals, patches=patches)
    before = (ws / "nb.py").read_text("utf-8")

    out = specs[PROPOSE_TOOL_NAME].handler(_invocation(code="y = x * 2", rationale="why"))

    assert not out.is_error
    assert out.text == "Proposed the cell to the analyst, who will review and apply it."
    assert proposals == [("y = x * 2", "why")]
    assert (ws / "nb.py").read_text("utf-8") == before  # nothing was written


def test_without_an_applier_an_edit_still_emits_the_patch_card(ws):
    proposals, patches = [], []
    specs = _specs(ws, proposals=proposals, patches=patches)
    out = specs[PROPOSE_TOOL_NAME].handler(
        _invocation(edits=[{"index": 1, "expect": "x = seed + 1", "code": "x = seed + 9"}])
    )
    assert not out.is_error and "review and apply" in out.text
    assert [p["kind"] for p in patches] == ["edit"]


# --- edit mode: applied -----------------------------------------------------


def test_an_applied_change_returns_the_observation_and_no_card(ws):
    # THE payload that makes the feature work: the model must be able to read the result
    # and write its next cell against the real schema.
    proposals, patches = [], []
    applier = _Applier(_Outcome("applied", _OBSERVATION))
    specs = _specs(ws, proposals=proposals, patches=patches, apply_edit=applier)

    out = _write_tool(specs).handler(_invocation(code="y = x * 2", rationale="why"))

    assert not out.is_error
    assert _OBSERVATION in out.text  # the observation IS the tool result
    assert "APPLIED" in out.text and "marimo has run it" in out.text
    assert "check this against what you intended" in out.text.lower()
    # The ops the applier got are the same normalised wire ops the analyst's Apply used
    # to get, and the rationale rides along with them.
    [(ops, rationale)] = applier.calls
    assert ops == [{"op": "append", "code": "y = x * 2"}]
    assert rationale == "why"
    # No review card: there is nothing to review, the change is already running.
    assert proposals == [] and patches == []


def test_an_applied_edit_passes_the_captured_anchor_through(ws):
    applier = _Applier()
    specs = _specs(ws, apply_edit=applier)
    _write_tool(specs).handler(
        _invocation(edits=[{"index": 1, "expect": "x = seed + 1", "code": "x = seed + 9"}])
    )
    [(ops, _)] = applier.calls
    assert ops == [
        {"op": "edit", "index": 1, "anchor": "x = seed + 1", "code": "x = seed + 9"}
    ]
    assert "expect" not in ops[0]  # a propose-time claim; it never reaches the write


def test_a_rewrite_is_applied_too_rather_than_carded(ws):
    proposals, patches = [], []
    applier = _Applier()
    specs = _specs(ws, proposals=proposals, patches=patches, apply_edit=applier)
    out = _write_tool(specs).handler(
        _invocation(cells=["a = 1", "b = a + 1"], expect_cells=2)
    )
    assert not out.is_error and _OBSERVATION in out.text
    [(ops, _)] = applier.calls
    assert ops == [{"op": "replace_all", "cells": ["a = 1", "b = a + 1"]}]
    assert proposals == [] and patches == []


def test_the_observation_still_passes_the_egress_floor(ws):
    # The observation is value-free by construction (status + names + dtypes), so this is
    # defence in depth — the same floor every other tool result in this module gets.
    card = "4012888888881881"  # Luhn-valid (shared with test_egress)
    applier = _Applier(_Outcome("applied", f"ran. account {card} appeared in a column name"))
    specs = _specs(ws, apply_edit=applier)
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert not out.is_error
    assert card not in out.text


def test_there_is_no_cap_on_how_many_changes_a_turn_may_land(ws):
    # Explicitly not wanted: the product intent is that the model works a hard analysis
    # through. Only REJECTED writes are bounded.
    applier = _Applier()
    specs = _specs(ws, apply_edit=applier)
    tool = _write_tool(specs)
    for i in range(12):
        out = tool.handler(_invocation(code=f"note_{i} = {i}"))
        assert not out.is_error, i
    assert len(applier.calls) == 12


# --- edit mode: held, conflict, cancelled, disabled, error -------------------


def test_a_held_change_tells_the_model_to_stop_writing_and_explain(ws):
    # NOT an error to retry: the change is sitting in front of the analyst waiting for a
    # confirm, so a repeat writes nothing and burns the turn.
    proposals, patches = [], []
    applier = _Applier(_Outcome("held", "it drops a database table"))
    specs = _specs(ws, proposals=proposals, patches=patches, apply_edit=applier)

    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))

    assert not out.is_error  # an error result invites exactly the retry this forbids
    assert "HELD" in out.text and "NOT running yet" in out.text
    assert "it drops a database table" in out.text  # the value-free finding labels
    assert "Do NOT send this change again" in out.text
    assert "reply to the analyst" in out.text
    assert proposals == [] and patches == []


def test_a_conflict_repoints_the_model_at_a_fresh_read(ws):
    # Consistent with how a stale index is already handled: say nothing about what is
    # really there, send it back to read the notebook as it is now.
    proposals, patches = [], []
    applier = _Applier(_Outcome("conflict", "- cell 1 no longer matches its anchor", True))
    specs = _specs(ws, proposals=proposals, patches=patches, apply_edit=applier)

    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))

    assert out.is_error
    assert "mooring_read_notebook_source" in out.text
    assert "the notebook changed under you" in out.text
    assert proposals == [] and patches == []


def test_a_cancelled_write_is_terminal(ws):
    # The analyst pressed Cancel while the write was in flight.
    proposals, patches = [], []
    applier = _Applier(_Outcome("cancelled", "", True))
    specs = _specs(ws, proposals=proposals, patches=patches, apply_edit=applier)

    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))

    assert out.is_error
    assert "CANCELLED by the analyst" in out.text
    assert "Stop calling tools" in out.text
    assert proposals == [] and patches == []


def test_a_disabled_session_says_so_without_inviting_a_retry(ws):
    applier = _Applier(_Outcome("disabled", "auto-apply is off for this notebook", True))
    specs = _specs(ws, apply_edit=applier)
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert out.is_error
    assert "auto-apply is off for this notebook" in out.text
    assert "Retrying will not change that" in out.text


def test_a_failed_write_surfaces_the_reason(ws):
    applier = _Applier(_Outcome("error", "the notebook file is read-only", True))
    specs = _specs(ws, apply_edit=applier)
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert out.is_error
    assert "the notebook file is read-only" in out.text
    assert "Do not just send the same change again" in out.text


def test_an_unrecognised_status_is_a_failure_not_a_success(ws):
    # Reading an unknown status as success would tell the model a cell is running when
    # it is not — the one lie this whole feature exists to stop telling.
    applier = _Applier(_Outcome("something_new", "?"))
    specs = _specs(ws, apply_edit=applier)
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert out.is_error and "NOT applied" in out.text


def test_an_applied_outcome_that_also_says_error_is_not_reported_as_running(ws):
    # `.status` is the discriminator (a boolean cannot tell `held` from `conflict`), but
    # `.is_error` still vetoes: an outcome claiming both must not tell the model a cell is
    # running when the applier itself says something went wrong.
    applier = _Applier(_Outcome("applied", "half-written", is_error=True))
    specs = _specs(ws, apply_edit=applier)
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert out.is_error and "APPLIED" not in out.text


def test_an_applier_that_raises_does_not_kill_the_turn(ws):
    def boom(_ops, _rationale):
        raise RuntimeError("kaboom")

    specs = _specs(ws, apply_edit=boom)
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert out.is_error
    assert "could not be written" in out.text and "kaboom" in out.text


def test_an_outcome_missing_its_fields_degrades_instead_of_exploding(ws):
    # A duck-typed contract has to survive a duck that is missing a foot.
    specs = _specs(ws, apply_edit=lambda ops, rationale: object())
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert out.is_error and "NOT applied" in out.text


# --- the gate still runs first ----------------------------------------------


def test_the_gate_refuses_a_broken_candidate_before_anything_is_written(ws):
    # THE weak-model failure — the Jupyter reflex of redefining a name in a new cell —
    # and the reason the static check must stay in front of the write: applied, it stops
    # both cells and everything downstream, in the analyst's open notebook.
    applier = _Applier()
    specs = _specs(ws, apply_edit=applier)

    out = _write_tool(specs).handler(_invocation(code="seed = 2"))

    assert out.is_error and "MB002" in out.text
    assert "NOT applied" in out.text  # and it is told what did NOT happen, accurately
    assert applier.calls == []  # the applier was never reached


def test_the_gate_refuses_a_nested_app_cell_body_before_writing(ws):
    applier = _Applier()
    specs = _specs(ws, apply_edit=applier)
    body = (
        "@app.cell\ndef _():\n    a = 1\n    return (a,)\n\n\n"
        "@app.cell\ndef _():\n    b = 2\n    return (b,)"
    )
    out = _write_tool(specs).handler(_invocation(code=body))
    assert out.is_error and "MOOR002" in out.text
    assert applier.calls == []


def test_the_expect_check_still_runs_before_a_write(ws):
    applier = _Applier()
    specs = _specs(ws, apply_edit=applier)
    out = _write_tool(specs).handler(
        _invocation(edits=[{"index": 1, "expect": "seed = 1", "code": "seed = 9"}])
    )
    assert out.is_error and "mooring_read_notebook_source" in out.text
    assert applier.calls == []


# --- the thrash brake -------------------------------------------------------


def test_six_refusals_in_a_row_hand_back_to_the_analyst(ws):
    # Raised from three because the model now gets REAL feedback and should converge —
    # but a model that has acted on none of six refusals is thrashing, and the right
    # answer is still to stop.
    applier = _Applier()
    specs = _specs(ws, apply_edit=applier)
    tool = _write_tool(specs)
    bad = _invocation(code="seed = 2")

    for _ in range(6):
        assert "MB002" in tool.handler(bad).text
    out = tool.handler(bad)
    assert out.is_error and "MB002" not in out.text
    assert f"Stop calling {EDIT_TOOL_NAME}" in out.text
    assert "let them decide" in out.text
    assert applier.calls == []


def test_a_change_that_lands_resets_the_thrash_brake(ws):
    # The budget measures "stuck", not "has been wrong before".
    applier = _Applier()
    specs = _specs(ws, apply_edit=applier)
    tool = _write_tool(specs)
    bad = _invocation(code="seed = 2")

    for _ in range(6):
        assert "MB002" in tool.handler(bad).text
    assert not tool.handler(_invocation(code="y = x * 2")).is_error  # applied
    assert len(applier.calls) == 1
    # ...and the very next mistake gets the diagnostics again, not the give-up message.
    out = tool.handler(bad)
    assert "MB002" in out.text and "Stop calling" not in out.text


def test_repeated_conflicts_cannot_become_an_unbounded_retry_loop(ws):
    applier = _Applier(_Outcome("conflict", "- the cell moved", True))
    specs = _specs(ws, apply_edit=applier)
    tool = _write_tool(specs)
    for _ in range(6):
        assert "mooring_read_notebook_source" in tool.handler(_invocation(code="y = x * 2")).text
    out = tool.handler(_invocation(code="y = x * 2"))
    assert f"Stop calling {EDIT_TOOL_NAME}" in out.text


def test_a_held_change_does_not_spend_the_thrash_brake(ws):
    # A hold is not the model being wrong: the code passed the gate and the analyst is
    # simply being asked. Counting it would end the turn for a model doing fine work.
    applier = _Applier(_Outcome("held", "it writes to a database"))
    specs = _specs(ws, apply_edit=applier)
    tool = _write_tool(specs)
    for _ in range(8):
        out = tool.handler(_invocation(code="y = x * 2"))
        assert not out.is_error and "HELD" in out.text


# --- cancellation at the tool boundary --------------------------------------


def test_cancellation_stops_every_tool_including_the_reads(ws):
    # A cancelled turn that still services schema lookups is a turn the analyst stopped
    # and is still paying for.
    stop = [False]
    specs = _specs(ws, apply_edit=_Applier(), cancelled=lambda: stop[0])
    assert not specs["mooring_list_datasets"].handler(_invocation()).is_error

    stop[0] = True
    for name, spec in specs.items():
        out = spec.handler(_invocation(dataset="data/sales.parquet", code="y = x * 2"))
        assert out.is_error, name
        assert "CANCELLED by the analyst" in out.text, name
        assert "Stop calling tools" in out.text, name


def test_cancellation_is_checked_before_the_handler_runs(ws):
    # Cheaply, and in ONE place: the point is that no handler body executes at all — no
    # dataset is opened, no notebook is written.
    applier = _Applier()
    proposals, patches = [], []
    specs = _specs(
        ws,
        proposals=proposals,
        patches=patches,
        apply_edit=applier,
        cancelled=lambda: True,
    )
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert out.is_error and "CANCELLED" in out.text
    assert applier.calls == [] and proposals == [] and patches == []

    # ...and a read that WOULD have opened the parquet does not open it.
    schema_out = specs["mooring_get_schema"].handler(
        _invocation(dataset="data/sales.parquet")
    )
    assert schema_out.is_error and SECRET not in schema_out.text
    assert "region" not in schema_out.text


def test_a_cancel_probe_that_raises_does_not_refuse_every_tool(ws):
    # Fail OPEN, the house rule for a check that cannot run: a raising probe must not
    # take down every tool call in every turn.
    def boom():
        raise RuntimeError("probe is broken")

    specs = _specs(ws, apply_edit=_Applier(), cancelled=boom)
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert not out.is_error and _OBSERVATION in out.text


def test_no_cancel_callback_means_no_wrapper_at_all(ws):
    # The default path stays exactly as it was: nothing to call, nothing to fail.
    specs = _specs(ws)
    assert not specs["mooring_list_datasets"].handler(_invocation()).is_error


# --- both adapters ----------------------------------------------------------


def _call(tool, invocation):
    """Invoke a copilot ``Tool`` handler, awaiting it when the spec is ``blocking``."""
    import asyncio
    import inspect

    out = tool.handler(invocation)
    return asyncio.run(out) if inspect.isawaitable(out) else out


def test_the_copilot_adapter_exposes_the_renamed_tool_and_applies(ws):
    applier = _Applier()
    tools = {
        t.name: t
        for t in build_tools(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda code, rationale="": None,
            apply_edit=applier,
            cancelled=lambda: False,
        )
    }
    assert EDIT_TOOL_NAME in tools and PROPOSE_TOOL_NAME not in tools
    res = _call(tools[EDIT_TOOL_NAME], _invocation(code="y = x * 2"))
    assert _OBSERVATION in res.text_result_for_llm
    assert len(applier.calls) == 1


def test_the_openai_adapter_dispatches_the_renamed_tool(ws):
    applier = _Applier()
    tool_specs, dispatch = build_openai_tools(
        workspace=ws,
        folders=("data",),
        notebook_rel="nb.py",
        emit_proposal=lambda code, rationale="": None,
        apply_edit=applier,
        cancelled=lambda: False,
    )
    advertised = [t["function"]["name"] for t in tool_specs]
    assert EDIT_TOOL_NAME in advertised and PROPOSE_TOOL_NAME not in advertised
    assert EDIT_TOOL_NAME in dispatch and PROPOSE_TOOL_NAME not in dispatch
    out = dispatch[EDIT_TOOL_NAME](_invocation(code="y = x * 2"))
    assert not out.is_error and _OBSERVATION in out.text


def test_the_output_guard_still_wraps_an_applied_result(ws):
    # The approved-data policy check sits INSIDE the cancel wrapper, so an observation it
    # refuses is still withheld.
    specs = {
        s.name: s
        for s in build_tool_specs(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            apply_edit=_Applier(),
            cancelled=lambda: False,
            output_guard=lambda text: False,
        )
    }
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert out.is_error
    assert out.text == "tool output withheld by the approved data policy"
    assert _OBSERVATION not in out.text


def test_a_cancelled_turn_is_not_swallowed_by_a_refusing_output_guard(ws):
    # The cancel wrapper sits OUTSIDE the guard on purpose: the analyst's stop signal is
    # mooring's own fixed sentence and must reach the model even when the data policy is
    # withholding everything else.
    specs = {
        s.name: s
        for s in build_tool_specs(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            apply_edit=_Applier(),
            cancelled=lambda: True,
            output_guard=lambda text: False,
        )
    }
    out = _write_tool(specs).handler(_invocation(code="y = x * 2"))
    assert out.is_error and "CANCELLED by the analyst" in out.text


# --- the rename cannot desynchronise -----------------------------------------
#
# The ONE write tool is registered under one of TWO names, chosen per session by
# ``[ai] auto_apply``. Several places outside ``ai/tools.py`` have to agree with that
# choice, and each of them used to hold its own copy of the string:
#
#   * the system prompt, which TELLS the model the tool's name on every turn — a stale
#     name there is a standing instruction to call a tool the session does not have,
#     and nothing raises; the model simply cannot write;
#   * the eval harness, which counts refused write calls BY NAME — a stale name scores
#     an edit-mode sweep as "the gate never fired";
#   * the chat page's tool-progress labels.
#
# Not one of those fails loudly, which is exactly why they are pinned here. Every test
# below derives its expectation from the constants, so a future rename that misses a
# consumer FAILS instead of quietly degrading.


def _session_prompt(cls, *, applier, **kw):
    """The full system message ONE session would send, built by its real ctor.

    Deliberately not a re-derivation: the ctor is where the mode becomes a guide, so
    the ctor is the thing that has to be right. Both providers assemble it from the
    same ``ai/session.py`` helpers, so both are put through this.
    """
    session = cls(
        model="m",
        system_context="CONTEXT",
        workspace=".",
        folders=(),
        notebook_rel="nb.py",
        applier=applier,
        **kw,
    )
    return session._system_context


@pytest.mark.parametrize("edit_mode", [False, True])
@pytest.mark.parametrize("allow_read_tools", [True, False])
def test_the_system_prompt_names_the_write_tool_the_session_registered(
    ws, edit_mode, allow_read_tools
):
    from mooring.ai.openai_session import OpenAIChatSession
    from mooring.ai.session import CopilotChatSession

    applier = _Applier() if edit_mode else None
    # What the session will actually REGISTER — read off the built specs, from the same
    # `apply_edit is not None` switch the session passes down, never assumed.
    registered = _write_tool(_specs(ws, apply_edit=applier)).name
    absent = set(WRITE_TOOL_NAMES) - {registered}

    for cls, extra in (
        (CopilotChatSession, {}),
        (OpenAIChatSession, {"client_factory": lambda: None}),
    ):
        prompt = _session_prompt(
            cls, applier=applier, allow_read_tools=allow_read_tools, **extra
        )
        assert registered in prompt, (cls.__name__, registered)
        for name in absent:
            assert name not in prompt, (cls.__name__, name)


def test_a_read_only_sub_agent_is_told_about_no_write_tool_at_all(ws):
    # The load-bearing privacy gate, from the prompt side: an investigate sub-agent
    # registers NO write surface, so its prompt must name none either — even when an
    # applier is mis-wired in, which `read_only` drops on the floor.
    from mooring.ai.session import CopilotChatSession

    # How a read-only session builds its toolset: every write callback None'd out.
    read_only_specs = build_tool_specs(
        workspace=ws,
        folders=("data",),
        notebook_rel="nb.py",
        emit_proposal=None,
        emit_proposal_patch=None,
        apply_edit=None,
    )
    assert not [s for s in read_only_specs if s.name in WRITE_TOOL_NAMES]
    prompt = _session_prompt(CopilotChatSession, applier=_Applier(), read_only=True)
    for name in WRITE_TOOL_NAMES:
        assert name not in prompt, name


def test_the_eval_harness_counts_refusals_under_either_write_tool_name():
    # `Attempt.refusals` used to match a "mooring_propose" PREFIX, so an edit-mode sweep
    # reported 0 refusals however many the gate handed back — a capability card reading
    # "the in-loop diagnostics never fired" when they fired on every call.
    from evals.harness import Attempt

    for name in WRITE_TOOL_NAMES:
        attempt = Attempt(
            workspace=".",
            notebook_rel="nb.py",
            base_source="",
            known_columns=(),
            tool_results=((name, False), (name, True)),
        )
        assert attempt.refusals == 1, name


def test_the_proposed_check_recognises_either_write_tool_name():
    # The same prefix bug in the scoring vocabulary: a model that called the edit tool
    # and emitted nothing was scored as having "answered in prose", which is the
    # opposite of what happened and sends a reader after the wrong failure.
    from evals import checks
    from evals.harness import Attempt

    check = checks.proposed()
    for name in WRITE_TOOL_NAMES:
        attempt = Attempt(
            workspace=".",
            notebook_rel="nb.py",
            base_source="",
            known_columns=(),
            tool_calls=(name,),
        )
        reason = check.run(attempt)
        assert "the write tool was called but emitted nothing" in reason, name
        assert "in prose" not in reason, name


def _static(name: str) -> str:
    from pathlib import Path

    import mooring

    return (Path(mooring.__file__).parent / "hub" / "static" / name).read_text("utf-8")


def test_the_chat_page_labels_every_write_tool_name():
    # chat.js keys its tool-progress labels by the name the SERVER sends, and is never
    # told which mode the session is in, so both names need an entry. A missing one
    # raises nothing; the raw tool name just leaks into the analyst's transcript.
    labels = _static("chat.js").split("const TOOL_LABELS = {", 1)[1].split("};", 1)[0]
    for name in WRITE_TOOL_NAMES:
        assert f"{name}:" in labels, name


def test_the_browser_prompt_text_names_no_write_tool():
    # chat_core.js builds the canned /checks, /sql and "Add as notes cell" prompts, and
    # those are sent to the MODEL. The page cannot know the session's mode, so it names
    # no write tool at all and lets the system prompt (which does know) name it; what
    # these prompts pin instead is the FIELD, which is the same in both modes.
    core_js = _static("chat_core.js")
    for name in WRITE_TOOL_NAMES:
        assert name not in core_js, name
