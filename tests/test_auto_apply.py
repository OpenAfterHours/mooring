"""The model's own write: what it is still not allowed to do, and what one turn costs.

The Apply button is gone for a REVERSIBLE change — the write lands inside the tool call
and the model reads back a value-free observation. Everything that made that safe has to
survive the removal, so this file is mostly about the things that must still be true:

* a hold is still a hold (``auto_apply = false``, a policy that pins it, and a codeguard
  ``floor`` cell all stop the write dead, and nothing reaches the file);
* a stopped turn writes nothing;
* one TURN is one undo step, so "put it back the way it was" means the whole turn;
* "mooring could not see it run" is never reported as "the cell failed";
* the automatic re-run fires only where re-running is safe.

The knobs are read off DISK at every write (like ``[ai] apply_guard``), so every test
here writes a real config file rather than handing a value in — the point being tested is
that a mid-session change bites on the very next write.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mooring import config, notebook_undo, paths, workspace_config
from mooring.ai import introspect
from mooring.ai.chat import _event_payload
from mooring.app import auto_apply, notebook_run
from mooring.app.apply import ApplyGuard

_NB_SRC = (
    "import marimo\n\n"
    '__generated_with = "0.23.9"\n'
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    seed = 1\n"
    "    return (seed,)\n\n\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)

# A notebook that ALREADY contains something codeguard bands as `floor`. Re-running it
# is not safe, so the automatic run-report must not touch it — even though the change
# arriving is perfectly clean.
_NB_DESTRUCTIVE = _NB_SRC.replace(
    "    seed = 1\n    return (seed,)\n",
    '    import os\n    os.remove("data/old.csv")\n    seed = 1\n    return (seed,)\n',
)

_FLOOR_CELL = 'import os\nos.remove("data/old.csv")\n'
_CLEAN_CELL = "total = 41 + 1\n"
_SECOND_CELL = "doubled = 2\n"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace plus an isolated per-machine config.toml.

    Without the config isolation the developer's own ``[ai]`` block would decide whether
    auto-apply is on — which is exactly the value these tests are varying.
    """
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    for var in (
        "MOORING_AI_AUTO_APPLY",
        "MOORING_AI_AUTO_RUN_REPORT",
        "MOORING_AI_APPLY_GUARD",
    ):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "appdata").mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "nb.py").write_text(_NB_SRC, "utf-8", newline="\n")
    return workspace


def _set_local(text: str) -> None:
    paths.user_config_file().write_text(text, "utf-8", newline="\n")


def _set_shared(workspace: Path, text: str) -> None:
    """The SYNCED mooring.toml — the channel a team (or an attacker) controls."""
    (workspace / workspace_config.WORKSPACE_CONFIG_NAME).write_text(text, "utf-8", newline="\n")


class _FakeSession:
    """Just enough session for the applier: the cancel flag and the report composer."""

    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled
        self.reported: list = []

    def cancel_requested(self) -> bool:
        return self._cancelled

    def run_failure_report(self, failures):
        self.reported.append(list(failures))
        return ("the notebook did not run clean", [])


def _applier(workspace: Path, *, guard=None, editor=None, session=None):
    made = auto_apply.make_applier(
        workspace=workspace,
        notebook_rel="nb.py",
        guard=guard or ApplyGuard(),
        cfg_fn=lambda: config.Config(workspace_path=str(workspace)),
        editor_fn=lambda: editor,
    )
    made.bind(session if session is not None else _FakeSession())
    return made


def _append(code: str) -> list[dict]:
    return [{"op": "append", "code": code}]


# -- a hold is still a hold ----------------------------------------------------


def test_auto_apply_off_mid_session_holds_the_very_next_write(ws):
    """The knob is read at the WRITE, not at chat-open: one applier, two answers."""
    apply_edit = _applier(ws)
    first = apply_edit(_append(_CLEAN_CELL), "add a total")
    assert first.status == "applied"
    assert "total" in (ws / "nb.py").read_text("utf-8")

    after_first = (ws / "nb.py").read_bytes()
    _set_local("[ai]\nauto_apply = false\n")

    second = apply_edit(_append(_SECOND_CELL), "and a double")
    assert second.status == "held"
    assert second.is_error is False  # a hold is not a failure — nothing to retry
    assert (ws / "nb.py").read_bytes() == after_first  # nothing was written


