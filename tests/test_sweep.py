"""The catalog-wide verify sweep, and the gate it puts on a dependency change.

Two claims are on trial here, and both are about a NUMBER being honest:

* *"N of your notebooks run"* — an aggregate is a much easier thing to lie with than a
  per-notebook badge, because it outlives the code it described. These pin that the
  aggregate inherits the badge's SHA auto-clear rather than freezing a count, that one
  broken notebook never truncates the sweep, and that a swept receipt is indistinguishable
  from a hand-verified one.
* *"this lock change breaks 3 notebooks"* — the gate warns rather than blocks, and its
  confirm token is bound to the actual result, so a run that has since broken one MORE
  notebook invalidates a stale acknowledgement (the push guard's rule).

The marimo export subprocess is faked at the shared runner's ``_exec`` seam, exactly as
``tests/test_refresh.py`` does.
"""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from pathlib import Path

import pytest

from mooring import config, gitsha, pushguard, sweep, sync, verify
from mooring.app import notebook_run, refresh, sweep_run
from mooring.config import Config

NOTEBOOK = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"
MODULE = "def helper():\n    return 1\n"
LOCK = 'version = 1\n\n[[package]]\nname = "polars"\n'


def _cfg(tmp_path) -> Config:
    return Config(client_id="cid", owner="acme", repo="nbs", workspace_path=str(tmp_path / "ws"))


def _mk(tmp_path, *rels, lock: str | None = LOCK) -> tuple[Config, Path]:
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    for rel in rels or ("notebooks/a.py",):
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(NOTEBOOK, encoding="utf-8")
    if lock is not None:
        ws.mkdir(parents=True, exist_ok=True)
        # write_BYTES: the gate fingerprints the exact bytes about to upload, and on
        # Windows write_text would translate the line endings so the two never match.
        (ws / "uv.lock").write_bytes(lock.encode("utf-8"))
    return cfg, ws


def _out_of(cmd):
    for i, tok in enumerate(cmd):
        if tok == "-o":
            return Path(cmd[i + 1])
    return None


def _fake_exec(outcomes=None, *, default=(0, ""), calls=None, during=None):
    """Stand in for ``notebook_run._exec``. ``outcomes`` maps a notebook's basename to
    ``(returncode, stderr)`` — or to None, meaning "marimo never wrote a render" (the
    environment failure a broken lock produces)."""
    outcomes = outcomes or {}

    def _run(cmd, cwd, env, timeout, *, cancel=None):
        out = _out_of(cmd)
        name = out.name
        if calls is not None:
            calls.append(name)
        if during is not None:
            during()
        spec = outcomes.get(name, default)
        if spec is None:  # never ran: no render at all
            return subprocess.CompletedProcess(cmd, 1, "", "could not resolve dependencies")
        code, stderr = spec
        out.parent.mkdir(parents=True, exist_ok=True)
        # A real render embeds data values; plant one to prove it never survives.
        out.write_text("<html>SECRET_VALUE_DO_NOT_LEAK</html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, code, "", stderr)

    return _run


def _set_event() -> threading.Event:
    """An already-set cancel — "the user pressed Cancel before anything ran"."""
    event = threading.Event()
    event.set()
    return event


def _slug(rel: str) -> str:
    """The render filename verify.render_target derives — how a fake keys one notebook."""
    return verify.render_target(Path("."), rel).name


# -- enumeration -------------------------------------------------------------


def test_the_plan_is_the_synced_notebooks_only(tmp_path):
    # The sweep covers what the TEAM shares, and never executes a plain helper module
    # (running one would run something that was never a notebook).
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    (ws / "notebooks" / "helpers.py").write_text(MODULE, encoding="utf-8")
    (ws / "scratch").mkdir()
    (ws / "scratch" / "c.py").write_text(NOTEBOOK, encoding="utf-8")

    assert sweep_run.plan(cfg) == ["notebooks/a.py", "notebooks/b.py"]


# -- one failure never stops the sweep ---------------------------------------


def test_one_failing_notebook_does_not_stop_the_sweep(monkeypatch, tmp_path):
    # THE point of a sweep is the notebooks AFTER the broken one.
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py", "notebooks/c.py")
    calls: list[str] = []
    monkeypatch.setattr(
        notebook_run,
        "_exec",
        _fake_exec(
            {_slug("notebooks/b.py"): (1, "MarimoExceptionRaisedError: boom\n")}, calls=calls
        ),
    )

    report = sweep_run.sweep_workspace(cfg)

    assert len(calls) == 3  # every notebook was attempted
    assert report.clean == 2 and report.failed == 1
    assert [i.notebook for i in report.of(sweep.FAILED)] == ["notebooks/b.py"]
    assert report.of(sweep.FAILED)[0].reason == "1 cell failed to run"


def test_a_notebook_that_could_not_run_is_its_own_outcome(monkeypatch, tmp_path):
    # marimo never wrote a render: the ENVIRONMENT failed before the notebook ran. That
    # must not badge a good notebook red (app/notebook_run.py rule 4) — but it is still a
    # reason the repo is not currently runnable, so it counts as broken.
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec({_slug("notebooks/a.py"): None}))

    report = sweep_run.sweep_workspace(cfg)

    assert report.blocked == 1 and report.clean == 1 and report.failed == 0
    assert len(report.broken) == 1
    # No red receipt was written for it — verify's own attribution rule is intact.
    assert "notebooks/a.py" not in verify.read_results(ws)


