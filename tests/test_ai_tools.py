"""The agent's safe tools must be value-free by construction."""

from __future__ import annotations

import types

import polars as pl
import pytest

from mooring.ai.tools import TOOL_NAMES, build_tool_specs, build_tools

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


def _call(tool, invocation):
    """Invoke a copilot ``Tool`` handler, awaiting it when the spec is ``blocking``.

    Every propose tool is: each builds the candidate notebook and statically validates
    it, which must not happen on the SDK's event-loop thread, so ``build_tools`` hands
    those handlers to a worker thread and they come back as coroutines.
    """
    import asyncio
    import inspect

    out = tool.handler(invocation)
    return asyncio.run(out) if inspect.isawaitable(out) else out


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
            emit_proposal=lambda code, rationale="": (
                proposals if proposals is not None else []
            ).append((code, rationale)),
            emit_proposal_patch=patches.append,
        )
    }


def _spec_names(ws, **kw):
    return [
        s.name
        for s in build_tool_specs(workspace=ws, folders=("data",), notebook_rel="nb.py", **kw)
    ]


def test_tool_names_match_built_tools(ws):
    tools = _tools(ws, [])
    assert sorted(tools) == sorted(TOOL_NAMES)


def test_readonly_build_registers_no_write_tool(ws):
    # No emit_proposal / emit_proposal_patch => a READ-ONLY session (an investigate
    # sub-agent): only the value-free read tools, NEVER a propose/edit tool. This gate is
    # the load-bearing privacy invariant — a sub-agent's finding is trusted BECAUSE it is
    # structurally value-blind, which holds only if it can never write or return a value.
    names = _spec_names(ws)
    assert names == ["mooring_list_datasets", "mooring_get_schema", "mooring_read_notebook_source"]
    assert not any("propose" in n for n in names)
    assert "mooring_investigate" not in names


def test_investigate_tool_only_registered_with_run_investigation(ws):
    assert "mooring_investigate" not in _spec_names(ws, emit_proposal=lambda *a, **k: None)
    with_inv = _spec_names(
        ws, emit_proposal=lambda *a, **k: None, run_investigation=lambda b: "findings"
    )
    assert "mooring_investigate" in with_inv


def test_investigate_tool_calls_the_coordinator_and_returns_its_findings(ws):
    seen = {}

    def run_investigation(branches, on_progress=None):
        seen["branches"] = branches
        return "## what columns?\norders has id, ts, amount"

    spec = {
        s.name: s
        for s in build_tool_specs(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda *a, **k: None,
            run_investigation=run_investigation,
        )
    }["mooring_investigate"]
    out = spec.handler(_invocation(branches=[{"question": "what columns?"}]))
    assert not out.is_error
    assert "orders has id, ts, amount" in out.text
    assert seen["branches"] == [{"question": "what columns?"}]


def test_investigate_tool_streams_value_free_progress_cues(ws):
    cues: list[str] = []
    question = "SENTINEL_QUESTION"
    finding = "SENTINEL_FINDING"

    def run_investigation(branches, on_progress=None):
        # Replay the planner's value-free lifecycle events.
        on_progress({"phase": "start", "done": 0, "total": 3})
        on_progress({"phase": "branch", "done": 1, "total": 3, "status": "finding"})
        on_progress({"phase": "done", "done": 3, "total": 3, "found": 2})
        return finding

    spec = {
        s.name: s
        for s in build_tool_specs(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda *a, **k: None,
            run_investigation=run_investigation,
            emit_tool_progress=cues.append,
        )
    }["mooring_investigate"]
    out = spec.handler(_invocation(branches=[{"question": question}]))
    assert cues == [
        "researching 3 questions in parallel…",
        "researched 1 of 3…",
        "merging findings from 2 of 3 branches…",
    ]
    # The cue carries COUNTS only — never a sub-question's text nor a finding's text.
    # (The findings themselves still reach the model, but as the tool RESULT.)
    assert not any(question in c or finding in c for c in cues)
    assert finding in out.text


def test_investigate_progress_is_a_noop_without_a_sink(ws):
    def run_investigation(branches, on_progress=None):
        on_progress({"phase": "start", "done": 0, "total": 2})  # must not raise
        return "findings"

    spec = {
        s.name: s
        for s in build_tool_specs(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda *a, **k: None,
            run_investigation=run_investigation,
        )
    }["mooring_investigate"]
    assert not spec.handler(_invocation(branches=[{"question": "q"}])).is_error


def test_investigate_copilot_handler_is_async_so_it_cannot_wedge_the_event_loop(ws):
    # The Copilot SDK calls tool handlers ON the session's asyncio loop thread and awaits an
    # awaitable result. The fan-out blocks for as long as its slowest branch, so its handler
    # MUST be a coroutine that offloads to a worker thread — otherwise close()/teardown
    # (scheduled with run_coroutine_threadsafe) can never run until the fan-out finishes.
    import asyncio
    import inspect

    tools = {
        t.name: t
        for t in build_tools(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda *a, **k: None,
            run_investigation=lambda b, on_progress=None: "merged findings",
        )
    }
    assert inspect.iscoroutinefunction(tools["mooring_investigate"].handler)
    res = asyncio.run(tools["mooring_investigate"].handler(_invocation(branches=[{"question": "q"}])))
    assert "merged findings" in res.text_result_for_llm
    # Every other (fast) tool stays a plain sync callable — no needless thread hop.
    assert not inspect.iscoroutinefunction(tools["mooring_get_schema"].handler)


def test_investigate_tool_rejects_empty_branches(ws):
    spec = {
        s.name: s
        for s in build_tool_specs(
            workspace=ws,
            folders=("data",),
            notebook_rel="nb.py",
            emit_proposal=lambda *a, **k: None,
            run_investigation=lambda b: "x",
        )
    }["mooring_investigate"]
    assert spec.handler(_invocation(branches=[])).is_error
    assert spec.handler(_invocation()).is_error


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
    out = (
        tools["mooring_get_schema"]
        .handler(_invocation(dataset="data/wide.parquet"))
        .text_result_for_llm
    )
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
    res = _call(tools["mooring_propose_notebook_edit"], _invocation(code="x = 1 + 1", rationale="demo"))
    assert proposals == [("x = 1 + 1", "demo")]  # surfaced to the analyst
    assert "apply" in res.text_result_for_llm.lower()  # the agent did not inject


def test_propose_cell_preserves_a_mo_sql_body(ws):
    # "Speak SQL": a marimo SQL cell is just `x = mo.sql(...)` — it flows through the
    # SAME propose path as any cell, unchanged (no SQL-specific handling, no mangling).
    proposals = []
    tools = _tools(ws, proposals)
    body = 'monthly = mo.sql("""SELECT region, SUM(amount) AS total FROM sales GROUP BY region""")'
    _call(tools["mooring_propose_notebook_edit"], _invocation(code=body))
    assert proposals == [(body, "")]


