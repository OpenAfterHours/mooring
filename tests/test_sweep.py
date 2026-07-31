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
from pathlib import Path

import pytest

from mooring import config, pushguard, sweep, sync, verify
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

    def _run(cmd, cwd, env, timeout):
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
    stop = {"now": False}

    def _after_first(done, total, item):
        stop["now"] = True

    report = sweep_run.sweep_workspace(
        cfg, cancel=lambda: stop["now"], on_progress=_after_first
    )

    assert len(calls) == 1  # only the first notebook actually ran
    assert report.cancelled is True
    assert report.clean == 1 and report.skipped == 2
    assert "(cancelled)" in sweep.headline(report)


def test_resume_skips_what_is_still_verified(monkeypatch, tmp_path):
    # The "resumable-ish" half: finishing a cancelled sweep must not re-run what already
    # passed AT ITS CURRENT BYTES (read_results only returns SHA-valid receipts).
    cfg, ws = _mk(tmp_path, "notebooks/a.py", "notebooks/b.py")
    calls: list[str] = []
    monkeypatch.setattr(notebook_run, "_exec", _fake_exec(calls=calls))
    sweep_run.sweep_workspace(cfg, rels=["notebooks/a.py"])
    calls.clear()

    report = sweep_run.sweep_workspace(cfg, skip_verified=True)

    assert calls == [_slug("notebooks/b.py")]
    assert report.clean == 2


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


def test_the_report_is_structurally_unsyncable(tmp_path):
    assert sync.is_synced_path(".mooring/sweep.json") is False


# -- the dependency gate -----------------------------------------------------


def _guard(ws, tokens=frozenset()):
    return pushguard.make_lock_guard(ws, tokens)


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
    sweep_run.sweep_workspace(cfg, cancel=lambda: True)

    guard_fn, _ = _guard(ws)
    assert "cancelled" in guard_fn("uv.lock", LOCK.encode())[0]


def test_combine_runs_both_guards_behind_one_guard_fn(tmp_path):
    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    content_fn, content_collected = pushguard.make_guard()
    lock_fn, lock_collected = _guard(ws)
    guard_fn = pushguard.combine(content_fn, lock_fn)

    assert guard_fn("uv.lock", LOCK.encode())  # the deps gate fires
    assert guard_fn("notes.md", b"ghp_" + b"a" * 36 + b"\n")  # the content scan fires
    assert set(content_collected) == {"notes.md"}
    assert set(lock_collected) == {"uv.lock"}


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
    guard_fn, collected, lock_collected, mode, acknowledged = cli._push_guard_fn(cfg, False)
    assert guard_fn("uv.lock", LOCK.encode())
    assert set(lock_collected) == {"uv.lock"} and collected == {}

    # --acknowledge-findings does NOT turn the gate off: it lets the push through and
    # SHOWS what was let through.
    guard_fn, collected, lock_collected, mode, acknowledged = cli._push_guard_fn(cfg, True)
    assert guard_fn("uv.lock", LOCK.encode()) == []
    assert set(acknowledged) == {"uv.lock"}


def test_block_mode_never_walls_off_a_dependency_warning(tmp_path, monkeypatch):
    # [guard] push = "block" is a policy about sensitive CONTENT. It must not silently
    # become "you may never push a lock file that breaks a notebook".
    from mooring import cli, workspace_config

    cfg, ws = _mk(tmp_path, "notebooks/a.py")
    monkeypatch.setattr(workspace_config, "guard_mode", lambda ws_: "block")

    guard_fn, collected, lock_collected, mode, acknowledged = cli._push_guard_fn(cfg, True)

    assert mode == "block"
    assert guard_fn("uv.lock", LOCK.encode()) == []  # the deps gate stays acknowledgeable
    assert set(acknowledged) == {"uv.lock"}


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
        assert client.get("/api/sweep/plan").json() == {"total": 2}
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