def test_a_blocked_reason_carries_the_exception_TYPE_not_its_path(monkeypatch, tmp_path):
    # notebook_run's OSError message interpolates str(exc), which on Windows carries an
    # absolute path. That was fine while it only reached a console; the sweep PERSISTS its
    # reasons to .mooring/sweep.json, so the exception contributes its type only.
    cfg, ws = _mk(tmp_path, "notebooks/a.py")

    def _boom(cmd, cwd, env, timeout, *, cancel=None):
        raise PermissionError(r"[Errno 13] Permission denied: 'C:\Users\alice\secret\x.py'")

    monkeypatch.setattr(notebook_run, "_exec", _boom)
    report = sweep_run.sweep_workspace(cfg)

    reason = report.items[0].reason
    assert report.blocked == 1
    assert "PermissionError" in reason
    assert "alice" not in reason and "C:" not in reason
    assert "alice" not in sweep.sweep_path(ws).read_text("utf-8")


def test_stderr_never_reaches_the_report(monkeypatch, tmp_path):
    # marimo's stderr can quote a data value; only the marker COUNT is value-free.
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(
        notebook_run,
        "_exec",
        _fake_exec(default=(1, "MarimoExceptionRaisedError: 'SECRET_VALUE_DO_NOT_LEAK'\n")),
    )

    sweep_run.sweep_workspace(cfg)

    assert "SECRET" not in sweep.sweep_path(ws).read_text("utf-8")


# -- swept receipts are hand-verified receipts -------------------------------


def test_a_swept_receipt_is_byte_identical_to_a_hand_verified_one(monkeypatch, tmp_path):
    # A swept notebook must badge in the hub exactly like one someone clicked Verify on —
    # which is structural here, because the sweep runs the same verify_notebook.
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    from mooring.app import verify_run

    verify_run.verify_notebook(cfg, "notebooks/a.py")
    by_hand = (verify.verify_dir(ws) / "notebooks__a.py.json").read_bytes()
    verify.clear(ws)

    sweep_run.sweep_workspace(cfg)
    swept = (verify.verify_dir(ws) / "notebooks__a.py.json").read_bytes()

    import json

    hand_receipt, sweep_receipt = json.loads(by_hand), json.loads(swept)
    assert hand_receipt.keys() == sweep_receipt.keys()
    hand_receipt.pop("ran_at"), sweep_receipt.pop("ran_at")  # the only field that may differ
    assert hand_receipt == sweep_receipt


def test_a_swept_badge_still_auto_clears_on_edit(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)
    assert verify.read_results(ws)["notebooks/a.py"]["passed"] is True

    (ws / "notebooks/a.py").write_text(NOTEBOOK + "\n# edited\n", encoding="utf-8")

    assert verify.read_results(ws) == {}


# -- the aggregate cannot outlive an edit ------------------------------------


def test_the_aggregate_claim_cannot_outlive_an_edit_it_covered(monkeypatch, tmp_path):
    # THE integrity rule for an aggregate: "3 ran clean" is a claim about specific bytes.
    # Edit one of them and the number must SHRINK, not sit there vouching.
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py", "notebooks/c.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)
    assert sweep.read(ws).clean == 3

    (ws / "notebooks/b.py").write_text(NOTEBOOK + "\n# edited after the sweep\n", encoding="utf-8")

    stale = sweep.read(ws)
    assert stale.clean == 2
    assert stale.stale == ("notebooks/b.py",)
    assert "1 edited since" in sweep.headline(stale)


def test_an_item_is_keyed_to_the_bytes_that_RAN_not_to_a_post_run_re_hash(
    monkeypatch, tmp_path
):
    # THE reason app/verify_run.VerifyResult carries `sha` at all: hashing after the run
    # would key a "ran clean" claim to the edited-and-maybe-broken bytes — the exact
    # false-green the SHA-before-run rule exists to prevent. Re-hashing here would make
    # this notebook look covered by a run that never executed it.
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    before = gitsha.local_blob_sha(ws / "notebooks/a.py", "notebooks/a.py")

    def _edit_midrun():
        (ws / "notebooks/a.py").write_text(NOTEBOOK + "\n# edited mid-run\n", encoding="utf-8")

    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(during=_edit_midrun))
    report = sweep_run.sweep_workspace(cfg)

    assert report.items[0].sha == before  # the PRE-run bytes, not the file on disk
    # ...so the aggregate disowns it on read, exactly as the badge clears.
    assert sweep.read(ws).stale == ("notebooks/a.py",)
    assert sweep.read(ws).clean == 0
    assert verify.read_results(ws) == {}


def test_the_report_records_the_lock_the_runs_ACTUALLY_ran_under(monkeypatch, tmp_path):
    # Fingerprinting after the runs would let a `deps` command landing mid-sweep stamp the
    # NEW lock onto results produced under the old one — a green report for an environment
    # nothing was executed against.
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    old = sweep.lock_fingerprint(ws)
    new_lock = LOCK + '\n[[package]]\nname = "pyarrow"\n'

    def _deps_add_midrun():
        (ws / "uv.lock").write_bytes(new_lock.encode())

    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(during=_deps_add_midrun))
    report = sweep_run.sweep_workspace(cfg)

    assert report.lock == old
    # ...and the gate refuses to let it vouch for the lock that is actually being pushed.
    guard_fn, _ = _guard(ws)
    assert "different uv.lock" in guard_fn("uv.lock", new_lock.encode())[0]