def test_sql_cell_guide_is_value_free_and_names_the_idiom():
    from mooring.ai import tools

    guide = tools.sql_cell_guide()
    assert "mo.sql" in guide and "DuckDB" in guide
    assert "mooring_propose_notebook_edit" in guide
    # It teaches the schema-only discipline, never a data value.
    assert "never inline a data value" in guide
    # The two "applied cell must actually run" requirements (review findings).
    assert "import marimo as mo" in guide
    assert "duckdb" in guide.lower()
    # The value-blindness caveat: a value->header pivot would smuggle data values into the
    # column names the live-schema probe reports to the model.
    assert "PIVOT" in guide
    assert SECRET not in guide


def test_sql_cell_guide_teaches_read_only_sql():
    # The guide taught the mo.sql idiom and nothing else — so nothing discouraged authoring
    # a cell that writes, and an applied cell runs at once (undo restores only the text).
    from mooring.ai import tools

    guide = tools.sql_cell_guide()
    assert "READ-ONLY" in guide
    assert "SELECT / WITH ... SELECT" in guide
    for keyword in ("DROP", "TRUNCATE", "DELETE", "INSERT", "UPDATE", "ALTER", "MERGE"):
        assert keyword in guide
    # The value-blindness caveat is untouched by the safety rule.
    assert "Do NOT pivot or crosstab row VALUES into column headers" in guide
    assert SECRET not in guide


def test_build_system_context_folds_in_the_sql_help():
    # The SQL capability reaches the model through the ONE context choke point, and only
    # when passed (default omits it). It introduces no data value — SQL is authored code.
    from mooring.ai import egress, tools

    ctx = egress.build_system_context(
        schema_text="amount: float",
        notebook_source=f"{SECRET} = 1\ndf = pl.read_csv('x')",
        notebook_rel="nb.py",
        sql_help=tools.sql_cell_guide(),
    )
    assert "mo.sql" in ctx and "DuckDB" in ctx

    without = egress.build_system_context(
        schema_text="amount: float", notebook_source="df = 1", notebook_rel="nb.py"
    )
    assert "mo.sql" not in without  # omitted unless explicitly provided


def test_there_is_exactly_one_propose_tool(ws):
    # The consolidation. There used to be FOUR overlapping propose tools, and picking
    # the wrong one was not graceful: asked to FIX cell 3, a model that reached for the
    # APPEND tool wrote a second definition of the same name and stopped both cells.
    # Tool selection is one of the sharpest capability gradients between model tiers, so
    # the choice was removed rather than documented.
    full = _edit_tools(ws, [])
    assert sorted(full) == sorted(TOOL_NAMES)
    assert [n for n in full if "propose" in n] == ["mooring_propose_notebook_edit"]
    for tool in full.values():
        assert tool.skip_permission is True  # still value-free by construction
    # ...and the retired names are gone, not aliased: an alias is still ADVERTISED (on
    # the copilot path `available_tools` IS the tool list), so it would leave the model
    # with the same four-way choice this removes.
    for retired in (
        "mooring_propose_cell",
        "mooring_propose_cell_edit",
        "mooring_propose_notebook_rewrite",
    ):
        assert retired not in full


def test_the_propose_tool_is_on_with_either_proposal_callback(ws):
    # Both real sessions wire both callbacks; each on its own is enough to register the
    # write surface, and NEITHER is the read-only investigate sub-agent (tested above).
    assert "mooring_propose_notebook_edit" in _spec_names(ws, emit_proposal=lambda *a, **k: None)
    assert "mooring_propose_notebook_edit" in _spec_names(
        ws, emit_proposal_patch=lambda payload: None
    )


def test_an_edit_captures_the_anchor_and_does_not_write(ws):
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    before = (ws / "nb.py").read_text("utf-8")
    patches = []
    res = _call(
        _edit_tools(ws, patches)["mooring_propose_notebook_edit"],
        _invocation(
            edits=[{"index": 1, "expect": "x = seed + 1", "code": "x = seed + 99"}],
            rationale="bump",
        ),
    )
    assert "apply" in res.text_result_for_llm.lower()
    [payload] = patches
    assert payload["kind"] == "edit"  # a lone edit keeps its own card, as before
    assert payload["ops"][0] == {
        "op": "edit",
        "index": 1,
        "anchor": "x = seed + 1",
        "code": "x = seed + 99",
    }
    # The model's `expect` is a propose-time claim ONLY: it never enters the ops, so the
    # payload the analyst applies is byte-identical to what four tools used to emit.
    assert "expect" not in payload["ops"][0]
    assert payload["diffs"][0]["before"] == "x = seed + 1"  # diff view gets the old code
    assert (ws / "nb.py").read_text("utf-8") == before  # propose-only; the analyst applies


def test_an_edit_out_of_range_errors(ws):
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    res = _call(
        _edit_tools(ws, [])["mooring_propose_notebook_edit"],
        _invocation(edits=[{"index": 9, "expect": "z = 0", "code": "z = 0"}]),
    )
    assert res.result_type == "error"


def test_propose_notebook_edit_builds_combined_ops(ws):
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    patches = []
    _call(
        _edit_tools(ws, patches)["mooring_propose_notebook_edit"],
        _invocation(
            edits=[{"index": 0, "expect": "seed = 1", "code": "seed = 2"}],
            appends=["extra = 1"],
            deletes=[{"index": 1, "expect": "x = seed + 1"}],
        ),
    )
    [payload] = patches
    assert payload["kind"] == "patch"
    assert [o["op"] for o in payload["ops"]] == ["edit", "delete", "append"]
    assert payload["ops"][0]["anchor"] == "seed = 1"  # server-captured, not retyped


def test_the_cells_field_rewrites_the_whole_notebook(ws):
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    patches = []
    _call(
        _edit_tools(ws, patches)["mooring_propose_notebook_edit"],
        _invocation(cells=["a = 1", "b = a + 1"], expect_cells=2),
    )
    [payload] = patches
    assert payload["kind"] == "rewrite"
    assert payload["ops"][0] == {"op": "replace_all", "cells": ["a = 1", "b = a + 1"]}


def test_a_rewrite_cannot_be_mixed_with_targeted_changes(ws):
    # `cells` is the most destructive shape the one tool has, and the slip it guards
    # against is a model that meant to APPEND filling it in: exclusivity means a
    # half-filled rewrite is an error, never a silent wipe.
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    patches = []
    out = _edit_tools(ws, patches)["mooring_propose_notebook_edit"]
    for extra in ({"appends": ["z = 1"]}, {"deletes": [{"index": 0, "expect": "seed = 1"}]}):
        res = _call(out, _invocation(cells=["a = 1"], expect_cells=2, **extra))
        assert res.result_type == "error"
        assert "cannot be combined" in res.error
    assert patches == []


