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
    """Just enough session for the applier: the cancel flag, the report composer, the
    outbound PII valve the automatic report has to pass, and the event fan-out the
    analyst's transcript is drawn from."""

    def __init__(self, cancelled: bool = False, hold: bool = False) -> None:
        self._cancelled = cancelled
        self._hold = hold
        self.reported: list = []
        self.events: list = []

    def cancel_requested(self) -> bool:
        return self._cancelled

    def run_failure_report(self, failures):
        self.reported.append(list(failures))
        return ("the notebook did not run clean", [])

    def _scan_prompt(self, text: str):
        return (self._hold, [], "")

    def _broadcast(self, event) -> None:
        self.events.append((event.kind, event.data))


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


# -- the turn boundary (ChatService.begin_turn) --------------------------------


def _service(ws, applier, session=None):
    from mooring.app.chat_service import ChatService

    service = ChatService()
    service.register("sid-1", session or _FakeSession(), ws, "nb.py", applier=applier)
    return service


def test_begin_turn_rotates_the_id_for_a_new_turn(ws):
    applier = _applier(ws)
    service = _service(ws, applier)
    first = applier.turn_id

    rotated = service.begin_turn("sid-1")

    assert rotated and rotated != first and applier.turn_id == rotated


def test_begin_turn_does_not_split_a_turn_that_is_still_running(ws):
    """The analyst types a second message while the assistant is still working. That
    reaches begin_turn BEFORE the session refuses the concurrent send — and rotating
    there splits the running turn: its next write takes a second snapshot, so "undo
    what the assistant just did" undoes only the tail of it."""

    class _Busy(_FakeSession):
        def turn_in_flight(self) -> bool:
            return True

    applier = _applier(ws)
    service = _service(ws, applier, session=_Busy())
    live = applier.turn_id

    assert service.begin_turn("sid-1") == live
    assert applier.turn_id == live

    # ...and the running turn's second write still shares its ONE checkpoint.
    applier(_append(_CLEAN_CELL), "")
    applier(_append(_SECOND_CELL), "")
    assert notebook_undo.depth(ws, "nb.py") == 1


def test_the_in_flight_check_fails_open(ws):
    """A backend that does not track the concept — or one that cannot answer — must
    rotate exactly as it always did; a stuck "busy" would freeze the turn id for the
    life of the chat, which is the worse failure of the two."""

    class _Broken(_FakeSession):
        def turn_in_flight(self):
            raise RuntimeError("no idea")

    for session in (_FakeSession(), _Broken()):  # no method at all, then one that raises
        applier = _applier(ws)
        first = applier.turn_id
        assert _service(ws, applier, session=session).begin_turn("sid-1") != first


def test_a_failed_turn_boundary_is_loud_rather_than_silent(ws, monkeypatch):
    """Swallowing it left the applier on the PREVIOUS turn id, so the next write
    extended that turn's checkpoint and Revert quietly rolled back more than it said.
    Nothing has been written at this point, so raising costs a retry."""
    from mooring import telemetry

    errors: list = []
    monkeypatch.setattr(telemetry, "log_error", lambda **kw: errors.append(kw))
    applier = _applier(ws)
    monkeypatch.setattr(
        applier, "begin_turn", lambda: (_ for _ in ()).throw(RuntimeError("the lock is gone"))
    )
    service = _service(ws, applier)

    with pytest.raises(RuntimeError):
        service.begin_turn("sid-1")
    assert errors and errors[0]["op"] == "ai_chat_begin_turn"


def test_begin_turn_is_silent_when_there_is_no_applier(ws):
    from mooring.app.chat_service import ChatService

    service = ChatService()
    service.register("sid-1", _FakeSession(), ws, "nb.py")
    assert service.begin_turn("sid-1") == ""  # manual mode: nothing to tell, not an error


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