def test_a_deleted_notebook_drops_out_of_the_claim(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)

    (ws / "notebooks/b.py").unlink()

    assert sweep.read(ws).clean == 1


def test_a_corrupt_or_missing_report_reads_as_no_sweep(tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    assert sweep.read(ws) is None
    sweep.sweep_path(ws).parent.mkdir(parents=True, exist_ok=True)
    sweep.sweep_path(ws).write_text("{not json", encoding="utf-8")
    assert sweep.read(ws) is None


# -- cancel ------------------------------------------------------------------


def test_cancel_actually_stops_the_sweep(monkeypatch, tmp_path):
    # A sweep is expensive, so Cancel has to be REAL: the notebooks after the cancel are
    # never executed (they are recorded as skipped, not quietly dropped).
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py", "notebooks/c.py")
    calls: list[str] = []
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(calls=calls))
    stop = threading.Event()

    def _after_first(done, total, item):
        stop.set()

    report = sweep_run.sweep_workspace(cfg, cancel=stop, on_progress=_after_first)

    assert len(calls) == 1  # only the first notebook actually ran
    assert report.cancelled is True
    assert report.clean == 1 and report.skipped == 2
    assert "(cancelled)" in sweep.headline(report)


def test_cancel_kills_the_notebook_that_is_RUNNING_not_just_the_next_one(
    monkeypatch, tmp_path
):
    # The runner grew a real cancel (a process-TREE kill) with the parameterised-run
    # merge, so the sweep no longer has to let a five-minute notebook finish before
    # noticing. A killed notebook is SKIPPED — it was never given a chance to answer, so
    # it is neither a failure nor a blocked environment.
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    stop = threading.Event()
    calls: list[str] = []

    def _cancel_midway(cmd, cwd, env, timeout, *, cancel=None):
        # The runner only passes `cancel` through when it HAS one — so receiving it here
        # is itself the proof that the sweep wired its event to the process-tree kill.
        calls.append(_out_of(cmd).name)
        assert cancel is stop, "the sweep must hand its cancel event to the RUNNER"
        cancel.set()  # the user hits Cancel while THIS notebook is executing
        # What the real _exec returns after its watchdog kills the tree: a dead process
        # and no render (the runner then sees cancel.is_set() and raises RunCancelled).
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(notebook_run, "_exec", _cancel_midway)

    report = sweep_run.sweep_workspace(cfg, cancel=stop)

    assert len(calls) == 1  # the second notebook was never launched

    assert report.cancelled is True
    assert report.skipped == 2 and report.clean == 0 and report.failed == 0
    assert report.of(sweep.SKIPPED)[0].reason == "cancelled while it was running"


def test_resume_skips_what_is_still_verified(monkeypatch, tmp_path):
    # The "resumable-ish" half: finishing a cancelled sweep must not re-run what already
    # passed AT ITS CURRENT BYTES, under THESE dependencies.
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    calls: list[str] = []
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(calls=calls))
    sweep_run.sweep_workspace(cfg, rels=["notebooks/a.py"])
    calls.clear()

    report = sweep_run.sweep_workspace(cfg, skip_verified=True)

    assert calls == [_slug("notebooks/b.py")]
    assert report.clean == 2


# -- resume can never manufacture a green report -----------------------------


def test_resume_across_a_lock_change_runs_everything(monkeypatch, tmp_path):
    # THE false green this gate exists to prevent, and it was reachable in one documented
    # command: a verify receipt is keyed to the NOTEBOOK's bytes and knows nothing about
    # uv.lock, so resuming on receipts would stamp the NEW lock fingerprint onto an OLD
    # sweep having executed nothing at all — a green report and a disarmed push gate.
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py", "notebooks/c.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)  # all clean under the OLD lock

    new_lock = LOCK + '\n[[package]]\nname = "pyarrow"\n'
    (ws / "uv.lock").write_bytes(new_lock.encode())
    # Everything now fails under the new lock.
    calls: list[str] = []
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(default=(1, "MarimoExc\n"), calls=calls))

    report = sweep_run.sweep_workspace(cfg, skip_verified=True)

    assert len(calls) == 3, "a resume across a lock change must re-run everything"
    assert report.clean == 0 and report.failed == 3
    guard_fn, _ = _guard(ws)
    assert "breaks 3 notebooks" in guard_fn("uv.lock", new_lock.encode())[0]


def test_resume_scope_says_why_it_cannot_resume(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    assert sweep_run.resume_scope(cfg) == (frozenset(), "there is no previous check to resume")

    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)
    assert sweep_run.resume_scope(cfg) == (frozenset({"notebooks/a.py"}), "")

    (ws / "uv.lock").write_bytes((LOCK + "# moved\n").encode())
    scope, why = sweep_run.resume_scope(cfg)
    assert scope == frozenset() and "different dependencies" in why


def test_resume_re_runs_a_notebook_edited_since_the_last_sweep(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)
    (ws / "notebooks/a.py").write_text(NOTEBOOK + "\n# edited\n", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(calls=calls))
    sweep_run.sweep_workspace(cfg, skip_verified=True)

    assert calls == [_slug("notebooks/a.py")]