def test_a_bare_string_of_cells_is_refused_not_split(ws):
    # `_ops_from_wire` guards this too, but a string here would iterate per CHARACTER —
    # a whole notebook of one-letter cells. Refuse it where the model can be told why.
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    patches = []
    res = _call(
        _edit_tools(ws, patches)["mooring_propose_notebook_edit"],
        _invocation(cells="a = 1", expect_cells=2),
    )
    assert res.result_type == "error" and "must be a LIST" in res.error
    assert patches == []


def test_propose_tools_normalize_returns_in_cell_bodies(ws):
    # The model copies marimo's auto-generated `return` back from the file source; the
    # proposal (ops AND the diff preview) must show the cleaned BODY, not the return.
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    patches = []
    tools = _edit_tools(ws, patches)
    _call(
        tools["mooring_propose_notebook_edit"],
        _invocation(
            cells=["import marimo as mo\nreturn (mo,)", "z = 1\nreturn (z,)"],
            expect_cells=2,
        ),
    )
    assert patches[-1]["ops"][0]["cells"] == ["import marimo as mo", "z = 1"]
    assert "return" not in patches[-1]["diffs"][0]["after"]  # the preview matches the result

    _call(
        tools["mooring_propose_notebook_edit"],
        _invocation(index=0, expect="seed = 1", code="seed = 5\nreturn (seed,)"),
    )
    assert patches[-1]["ops"][0]["code"] == "seed = 5"

    # The flat append shape and the multi-cell patch's edits/appends are cleaned the
    # same way — every code-carrying path through the one tool normalizes.
    appended = []
    tools = _edit_tools(ws, patches, proposals=appended)
    _call(tools["mooring_propose_notebook_edit"], _invocation(code="total = 1\nreturn (total,)"))
    assert appended[-1] == ("total = 1", "")

    _call(
        tools["mooring_propose_notebook_edit"],
        _invocation(
            edits=[
                {"index": 1, "expect": "x = seed + 1", "code": "x = seed + 9\nreturn (x,)"}
            ],
            appends=["@app.cell\ndef _():\n    extra = 2\n    return (extra,)"],
        ),
    )
    ops = patches[-1]["ops"]
    assert ops[0]["code"] == "x = seed + 9"  # edit normalized
    assert ops[1]["code"] == "extra = 2"  # append normalized AND wrapper unwrapped


# --- the propose gate ------------------------------------------------------
#
# Until this existed every propose handler checked that the code string was non-empty,
# emitted the proposal, and told the model it had succeeded — whatever it had written.
# A strong model re-reads its own work; a weaker one believes the environment. These
# pin that a proposal which would break the notebook is REFUSED with the diagnostics,
# that nothing reaches the analyst when it is, and the places the gate deliberately
# does NOT block.


def _real(ws):
    """The workspace with a REAL 2-cell notebook: `seed = 1`, then `x = seed + 1`."""
    (ws / "nb.py").write_text(_REAL_NB, "utf-8")
    return ws


def _nb(*bodies: str) -> str:
    """A marimo notebook whose cells have the given bodies (marimo's own file shape)."""
    cells = "".join(
        "@app.cell\ndef _():\n"
        + "\n".join(f"    {line}" for line in body.splitlines())
        + "\n    return\n\n\n"
        for body in bodies
    )
    return (
        'import marimo\n\n__generated_with = "0.23.9"\napp = marimo.App()\n\n\n'
        + cells
        + 'if __name__ == "__main__":\n    app.run()\n'
    )


def _fake_validator(diagnostic, marker):
    """A validator reporting ``diagnostic`` only for a source containing ``marker``.

    The gate validates the BASE too whenever the candidate looks broken, so that a fault
    the notebook already had is not blamed on the model. A fake that answered the same
    for every source would therefore look pre-existing; keying on the proposed code
    makes it the candidate's.
    """
    return lambda source: [diagnostic] if marker in source else []


def _specs(ws, proposals=None, patches=None):
    """The provider-neutral specs (the handlers BOTH backends share) by name."""
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
        )
    }


def test_a_valid_proposal_still_emits_and_reports_success(ws):
    # The no-regression case: on a proposal that checks out, nothing about the
    # analyst-facing behaviour changes — same event, same payload, same success text.
    proposals, patches = [], []
    specs = _specs(_real(ws), proposals, patches)
    before = (ws / "nb.py").read_text("utf-8")
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="y = x * 2", rationale="why"))
    assert not out.is_error
    assert proposals == [("y = x * 2", "why")]
    assert out.text == "Proposed the cell to the analyst, who will review and apply it."
    assert (ws / "nb.py").read_text("utf-8") == before  # the check never writes


def test_a_duplicate_definition_is_refused_and_nothing_is_proposed(ws):
    # THE weak-model failure: the Jupyter reflex of redefining a name in a new cell.
    # It applies cleanly and then stops both cells and everything downstream of them.
    proposals = []
    specs = _specs(_real(ws), proposals)
    before = (ws / "nb.py").read_text("utf-8")
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="seed = 2"))
    assert out.is_error
    assert "MB002" in out.text and "seed" in out.text
    assert "NOT proposed" in out.text
    assert proposals == []  # the analyst was shown nothing
    assert (ws / "nb.py").read_text("utf-8") == before


def test_a_nested_app_cell_body_is_refused(ws):
    # Valid Python, nonsense marimo (marimo's own linter says nothing about it): the
    # model handed back two whole @app.cell blocks as ONE cell body.
    proposals = []
    specs = _specs(_real(ws), proposals)
    body = (
        "@app.cell\ndef _():\n    a = 1\n    return (a,)\n\n\n"
        "@app.cell\ndef _():\n    b = 2\n    return (b,)"
    )
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code=body))
    assert out.is_error and "MOOR002" in out.text
    assert proposals == []


def test_a_cell_that_does_not_parse_is_refused(ws):
    # Caught while BUILDING the candidate (marimo's codegen would wrap an unparseable
    # cell in `app._unparsable_cell(...)` and re-parse as valid), not by the validator.
    proposals = []
    specs = _specs(_real(ws), proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="def broken(:\n    pass"))
    assert out.is_error and "NOT proposed" in out.text
    assert "invalid syntax" in out.text
    assert proposals == []


def test_the_edit_path_is_gated(ws):
    patches = []
    specs = _specs(_real(ws), patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(edits=[{"index": 1, "expect": "x = seed + 1", "code": "seed = 2"}])
    )
    assert out.is_error and "MB002" in out.text
    assert patches == []


def test_the_multi_edit_path_is_gated(ws):
    patches = []
    specs = _specs(_real(ws), patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(
            edits=[{"index": 1, "expect": "x = seed + 1", "code": "x = seed + 2"}],
            appends=["seed = 99"],
        )
    )
    assert out.is_error and "MB002" in out.text
    assert patches == []


