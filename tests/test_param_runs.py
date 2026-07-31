"""The attended parameterised run: one notebook, once per value, one artifact per value.

The failures this feature could plausibly introduce are all worse than "it didn't run", so
they get the tests:

* a MISLABELLED artifact (a file named APAC holding EMEA's numbers),
* a PARTIAL pack that reads as complete,
* one bad value silently stopping the rest,
* a fan-out and a scheduled refresh running over each other,
* a parameter value or marimo's stderr leaking into telemetry or a receipt,
* a "cancel" that leaves a marimo kernel alive to finish behind everyone's back.

The marimo export subprocess is faked at the shared runner's ``_exec`` seam, exactly as
``test_refresh.py`` does — so this exercises the real runner, the real deliver promotion,
and the real workspace lock.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from mooring import activity, params, telemetry
from mooring.app import deliver, notebook_run, param_runs, refresh
from mooring.config import Config

REL = "notebooks/board.py"
# A notebook that READS the parameter — the fan-out refuses anything else (see the guard
# tests at the bottom), so every happy-path fixture must look like this.
NOTEBOOK = (
    "import marimo\n\n"
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    import mooring_params\n"
    '    region = mooring_params.get("region", "EMEA")\n'
    "    return\n"
)


def _cfg(tmp_path) -> Config:
    return Config(client_id="cid", owner="acme", repo="nbs", workspace_path=str(tmp_path / "ws"))


def _mk(tmp_path, rel=REL, source=NOTEBOOK):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    (ws / rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / rel).write_text(source, encoding="utf-8")
    return cfg, ws


def _out_of(cmd):
    for i, tok in enumerate(cmd):
        if tok == "-o":
            return Path(cmd[i + 1])
    return None


def _value_of(env):
    """The parameter value THIS invocation was handed, read back off the run's environment
    — which is the channel under test."""
    raw = (env or {}).get(params.ENV_VAR)
    return next(iter(json.loads(raw).values())) if raw else None


def _fake_exec(*, fails=(), stderr="", produce=True, during=None, seen=None):
    """Stand in for ``notebook_run._exec``. ``fails`` names the parameter values whose run
    should exit non-zero; every other value succeeds, which is how "one failing value does
    not stop the others" is exercised."""

    def _run(cmd, cwd, env, timeout, cancel=None):
        value = _value_of(env)
        if seen is not None:
            seen.append(value)
        if during is not None:
            during(value)
        failing = value in fails
        if produce:
            out = _out_of(cmd)
            out.parent.mkdir(parents=True, exist_ok=True)
            # A real render embeds data values AND is per-value; both are asserted on.
            out.write_text(
                f"<html><body>SECRET_VALUE_DO_NOT_LEAK numbers for {value}</body></html>",
                encoding="utf-8",
            )
        code = 1 if failing else 0
        text = stderr if failing else ""
        return subprocess.CompletedProcess(cmd, code, "", text)

    return _run


def _spec(text="region=EMEA,APAC,AMER"):
    return params.parse_spec(text)


# -- a notebook with no parameter is untouched -------------------------------


def test_a_notebook_with_no_parameter_runs_byte_identically_to_today(monkeypatch, tmp_path):
    # THE hard requirement. A plain run (verify) must issue exactly the command and the
    # environment it issued before parameterised runs existed — no MOORING_PARAMS, and no
    # change to argv, so the notebook cannot behave differently.
    from mooring.app import verify_run

    cfg, ws = _mk(tmp_path)
    calls = []

    def _run(cmd, cwd, env, timeout, cancel=None):
        calls.append((list(cmd), dict(env) if env else None, cancel))
        out = _out_of(cmd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html></html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notebook_run, "_exec", _run)
    verify_run.verify_notebook(cfg, REL)

    (cmd, env, cancel) = calls[0]
    assert params.ENV_VAR not in (env or {})
    assert not any(params.ENV_VAR in tok for tok in cmd)
    assert cancel is None  # no watchdog thread is even started for an unparameterised run