def test_the_cli_says_when_a_resume_cannot_be_honoured(monkeypatch, tmp_path, capsys):
    from mooring import cli

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)
    (ws / "uv.lock").write_bytes((LOCK + "# moved\n").encode())

    cli.cmd_verify(cfg, _verify_args(resume=True))
    out = capsys.readouterr().out

    assert "Nothing to resume" in out and "different dependencies" in out


# -- serialization with the scheduled refresh --------------------------------


def test_a_sweep_and_a_scheduled_refresh_cannot_run_concurrently(monkeypatch, tmp_path):
    # Both pull CPU and both write receipts; they serialize on the SAME cross-process
    # lockfile the background refresh agent takes.
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())

    with refresh.workspace_guard(ws):  # stand in for a refresh in flight
        with pytest.raises(refresh.RefreshBusy):
            sweep_run.sweep_workspace(cfg)

    # ...and the other way round: a refresh steps aside while a sweep holds it.
    seen = {}

    def _during():
        try:
            with refresh.workspace_guard(ws):
                seen["refresh_got_in"] = True
        except refresh.RefreshBusy:
            seen["refresh_got_in"] = False

    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(during=_during))
    sweep_run.sweep_workspace(cfg)
    assert seen["refresh_got_in"] is False


def test_the_lock_is_heartbeaten_WHILE_a_child_process_blocks(monkeypatch, tmp_path):
    """Does the heartbeat tick during a blocking child, or only between operations?

    This is the question a sweep makes load-bearing and no other caller does. A sweep holds
    ONE lock across N sequential ``notebook_run.run`` calls, each of which blocks the
    holding thread inside ``proc.communicate`` for as long as marimo takes. If the mtime
    were only refreshed between operations, a sweep longer than ``_LOCK_STALE_S`` would
    have its lock STOLEN mid-run by a scheduled refresh — two kernels on one CPU, two
    writers of the same throwaway render, and this sweep's ``finally`` then unlinking the
    lock the refresh holds.

    Answer, pinned here: it ticks. ``workspace_guard`` runs the heartbeat on its own daemon
    thread, so it is independent of whatever the holding thread is blocked on. The window
    and beat are shrunk so a real blocking child crosses the staleness threshold several
    times over."""
    monkeypatch.setattr(notebook_run, "_LOCK_STALE_S", 0.4)
    monkeypatch.setattr(notebook_run, "_LOCK_BEAT_S", 0.05)
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")

    def _slow_child(cmd, cwd, env, timeout, *, cancel=None):
        time.sleep(0.6)  # a child that blocks the holder for longer than the stale window
        out = _out_of(cmd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html>ok</html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(notebook_run, "_exec", _slow_child)

    refusals: list[bool] = []
    done = threading.Event()

    def _probe() -> None:
        # A background refresh trying to take the workspace, over and over, for the whole
        # sweep. Every single attempt must be refused.
        while not done.wait(0.1):
            try:
                with notebook_run.workspace_guard(ws):
                    refusals.append(False)  # STOLE the lock — the heartbeat is not ticking
            except notebook_run.RunBusy:
                refusals.append(True)

    prober = threading.Thread(target=_probe, daemon=True)
    prober.start()
    try:
        report = sweep_run.sweep_workspace(cfg)
    finally:
        done.set()
        prober.join(timeout=5)

    assert report.clean == 2
    assert len(refusals) >= 4, "the probe must have tried across more than one stale window"
    assert all(refusals), "a live sweep's lock was stolen — the heartbeat does NOT tick"


def test_the_report_is_structurally_unsyncable(tmp_path):
    assert sync.is_synced_path(".mooring/sweep.json") is False


# -- the dependency gate -----------------------------------------------------


def _guard(ws, tokens=frozenset(), notebooks_fn=None):
    return pushguard.make_lock_guard(ws, tokens, notebooks_fn=notebooks_fn)


def test_the_gate_only_looks_at_the_lock(tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    guard_fn, collected = _guard(ws)
    assert guard_fn("notebooks/a.py", NOTEBOOK.encode()) == []
    assert guard_fn("pyproject.toml", b"[project]\n") == []
    assert collected == {}


def test_an_unchecked_lock_change_warns(tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    guard_fn, collected = _guard(ws)

    findings = guard_fn("uv.lock", LOCK.encode())

    assert findings and "not checked" in findings[0]
    assert collected["uv.lock"]["token"]


def test_a_clean_sweep_lets_the_lock_through(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)

    guard_fn, collected = _guard(ws)

    assert guard_fn("uv.lock", LOCK.encode()) == []
    assert collected == {}


def test_a_sweep_against_a_DIFFERENT_lock_never_vouches(monkeypatch, tmp_path):
    # A verify receipt is keyed to the NOTEBOOK's bytes and says nothing about uv.lock.
    # Without this the whole gate is theatre: change the lock and every receipt is still
    # "valid" over an environment nothing has run against.
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)

    guard_fn, _ = _guard(ws)
    findings = guard_fn("uv.lock", (LOCK + '\n[[package]]\nname = "pyarrow"\n').encode())

    assert findings and "different uv.lock" in findings[0]


def test_the_gate_warns_rather_than_blocks(monkeypatch, tmp_path):
    # Warn-and-confirm, never a wall: the SAME token the guard collected lets the very
    # next push through untouched.
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(default=(1, "MarimoExc\n")))
    sweep_run.sweep_workspace(cfg)

    guard_fn, collected = _guard(ws)
    findings = guard_fn("uv.lock", LOCK.encode())
    assert findings and "breaks 1 notebook" in findings[0]

    token = collected["uv.lock"]["token"]
    allowed_fn, allowed_collected = _guard(ws, frozenset({token}))
    assert allowed_fn("uv.lock", LOCK.encode()) == []  # "push anyway" goes through
    assert allowed_collected == {}


def test_a_new_failure_invalidates_a_stale_confirm(monkeypatch, tmp_path):
    # The push guard's token rule, applied to a RESULT rather than to file content: the
    # token binds the exact findings to the exact bytes, so a sweep that has since broken
    # one more notebook is never covered by yesterday's acknowledgement.
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    monkeypatch.setattr(
        notebook_run, "_exec", _fake_exec({_slug("notebooks/a.py"): (1, "MarimoExc\n")})
    )
    sweep_run.sweep_workspace(cfg)
    guard_fn, collected = _guard(ws)
    guard_fn("uv.lock", LOCK.encode())
    stale_token = collected["uv.lock"]["token"]

    # A second notebook has since broken too.
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(default=(1, "MarimoExc\n")))
    sweep_run.sweep_workspace(cfg)

    guard_fn, collected = _guard(ws, frozenset({stale_token}))
    findings = guard_fn("uv.lock", LOCK.encode())

    assert findings and "breaks 2 notebooks" in findings[0]
    assert collected["uv.lock"]["token"] != stale_token