def test_the_rewrite_path_is_gated(ws):
    # A rewrite replaces every cell, so its candidate is the model's cells wholesale —
    # the least constrained path, and the one where an unchecked proposal costs the
    # whole notebook.
    patches = []
    specs = _specs(_real(ws), patches=patches)
    before = (ws / "nb.py").read_text("utf-8")
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(cells=["total = 1", "total = 2"], expect_cells=2)
    )
    assert out.is_error and "MB002" in out.text
    assert patches == []
    assert (ws / "nb.py").read_text("utf-8") == before


def test_diagnostics_reach_the_model_only_through_the_egress_scrub(ws, monkeypatch):
    # The validator forwards marimo's `message`/`fix` VERBATIM and MOOR000 embeds a
    # marimo internal's str(exc). No marimo rule quotes notebook text today, but
    # nothing structurally stops one starting — ruff's messages do exactly that. So
    # the rendered diagnostics take the same egress floor as every other tool result
    # here. Faked, because no rule reachable today can be made to quote a value.
    from mooring import marimo_rt

    card = "4012888888881881"  # Luhn-valid (shared with test_egress)
    monkeypatch.setattr(
        marimo_rt,
        "validate_notebook_source",
        _fake_validator(
            marimo_rt.Diagnostic(
                code="MB002",
                name="multiple-definitions",
                message=f"Variable 'seed' is defined in multiple cells: acct = {card}",
                lines=(8,),
                fix=f"drop the second one (acct = {card})",
            ),
            "y = x * 2",
        ),
    )
    proposals = []
    specs = _specs(_real(ws), proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="y = x * 2"))
    assert out.is_error  # still refused — the scrub changes what is SAID, not the verdict
    assert card not in out.text
    assert "MB002" in out.text  # the rule code survives, so the model can still act
    assert proposals == []


def test_an_unavailable_checker_does_not_block_the_proposal(ws, monkeypatch):
    # FAIL OPEN. A checker that cannot run says nothing about the proposal, and
    # refusing on it would turn a marimo upgrade or a slow machine into a dead
    # copilot — strictly worse than the behaviour this gate replaced. But the model
    # is TOLD, so "checked and clean" and "not checked" never read the same.
    from mooring import marimo_rt

    monkeypatch.setattr(
        marimo_rt,
        "validate_notebook_source",
        lambda source: [
            marimo_rt.Diagnostic(
                code=marimo_rt.DIAG_VALIDATOR_UNAVAILABLE,
                name="validator-unavailable",
                message="mooring could not statically validate this notebook: it timed out",
            )
        ],
    )
    proposals = []
    specs = _specs(_real(ws), proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="y = x * 2"))
    assert not out.is_error
    assert proposals == [("y = x * 2", "")]
    assert "Not blocking" in out.text and marimo_rt.DIAG_VALIDATOR_UNAVAILABLE in out.text


def test_a_notebook_too_large_to_check_does_not_block_the_proposal(ws, monkeypatch):
    # Same call as MOOR000: declined, not cleared. Nothing is known to be wrong.
    from mooring import marimo_rt

    monkeypatch.setattr(
        marimo_rt,
        "validate_notebook_source",
        lambda source: [
            marimo_rt.Diagnostic(
                code=marimo_rt.DIAG_TOO_LARGE,
                name="notebook-too-large",
                message="mooring did not statically validate this notebook: it has 200 cells",
            )
        ],
    )
    proposals = []
    specs = _specs(_real(ws), proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="y = x * 2"))
    assert not out.is_error and proposals
    assert marimo_rt.DIAG_TOO_LARGE in out.text


def test_an_unresolved_name_is_a_note_not_a_refusal(ws, monkeypatch):
    # The one diagnostic whose correctness depends on what happens NEXT: a model may
    # legitimately propose a cell using a name it defines in the FOLLOWING proposal,
    # and the analyst applies them in order. Refusing would break that plan, so it
    # rides along as a note. (It cannot fire on this path today anyway — marimo's
    # codegen writes each cell's refs into its `def _(name):` signature, which the
    # validator's own `_bound_names` backstop then treats as bound. Faked here so the
    # classification is pinned regardless.)
    from mooring import marimo_rt

    monkeypatch.setattr(
        marimo_rt,
        "validate_notebook_source",
        lambda source: [
            marimo_rt.Diagnostic(
                code=marimo_rt.DIAG_UNRESOLVED_REFERENCE,
                name="unresolved-reference",
                message="cell 2 uses customer_frame, which no cell defines",
                lines=(20,),
            )
        ],
    )
    proposals = []
    specs = _specs(_real(ws), proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="out = customer_frame.head()"))
    assert not out.is_error
    assert proposals == [("out = customer_frame.head()", "")]
    assert "customer_frame" in out.text and "Not blocking" in out.text


def test_an_unknown_diagnostic_code_refuses(ws, monkeypatch):
    # Default-refuse for a code neither list names: marimo's rule allowlist is curated
    # and every entry is `breaking`, so a new code is likelier to be a new breaking
    # rule than a new advisory. A maintainer adding one classifies it deliberately.
    from mooring import marimo_rt

    monkeypatch.setattr(
        marimo_rt,
        "validate_notebook_source",
        _fake_validator(
            marimo_rt.Diagnostic(code="MB009", name="new-rule", message="nope"), "y = x * 2"
        ),
    )
    proposals = []
    specs = _specs(_real(ws), proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="y = x * 2"))
    assert out.is_error and proposals == []


def test_repeated_failures_stop_the_model_retrying(ws):
    # A refusal only helps while the model can act on it. The copilot SDK drives its
    # own tool loop with no mooring-side bound, and the OpenAI loop's 12 round-trips
    # cover the WHOLE turn — so a stuck model must not be able to spend the lot
    # re-proposing one cell.
    proposals = []
    specs = _specs(_real(ws), proposals)
    bad = _invocation(code="seed = 2")
    for _ in range(3):
        out = specs["mooring_propose_notebook_edit"].handler(bad)
        assert out.is_error and "MB002" in out.text  # the diagnostics, three times
    out = specs["mooring_propose_notebook_edit"].handler(bad)
    assert out.is_error and "MB002" not in out.text
    assert "Stop calling the propose tools" in out.text
    assert proposals == []

    # The budget measures "stuck", not "has been wrong before": an accepted proposal
    # resets it, so a later mistake gets the diagnostics again.
    assert not specs["mooring_propose_notebook_edit"].handler(_invocation(code="y = x * 2")).is_error
    assert "MB002" in specs["mooring_propose_notebook_edit"].handler(bad).text


def test_a_problem_the_notebook_already_had_does_not_refuse_the_proposal(ws):
    # An analyst opens the copilot on a broken notebook more often than on a healthy
    # one, and a fault that was already there is not the model's to fix before it may
    # propose anything else. Blaming it would refuse EVERY proposal, spend the retry
    # budget, and end with the model told to give up — the gate at its most obstructive
    # in exactly the situation the copilot is most often opened for.
    (ws / "nb.py").write_text(_REAL_NB.replace("x = seed + 1", "seed = 2"), "utf-8")
    proposals = []
    specs = _specs(ws, proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="note = 'hello'"))
    assert not out.is_error
    assert proposals == [("note = 'hello'", "")]
    assert "ALREADY had these problems" in out.text and "MB002" in out.text


