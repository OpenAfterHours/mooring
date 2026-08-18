"""The scheduled-refresh orchestrator: no write authority, honest degradation, live receipts.

The two objections that got scheduled refresh rejected the first time round are the two
things these pin:

* *"unattended runs are a support tarpit"* → a refresh has NO write authority. It cannot
  push or propose (pinned by source scan), and its worst failure is a local file that did
  not get written.
* *"a silently stale board report is worse than no feature"* → a run that could not pull is
  DEGRADED with a curated reason, never silently clean; a failed run never overwrites the
  last good artifact; and a delivered artifact carries its own next-due date.

The marimo export subprocess is faked at the shared runner's ``_exec`` seam.
"""

from __future__ import annotations

import ast
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mooring import activity, checks, gitsha, inputs, schedule, verify
from mooring.app import deliver, notebook_run, refresh
from mooring.config import Config
from mooring.github import AuthFailed, Unreachable

NOTEBOOK = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"
REL = "notebooks/board.py"


def _cfg(tmp_path) -> Config:
    return Config(client_id="cid", owner="acme", repo="nbs", workspace_path=str(tmp_path / "ws"))


def _mk(tmp_path, rel=REL, *, verified=True, sched=True, **kw):
    """A workspace with the notebook, its passing verify receipt, and a schedule."""
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    (ws / rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / rel).write_text(NOTEBOOK, encoding="utf-8")
    if verified:
        verify.record(
            ws,
            rel,
            passed=True,
            sha=gitsha.local_blob_sha(ws / rel, rel),
            cells_failed=None,
            ran_at="2026-07-30T06:00:00+00:00",
        )
    entry = None
    if sched:
        entry = schedule.put(ws, schedule.Schedule(notebook=rel, **kw))
    return cfg, ws, entry


def _out_of(cmd):
    for i, tok in enumerate(cmd):
        if tok == "-o":
            return Path(cmd[i + 1])
    return None


def _fake_exec(returncode=0, stderr="", *, produce=True, during=None):
    """Stand in for `notebook_run._exec`. ``during`` runs while the notebook is "executing",
    which is how a test plants the receipts a real run's mooring_checks cell would write."""

    def _run(cmd, cwd, env, timeout, cancel=None):
        if during is not None:
            during()
        if produce:
            out = _out_of(cmd)
            out.parent.mkdir(parents=True, exist_ok=True)
            # A real render embeds data values; plant one to prove it never survives.
            out.write_text("<html><body>SECRET_VALUE_DO_NOT_LEAK</body></html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return _run


def _write_receipt(directory: Path, rel: str, key: str, payload: dict, *, age_s: int = 0) -> None:
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{rel.replace('/', '__')}.json").write_text(
        json.dumps(
            {"notebook": rel, "updated": stamp.isoformat(timespec="seconds"), key: payload}
        ),
        encoding="utf-8",
    )


def _checks(ws, rel=REL, *, failed=0, total=1, age_s=0):
    entries = {f"c{i}": {"passed": i >= failed} for i in range(total)}
    _write_receipt(checks.checks_dir(ws), rel, "checks", entries, age_s=age_s)


def _no_pull(monkeypatch):
    """Make client_for fail the way a signed-out machine does."""
    monkeypatch.setattr(
        refresh.notebooks, "client_for", lambda cfg: (_ for _ in ()).throw(AuthFailed("no token"))
    )


def _good_pull(monkeypatch, *, pulled=2, conflicts=()):
    monkeypatch.setattr(refresh.notebooks, "client_for", lambda cfg: object())
    result = type("R", (), {"pulled": pulled, "skipped_conflicts": list(conflicts)})()
    monkeypatch.setattr(refresh.sync, "pull", lambda client, cfg, strategy: result)


# -- no write authority ------------------------------------------------------


def test_refresh_has_no_path_to_push_or_propose():
    # THE blast-radius guarantee: a scheduled run may READ from the team repo and may never
    # write to it. import-linter works at module granularity and cannot express "not
    # sync.push", so this pins it by parsing the module and allowlisting exactly which parts
    # of the sync domain it is permitted to touch. Adding sync.push here would be a
    # deliberate act with a failing test attached, not an accident.
    tree = ast.parse(Path(refresh.__file__).read_text("utf-8"))
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sync"
    }
    assert used <= {"pull", "ConflictStrategy"}, f"a scheduled run must not reach sync.{used}"