def test_a_swapped_failure_invalidates_a_confirm_even_though_the_count_holds(
    monkeypatch, tmp_path
):
    # The sharper version of the token rule: the finding COLLAPSES to a count, so one
    # notebook being fixed while another breaks reads identically. Binding the confirm to
    # the wording would let a stale acknowledgement cover a result nobody has read; it is
    # bound to a per-item digest of the report instead.
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    monkeypatch.setattr(
        notebook_run, "_exec", _fake_exec({_slug("notebooks/a.py"): (1, "MarimoExc\n")})
    )
    sweep_run.sweep_workspace(cfg)
    guard_fn, collected = _guard(ws)
    first = guard_fn("uv.lock", LOCK.encode())
    stale_token = collected["uv.lock"]["token"]

    # a is fixed, b breaks: the SAME sentence, a different world.
    monkeypatch.setattr(
        notebook_run, "_exec", _fake_exec({_slug("notebooks/b.py"): (1, "MarimoExc\n")})
    )
    sweep_run.sweep_workspace(cfg)

    guard_fn, collected = _guard(ws, frozenset({stale_token}))
    second = guard_fn("uv.lock", LOCK.encode())

    assert second == first, "the wording is identical — that is the point"
    assert second, "...but the stale confirm must NOT cover it"
    assert collected["uv.lock"]["token"] != stale_token


def test_shrinking_coverage_invalidates_a_confirm_the_count_would_hide(monkeypatch, tmp_path):
    # `broken` masks `stale`: acknowledge while nothing is stale, then edit two notebooks,
    # and the breakage count is unchanged while the sweep now covers less.
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py", "notebooks/c.py")
    monkeypatch.setattr(
        notebook_run, "_exec", _fake_exec({_slug("notebooks/a.py"): (1, "MarimoExc\n")})
    )
    sweep_run.sweep_workspace(cfg)
    guard_fn, collected = _guard(ws)
    guard_fn("uv.lock", LOCK.encode())
    token = collected["uv.lock"]["token"]

    (ws / "notebooks/b.py").write_text(NOTEBOOK + "\n# edited\n", encoding="utf-8")
    (ws / "notebooks/c.py").write_text(NOTEBOOK + "\n# edited\n", encoding="utf-8")

    guard_fn, collected = _guard(ws, frozenset({token}))
    assert guard_fn("uv.lock", LOCK.encode()), "coverage shrank — the old confirm must lapse"


def test_a_notebook_added_since_the_sweep_reopens_the_gate(monkeypatch, tmp_path):
    # The gate's SILENCE reads as "everything was checked", so it must know what "everything"
    # is now — not just what the sweep happened to cover.
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)
    guard_fn, _ = _guard(ws, notebooks_fn=lambda: sweep_run.plan(cfg))
    assert guard_fn("uv.lock", LOCK.encode()) == []  # covered

    (ws / "notebooks" / "new.py").write_text(NOTEBOOK, encoding="utf-8")

    guard_fn, _ = _guard(ws, notebooks_fn=lambda: sweep_run.plan(cfg))
    findings = guard_fn("uv.lock", LOCK.encode())
    assert findings and "added since the sweep" in findings[0]


