"""CLI ``mooring run <path> --for NAME=VALUES`` — the attended fan-out.

``run`` is deliberately its OWN command rather than a ``refresh --for`` flag: ``refresh`` is
bound to the schedule model (it pulls, it records against a cadence, its exit codes describe
a cadence's states, and it is designed to happen unattended). These tests pin the things a
wrapper script depends on — the exit codes, the ``--json`` shape, and that an INCOMPLETE
pack never reports success.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mooring import cli, params, paths
from mooring.app import notebook_run

REL = "notebooks/board.py"
NOTEBOOK = (
    "import marimo\n\n"
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    import mooring_params\n"
    '    region = mooring_params.get("region", "EMEA")\n'
    "    return\n"
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    ws = tmp_path / "ws"
    (ws / REL).parent.mkdir(parents=True, exist_ok=True)
    (ws / REL).write_text(NOTEBOOK, encoding="utf-8")
    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")
    monkeypatch.setenv("MOORING_OWNER", "acme")
    monkeypatch.setenv("MOORING_REPO", "nbs")
    monkeypatch.setenv("MOORING_WORKSPACE", str(ws))
    monkeypatch.setenv("MOORING_TRUSTSTORE", "0")
    for var in ("MOORING_BRANCH", "MOORING_ACTIVE_REPO", "MOORING_GITHUB_HOST", "MOORING_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return ws


def _out_of(cmd):
    for i, tok in enumerate(cmd):
        if tok == "-o":
            return Path(cmd[i + 1])
    return None


def _fake_exec(monkeypatch, *, fails=()):
    # A failing value emits the marker marimo prints per failed cell, plus a data value in
    # the same line — which is exactly what must NOT reach the report.
    stderr = "MarimoExceptionRaisedError: 'SECRET_VALUE_DO_NOT_LEAK'\n"

    def _run(cmd, cwd, env, timeout, cancel=None):
        raw = (env or {}).get(params.ENV_VAR)
        value = next(iter(json.loads(raw).values())) if raw else None
        out = _out_of(cmd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"<html><body>{value}</body></html>", encoding="utf-8")
        failing = value in fails
        return subprocess.CompletedProcess(cmd, 1 if failing else 0, "", stderr if failing else "")

    monkeypatch.setattr(notebook_run, "_exec", _run)


def test_a_clean_fan_out_exits_zero_and_names_every_artifact(workspace, monkeypatch, capsys):
    _fake_exec(monkeypatch)
    assert cli.main(["run", REL, "--for", "region=EMEA,APAC"]) == 0
    out = capsys.readouterr().out
    assert "EMEA — ran clean" in out and "APAC — ran clean" in out
    assert out.count(".mooring/outbox") >= 2
    assert "INCOMPLETE" not in out


def test_one_failing_value_exits_one_and_the_pack_reads_incomplete(
    workspace, monkeypatch, capsys
):
    _fake_exec(monkeypatch, fails={"APAC"})
    assert cli.main(["run", REL, "--for", "region=EMEA,APAC,AMER"]) == 1
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out and "2 of 3" in out
    assert "AMER — ran clean" in out  # the failure did not stop the rest


def test_json_output_is_machine_readable_and_value_free_of_stderr(
    workspace, monkeypatch, capsys
):
    _fake_exec(monkeypatch, fails={"APAC"})
    assert cli.main(["run", REL, "--for", "region=EMEA,APAC", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["notebook"] == REL and data["param"] == "region"
    assert data["values"] == ["EMEA", "APAC"] and data["complete"] is False
    assert [r["outcome"] for r in data["runs"]] == ["ok", "failed"]
    # The failed-cell COUNT survives; marimo's stderr text never does.
    assert data["runs"][1]["reason"] == "1 cell failed to run"
    assert data["runs"][1]["cells_failed"] == 1
    assert "SECRET" not in json.dumps(data)


def test_no_deliver_runs_every_value_and_writes_nothing(workspace, monkeypatch, capsys):
    from mooring.app import deliver

    _fake_exec(monkeypatch)
    assert cli.main(["run", REL, "--for", "region=EMEA,APAC", "--no-deliver"]) == 0
    assert not deliver.outbox_dir(workspace).exists()


def test_ctrl_c_exits_4_and_reports_the_partial_pack(workspace, monkeypatch, capsys):
    # It exited 1 with no report, which is the "did not run" code a wrapper script branches
    # on — and printed nothing at all under --json.
    killed = []
    monkeypatch.setattr(notebook_run, "_kill_tree", lambda proc: killed.append(proc))

    class _Proc:
        pid = 31337
        returncode = 1

        def communicate(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(notebook_run.subprocess, "Popen", lambda *a, **k: _Proc())

    assert cli.main(["run", REL, "--for", "region=EMEA,APAC,AMER"]) == 4
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out
    assert killed  # the marimo tree was actually torn down


def test_ctrl_c_still_emits_json(workspace, monkeypatch, capsys):
    monkeypatch.setattr(notebook_run, "_kill_tree", lambda proc: None)

    class _Proc:
        pid = 1
        returncode = 1

        def communicate(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(notebook_run.subprocess, "Popen", lambda *a, **k: _Proc())

    assert cli.main(["run", REL, "--for", "region=EMEA,APAC", "--json"]) == 4
    data = json.loads(capsys.readouterr().out)
    assert data["complete"] is False and data["cancelled"] is True
    assert [r["outcome"] for r in data["runs"]] == ["cancelled", "skipped"]


def test_a_bad_spec_exits_with_the_curated_reason(workspace, monkeypatch, capsys):
    _fake_exec(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", REL, "--for", "region"])
    assert "NAME=VALUES" in str(exc.value)


def test_a_notebook_that_ignores_the_parameter_exits_with_the_fix(workspace, monkeypatch):
    _fake_exec(monkeypatch)
    (workspace / "plain.py").write_text(
        "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "plain.py", "--for", "region=EMEA,APAC"])
    assert "mooring_params" in str(exc.value)


def test_for_is_required(workspace):
    with pytest.raises(SystemExit):
        cli.main(["run", REL])