def test_the_pull_is_always_conflict_skipping(monkeypatch, tmp_path):
    # Resolving a conflict unattended would be exactly the silent decision mooring exists to
    # prevent, so the strategy is pinned rather than merely conventional.
    cfg, ws, sched = _mk(tmp_path)
    seen = {}
    monkeypatch.setattr(refresh.notebooks, "client_for", lambda cfg: object())

    def _pull(client, cfg, strategy):
        seen["strategy"] = strategy
        return type("R", (), {"pulled": 0, "skipped_conflicts": []})()

    monkeypatch.setattr(refresh.sync, "pull", _pull)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))
    refresh.refresh_notebook(cfg, REL, sched=sched)
    assert seen["strategy"] is refresh.sync.ConflictStrategy.SKIP


# -- the pull, and honest degradation ----------------------------------------


def test_a_clean_run_with_a_good_pull_is_ok(monkeypatch, tmp_path):
    cfg, ws, sched = _mk(tmp_path, deliver=False)
    _good_pull(monkeypatch, pulled=3)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    result = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert result.outcome == schedule.OK
    assert result.pulled == 3 and result.reason == ""


def test_being_signed_out_degrades_it_never_fails_it(monkeypatch, tmp_path):
    # "Pull if possible": not being able to pull is a DEGRADED run against the local copy,
    # never a failed one — but it must never pass as clean either.
    cfg, ws, sched = _mk(tmp_path, deliver=False)
    _no_pull(monkeypatch)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    result = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert result.outcome == schedule.DEGRADED
    assert "not signed in" in result.reason and "local copy" in result.reason


def test_offline_degrades_with_a_curated_reason(monkeypatch, tmp_path):
    cfg, ws, sched = _mk(tmp_path, deliver=False)
    monkeypatch.setattr(refresh.notebooks, "client_for", lambda cfg: object())
    monkeypatch.setattr(
        refresh.sync,
        "pull",
        lambda *a, **k: (_ for _ in ()).throw(Unreachable("https://api.github.com/repos/x/y")),
    )
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    result = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert result.outcome == schedule.DEGRADED
    assert "unreachable" in result.reason
    # The URL inside the exception must not ride the receipt.
    assert "api.github.com" not in result.reason


def test_a_skipped_conflict_degrades_the_run(monkeypatch, tmp_path):
    # A skipped conflict means the run executed against something that is NOT the team's
    # latest. Passing that off as clean is the failure mode this feature is accused of.
    cfg, ws, sched = _mk(tmp_path, deliver=False)
    _good_pull(monkeypatch, pulled=1, conflicts=["notebooks/other.py"])
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    result = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert result.outcome == schedule.DEGRADED and result.conflicts == 1
    assert "conflict" in result.reason


def test_pull_can_be_turned_off(monkeypatch, tmp_path):
    cfg, ws, sched = _mk(tmp_path, deliver=False, pull=False)
    monkeypatch.setattr(
        refresh.notebooks, "client_for", lambda cfg: pytest.fail("must not pull")
    )
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    assert refresh.refresh_notebook(cfg, REL, sched=sched).outcome == schedule.OK


# -- outcomes ----------------------------------------------------------------


def test_a_failing_cell_is_a_failed_run(monkeypatch, tmp_path):
    cfg, ws, sched = _mk(tmp_path, deliver=False)
    _no_pull(monkeypatch)
    stderr = "MarimoExceptionRaisedError: 'SECRET_VALUE_DO_NOT_LEAK'\n"
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(1, stderr))

    result = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert result.outcome == schedule.FAILED
    assert result.reason == "1 cell failed to run"
    # Value-free: the count survives, the stderr text never does.
    assert "SECRET" not in json.dumps(schedule.get(ws, REL).to_dict())


def test_failing_tie_outs_are_their_own_red_outcome(monkeypatch, tmp_path):
    # The headline of the whole feature: "it ran, but the numbers no longer reconcile".
    cfg, ws, sched = _mk(tmp_path, deliver=False)
    _no_pull(monkeypatch)
    monkeypatch.setattr(
        notebook_run, "_exec", _fake_exec(0, during=lambda: _checks(ws, failed=2, total=5))
    )

    result = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert result.outcome == schedule.CHECKS_FAILED
    assert result.checks_failed == 2 and result.checks_total == 5
    # ...and it does NOT spend the failure budget (see test_schedule).
    assert schedule.get(ws, REL).consecutive_failures == 0