def test_the_gate_never_walks_the_workspace_for_an_ordinary_file(tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    walked = []

    def _boom():
        walked.append(True)
        return []

    guard_fn, _ = _guard(ws, notebooks_fn=_boom)
    assert guard_fn("notebooks/a.py", NOTEBOOK.encode()) == []
    assert walked == [], "enumerating is only worth it when a uv.lock is actually going"


def test_an_absent_lock_is_not_an_empty_lock(tmp_path):
    # "" is the sentinel for "there is no lock file". Collapsing an EMPTY lock onto it
    # would make a no-lock sweep silently vouch for a zero-byte lock being pushed.
    assert sweep.fingerprint(None) == sweep.NO_LOCK
    assert sweep.fingerprint(b"") != sweep.NO_LOCK

    cfg, ws = _mk(tmp_path, "notebooks/a.py", lock=None)
    assert sweep.lock_fingerprint(ws) == sweep.NO_LOCK
    sweep.record(ws, sweep.SweepReport(items=(), lock=sweep.NO_LOCK))
    guard_fn, _ = _guard(ws)
    assert guard_fn("uv.lock", b""), "a lock appearing out of nowhere is still a change"


def test_changed_lock_bytes_invalidate_a_confirm_too(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(default=(1, "MarimoExc\n")))
    sweep_run.sweep_workspace(cfg)
    guard_fn, collected = _guard(ws)
    guard_fn("uv.lock", LOCK.encode())
    token = collected["uv.lock"]["token"]

    guard_fn, collected = _guard(ws, frozenset({token}))
    assert guard_fn("uv.lock", (LOCK + "# changed\n").encode())  # not covered


def test_an_edit_since_the_sweep_reopens_the_gate(monkeypatch, tmp_path):
    # The aggregate's staleness feeds the gate: a notebook edited since the sweep means
    # the "it all runs" claim no longer covers the repo being pushed.
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)
    (ws / "notebooks/a.py").write_text(NOTEBOOK + "\n# later\n", encoding="utf-8")

    guard_fn, _ = _guard(ws)
    findings = guard_fn("uv.lock", LOCK.encode())

    assert findings and "changed since the sweep" in findings[0]


def test_a_cancelled_sweep_does_not_vouch(monkeypatch, tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg, cancel=_set_event())

    guard_fn, _ = _guard(ws)
    assert "cancelled" in guard_fn("uv.lock", LOCK.encode())[0]


def test_all_three_guards_ride_one_seam_with_their_own_ledgers(tmp_path, monkeypatch):
    # THE composition, once admin policy landed a third guard: one guard_fn, three
    # `collected` maps, three override rules. Each finding must land in its OWN ledger —
    # merging any two of them would quietly give one guard the other's override policy.
    from mooring import policy, workspace_config

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    (ws / "reports").mkdir()
    (ws / "reports" / "q1.py").write_text(NOTEBOOK, encoding="utf-8")
    (ws / "mooring.toml").write_text(
        '[policy]\npropose_only = ["reports/**"]\n', encoding="utf-8"
    )
    pol = policy.load(ws)
    assert pol.propose_only.patterns  # the fixture really did register a rule

    content_fn, content_collected = pushguard.make_guard()
    lock_fn, lock_collected = _guard(ws)
    gate_fn, blocked = policy.make_propose_gate(pol)
    guard_fn = policy.compose_guards(content_fn, lock_fn, gate_fn)

    assert guard_fn("uv.lock", LOCK.encode())  # the deps gate fires
    assert guard_fn("notes.md", b"ghp_" + b"a" * 36 + b"\n")  # the content scan fires
    assert guard_fn("reports/q1.py", NOTEBOOK.encode())  # the policy gate fires

    assert set(content_collected) == {"notes.md"}
    assert set(lock_collected) == {"uv.lock"}
    assert set(blocked) == {"reports/q1.py"}
    # Only the two scanner guards mint a token; a propose-only block has no override.
    assert content_collected["notes.md"]["token"]
    assert lock_collected["uv.lock"]["token"]
    assert isinstance(blocked["reports/q1.py"], str)
    # ...and a deletion reaches every guard (sync offers data=None for one).
    assert guard_fn("reports/q1.py", None), "a propose-only DELETE is still a direct write"
    assert workspace_config.guard_mode(ws) == "warn"


def test_a_deleted_lock_is_questioned_like_a_rewrite(monkeypatch, tmp_path):
    # sync now offers every candidate to the guard, deletions included. Removing the
    # team's uv.lock is the most drastic environment change there is — everyone stops
    # running against a pinned resolution — so it must not slip through as "no bytes".
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)  # a clean sweep UNDER the lock

    guard_fn, collected = _guard(ws)
    assert guard_fn("uv.lock", LOCK.encode()) == []  # the rewrite it covers passes

    guard_fn, collected = _guard(ws)
    findings = guard_fn("uv.lock", None)  # ...the DELETION does not
    assert findings and "different uv.lock" in findings[0]
    assert collected["uv.lock"]["token"]


# -- the CLI surface ---------------------------------------------------------


def _verify_args(**kw):
    base = dict(path=None, all_notebooks=True, resume=False, yes=True, clear=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_verify_all_sweeps_and_exits_nonzero_on_a_failure(monkeypatch, tmp_path, capsys):
    from mooring import cli

    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    monkeypatch.setattr(
        notebook_run, "_exec", _fake_exec({_slug("notebooks/b.py"): (1, "MarimoExc\n")})
    )

    rc = cli.cmd_verify(cfg, _verify_args())
    out = capsys.readouterr().out

    assert rc == 1
    assert "This runs 2 notebooks" in out  # the cost, stated before it starts
    assert "1 ran clean, 1 failed" in out
    assert "not that its numbers are right" in out  # the honesty line


def test_cli_verify_all_refuses_a_path(tmp_path):
    from mooring import cli

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    with pytest.raises(SystemExit):
        cli.cmd_verify(cfg, _verify_args(path="notebooks/a.py"))


def test_cli_verify_clear_forgets_the_aggregate_too(monkeypatch, tmp_path):
    from mooring import cli

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)
    assert sweep.read(ws) is not None

    cli.cmd_verify(cfg, argparse.Namespace(path=None, clear=True, all_notebooks=False))

    assert sweep.read(ws) is None