def test_a_policy_can_take_the_write_away_and_the_next_one_is_held(ws):
    """``ai.auto_apply``'s safe value is False, so a synced policy pinning it survives
    and the local ``true`` cannot answer back."""
    _set_local("[ai]\nauto_apply = true\n")
    _set_shared(ws, '[policy.settings]\n"ai.auto_apply" = false\n')

    outcome = _applier(ws)(_append(_CLEAN_CELL), "")

    assert outcome.status == "held"
    assert (ws / "nb.py").read_text("utf-8") == _NB_SRC
    assert notebook_undo.depth(ws, "nb.py") == 0


def test_a_policy_cannot_turn_the_write_on(ws):
    """The direction that must be impossible: a repo cannot make a teammate's copilot
    write for itself. The permissive spelling is dropped at parse."""
    from mooring import policy

    _set_local("[ai]\nauto_apply = false\n")
    _set_shared(ws, '[policy.settings]\n"ai.auto_apply" = true\n')

    pol = policy.load(ws)
    assert pol.settings == {}
    assert any("ai.auto_apply" in reason for reason in pol.ignored)
    assert _applier(ws)(_append(_CLEAN_CELL), "").status == "held"


def test_manual_mode_hold_carries_the_ordinary_proposal_card(ws):
    """Manual mode reuses the hold path on purpose, so there is no second UI: the
    payload is the card the analyst already knows, with no gate block on it."""
    _set_local("[ai]\nauto_apply = false\n")

    outcome = _applier(ws)(_append(_CLEAN_CELL), "add a total")

    # A lone append is the legacy additive-block card, exactly as propose mode emits it.
    assert outcome.payload["kind"] == "append"
    assert outcome.payload["code"] == _CLEAN_CELL
    assert outcome.payload["ops"] == _append(_CLEAN_CELL)
    assert outcome.payload["diffs"] == [{"label": "new cell", "before": "", "after": _CLEAN_CELL}]
    assert outcome.payload["rationale"] == "add a total"
    assert "gate" not in outcome.payload  # nothing to confirm — a human just has to act


def test_a_held_edit_and_rewrite_keep_their_own_card_shapes(ws):
    """Kind for kind with propose mode: an append is an additive block, one edit is a
    one-cell diff, a rewrite says so. A hold must not change what the card looks like."""
    _set_local("[ai]\nauto_apply = false\n")
    apply_edit = _applier(ws)

    edit = apply_edit([{"op": "edit", "index": 0, "anchor": "seed = 1", "code": "seed = 2\n"}], "")
    assert edit.payload["kind"] == "edit"
    assert edit.payload["diffs"] == [
        {"label": "cell 0", "before": "seed = 1", "after": "seed = 2\n"}
    ]

    rewrite = apply_edit([{"op": "replace_all", "cells": ["a = 1\n"]}], "")
    assert rewrite.payload["kind"] == "rewrite"

    mixed = apply_edit(_append(_CLEAN_CELL) + [{"op": "delete", "index": 0, "anchor": "s"}], "")
    assert mixed.payload["kind"] == "patch"


def test_a_floor_cell_is_still_held_and_nothing_is_written(ws):
    """The gate the feature keeps. ``os.remove`` is irreversible, Undo is not a remedy
    for it, and no amount of auto-apply changes that."""
    guard = ApplyGuard()

    outcome = _applier(ws, guard=guard)(_append(_FLOOR_CELL), "tidy up")

    assert outcome.status == "held"
    assert outcome.payload["gate"]["band"] == "floor"
    assert outcome.payload["gate"]["token"]  # the confirm the analyst's Apply re-derives
    assert {f["kind"] for f in outcome.payload["gate"]["findings"]} == {"deletes_files"}
    # No bytes AND no snapshot: a held write must leave nothing behind at all.
    assert (ws / "nb.py").read_text("utf-8") == _NB_SRC
    assert notebook_undo.depth(ws, "nb.py") == 0


def test_the_held_text_tells_the_model_why_without_quoting_the_cell(ws):
    outcome = _applier(ws)(_append(_FLOOR_CELL), "")
    assert "Deletes files or folders" in outcome.text  # codeguard's fixed label
    assert "os.remove" not in outcome.text
    assert "data/old.csv" not in outcome.text