def test_a_stale_checks_receipt_is_not_counted(monkeypatch, tmp_path):
    # Receipts persist between runs. A notebook that no longer calls mooring_checks would
    # otherwise stay pinned red on a receipt no run wrote.
    cfg, ws, sched = _mk(tmp_path, deliver=False)
    _checks(ws, failed=3, total=3, age_s=86400)  # yesterday's failures
    _no_pull(monkeypatch)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    result = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert result.checks_failed == 0
    assert result.outcome == schedule.DEGRADED  # degraded by the pull only, not by checks


def test_changed_inputs_are_reported_but_do_not_degrade(monkeypatch, tmp_path):
    # New data is the POINT of a refresh — flagging it as a problem would cry wolf daily.
    cfg, ws, sched = _mk(tmp_path, deliver=False, pull=False)

    def _plant():
        _write_receipt(
            inputs.inputs_dir(ws), REL, "inputs", {"sales": {"changed": True}}
        )

    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0, during=_plant))

    result = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert result.inputs_changed == 1
    assert result.outcome == schedule.OK


def test_an_environment_failure_is_recorded_not_raised(monkeypatch, tmp_path):
    cfg, ws, sched = _mk(tmp_path, deliver=False, pull=False)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(1, "no such dependency", produce=False))

    result = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert result.outcome == schedule.FAILED and result.ran is False
    assert schedule.get(ws, REL).consecutive_failures == 1


# -- the artifact ------------------------------------------------------------


def test_a_delivered_artifact_carries_its_own_next_due_date(monkeypatch, tmp_path):
    # THE mechanism that makes staleness travel with the output: a stakeholder holding the
    # emailed HTML weeks later can see it is overdue with no access to mooring.
    cfg, ws, sched = _mk(tmp_path, deliver=True, pull=False, cadence="weekdays", at="07:30")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    result = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert result.artifact
    html = (ws / result.artifact).read_text("utf-8")
    assert "scheduled every weekday at 07:30" in html
    assert "next refresh due" in html


def test_a_failed_run_never_overwrites_the_last_good_artifact(monkeypatch, tmp_path):
    cfg, ws, sched = _mk(tmp_path, deliver=True, pull=False)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))
    good = refresh.refresh_notebook(cfg, REL, sched=sched)
    kept = (ws / good.artifact).read_text("utf-8")

    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(1, "MarimoExceptionRaisedError: x\n"))
    failed = refresh.refresh_notebook(cfg, REL, sched=sched)

    assert failed.outcome == schedule.FAILED and failed.artifact == ""
    # The artifact on disk is still the last COMPLETE run — never half-rendered, never a
    # stale-values-under-a-new-date lie.
    assert (ws / good.artifact).read_text("utf-8") == kept


def test_the_value_bearing_render_never_survives_a_receipt_only_run(monkeypatch, tmp_path):
    cfg, ws, sched = _mk(tmp_path, deliver=False, pull=False)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    refresh.refresh_notebook(cfg, REL, sched=sched)

    assert not verify.render_target(ws, REL).is_file()
    assert list(verify.verify_dir(ws).glob("*.html")) == []
    assert not deliver.outbox_dir(ws).exists()


def test_the_render_is_cleaned_up_when_the_run_fails(monkeypatch, tmp_path):
    cfg, ws, sched = _mk(tmp_path, deliver=True, pull=False)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(1, "boom"))

    refresh.refresh_notebook(cfg, REL, sched=sched)

    assert list(verify.verify_dir(ws).glob("*.html")) == []


# -- receipts and preflight --------------------------------------------------


def test_a_refresh_keeps_the_trust_badge_current(monkeypatch, tmp_path):
    # A scheduled run re-verifies for free, which matters because a LAPSED verification is
    # what drops a schedule to a one-strike budget.
    cfg, ws, sched = _mk(tmp_path, deliver=False, pull=False, verified=False)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    refresh.refresh_notebook(cfg, REL, sched=sched)

    assert verify.read_results(ws)[REL]["passed"] is True


def test_a_mid_run_edit_clears_the_badge_rather_than_vouching(monkeypatch, tmp_path):
    cfg, ws, sched = _mk(tmp_path, deliver=False, pull=False, verified=False)

    def _edit():
        (ws / REL).write_text(NOTEBOOK + "\n# edited mid-run\n", encoding="utf-8")

    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0, during=_edit))

    refresh.refresh_notebook(cfg, REL, sched=sched)

    assert verify.read_results(ws) == {}