def test_a_second_cycle_is_not_laundered_by_the_first(ws):
    # Three of the five allowlisted marimo rules carry a CONSTANT message, so comparing
    # base to candidate by membership would let ONE pre-existing instance whitelist every
    # new one. Here the notebook already has the cycle a<->b and the change adds a whole
    # new, unrelated cycle c<->d: two MB003s with identical (code, message).
    cell = "@app.cell\ndef _({sig}):\n    {body}\n    return ({ret},)\n\n\n"
    (ws / "nb.py").write_text(
        "import marimo\n\n"
        '__generated_with = "0.23.9"\n'
        "app = marimo.App()\n\n\n"
        + cell.format(sig="b", body="a = b + 1", ret="a")
        + cell.format(sig="a", body="b = a + 1", ret="b")
        + 'if __name__ == "__main__":\n    app.run()\n',
        "utf-8",
    )
    patches = []
    specs = _specs(ws, patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(appends=["c = d + 1", "d = c + 1"])
    )
    assert out.is_error and "MB003" in out.text
    assert "ALREADY had" not in out.text  # never told it did not cause what it caused
    assert patches == []


def test_an_nth_definition_of_an_already_duplicated_name_is_not_laundered(ws):
    # marimo reports ONE MB002 per duplicated NAME, so a third definition of a name
    # already defined twice is the same code, the same message AND the same count as the
    # second. What changes is how many cells the finding names — 2 lines becomes 3.
    (ws / "nb.py").write_text(_REAL_NB.replace("x = seed + 1", "seed = 2"), "utf-8")
    proposals = []
    specs = _specs(ws, proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="seed = 3"))
    assert out.is_error and "MB002" in out.text
    assert proposals == []


@pytest.mark.parametrize(
    "invocation, expected",
    [
        # A DELETE renumbers every cell below it, so the pre-existing fault marimo
        # reports as "cell 2" on the base is "cell 1" on the candidate. Nothing about the
        # fault changed; only its ordinal did. An APPEND shifts nothing, so the same
        # notebook and the same fault take the other branch — which is exactly why this
        # is a pair: the bug was invisible from the append side.
        (lambda: _invocation(deletes=[{"index": 0, "expect": "unused = 0"}]), "delete"),
        (lambda: _invocation(appends=["fresh = 2"]), "append"),
    ],
    ids=["delete-shifts-the-ordinal", "append-does-not"],
)
def test_a_pre_existing_fault_reads_the_same_whether_or_not_ordinals_shift(
    ws, invocation, expected
):
    # `return 5` at a cell's top level is MOOR001, and mooring writes the cell ORDINAL
    # into that message — so the comparison key has to normalise it out for the same
    # reason it excludes line numbers.
    (ws / "nb.py").write_text(_nb("unused = 0", "keep = 1", "return 5"), "utf-8")
    proposals, patches = [], []
    specs = _specs(ws, proposals, patches)
    out = specs["mooring_propose_notebook_edit"].handler(invocation())
    assert not out.is_error, f"the {expected} was refused for a fault it did not cause"
    # A lone append keeps the legacy `{code, rationale}` event; a delete is a patch.
    assert len(proposals) + len(patches) == 1
    assert "ALREADY had these problems" in out.text and "MOOR001" in out.text


def test_normalising_the_ordinal_does_not_open_a_new_laundering_hole(ws):
    # Normalising "cell 2" to "cell _" makes the SAME fault in two different cells share
    # a key — which is only safe because the comparison counts. Base has one nested-cell
    # fault; the change adds a second, so the count goes 1 -> 2 and it is still refused.
    nested = (
        "@app.cell\ndef _():\n    a = 1\n    return (a,)\n\n\n"
        "@app.cell\ndef _():\n    b = 2\n    return (b,)"
    )
    (ws / "nb.py").write_text(_nb("keep = 1", nested), "utf-8")
    proposals = []
    specs = _specs(ws, proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code=nested))
    assert out.is_error and "MOOR002" in out.text
    assert proposals == []


def test_an_unchecked_base_does_not_refuse_the_proposal(ws):
    # The ceilings are per-notebook, so the base can decline while the candidate
    # validates: here a 151-cell notebook (over VALIDATE_MAX_CELLS, and already carrying
    # a duplicate definition) and a proposal DELETING a cell, which brings the candidate
    # to 150. Folding the base's MOOR005 into the comparison as though it were a finding
    # would make the pre-existing MB002 read as newly introduced — refusing a correct
    # change three times and then declaring the propose tools dead, on exactly the shape
    # most likely to need one.
    from mooring.marimo_rt import VALIDATE_MAX_CELLS

    bodies = [f"v{i} = {i}" for i in range(VALIDATE_MAX_CELLS - 1)] + ["dup = 1", "dup = 2"]
    assert len(bodies) == VALIDATE_MAX_CELLS + 1
    cells = "".join(f"@app.cell\ndef _():\n    {b}\n    return\n\n\n" for b in bodies)
    (ws / "nb.py").write_text(
        'import marimo\n\n__generated_with = "0.23.9"\napp = marimo.App()\n\n\n'
        + cells
        + 'if __name__ == "__main__":\n    app.run()\n',
        "utf-8",
    )
    patches = []
    specs = _specs(ws, patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(deletes=[{"index": 0, "expect": "v0 = 0"}])
    )
    assert not out.is_error
    assert len(patches) == 1  # the delete reached the analyst
    # Stated honestly: the result HAS the fault, but nothing claims the change is or is
    # not to blame, because the base could not be checked.
    assert "could not check the notebook as it was BEFORE" in out.text
    assert "MB002" in out.text
    assert "ALREADY had" not in out.text


def test_a_poisoned_base_check_does_not_refuse_the_proposal(ws, monkeypatch):
    # The other way into the same branch, and the nastier one: an orphaned validator pass
    # makes marimo_rt answer MOOR000 for as long as it stays alive, so ONE overrun would
    # poison base subtraction session-wide — every later proposal blamed for a fault it
    # did not cause. (The ceilings, tested above, are per-notebook; this is per-process.)
    from mooring import marimo_rt

    real = marimo_rt.validate_notebook_source
    unavailable = marimo_rt.Diagnostic(
        code=marimo_rt.DIAG_VALIDATOR_UNAVAILABLE,
        name="validator-unavailable",
        message=(
            "mooring could not statically validate this notebook: an earlier validation "
            "is still running"
        ),
    )
    # The candidate carries the proposed code; the base does not. So this fails only the
    # BASE check, exactly as a live orphan thread would.
    monkeypatch.setattr(
        marimo_rt,
        "validate_notebook_source",
        lambda source: real(source) if "note = 2" in source else [unavailable],
    )
    (ws / "nb.py").write_text(_REAL_NB.replace("x = seed + 1", "seed = 2"), "utf-8")
    proposals = []
    specs = _specs(ws, proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="note = 2"))
    assert not out.is_error
    assert proposals == [("note = 2", "")]
    assert "could not check the notebook as it was BEFORE" in out.text and "MB002" in out.text