def test_a_staged_cell_is_never_reported_as_one_that_failed(ws, monkeypatch):
    """``apply_runs = false`` writes marimo's ``lazy`` mode: the applied cell arrives
    STALE and nothing executes. Probing then would report the names as missing and say
    "the code that defines them did not run to completion" — flatly false about a cell
    nobody has run. Nothing is probed, and nothing is claimed."""
    _set_local("[ai]\napply_runs = false\n")
    probed: list = []
    monkeypatch.setattr(introspect, "observe", lambda *a, **k: probed.append(a))

    outcome = _applier(ws)(_append(_CLEAN_CELL), "")

    assert outcome.status == "applied"  # the write itself still landed
    assert "total" in (ws / "nb.py").read_text("utf-8")
    assert probed == [], "the kernel was not probed at all"
    assert "could not observe" in outcome.text
    assert "apply_runs" in outcome.text and "waiting in the notebook" in outcome.text
    assert "NOT bound" not in outcome.text and "did not run to completion" not in outcome.text


def test_a_staged_cell_never_triggers_the_automatic_re_run(ws, monkeypatch):
    """The knob says a human runs the model's code. Re-running the whole notebook
    ourselves to find out why it "failed" would defeat exactly that."""
    _set_local("[ai]\napply_runs = false\nauto_run_report = true\n")
    ran: list = []
    monkeypatch.setattr(notebook_run, "_exec", lambda *a, **k: ran.append(a))

    _applier(ws)(_append(_CLEAN_CELL), "")

    assert ran == []


def test_live_schema_off_means_the_kernel_is_not_read_through_this_door_either(
    ws, monkeypatch
):
    """The observation IS a live-kernel read, over the same frozen probe — so
    ``[ai] live_schema = false`` (documented as "no live kernel schema reads", and
    policy-pinnable) has to bite here too, not just on the chat context."""
    _set_local("[ai]\nlive_schema = false\n")
    probed: list = []
    monkeypatch.setattr(introspect, "observe", lambda *a, **k: probed.append(a))

    outcome = _applier(ws)(_append(_CLEAN_CELL), "")

    assert probed == []
    assert outcome.status == "applied"
    assert "could not observe" in outcome.text and "live_schema" in outcome.text
    assert "NOT bound" not in outcome.text


def test_a_policy_can_take_the_kernel_read_away(ws, monkeypatch):
    _set_local("[ai]\nlive_schema = true\n")
    _set_shared(ws, '[policy.settings]\n"ai.live_schema" = false\n')
    probed: list = []
    monkeypatch.setattr(introspect, "observe", lambda *a, **k: probed.append(a))

    assert _applier(ws)(_append(_CLEAN_CELL), "").status == "applied"
    assert probed == []


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
        "id",
        "summary",
        "rationale",
        "undo_depth",
        "checkpoint_writes",
        "turn_id",
        "observation",
    }


def test_every_receipt_carries_its_own_id(ws):
    """The SSE layer replays receipts after a dropped stream and dedupes on this — and
    replays ONLY payloads that carry one, so an empty id is a receipt the analyst
    silently loses on a reconnect."""
    apply_edit = _applier(ws)
    ids = [
        apply_edit(_append(_CLEAN_CELL), "").payload["id"],
        apply_edit(_append(_SECOND_CELL), "").payload["id"],
    ]
    assert all(ids) and len(set(ids)) == 2


def test_the_receipt_says_how_many_writes_its_revert_would_take_back(ws):
    """Revert restores the whole TURN, so the fifth receipt's button takes back five
    changes. The UI cannot work that out for itself: the undo stack is capped, so "the
    depth did not move" is ambiguous between "extended" and "the oldest was pruned"."""
    guard = ApplyGuard()
    apply_edit = _applier(ws, guard=guard)

    first = apply_edit(_append(_CLEAN_CELL), "")
    second = apply_edit(_append(_SECOND_CELL), "")

    assert first.payload["checkpoint_writes"] == 1
    assert second.payload["checkpoint_writes"] == 2
    assert first.payload["undo_depth"] == second.payload["undo_depth"] == 1

    # A new turn opens a new checkpoint, so its first receipt is back to covering one.
    apply_edit.begin_turn()
    third = apply_edit(_append("third = 3\n"), "")
    assert third.payload["checkpoint_writes"] == 1