def test_the_parameter_never_reaches_the_command_line(monkeypatch, tmp_path):
    # The value rides the ENVIRONMENT, never argv: argv would have to survive `uv run`'s
    # own parser on one of the two launch backends, and a value that silently failed to
    # arrive would produce a MISLABELLED artifact.
    cfg, ws = _mk(tmp_path)
    calls = []

    def _run(cmd, cwd, env, timeout, cancel=None):
        calls.append((list(cmd), dict(env)))
        out = _out_of(cmd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html></html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notebook_run, "_exec", _run)
    param_runs.fan_out(cfg, REL, _spec("region=EMEA"), do_deliver=False)

    (cmd, env) = calls[0]
    assert "EMEA" not in " ".join(cmd)
    assert json.loads(env[params.ENV_VAR]) == {"region": "EMEA"}
    # The rest of the parent environment survives — marimo still needs PATH etc.
    assert len(env) > 1


# -- one value per run, in order, sequentially -------------------------------


def test_each_value_gets_its_own_run_in_order(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    seen = []
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(seen=seen))

    result = param_runs.fan_out(cfg, REL, _spec(), do_deliver=False)

    assert seen == ["EMEA", "APAC", "AMER"]
    assert [run.value for run in result.runs] == ["EMEA", "APAC", "AMER"]
    assert result.complete is True and result.clean == 3


def test_only_one_notebook_runs_at_a_time(monkeypatch, tmp_path):
    # Sequential is a promise, not an accident: N marimo kernels on a laptop fight over CPU,
    # the workspace files, and the single throwaway render path.
    cfg, ws = _mk(tmp_path)
    live = []
    peak = []

    def _during(value):
        live.append(value)
        peak.append(len(live))
        live.pop()

    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(during=_during))
    param_runs.fan_out(cfg, REL, _spec(), do_deliver=False)
    assert max(peak) == 1


# -- one failing value does not stop the others ------------------------------


def test_one_failing_value_does_not_stop_the_others_and_is_reported(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    stderr = "MarimoExceptionRaisedError: 'SECRET_VALUE_DO_NOT_LEAK'\n"
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(fails={"APAC"}, stderr=stderr))

    result = param_runs.fan_out(cfg, REL, _spec())

    by_value = {run.value: run for run in result.runs}
    assert by_value["EMEA"].outcome == param_runs.OK
    assert by_value["AMER"].outcome == param_runs.OK  # ran AFTER the failure
    assert by_value["APAC"].outcome == param_runs.FAILED
    assert by_value["APAC"].reason == "1 cell failed to run"
    assert result.failed == 1 and result.clean == 2


def test_an_environment_failure_for_one_value_is_reported_not_raised(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)

    def _run(cmd, cwd, env, timeout, cancel=None):
        if _value_of(env) == "APAC":  # marimo never wrote its render: the env broke
            return subprocess.CompletedProcess(cmd, 1, "", "no such dependency")
        out = _out_of(cmd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html></html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notebook_run, "_exec", _run)
    result = param_runs.fan_out(cfg, REL, _spec(), do_deliver=False)

    by_value = {run.value: run for run in result.runs}
    assert by_value["APAC"].outcome == param_runs.FAILED and by_value["APAC"].ran is False
    assert "dependencies" in by_value["APAC"].reason  # the runner's curated sentence
    assert [r.outcome for r in result.runs].count(param_runs.OK) == 2


def test_the_curated_reason_never_carries_marimos_stderr(monkeypatch, tmp_path):
    # marimo's stderr can quote a data value, so it is counted and never stored.
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(
        notebook_run,
        "_exec",
        _fake_exec(fails={"EMEA"}, stderr="MarimoExceptionRaisedError: 'SECRET_VALUE_DO_NOT_LEAK'\n"),
    )
    result = param_runs.fan_out(cfg, REL, _spec("region=EMEA"), do_deliver=False)
    assert "SECRET" not in json.dumps([r.__dict__ for r in result.runs])
    assert "SECRET" not in param_runs.describe_run(result.runs[0])


# -- artifacts: one per value, never colliding -------------------------------