def test_a_new_problem_on_an_already_broken_notebook_is_still_refused(ws):
    # The subtraction is by (code, message), so a DIFFERENT fault the change does
    # introduce still surfaces — and only that one is reported.
    (ws / "nb.py").write_text(_REAL_NB.replace("x = seed + 1", "seed = 2"), "utf-8")
    proposals = []
    specs = _specs(ws, proposals)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(
            code=(
                "@app.cell\ndef _():\n    a = 1\n    return (a,)\n\n\n"
                "@app.cell\ndef _():\n    b = 2\n    return (b,)"
            )
        )
    )
    assert out.is_error and "MOOR002" in out.text
    assert "MB002" not in out.text  # the pre-existing fault is not piled on top
    assert proposals == []


def test_a_notebook_that_cannot_be_parsed_at_all_does_not_crash_the_tool(ws):
    # `ws` ships a stub `nb.py` that is not a marimo notebook at all. There is no
    # candidate to build and nothing to judge the proposal against — and that is the
    # ANALYST's file, not something the model can fix. Behave exactly as before.
    proposals = []
    specs = _specs(ws, proposals)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(code="x = 1 + 1"))
    assert not out.is_error
    assert proposals == [("x = 1 + 1", "")]
    assert out.text == "Proposed the cell to the analyst, who will review and apply it."


def test_a_missing_notebook_does_not_crash_the_tool(ws):
    (ws / "nb.py").unlink()
    proposals = []
    specs = _specs(ws, proposals)
    assert not specs["mooring_propose_notebook_edit"].handler(_invocation(code="x = 1")).is_error
    assert proposals == [("x = 1", "")]


def test_propose_handlers_run_off_the_copilot_event_loop(ws):
    # The SDK calls tool handlers ON the session's loop thread. The gate builds a
    # candidate and runs marimo_rt's validator, which joins its own worker thread and
    # serializes on a module-wide lock (so one chat's propose can queue behind
    # another's). None of that may happen on the loop, so the copilot adapter offloads
    # these handlers to a worker thread.
    import inspect

    tools = _edit_tools(_real(ws), [])
    assert inspect.iscoroutinefunction(tools["mooring_propose_notebook_edit"].handler)
    # ...and the fast, value-free read tools stay plain sync callables.
    assert not inspect.iscoroutinefunction(tools["mooring_read_notebook_source"].handler)


def test_the_copilot_error_channel_carries_the_diagnostics(ws):
    # End-to-end through the copilot minter: a refusal is a failed ToolResult whose
    # `error` field (not text_result_for_llm) carries the diagnostics.
    proposals = []
    res = _call(_tools(_real(ws), proposals)["mooring_propose_notebook_edit"], _invocation(code="seed = 2"))
    assert res.result_type == "error"
    assert "MB002" in res.error
    assert proposals == []


# --- the ONE tool: every op kind through one entry point --------------------


def test_one_tool_covers_append_edit_delete_and_rewrite(ws):
    # The consolidation's actual contract: whatever the change is, it goes through this
    # tool — and what comes OUT is byte-identical to what four separate tools emitted, so
    # the analyst's card and both Apply routes (hub/static/chat.js, app/apply.py) see the
    # shapes they already consume.
    proposals, patches = [], []
    specs = _specs(_real(ws), proposals, patches)
    tool = specs["mooring_propose_notebook_edit"]

    assert not tool.handler(_invocation(appends=["fresh = 1"])).is_error
    assert proposals == [("fresh = 1", "")]  # lone append -> the legacy {code, rationale}

    assert not tool.handler(
        _invocation(edits=[{"index": 1, "expect": "x = seed + 1", "code": "x = seed + 2"}])
    ).is_error
    assert patches[-1]["kind"] == "edit"

    assert not tool.handler(
        _invocation(deletes=[{"index": 1, "expect": "x = seed + 1"}])
    ).is_error
    assert patches[-1]["kind"] == "patch"
    assert [o["op"] for o in patches[-1]["ops"]] == ["delete"]

    assert not tool.handler(_invocation(cells=["a = 1"], expect_cells=2)).is_error
    assert patches[-1]["kind"] == "rewrite"


def test_the_retired_tools_flat_arguments_still_land(ws):
    # Shape tolerance, not an alias: ONE tool with ONE schema, which also answers a model
    # that reaches for the retired append/edit tools' flat arguments instead of failing it
    # on a near-miss. The change still goes through every check — `expect` included.
    proposals, patches = [], []
    specs = _specs(_real(ws), proposals, patches)
    tool = specs["mooring_propose_notebook_edit"]

    assert not tool.handler(_invocation(code="fresh = 1", rationale="why")).is_error
    assert proposals == [("fresh = 1", "why")]  # `code` alone -> one appended cell

    out = tool.handler(_invocation(index=1, code="x = seed + 2", expect="x = seed + 1"))
    assert not out.is_error
    assert patches[-1]["ops"][0]["index"] == 1  # `code` + `index` -> an edit

    # ...and the flat form gets no discount on the verification.
    out = tool.handler(_invocation(index=1, code="x = seed + 3"))
    assert out.is_error and "expect" in out.text


def test_a_scalar_where_a_list_belongs_is_taken_as_one_item(ws):
    proposals = []
    specs = _specs(_real(ws), proposals)
    assert not specs["mooring_propose_notebook_edit"].handler(
        _invocation(appends="fresh = 1")
    ).is_error
    assert proposals == [("fresh = 1", "")]


# --- `expect`: the model says which cell it believes it is editing ----------
#
# The server-captured `anchor` is read LIVE at whatever index the model supplied, so it
# always matches and can only guard the propose->apply race. It cannot tell a
# well-aimed edit from one aimed at a cell the model never read — which is what a stale
# system-context index (built once per session, never refreshed) produces on its own,
# with no attacker involved. `expect` is the model's own claim about what is there, and
# it is REQUIRED: a weaker model omitting it is exactly the model most likely to
# mis-target, so an optional check would be absent precisely where it is needed.


def test_an_edit_aimed_at_the_wrong_cell_is_refused(ws):
    # The demonstrated failure: the model means the cell it read as index 1, an Apply has
    # since renumbered things, and index 1 now holds something else. The anchor matches
    # (it is read at index 1) and the write goes through. `expect` does not.
    patches = []
    specs = _specs(_real(ws), patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(edits=[{"index": 1, "expect": "seed = 1", "code": "seed = 99"}])
    )
    assert out.is_error and "NOT proposed" in out.text
    assert patches == []  # the analyst was shown nothing