def test_clearing_ONE_badge_takes_it_out_of_the_aggregate_too(monkeypatch, tmp_path):
    # Asymmetric otherwise: the badge goes, but "2 ran clean" keeps standing over a
    # receipt the user just asked mooring to forget.
    from mooring import cli

    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    sweep_run.sweep_workspace(cfg)
    assert sweep.read(ws).clean == 2

    cli.cmd_verify(
        cfg, argparse.Namespace(path=None, clear="notebooks/b.py", all_notebooks=False)
    )

    after = sweep.read(ws)
    assert after.clean == 1 and after.total == 1
    assert [i.notebook for i in after.items] == ["notebooks/a.py"]


def test_the_cost_line_prices_itself_from_the_last_check(monkeypatch, tmp_path):
    # "It can take a while" is not a number, and the worst case (RUN_TIMEOUT per notebook)
    # is over an hour. Once one sweep has been timed, say roughly how long.
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    assert "can take a while" in sweep_run.describe_cost(cfg, 2)

    sweep.record(
        ws,
        sweep.SweepReport(
            items=(
                sweep.SweepItem("notebooks/a.py", sweep.CLEAN, sha="x", seconds=90),
                sweep.SweepItem("notebooks/b.py", sweep.CLEAN, sha="y", seconds=90),
            ),
            lock=sweep.lock_fingerprint(ws),
        ),
    )

    assert sweep_run.estimate_minutes(cfg, 8) == 12
    assert "roughly 12 min" in sweep_run.describe_cost(cfg, 8)


def test_deps_offers_the_sweep_only_when_the_lock_actually_moved(monkeypatch, tmp_path, capsys):
    from mooring import cli, pyproject_env

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    monkeypatch.setattr(pyproject_env, "add", lambda ws_, pkgs: None)  # a no-op uv

    args = argparse.Namespace(deps_command="add", packages=["polars"], sweep=True)
    cli.cmd_deps(cfg, args)
    assert "run against it" not in capsys.readouterr().out  # nothing resolved differently

    def _rewrite(ws_, pkgs):
        (ws / "uv.lock").write_text(LOCK + '\n[[package]]\nname = "polars"\n', encoding="utf-8")

    monkeypatch.setattr(pyproject_env, "add", _rewrite)
    cli.cmd_deps(cfg, args)
    out = capsys.readouterr().out

    assert "uv.lock changed — 1 notebook run against it." in out
    assert "1 ran clean" in out  # --sweep ran it without prompting


def test_deps_no_sweep_points_at_the_command_instead(monkeypatch, tmp_path, capsys):
    from mooring import cli, pyproject_env

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.setattr(
        pyproject_env, "run_lock", lambda ws_: (ws / "uv.lock").write_text("v2\n", "utf-8")
    )

    cli.cmd_deps(cfg, argparse.Namespace(deps_command="lock", sweep=False))
    out = capsys.readouterr().out

    assert "Not checked." in out and "mooring verify --all" in out


def test_cli_push_warns_about_an_unchecked_lock_and_acknowledging_shows_it(
    monkeypatch, tmp_path, capsys
):
    from mooring import cli

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    g = cli._push_guard_fn(cfg, False)
    assert g.guard_fn("uv.lock", LOCK.encode())
    assert set(g.lock_collected) == {"uv.lock"}
    assert g.collected == {} and g.blocked == {}

    # --acknowledge-findings does NOT turn the gate off: it lets the push through and
    # SHOWS what was let through — in the DEPS ledger, not the content one, so it can
    # never print in the content guard's "now visible to everyone" vocabulary.
    g = cli._push_guard_fn(cfg, True)
    assert g.guard_fn("uv.lock", LOCK.encode()) == []
    assert set(g.lock_acknowledged) == {"uv.lock"} and g.acknowledged == {}


def test_block_mode_never_walls_off_a_dependency_warning(tmp_path, monkeypatch):
    # [guard] push = "block" is a policy about sensitive CONTENT. It must not silently
    # become "you may never push a lock file that breaks a notebook".
    from mooring import cli, workspace_config

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(workspace_config, "guard_mode", lambda ws_: "block")

    g = cli._push_guard_fn(cfg, True)

    assert g.mode == "block"
    assert g.guard_fn("uv.lock", LOCK.encode()) == []  # the deps gate stays acknowledgeable
    assert set(g.lock_acknowledged) == {"uv.lock"}


# -- the hub surface ---------------------------------------------------------


def _hub(tmp_path, monkeypatch):
    from mooring import paths
    from mooring.hub.server import Hub

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    ws = tmp_path / "ws"
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(ws))
    return Hub(config.AppConfig(repos=(spec,), active_alias="ws"))