def test_every_value_gets_its_own_uniquely_named_artifact(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())

    result = param_runs.fan_out(cfg, REL, _spec())

    artifacts = result.artifacts
    assert len(artifacts) == 3 and len(set(artifacts)) == 3
    for value in ("EMEA", "APAC", "AMER"):
        # A stakeholder tells them apart from the FILENAME.
        match = [a for a in artifacts if f"region-{value}" in a]
        assert len(match) == 1, artifacts
        assert (ws / match[0]).read_text("utf-8").count(f"numbers for {value}") == 1
    # ...and they live in the sync-excluded outbox, never in the repo.
    assert all(a.startswith(".mooring/outbox/") for a in artifacts)


def test_a_later_value_never_overwrites_an_earlier_ones_artifact(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    result = param_runs.fan_out(cfg, REL, _spec())
    first = ws / result.artifacts[0]
    assert "numbers for EMEA" in first.read_text("utf-8")
    assert "APAC" not in first.read_text("utf-8").replace("region-APAC", "")


def test_a_failed_value_never_overwrites_its_previous_good_artifact(monkeypatch, tmp_path):
    # Same discipline as the scheduled refresh: anything sitting in the outbox is always a
    # COMPLETE run, never a half-render or stale numbers under a fresh date.
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    good = param_runs.fan_out(cfg, REL, _spec("region=EMEA"))
    kept = (ws / good.artifacts[0]).read_text("utf-8")

    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(fails={"EMEA"}, stderr="boom\n"))
    failed = param_runs.fan_out(cfg, REL, _spec("region=EMEA"))

    assert failed.artifacts == []
    assert (ws / good.artifacts[0]).read_text("utf-8") == kept


def test_a_partial_fan_out_is_obvious_from_a_single_artifact(monkeypatch, tmp_path):
    # The mechanism that makes incompleteness travel WITH the output: the footer names the
    # value AND its place, so a stakeholder holding only EMEA can see two more were due.
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(fails={"APAC", "AMER"}))

    result = param_runs.fan_out(cfg, REL, _spec())

    html = (ws / result.artifacts[0]).read_text("utf-8")
    assert "region = EMEA" in html and "value 1 of 3" in html
    assert result.complete is False


def test_the_summary_never_reads_as_complete_when_it_is_not(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(fails={"APAC"}))
    partial = param_runs.fan_out(cfg, REL, _spec())
    text = param_runs.describe_result(partial)
    assert "INCOMPLETE" in text and "2 of 3" in text and "1 failed" in text
    assert param_runs.exit_code(partial) == 1

    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    whole = param_runs.fan_out(cfg, REL, _spec())
    assert "INCOMPLETE" not in param_runs.describe_result(whole)
    assert param_runs.exit_code(whole) == 0


def test_no_artifacts_are_written_when_deliver_is_off(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    result = param_runs.fan_out(cfg, REL, _spec(), do_deliver=False)
    assert result.artifacts == []
    assert not deliver.outbox_dir(ws).exists()


def test_the_value_bearing_render_never_survives_a_run(monkeypatch, tmp_path):
    from mooring import verify

    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(fails={"APAC"}))
    param_runs.fan_out(cfg, REL, _spec())
    assert list(verify.verify_dir(ws).glob("*.html")) == []


# -- cancel ------------------------------------------------------------------


def test_cancel_kills_the_running_kernel_and_stops_the_fan_out(monkeypatch, tmp_path):
    # A cancel must actually STOP the notebook, not just stop reporting on it: an orphaned
    # marimo kernel would go on to re-write the value-bearing render after cleanup.
    cfg, ws = _mk(tmp_path)
    cancel = threading.Event()
    killed = []

    def _run(cmd, cwd, env, timeout, cancel_event=None):
        # Fire the cancel while THIS value is "executing", exactly as the hub's button
        # would, and let the real runner notice it.
        cancel.set()
        killed.append(_value_of(env))
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(notebook_run, "_exec", _run)
    result = param_runs.fan_out(cfg, REL, _spec(), cancel=cancel)

    assert killed == ["EMEA"]  # nothing after the cancelled value was ever launched
    outcomes = {run.value: run.outcome for run in result.runs}
    assert outcomes["EMEA"] == param_runs.CANCELLED
    assert outcomes["APAC"] == outcomes["AMER"] == param_runs.SKIPPED
    assert result.cancelled is True and result.complete is False
    assert result.artifacts == []  # a cancelled render is never promoted
    assert param_runs.exit_code(result) == 4