# -- the stop ------------------------------------------------------------------


def test_a_cancelled_turn_writes_nothing(ws):
    apply_edit = _applier(ws, session=_FakeSession(cancelled=True))

    outcome = apply_edit(_append(_CLEAN_CELL), "")

    assert outcome.status == "cancelled"
    assert outcome.is_error is False
    assert (ws / "nb.py").read_text("utf-8") == _NB_SRC
    assert notebook_undo.depth(ws, "nb.py") == 0


# -- one turn is one undo step -------------------------------------------------


def test_two_writes_in_one_turn_are_one_undo_step_back_to_the_pre_turn_state(ws):
    guard = ApplyGuard()
    apply_edit = _applier(ws, guard=guard)
    before = (ws / "nb.py").read_bytes()

    assert apply_edit(_append(_CLEAN_CELL), "").status == "applied"
    assert apply_edit(_append(_SECOND_CELL), "").status == "applied"

    source = (ws / "nb.py").read_text("utf-8")
    assert "total" in source and "doubled" in source
    # ONE step, not two: "undo what the assistant just did" is the unit.
    assert notebook_undo.depth(ws, "nb.py") == 1

    remaining = guard.restore_undo(ws / "nb.py", ws, "nb.py")
    assert remaining == 0
    assert (ws / "nb.py").read_bytes() == before  # the whole turn, not the last write


def test_the_receipt_reports_the_depth_the_turn_actually_has(ws):
    apply_edit = _applier(ws)
    first = apply_edit(_append(_CLEAN_CELL), "")
    second = apply_edit(_append(_SECOND_CELL), "")
    # The second write extended the checkpoint rather than pushing one, so the depth it
    # reports is the depth the Revert button will really find.
    assert first.payload["undo_depth"] == 1
    assert second.payload["undo_depth"] == 1
    assert first.payload["turn_id"] == second.payload["turn_id"]


def test_a_new_turn_opens_a_new_checkpoint(ws):
    apply_edit = _applier(ws)
    apply_edit(_append(_CLEAN_CELL), "")
    apply_edit.begin_turn()
    second = apply_edit(_append(_SECOND_CELL), "")

    assert notebook_undo.depth(ws, "nb.py") == 2
    assert second.payload["undo_depth"] == 2


def test_a_manual_apply_between_two_writes_breaks_the_chain(ws):
    """The map is never trusted on its own. A write that lands from somewhere else moves
    the top of the stack, and extending a checkpoint that no longer describes the
    pre-turn bytes would revert the wrong layer."""
    guard = ApplyGuard()
    apply_edit = _applier(ws, guard=guard)
    apply_edit(_append(_CLEAN_CELL), "")
    # A manual Apply (no turn id) — the analyst clicking the button on an old card.
    guard.apply_with_undo(ws / "nb.py", ws, "nb.py", _append("manual = 3\n"))
    mid = (ws / "nb.py").read_bytes()

    apply_edit(_append(_SECOND_CELL), "")

    assert notebook_undo.depth(ws, "nb.py") == 3
    assert guard.restore_undo(ws / "nb.py", ws, "nb.py") == 2
    assert (ws / "nb.py").read_bytes() == mid


def test_a_failed_patch_in_a_turn_leaves_no_phantom_step(ws):
    """The discard-on-failure rule, checked through the turn path: the first write opens
    the checkpoint, the second is malformed, and the notebook keeps exactly one step."""
    apply_edit = _applier(ws)
    apply_edit(_append(_CLEAN_CELL), "")
    after_first = (ws / "nb.py").read_bytes()

    outcome = apply_edit([{"op": "edit", "index": 99, "anchor": "nope", "code": "x = 1\n"}], "")

    assert outcome.status in ("conflict", "error")
    assert outcome.is_error is True
    assert (ws / "nb.py").read_bytes() == after_first
    assert notebook_undo.depth(ws, "nb.py") == 1


# -- "could not see" is not "it failed" ----------------------------------------