def test_hub_sweep_reports_its_cost_then_runs_it(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    from mooring.hub.server import create_app

    _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    hub = _hub(tmp_path, monkeypatch)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    with TestClient(create_app(hub)) as client:
        plan = client.get("/api/sweep/plan").json()
        assert plan["total"] == 2 and "2 notebooks" in plan["cost"]
        assert client.post("/api/sweep", json={}).json()["running"] is True
        assert hub.sweep.wait(30) is True
        state = client.get("/api/sweep").json()

    assert state["running"] is False and state["finished"] is True
    assert state["clean"] == 2 and state["total"] == 2
    assert "not that its numbers are right" in state["warning"]


def test_hub_sweep_steps_aside_for_a_refresh(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    from mooring.hub.server import create_app

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    hub = _hub(tmp_path, monkeypatch)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec())
    with TestClient(create_app(hub)) as client, refresh.workspace_guard(ws):
        client.post("/api/sweep", json={})
        hub.sweep.wait(30)
        state = client.get("/api/sweep").json()

    assert "already running" in state["error"]


def test_hub_push_409_carries_the_sweep_finding_and_its_own_token(monkeypatch, tmp_path):
    # The hub's half of "make the result visible BEFORE the change is pushed": the lock is
    # withheld and the response upgrades to the same 409 needs_confirm shape the push guard
    # uses — but in its own list, so the UI can ask the right question.
    import json as _json

    from mooring.hub.routes import sync as sync_routes

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    hub = _hub(tmp_path, monkeypatch)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(default=(1, "MarimoExc\n")))
    sweep_run.sweep_workspace(hub.cfg)

    def _sync_op_body(name, op):
        op()  # exercises the composed guard_fn the way sync.push would
        return {"lines": [], "summary": ""}, 200

    monkeypatch.setattr(hub, "_sync_op_body", _sync_op_body)
    response = sync_routes._guarded_sync_op(
        hub, "push", {}, lambda guard_fn: guard_fn("uv.lock", LOCK.encode())
    )
    body = _json.loads(response.body)

    assert response.status_code == 409
    assert body["needs_confirm"] is True and body["guard_mode"] == "warn"
    assert body["guard_findings"] == []
    assert body["sweep_findings"][0]["path"] == "uv.lock"
    assert "breaks 1 notebook" in body["sweep_findings"][0]["findings"][0]["kind"]
    assert body["sweep_findings"][0]["token"]
    assert body["policy_blocked"] == []


def test_hub_409_reports_all_three_guards_each_under_its_own_rule(monkeypatch, tmp_path):
    """The composition the merge created: three guards, one push, three lists.

    Each keeps its own override rule, and the response has to carry all three faithfully —
    folding any pair together would silently give one guard the other's policy. Here the
    content policy is BLOCK, which is the sharpest case: content is unacknowledgeable,
    the deps gate is still acknowledgeable, and the policy block has no token at all.
    """
    import json as _json

    from mooring import policy
    from mooring.hub.routes import sync as sync_routes

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    (ws / "reports").mkdir()
    (ws / "reports" / "q1.py").write_text(NOTEBOOK, encoding="utf-8")
    (ws / "mooring.toml").write_text(
        '[policy]\npush_guard = "block"\npropose_only = ["reports/**"]\n', encoding="utf-8"
    )
    hub = _hub(tmp_path, monkeypatch)
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(default=(1, "MarimoExc\n")))
    sweep_run.sweep_workspace(hub.cfg)
    assert policy.load(ws).guard_mode("warn") == "block"

    def _sync_op_body(name, op):
        op()
        return {"lines": [], "summary": ""}, 200

    def _run(guard_fn):
        guard_fn("uv.lock", LOCK.encode())  # deps gate
        guard_fn("notes.md", b"ghp_" + b"a" * 36 + b"\n")  # content scanner
        guard_fn("reports/q1.py", NOTEBOOK.encode())  # policy propose-only

    monkeypatch.setattr(hub, "_sync_op_body", _sync_op_body)
    body = _json.loads(sync_routes._guarded_sync_op(hub, "push", {}, _run).body)

    assert [f["path"] for f in body["guard_findings"]] == ["notes.md"]
    assert [f["path"] for f in body["sweep_findings"]] == ["uv.lock"]
    assert [b["path"] for b in body["policy_blocked"]] == ["reports/q1.py"]
    # Content is unacknowledgeable under block, so the dialog offers no override —
    # but the response is still confirmable, because the deps gate always is.
    assert body["needs_confirm"] is True
    assert body["guard_mode"] == "block"
    # Only the two scanner guards mint tokens; a policy block carries none.
    assert body["guard_findings"][0]["token"] and body["sweep_findings"][0]["token"]
    assert "token" not in body["policy_blocked"][0]


def test_hub_propose_never_installs_the_propose_only_gate(monkeypatch, tmp_path):
    # It is the road that rule points at; firing there would strand those files entirely.
    # The other two guards DO run on propose — they are about what the bytes are.
    import json as _json

    from mooring.hub.routes import sync as sync_routes

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    (ws / "reports").mkdir()
    (ws / "reports" / "q1.py").write_text(NOTEBOOK, encoding="utf-8")
    (ws / "mooring.toml").write_text(
        '[policy]\npropose_only = ["reports/**"]\n', encoding="utf-8"
    )
    hub = _hub(tmp_path, monkeypatch)
    seen: dict = {}

    def _sync_op_body(name, op):
        op()
        return {"lines": [], "summary": ""}, 200

    def _run(guard_fn):
        seen["report"] = guard_fn("reports/q1.py", NOTEBOOK.encode())
        seen["lock"] = guard_fn("uv.lock", LOCK.encode())

    monkeypatch.setattr(hub, "_sync_op_body", _sync_op_body)
    response = sync_routes._guarded_sync_op(hub, "propose", {}, _run, direct=False)
    body = _json.loads(response.body)

    assert seen["report"] == []  # the propose-only gate is absent
    assert seen["lock"], "...but the dependency gate still guards a propose"
    assert body["policy_blocked"] == []
    assert [f["path"] for f in body["sweep_findings"]] == ["uv.lock"]