def test_a_mistargeted_edit_never_reveals_what_is_really_there(ws):
    # A rejection that quoted the real cell would hand a model that just wants the call to
    # succeed the exact string to paste back — and it would then write over a cell it has
    # still never read. So the refusal re-points instead: what you described is at index N,
    # go and read the notebook. The cell's own text is not in the message.
    (ws / "nb.py").write_text(_nb("seed = 1", "x = seed + 1", "SECRET_CELL_BODY = 3"), "utf-8")
    patches = []
    specs = _specs(ws, patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(edits=[{"index": 2, "expect": "x = seed + 1", "code": "x = seed + 9"}])
    )
    assert out.is_error
    assert "SECRET_CELL_BODY" not in out.text  # never quoted back
    assert "index 1" in out.text  # ...but the cell it MEANT is named, so it can re-aim
    assert "mooring_read_notebook_source" in out.text
    assert patches == []


def test_an_edit_without_expect_is_refused_and_told_to_read(ws):
    patches = []
    specs = _specs(_real(ws), patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(edits=[{"index": 1, "code": "x = seed + 9"}])
    )
    assert out.is_error and "no 'expect' was given" in out.text
    # It must not answer its own question: telling the model what is at index 1 would let
    # it paste that back and succeed without ever having looked.
    assert "x = seed + 1" not in out.text
    assert "mooring_read_notebook_source" in out.text
    assert patches == []


def test_a_delete_needs_expect_too(ws):
    # Deleting the wrong cell is worse than editing it, so a bare integer index — the
    # shape the retired tool took — is answered with the requirement, not accepted.
    patches = []
    specs = _specs(_real(ws), patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(deletes=[1]))
    assert out.is_error and "no 'expect' was given" in out.text
    assert patches == []
    assert not specs["mooring_propose_notebook_edit"].handler(
        _invocation(deletes=[{"index": 1, "expect": "x = seed + 1"}])
    ).is_error


@pytest.mark.parametrize(
    "expect",
    [
        "x = seed + 1",  # exactly as rendered
        "   x = seed + 1   ",  # re-indented / padded
        "x  =  seed  +  1",  # whitespace re-flowed
        "x = seed + 1\n",  # a trailing blank line
    ],
    ids=["verbatim", "padded", "re-flowed", "trailing-blank"],
)
def test_expect_is_tolerant_of_formatting(ws, expect):
    # A false PASS is the behaviour this replaces; a false FAIL blocks correct work. So
    # the comparison normalises whitespace and only ever looks at leading lines.
    patches = []
    specs = _specs(_real(ws), patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(edits=[{"index": 1, "expect": expect, "code": "x = seed + 9"}])
    )
    assert not out.is_error, out.text
    assert len(patches) == 1


def test_expect_accepts_more_than_the_first_line(ws):
    # A model that pastes the whole cell back makes a STRONGER claim and must still pass;
    # one that pastes a DIFFERENT cell's several lines must not.
    (ws / "nb.py").write_text(_nb("seed = 1", "a = 1\nb = 2\nc = 3"), "utf-8")
    patches = []
    specs = _specs(ws, patches=patches)
    tool = specs["mooring_propose_notebook_edit"]
    assert not tool.handler(
        _invocation(edits=[{"index": 1, "expect": "a = 1\nb = 2\nc = 3", "code": "a = 9"}])
    ).is_error
    assert tool.handler(
        _invocation(edits=[{"index": 0, "expect": "a = 1\nb = 2\nc = 3", "code": "seed = 9"}])
    ).is_error


def test_a_mistargeted_edit_spends_the_same_retry_budget(ws):
    # Being unable to aim is the same kind of stuck as being unable to write a cell the
    # notebook accepts, and the copilot SDK drives its own tool loop with no mooring-side
    # bound at all — so a model that cannot find the right cell must not be able to spend
    # a whole turn discovering that.
    patches = []
    specs = _specs(_real(ws), patches=patches)
    bad = _invocation(edits=[{"index": 1, "expect": "seed = 1", "code": "seed = 9"}])
    for _ in range(3):
        assert "NOT proposed" in specs["mooring_propose_notebook_edit"].handler(bad).text
    out = specs["mooring_propose_notebook_edit"].handler(bad)
    assert "Stop calling the propose tools" in out.text
    assert patches == []


def test_a_rewrite_must_say_how_many_cells_it_believes_it_replaces(ws):
    # A rewrite has no index to mis-aim, so `expect` has nothing to check — but it still
    # DELETES every cell, and one composed from a stale view deletes cells the model has
    # never seen. `expect_cells` is that claim, and the refusal withholds the real count
    # for the same reason a mis-target refusal withholds the real cell.
    patches = []
    specs = _specs(_real(ws), patches=patches)
    tool = specs["mooring_propose_notebook_edit"]

    out = tool.handler(_invocation(cells=["a = 1", "b = a + 1"]))
    assert out.is_error and "expect_cells" in out.text
    assert patches == []

    out = tool.handler(_invocation(cells=["a = 1", "b = a + 1"], expect_cells=7))
    assert out.is_error and "7 cell(s)" in out.text
    assert " 2 cell" not in out.text  # the real count is not handed over
    assert patches == []

    assert not tool.handler(
        _invocation(cells=["a = 1", "b = a + 1"], expect_cells=2)
    ).is_error