def test_an_unobservable_run_is_never_reported_as_a_failure(ws):
    """No editor is running, so nothing is known about whether the cell ran. Both the
    text the MODEL reads and the line the ANALYST reads have to say that — a confident
    "it failed" here would make the model rewrite working code."""
    outcome = _applier(ws, editor=None)(_append(_CLEAN_CELL), "")

    assert outcome.status == "applied" and outcome.is_error is False
    assert "could not observe" in outcome.text
    assert "NOT bound" not in outcome.text
    assert "did not run to completion" not in outcome.text
    assert "could not see the notebook run" in outcome.payload["observation"]
    assert "not bound" not in outcome.payload["observation"]


def test_a_broken_observation_is_still_an_applied_write(ws, monkeypatch):
    """The bytes are on disk and marimo is running them. Reporting an error because the
    LOOK failed would tell the model its change did not land — the one answer that makes
    it go and undo working code."""

    def boom(*a, **k):
        raise RuntimeError("the probe exploded")

    monkeypatch.setattr(introspect, "observe", boom)
    outcome = _applier(ws)(_append(_CLEAN_CELL), "")

    assert outcome.status == "applied" and outcome.is_error is False
    assert "could not observe" in outcome.text
    assert "exploded" not in outcome.text
    assert "total" in (ws / "nb.py").read_text("utf-8")


def test_an_unobservable_run_still_offers_the_way_back(ws):
    outcome = _applier(ws)(_append(_CLEAN_CELL), "")
    assert outcome.payload["undo_depth"] == 1
    assert outcome.payload["summary"]["appended"]  # the receipt still names what changed


def test_the_observation_reaches_the_model_and_the_receipt(ws, monkeypatch):
    """The whole point of the feature: what came BACK, in both places it is read."""
    monkeypatch.setattr(
        introspect,
        "observe",
        lambda *a, **k: introspect.Observation(present=("total",), observed=True),
    )
    outcome = _applier(ws)(_append(_CLEAN_CELL), "")

    assert "`total` is bound" in outcome.text
    assert outcome.payload["observation"] == "total bound"


def test_the_applier_asks_about_the_names_the_written_cell_defines(ws, monkeypatch):
    asked: list = []

    def spy(editor, notebook_rel, expect_names, **kw):
        asked.append(tuple(expect_names))
        return introspect.Observation(observed=True, present=tuple(expect_names))

    monkeypatch.setattr(introspect, "observe", spy)
    _applier(ws)(_append(_CLEAN_CELL), "")

    assert asked == [("total",)]  # the appended cell's own binding, not the seed cell's


# -- the payload the UI actually reads -----------------------------------------


def test_the_outcome_body_is_named_payload_and_the_session_finds_it(ws):
    """A mismatch here ships an EMPTY receipt and nothing complains, so it is pinned:
    ``ai.chat._event_payload`` is what turns an outcome into the browser event."""
    outcome = _applier(ws)(_append(_CLEAN_CELL), "why")

    assert isinstance(outcome.payload, dict) and outcome.payload
    assert _event_payload(outcome) == outcome.payload
    assert set(outcome.payload) >= {
        "summary",
        "rationale",
        "undo_depth",
        "turn_id",
        "observation",
    }


def test_the_receipt_summary_names_the_cells_that_changed(ws):
    apply_edit = _applier(ws)
    appended = apply_edit(_append(_CLEAN_CELL), "").payload["summary"]
    assert appended == {"edited": [], "appended": [1], "deleted": []}

    edited = apply_edit(
        [{"op": "edit", "index": 0, "anchor": "seed = 1", "code": "seed = 2\n"}], ""
    ).payload["summary"]
    assert edited == {"edited": [0], "appended": [], "deleted": []}


def test_the_receipt_payload_carries_no_code(ws):
    outcome = _applier(ws)(_append(_CLEAN_CELL), "add a total")
    body = repr(outcome.payload)
    assert "41 + 1" not in body and "total =" not in body


# -- the automatic run-report --------------------------------------------------