def test_cancel_fires_the_process_tree_kill(monkeypatch):
    # The cancel path reuses the SAME taskkill /T the timeout uses, so a cancelled run has
    # exactly the safety properties a timed-out one has.
    killed = []
    monkeypatch.setattr(notebook_run, "_kill_tree", lambda proc: killed.append(proc))
    cancel = threading.Event()

    class _Proc:
        pid = 4242
        returncode = 1

        def communicate(self, timeout=None):
            cancel.set()
            # Give the watchdog a moment to observe the event, as a real (slow) run would.
            for _ in range(200):
                if killed:
                    break
                threading.Event().wait(0.01)
            return "", ""

    monkeypatch.setattr(notebook_run.subprocess, "Popen", lambda *a, **k: _Proc())
    notebook_run._exec(["marimo"], ".", None, 5, cancel)
    assert killed and killed[0].pid == 4242


def test_a_cancelled_run_deletes_its_half_written_render(monkeypatch, tmp_path):
    from mooring import verify

    cfg, ws = _mk(tmp_path)
    cancel = threading.Event()

    def _run(cmd, cwd, env, timeout, cancel_event=None):
        out = _out_of(cmd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html>half a render with SECRET_VALUE_DO_NOT_LEAK</html>", encoding="utf-8")
        cancel.set()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notebook_run, "_exec", _run)
    result = param_runs.fan_out(cfg, REL, _spec("region=EMEA"), cancel=cancel)

    assert result.runs[0].outcome == param_runs.CANCELLED
    assert list(verify.verify_dir(ws).glob("*.html")) == []
    assert not deliver.outbox_dir(ws).exists()


def test_the_handle_reports_progress_and_cancels(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    gate = threading.Event()

    def _run(cmd, cwd, env, timeout, cancel_event=None):
        gate.wait(5)  # hold the first value open so the snapshot is observably mid-run
        out = _out_of(cmd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html></html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notebook_run, "_exec", _run)
    handle, thread = param_runs.start(cfg, REL, _spec(), do_deliver=False)
    try:
        for _ in range(500):
            if handle.snapshot()["running"]:
                break
            threading.Event().wait(0.01)
        snap = handle.snapshot()
        assert snap["running"] == "EMEA" and snap["done"] is False
        assert snap["total"] == 3 and snap["runs"] == []
        handle.cancel.set()
    finally:
        gate.set()
        thread.join(timeout=20)
    final = handle.snapshot()
    assert final["done"] is True and final["error"] == ""
    assert [r["outcome"] for r in final["runs"]][1:] == [param_runs.SKIPPED] * 2


# -- the workspace lock ------------------------------------------------------


def test_a_fan_out_and_a_scheduled_refresh_cannot_run_concurrently(monkeypatch, tmp_path):
    # ONE lock for every kind of whole-notebook run, in BOTH directions: two runs would
    # fight over the CPU, the same throwaway render path, and the files they read.
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())

    with refresh.workspace_guard(ws):  # a refresh (or a background agent) holds it
        with pytest.raises(refresh.RefreshBusy):
            param_runs.fan_out(cfg, REL, _spec(), do_deliver=False)

    started = threading.Event()
    release = threading.Event()

    def _hold(cmd, cwd, env, timeout, cancel=None):
        started.set()
        release.wait(5)
        out = _out_of(cmd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html></html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notebook_run, "_exec", _hold)
    handle, thread = param_runs.start(cfg, REL, _spec(), do_deliver=False)
    try:
        assert started.wait(10)
        # ...and now a refresh cannot start underneath the fan-out either.
        with pytest.raises(refresh.RefreshBusy):
            refresh.refresh_notebook(cfg, REL, pull=False)
    finally:
        handle.cancel.set()
        release.set()
        thread.join(timeout=20)