def test_a_rewrite_of_an_unreadable_notebook_still_goes_through(ws):
    # `ws` ships a stub that is not a marimo notebook: there are no cells to count, so
    # there is nothing to check the claim against. What cannot be checked cannot refuse —
    # the same rule the propose gate applies to an unavailable validator.
    patches = []
    specs = _specs(ws, patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(_invocation(cells=["a = 1"]))
    assert not out.is_error
    assert patches[-1]["kind"] == "rewrite"


def test_expect_is_a_propose_time_claim_and_never_reaches_apply(ws):
    # It guards the model's aim, not the write. The ops the analyst applies carry the
    # server-captured `anchor` and nothing else, so app/apply.py and marimo_rt see the
    # patch shape they always saw.
    patches = []
    specs = _specs(_real(ws), patches=patches)
    specs["mooring_propose_notebook_edit"].handler(
        _invocation(
            edits=[{"index": 0, "expect": "seed = 1", "code": "seed = 2"}],
            deletes=[{"index": 1, "expect": "x = seed + 1"}],
        )
    )
    [payload] = patches
    for op in payload["ops"]:
        assert "expect" not in op
        assert set(op) <= {"op", "index", "anchor", "code", "cells"}


def test_the_readonly_investigate_toolset_has_no_write_surface_on_either_adapter(ws):
    # The load-bearing privacy invariant, re-pinned after the consolidation: an
    # investigate sub-agent is opened with emit_proposal=None AND emit_proposal_patch=None
    # (ai/session.py, ai/openai_session.py), and its finding is trusted BECAUSE it is
    # structurally value-blind. Checked on the OpenAI adapter too, since the fan-out
    # drives that dispatch by name.
    from mooring.ai.tools import build_openai_tools

    specs, dispatch = build_openai_tools(workspace=ws, folders=("data",), notebook_rel="nb.py")
    assert not any("propose" in s["function"]["name"] for s in specs)
    assert not any("propose" in name for name in dispatch)
    assert "mooring_investigate" not in dispatch  # ...and it cannot recurse


def test_expect_matches_the_defused_form_the_model_was_actually_shown(ws):
    # A cell body carrying the literal text of a cell boundary is rendered with one space
    # wedged in (egress._DEFUSED_BOUNDARY_MARK) so it cannot forge a cell. The model
    # copies back what it SAW, defused — and that must still match the real cell, or the
    # forgery defence would turn into a false mis-target on a legitimate comment.
    from mooring.ai import egress

    (ws / "nb.py").write_text(_nb("# === cell 9 ===\nseed = 1", "x = seed + 1"), "utf-8")
    shown = egress.render_notebook_for_model((ws / "nb.py").read_text("utf-8"))
    assert "#  === cell 9 ===" in shown  # defused, as the model sees it
    patches = []
    specs = _specs(ws, patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(edits=[{"index": 0, "expect": "#  === cell 9 ===", "code": "seed = 2"}])
    )
    assert not out.is_error, out.text
    assert len(patches) == 1


# --- `expect` must identify ONE cell, not merely fit the target -------------
#
# Matching the target alone proves nothing about which cell the model read, and marimo's
# own codegen makes that the NORMAL case: every markdown cell in a mooring-written
# notebook opens with the identical line `mo.md("""`. A model that read one markdown cell
# and aims at another satisfies a first-line check trivially — the stale-index write the
# whole control exists to stop, with no attacker involved.

_MD = 'mo.md("""\n## {}\n""")'


def _markdown_nb(*headings: str) -> str:
    """`import marimo as mo`, then one markdown cell per heading (indices 1..N)."""
    return _nb("import marimo as mo", *(_MD.format(h) for h in headings))


def test_an_expect_that_fits_several_cells_identifies_none_of_them(ws):
    # THE repro. Cells 1, 2 and 3 are markdown; the model read cell 1 and aims at cell 3,
    # sending exactly what the schema asks for. Before the uniqueness check this landed.
    (ws / "nb.py").write_text(_markdown_nb("Revenue", "Costs", "Margin"), "utf-8")
    patches = []
    specs = _specs(ws, patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(
            edits=[{"index": 3, "expect": 'mo.md("""', "code": _MD.format("Rewritten")}]
        )
    )
    assert out.is_error and "NOT proposed" in out.text
    assert "describes 3 of this notebook's cells" in out.text
    assert "## Revenue" not in out.text and "## Margin" not in out.text  # no content leak
    assert patches == []


def test_more_lines_disambiguate_and_the_edit_lands(ws):
    # The retry the refusal asks for: one more line names exactly one cell, so a correct
    # model pays a single round-trip and the valid path still sails through.
    (ws / "nb.py").write_text(_markdown_nb("Revenue", "Costs", "Margin"), "utf-8")
    patches = []
    specs = _specs(ws, patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(
            edits=[
                {
                    "index": 3,
                    "expect": 'mo.md("""\n## Margin',
                    "code": _MD.format("Rewritten"),
                }
            ]
        )
    )
    assert not out.is_error, out.text
    assert patches[-1]["ops"][0]["index"] == 3
    assert patches[-1]["ops"][0]["anchor"] == _MD.format("Margin")


def test_a_delete_is_held_to_the_same_uniqueness(ws):
    # Deleting the wrong markdown cell is silent in a way an edit is not: nothing
    # redefines a name, so the static gate sees a perfectly healthy notebook.
    (ws / "nb.py").write_text(_markdown_nb("Revenue", "Costs", "Margin"), "utf-8")
    patches = []
    specs = _specs(ws, patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(deletes=[{"index": 2, "expect": 'mo.md("""'}])
    )
    assert out.is_error and "describes 3 of this notebook's cells" in out.text
    assert patches == []
    assert not specs["mooring_propose_notebook_edit"].handler(
        _invocation(deletes=[{"index": 2, "expect": 'mo.md("""\n## Costs'}])
    ).is_error


def test_identical_cells_are_not_ambiguous(ws):
    # The carve-out. Two byte-identical separator cells are legal marimo (they define
    # nothing), and the risk `expect` guards — writing over content you never saw — is
    # nil when every candidate holds exactly the content the model described. Without
    # this they would be permanently uneditable: no extra line could ever tell them apart.
    (ws / "nb.py").write_text(
        _nb("import marimo as mo", 'mo.md("""---""")', 'mo.md("""---""")'), "utf-8"
    )
    patches = []
    specs = _specs(ws, patches=patches)
    out = specs["mooring_propose_notebook_edit"].handler(
        _invocation(edits=[{"index": 2, "expect": 'mo.md("""---""")', "code": _MD.format("New")}])
    )
    assert not out.is_error, out.text
    assert patches[-1]["ops"][0]["index"] == 2


def test_a_unique_first_line_still_costs_only_one_line(ws):
    # The uniqueness rule must not tax the ordinary case: a cell whose first line is its
    # own still verifies from that one line.
    patches = []
    specs = _specs(_real(ws), patches=patches)
    assert not specs["mooring_propose_notebook_edit"].handler(
        _invocation(edits=[{"index": 1, "expect": "x = seed + 1", "code": "x = seed + 9"}])
    ).is_error
    assert len(patches) == 1


# --- a tolerance may decide how an argument READS, never which OP runs ------


def test_a_malformed_index_is_an_error_not_a_silent_append(ws):
    # The flat shape read a non-numeric `index` as "no index" and appended. That turns a
    # model's EDIT into an APPEND with no error — producing the second definition of a
    # name that stops both cells, which is the exact slip this consolidation removes.
    proposals, patches = [], []
    specs = _specs(_real(ws), proposals, patches)
    for bad in ("three", {}, [1], 1.5e400):
        out = specs["mooring_propose_notebook_edit"].handler(
            _invocation(index=bad, expect="x = seed + 1", code="x = seed + 9")
        )
        assert out.is_error, bad
        assert "must be an integer cell number" in out.text, bad
    assert proposals == [] and patches == []


def test_an_absent_or_null_index_still_means_append(ws):
    # ...but absence is not malformation: models routinely emit null for a parameter they
    # are omitting, and that has always meant "add a new cell".
    proposals = []
    specs = _specs(_real(ws), proposals)
    assert not specs["mooring_propose_notebook_edit"].handler(
        _invocation(code="fresh = 1")
    ).is_error
    assert not specs["mooring_propose_notebook_edit"].handler(
        _invocation(index=None, code="fresh2 = 1")
    ).is_error
    assert proposals == [("fresh = 1", ""), ("fresh2 = 1", "")]