def _fake_export(returncode, stderr=""):
    """Stand in for ``notebook_run._exec``; marimo writes its render whenever it ran."""

    def _run(cmd, cwd, env, timeout, cancel=None):
        out = Path(cmd[cmd.index("-o") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html>ok</html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return _run


@pytest.fixture
def missing_name(monkeypatch):
    """An observation that says a name the change should have bound is NOT there — the
    only condition under which the automatic re-run is even considered."""
    monkeypatch.setattr(
        introspect,
        "observe",
        lambda *a, **k: introspect.Observation(missing=("total",), observed=True),
    )


def test_the_auto_run_report_does_not_fire_on_a_non_clean_notebook(ws, monkeypatch, missing_name):
    """The codeguard band IS the condition: it answers "is re-executing this safe?".
    This notebook already deletes a file, so it is never re-run behind the analyst's
    back — however badly the model wants the error."""
    (ws / "nb.py").write_text(_NB_DESTRUCTIVE, "utf-8", newline="\n")
    ran: list = []
    monkeypatch.setattr(notebook_run, "_exec", lambda *a, **k: ran.append(a))

    outcome = _applier(ws)(_append(_CLEAN_CELL), "")

    assert outcome.status == "applied"
    assert ran == []
    assert "did not run clean" not in outcome.text


def test_the_auto_run_report_fires_on_a_clean_notebook_and_reaches_the_model(
    ws, monkeypatch, missing_name
):
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, "MarimoSyntaxError: bad\n"))
    session = _FakeSession()

    outcome = _applier(ws, session=session)(_append(_CLEAN_CELL), "")

    assert session.reported == [[("MarimoSyntaxError", "bad")]]
    assert "the notebook did not run clean" in outcome.text
    # The observation is still there — the run report is added to it, not instead of it.
    assert "NOT bound in the kernel" in outcome.text


def test_the_auto_run_report_is_off_when_the_knob_is(ws, monkeypatch, missing_name):
    _set_local("[ai]\nauto_run_report = false\n")
    ran: list = []
    monkeypatch.setattr(notebook_run, "_exec", lambda *a, **k: ran.append(a))

    _applier(ws)(_append(_CLEAN_CELL), "")

    assert ran == []


def test_a_policy_can_take_the_automatic_re_run_away(ws, monkeypatch, missing_name):
    _set_local("[ai]\nauto_run_report = true\n")
    _set_shared(ws, '[policy.settings]\n"ai.auto_run_report" = false\n')
    ran: list = []
    monkeypatch.setattr(notebook_run, "_exec", lambda *a, **k: ran.append(a))

    _applier(ws)(_append(_CLEAN_CELL), "")

    assert ran == []


def test_the_auto_run_report_does_not_fire_when_nothing_is_missing(ws, monkeypatch):
    """A change that worked costs no CPU: the run is a diagnosis, not a habit."""
    monkeypatch.setattr(
        introspect,
        "observe",
        lambda *a, **k: introspect.Observation(present=("total",), observed=True),
    )
    ran: list = []
    monkeypatch.setattr(notebook_run, "_exec", lambda *a, **k: ran.append(a))

    _applier(ws)(_append(_CLEAN_CELL), "")

    assert ran == []


def test_the_auto_run_report_does_not_fire_when_the_run_could_not_be_observed(ws, monkeypatch):
    """"Could not see" is not "it failed", so it is not grounds to re-execute the
    notebook either — the same rule, one step further down."""
    monkeypatch.setattr(
        introspect,
        "observe",
        lambda *a, **k: introspect.Observation(detail="the marimo editor is not running"),
    )
    ran: list = []
    monkeypatch.setattr(notebook_run, "_exec", lambda *a, **k: ran.append(a))

    _applier(ws)(_append(_CLEAN_CELL), "")

    assert ran == []


def test_a_cancelled_turn_does_not_start_the_re_run(ws, monkeypatch, missing_name):
    """The write already landed, but the analyst pressed stop while it was observing —
    a minutes-long re-run must not start on top of that."""
    ran: list = []
    monkeypatch.setattr(notebook_run, "_exec", lambda *a, **k: ran.append(a))

    class _StopsAfterWriting(_FakeSession):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def cancel_requested(self):
            self.calls += 1
            return self.calls > 1  # let the write through, stop before the run

    _applier(ws, session=_StopsAfterWriting())(_append(_CLEAN_CELL), "")

    assert ran == []


def test_a_broken_run_report_never_breaks_the_write(ws, monkeypatch, missing_name):
    def boom(*a, **k):
        raise RuntimeError("the workspace is busy")

    from mooring.app import run_report

    monkeypatch.setattr(run_report, "run_and_collect", boom)

    outcome = _applier(ws)(_append(_CLEAN_CELL), "")

    assert outcome.status == "applied"  # the bytes are on disk; the report was a bonus
    assert "busy" not in outcome.text