def test_the_lock_is_released_when_the_fan_out_finishes(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    param_runs.fan_out(cfg, REL, _spec(), do_deliver=False)
    with refresh.workspace_guard(ws):  # must not still be held
        pass


# -- the "the notebook ignores the parameter" guard --------------------------


def test_a_notebook_that_never_reads_the_parameter_is_refused(monkeypatch, tmp_path):
    # The single worst outcome this feature could produce: N identically-numbered artifacts
    # under N different names, which a stakeholder cannot possibly detect.
    plain = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"
    cfg, ws = _mk(tmp_path, source=plain)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())

    with pytest.raises(param_runs.FanOutRefused) as exc:
        param_runs.fan_out(cfg, REL, _spec(), do_deliver=False)
    assert "never reads a parameter" in str(exc.value)
    assert "mooring_params" in str(exc.value)  # the fix is in the message
    assert not deliver.outbox_dir(ws).exists()  # refused BEFORE anything ran


def test_a_typo_in_the_parameter_name_is_caught_before_anything_runs(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    ran = []
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(seen=ran))
    with pytest.raises(param_runs.FanOutRefused):
        param_runs.fan_out(cfg, REL, _spec("regoin=EMEA,APAC"), do_deliver=False)
    assert ran == []


def test_refuses_a_plain_module_and_an_escaping_path(tmp_path):
    cfg, ws = _mk(tmp_path)
    (ws / "helpers.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    with pytest.raises(param_runs.FanOutRefused):
        param_runs.fan_out(cfg, "helpers.py", _spec(), do_deliver=False)
    with pytest.raises(ValueError):
        param_runs.fan_out(cfg, "../secret.py", _spec(), do_deliver=False)


def test_start_refuses_synchronously_rather_than_minting_a_doomed_run(tmp_path):
    # The hub answers a real 4xx instead of a run that appears in the UI and dies.
    plain = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"
    cfg, ws = _mk(tmp_path, source=plain)
    with pytest.raises(param_runs.FanOutRefused):
        param_runs.start(cfg, REL, _spec())


# -- what is recorded --------------------------------------------------------


def test_no_parameter_value_reaches_telemetry(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    events = []
    monkeypatch.setattr(telemetry, "log_event", lambda name, **f: events.append((name, f)))
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(fails={"APAC"}))

    param_runs.fan_out(cfg, REL, _spec())

    assert events and events[0][0] == "param_run"
    blob = json.dumps(events)
    for forbidden in ("EMEA", "APAC", "AMER", "region", REL, "notebooks"):
        assert forbidden not in blob, forbidden
    # Counts only.
    assert events[0][1] == {"values": 3, "clean": 2, "failed": 1, "cancelled": 0}


def test_the_activity_entry_records_counts_not_values(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    param_runs.fan_out(cfg, REL, _spec(), do_deliver=False)

    entry = activity.read(ws)[0]
    assert entry["op"] == "run" and entry["path"] == REL
    assert entry["param"] == "region" and entry["values"] == 3
    assert "EMEA" not in json.dumps(entry)


def test_a_fan_out_records_no_verify_receipt_and_no_schedule_receipt(monkeypatch, tmp_path):
    # A parameterised run is not evidence the notebook "verifies": it ran clean for EMEA
    # and may have failed for APAC. Badging the file green off one value would be a false
    # green, and a fan-out has no cadence to record against at all.
    from mooring import schedule, verify

    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(fails={"APAC"}))
    param_runs.fan_out(cfg, REL, _spec(), do_deliver=False)

    assert verify.read_results(ws) == {}
    assert schedule.load(ws) == []


def test_a_fan_out_has_no_path_to_the_network_or_the_schedule(monkeypatch, tmp_path):
    # It is ATTENDED and local: no pull, no push, no propose, and nothing that could put a
    # GitHub error's str(exc) (which embeds a request URL) on a report.
    import ast

    tree = ast.parse(Path(param_runs.__file__).read_text("utf-8"))
    names = {
        node.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    assert "sync" not in names and "github" not in names and "schedule" not in names


def test_describe_run_wording():
    V = param_runs.ValueRun
    assert "ran clean" in param_runs.describe_run(V("EMEA", param_runs.OK, artifact="a.html"))
    assert "cancelled" in param_runs.describe_run(V("EMEA", param_runs.CANCELLED))
    assert "not run" in param_runs.describe_run(V("EMEA", param_runs.SKIPPED))
    assert "did not run" in param_runs.describe_run(
        V("EMEA", param_runs.FAILED, reason="2 cells failed to run")
    )