def test_a_manual_apply_between_two_writes_resets_the_count(ws):
    """It breaks the checkpoint chain (it takes its own snapshot), so the model write
    after it opens a fresh checkpoint — and the receipt must not inherit the old count."""
    guard = ApplyGuard()
    apply_edit = _applier(ws, guard=guard)
    apply_edit(_append(_CLEAN_CELL), "")
    assert apply_edit(_append(_SECOND_CELL), "").payload["checkpoint_writes"] == 2

    guard.apply_with_undo(ws / "nb.py", ws, "nb.py", _append("manual = 3\n"))

    assert apply_edit(_append("fourth = 4\n"), "").payload["checkpoint_writes"] == 1


def test_the_receipt_always_carries_a_rationale(ws):
    """It is the analyst's only account of what happened to their notebook, and nothing
    client-side can invent one — so a model that supplied none is SAID to have."""
    apply_edit = _applier(ws)
    assert apply_edit(_append(_CLEAN_CELL), "add a total").payload["rationale"] == "add a total"

    for empty in ("", "   ", None):
        outcome = apply_edit(_append(f"v{len(empty or '')} = 1\n"), empty)
        assert outcome.payload["rationale"].strip()
        assert "did not say why" in outcome.payload["rationale"]


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


def test_the_automatic_report_is_shown_to_the_analyst_too(ws, monkeypatch, missing_name):
    """The attended path appears in the transcript because it IS a turn; this one is a
    tool result the analyst never sees. "You are shown exactly what was sent" has to
    stay true of both, so the run announces itself and the exact text is broadcast.

    The event NAME is a contract with the chat page: EventSource has no wildcard
    listener, so anything sent under another name is dropped by the browser in silence.
    """
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, "MarimoSyntaxError: bad\n"))
    session = _FakeSession()

    _applier(ws, session=session)(_append(_CLEAN_CELL), "")

    events = [(kind, data) for kind, data in session.events if kind == "run_report"]
    assert [d.get("state") for _k, d in events][0] == "running", "said so before it began"
    sent = [d for _k, d in events if "sent" in d]
    assert len(sent) == 1
    assert sent[0]["sent"] == "the notebook did not run clean"  # verbatim, not a summary
    assert sent[0]["redactions"] == []


def test_a_clean_automatic_run_says_so_and_sends_the_model_nothing(ws, monkeypatch, missing_name):
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(0))
    session = _FakeSession()

    outcome = _applier(ws, session=session)(_append(_CLEAN_CELL), "")

    assert [d for k, d in session.events if k == "run_report"][-1] == {"ran_clean": True}
    assert session.reported == []  # nothing composed, so nothing to send
    assert "did not run clean" not in outcome.text


def test_an_announced_run_always_says_how_it_ended(ws, monkeypatch, missing_name):
    """The cue is drawn when the run starts; a run that then said nothing would leave
    "running your whole notebook…" on screen for ever."""
    from mooring.app import run_report

    def boom(*a, **k):
        raise run_report.ReportError("the workspace is busy")

    monkeypatch.setattr(run_report, "_run_and_compose", boom)
    session = _FakeSession()

    outcome = _applier(ws, session=session)(_append(_CLEAN_CELL), "")

    states = [d for k, d in session.events if k == "run_report"]
    assert states[0] == {"state": "running"} and len(states) == 2
    assert "could not run this notebook" in states[1]["text"]
    assert "busy" in states[1]["text"]
    assert outcome.status == "applied"  # a report that cannot run never breaks the write