def test_preflight_gives_an_unverified_notebook_one_strike(tmp_path):
    cfg, ws, sched = _mk(tmp_path, verified=False)
    check = refresh.preflight(cfg, sched)
    assert check.may_run is True and check.verified is False and check.budget == 1
    assert "re-verify" in check.reason


def test_preflight_refuses_a_paused_or_missing_schedule(tmp_path):
    cfg, ws, sched = _mk(tmp_path, paused=True)
    assert refresh.preflight(cfg, sched).may_run is False
    (ws / REL).unlink()
    gone = schedule.Schedule(notebook=REL)
    assert refresh.preflight(cfg, gone).may_run is False


def test_auto_run_is_only_for_the_boring_case(tmp_path):
    cfg, ws, sched = _mk(tmp_path)
    assert refresh.may_auto_run(cfg, sched) is True
    # Unverified -> a human decides.
    cfg2, ws2, unverified = _mk(tmp_path / "b", verified=False)
    assert refresh.may_auto_run(cfg2, unverified) is False
    # Failed last time -> a human decides.
    failed = schedule.put(
        ws, schedule.Schedule(notebook=REL, last_run=schedule.LastRun(outcome=schedule.FAILED))
    )
    assert refresh.may_auto_run(cfg, failed) is False


def test_records_a_value_free_activity_entry(monkeypatch, tmp_path):
    cfg, ws, sched = _mk(tmp_path, deliver=False, pull=False)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    refresh.refresh_notebook(cfg, REL, sched=sched)

    entries = activity.read(ws)
    assert entries and entries[0]["op"] == "refresh" and entries[0]["path"] == REL


# -- the cross-process lock --------------------------------------------------


def test_a_second_process_cannot_refresh_the_same_workspace(tmp_path):
    # Once background refresh is registered (an OS task or the sign-in agent) the hub's sweep
    # and that background process are genuinely separate processes on one workspace. A thread
    # lock says nothing about them; the lockfile does.
    cfg, ws, sched = _mk(tmp_path)
    with refresh.workspace_guard(ws):
        with pytest.raises(refresh.RefreshBusy):
            with refresh.workspace_guard(ws):
                pass


def test_the_lock_is_released_even_when_the_run_explodes(tmp_path):
    cfg, ws, sched = _mk(tmp_path)
    with pytest.raises(ZeroDivisionError):
        with refresh.workspace_guard(ws):
            raise ZeroDivisionError
    with refresh.workspace_guard(ws):  # must not still be held
        pass


def test_a_stale_lock_is_stolen_rather_than_wedging_forever(tmp_path):
    # A background agent killed mid-run must not stop every future refresh — that would be a
    # silent stop of exactly the kind this feature exists to prevent.
    cfg, ws, sched = _mk(tmp_path)
    lock = ws / ".mooring" / "refresh.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999", encoding="utf-8")
    import os as _os

    ancient = datetime.now(timezone.utc).timestamp() - (refresh._LOCK_STALE_S + 60)
    _os.utime(lock, (ancient, ancient))
    with refresh.workspace_guard(ws):
        pass


def test_the_lock_is_structurally_unsyncable():
    from mooring import sync

    assert sync.is_synced_path(".mooring/refresh.lock") is False


def test_a_busy_workspace_ends_the_sweep_without_recording_anything(monkeypatch, tmp_path):
    # Another process already holds the workspace and is doing this work. Recording a failure
    # would invent a problem AND spend the failure budget on it.
    cfg, ws, _ = _mk(tmp_path, deliver=False, pull=False, cadence="daily", at="00:01")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))
    with refresh.workspace_guard(ws):
        assert refresh.run_due(cfg) == []
    assert schedule.get(ws, REL).last_run.outcome == ""
    assert schedule.get(ws, REL).consecutive_failures == 0


# -- the sweep ---------------------------------------------------------------


def test_run_due_runs_only_what_is_due(monkeypatch, tmp_path):
    cfg, ws, _ = _mk(tmp_path, deliver=False, pull=False, cadence="daily", at="00:01")
    schedule.put(
        ws,
        schedule.Schedule(
            notebook=REL,
            cadence="daily",
            at="00:01",
            deliver=False,
            pull=False,
            last_run=schedule.LastRun(
                at=datetime.now(timezone.utc).isoformat(timespec="seconds"), outcome=schedule.OK
            ),
        ),
    )
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))
    assert refresh.run_due(cfg) == []  # already ran inside this window


def test_run_due_sweeps_and_records(monkeypatch, tmp_path):
    cfg, ws, _ = _mk(tmp_path, deliver=False, pull=False, cadence="daily", at="00:01")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    results = refresh.run_due(cfg)

    assert [r.notebook for r in results] == [REL]
    assert schedule.get(ws, REL).last_run.outcome == schedule.OK


def test_the_sweep_does_not_fire_a_one_shot_before_its_date(monkeypatch, tmp_path):
    # A one-shot booked for next month has no run preceding its window; the sweep must still
    # leave it alone rather than treating "never run" as "owed a run".
    #
    # Dated RELATIVE to now, never to a fixed calendar date: the sweep reads the real clock
    # (it is the orchestrator, not a predicate taking an injectable `now`), so a literal date
    # here would pass until it arrived and then fail for good. A month of slack also puts the
    # assertion out of reach of a run that straddles midnight.
    ahead = (datetime.now().astimezone() + timedelta(days=30)).strftime("%Y-%m-%d")
    cfg, ws, _ = _mk(
        tmp_path, deliver=False, pull=False, cadence="once", date=ahead, at="00:01"
    )
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    assert refresh.run_due(cfg) == []
    assert schedule.get(ws, REL).last_run.outcome == ""


def test_the_sweep_runs_a_one_shot_once_and_then_leaves_it_alone(monkeypatch, tmp_path):
    # ...and once its instant has passed it runs EXACTLY once: no next window means no second
    # run, and no "overdue" nag afterwards either. Relative to now for the same reason as
    # above — and a month back, so the receipt this run writes lands unambiguously inside the
    # one-shot's window whatever time of day (or timezone) the suite runs in.
    behind = (datetime.now().astimezone() - timedelta(days=30)).strftime("%Y-%m-%d")
    cfg, ws, _ = _mk(tmp_path, deliver=False, pull=False, cadence="once", date=behind)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    assert [r.notebook for r in refresh.run_due(cfg)] == [REL]
    done = schedule.get(ws, REL)
    assert done.last_run.outcome == schedule.OK
    assert schedule.is_complete(done) is True
    assert refresh.run_due(cfg) == []
    assert schedule.is_overdue(done) is False


def test_auto_only_skips_the_doubtful(monkeypatch, tmp_path):
    cfg, ws, _ = _mk(
        tmp_path, deliver=False, pull=False, cadence="daily", at="00:01", verified=False
    )
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    assert refresh.run_due(cfg, auto_only=True) == []
    assert [r.notebook for r in refresh.run_due(cfg)] == [REL]


def test_one_broken_notebook_does_not_stop_the_sweep(monkeypatch, tmp_path):
    cfg, ws, _ = _mk(tmp_path, deliver=False, pull=False, cadence="daily", at="00:01")
    # A second schedule pointing at a notebook that has been deleted.
    schedule.put(ws, schedule.Schedule(notebook="notebooks/gone.py", deliver=False, pull=False))
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(0))

    results = refresh.run_due(cfg)

    assert {r.notebook for r in results} == {REL, "notebooks/gone.py"}
    assert {r.outcome for r in results} == {schedule.OK, schedule.FAILED}


# -- refusals ----------------------------------------------------------------


def test_refuses_a_plain_module(tmp_path):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "helpers.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    with pytest.raises(refresh.RefreshRefused):
        refresh.refresh_notebook(cfg, "helpers.py")


def test_rejects_a_path_escaping_the_workspace(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.workspace().mkdir(parents=True)
    with pytest.raises(ValueError):
        refresh.refresh_notebook(cfg, "../secret.py")


def test_describe_result_wording():
    R = refresh.RefreshResult
    assert "refreshed clean" in refresh.describe_result(R(REL, schedule.OK, True))
    assert "did not refresh" in refresh.describe_result(
        R(REL, schedule.FAILED, False, reason="the notebook failed to run")
    )
    assert "ran, but" in refresh.describe_result(
        R(REL, schedule.CHECKS_FAILED, True, reason="2 of 5 tie-out check(s) failing")
    )