def test_a_report_the_pii_guard_would_hold_is_not_sent_at_all(ws, monkeypatch, missing_name):
    """Block mode means a human decides — and there is no human at a tool result. So it
    fails closed: nothing reaches the model, the analyst is told, and the model is told
    to ask rather than being left thinking the run found nothing."""
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, "MarimoSyntaxError: bad\n"))
    session = _FakeSession(hold=True)

    outcome = _applier(ws, session=session)(_append(_CLEAN_CELL), "")

    assert "the notebook did not run clean" not in outcome.text
    assert "PII guard held" in outcome.text and "Ask the analyst" in outcome.text
    held = [d for kind, d in session.events if kind == "run_report" and d.get("held")]
    assert held and "nothing was sent to the assistant" in held[0]["text"]
    assert not any("sent" in d for _k, d in session.events), "the summary never left"


def test_a_session_with_no_valve_at_all_is_treated_as_a_hold(ws, monkeypatch, missing_name):
    """A guard that cannot run must not become a bypass on the unattended path."""
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, "MarimoSyntaxError: bad\n"))

    class _NoValve(_FakeSession):
        _scan_prompt = None

    outcome = _applier(ws, session=_NoValve())(_append(_CLEAN_CELL), "")

    assert "the notebook did not run clean" not in outcome.text
    assert "PII guard held" in outcome.text


# -- the ledgers ---------------------------------------------------------------


def test_a_model_write_fills_both_ledgers_like_a_human_apply(ws, monkeypatch):
    """``ai_chat_apply`` + the local activity entry used to come from the Apply ROUTE
    only, so the now-default path emitted neither — while the docs still promised "each
    Apply emits a telemetry line ... for held, confirmed and clean applies alike"."""
    from mooring import activity, telemetry

    events: list = []
    monkeypatch.setattr(telemetry, "log_event", lambda name, **f: events.append((name, f)))

    _applier(ws)(_append(_CLEAN_CELL), "")

    assert events == [("ai_chat_apply", {"band": "clean", "findings": 0})]
    entries = activity.read(ws)
    assert [e["op"] for e in entries] == ["ai_apply"]
    assert entries[0]["path"] == "nb.py"
    assert "kinds" not in entries[0]  # a clean apply records no kinds (activity drops [])


def test_a_held_write_reports_its_band_and_count_and_nothing_else(ws, monkeypatch):
    from mooring import telemetry

    events: list = []
    monkeypatch.setattr(telemetry, "log_event", lambda name, **f: events.append((name, f)))

    outcome = _applier(ws)(_append(_FLOOR_CELL), "")

    assert outcome.status == "held"
    assert events == [("ai_chat_apply_held", {"band": "floor", "findings": 1})]
    # Count + band only: the central sink never carries the kinds or anything else.
    assert "deletes_files" not in repr(events) and "os.remove" not in repr(events)


def test_the_local_ledger_gets_the_kinds_the_central_sink_must_not(ws, monkeypatch):
    from mooring import activity, telemetry

    events: list = []
    monkeypatch.setattr(telemetry, "log_event", lambda name, **f: events.append((name, f)))
    # A confirmed floor cell: the guard off, so the same ops APPLY rather than hold.
    _set_local("[ai]\napply_guard = false\n")

    assert _applier(ws)(_append(_FLOOR_CELL), "").status == "applied"

    assert events == [("ai_chat_apply", {"band": "floor", "findings": 1})]
    assert activity.read(ws)[0]["kinds"] == ["deletes_files"]


def test_a_write_that_never_landed_records_nothing(ws, monkeypatch):
    from mooring import activity, telemetry

    events: list = []
    monkeypatch.setattr(telemetry, "log_event", lambda name, **f: events.append((name, f)))
    _set_local("[ai]\nauto_apply = false\n")

    assert _applier(ws)(_append(_CLEAN_CELL), "").status == "held"

    assert events == [], "manual mode is not an Apply, held or otherwise"
    assert activity.read(ws) == []


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
