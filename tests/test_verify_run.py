"""The verify runner: run a notebook, keep only a value-free receipt, delete the render.

The marimo export subprocess is faked at the ``_run_export`` seam (a real one spawns a
kernel); these pin the pass/fail-from-exit-code-AND-render contract, the value-free
failed-cell count, that the value-bearing HTML is deleted, the environment-vs-notebook
attribution, the SHA-before-run fail-safe, and the activity receipt.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from mooring import activity, cli, gitsha, verify
from mooring.app import verify_run
from mooring.config import Config

NOTEBOOK = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"


def _cfg(tmp_path):
    return Config(client_id="cid", owner="acme", repo="nbs", workspace_path=str(tmp_path / "ws"))


def _out_of(cmd):
    for i, tok in enumerate(cmd):
        if tok == "-o":
            return Path(cmd[i + 1])
    return None


def _fake_export(returncode, stderr="", *, produce=True, before=None):
    """Stand in for `_run_export`: write an HTML at the `-o` target (marimo writes one
    whenever it actually runs — even on a cell failure) unless ``produce`` is False (an
    environment failure where marimo never starts), and return the given exit code.
    ``before`` runs first, to simulate an edit landing mid-run."""

    def _run(cmd, cwd, env):
        if before is not None:
            before()
        if produce:
            out = _out_of(cmd)
            out.parent.mkdir(parents=True, exist_ok=True)
            # The real render embeds data values; plant one to prove it's deleted.
            out.write_text("<html>SECRET_VALUE_DO_NOT_LEAK</html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return _run


def _mk(tmp_path, rel="sales.py", src=NOTEBOOK):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    (ws / rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / rel).write_text(src, encoding="utf-8")
    return cfg, ws


def test_clean_run_records_a_passing_receipt(tmp_path, monkeypatch):
    cfg, ws = _mk(tmp_path, "notebooks/sales.py")
    monkeypatch.setattr(verify_run, "_run_export", _fake_export(0))

    result = verify_run.verify_notebook(cfg, "notebooks/sales.py")

    assert result.passed is True
    assert result.cells_failed is None
    assert verify.read_results(ws)["notebooks/sales.py"]["passed"] is True


def test_failing_run_counts_cells_value_free(tmp_path, monkeypatch):
    cfg, ws = _mk(tmp_path)
    # stderr quotes a value in the message — we must count markers, never store the text.
    stderr = (
        "MarimoExceptionRaisedError: division by zero\n"
        "MarimoExceptionRaisedError: 'SECRET_VALUE_DO_NOT_LEAK'\n"
        "Error: Export was successful, but some cells failed to execute.\n"
    )
    monkeypatch.setattr(verify_run, "_run_export", _fake_export(1, stderr))

    result = verify_run.verify_notebook(cfg, "sales.py")

    assert result.passed is False
    assert result.cells_failed == 2
    receipt = verify.read_results(ws)["sales.py"]
    assert receipt["passed"] is False
    assert receipt["cells_failed"] == 2


def test_marker_count_is_line_anchored(tmp_path, monkeypatch):
    # A cell that PRINTS the marker token mid-line must not inflate the failed-cell
    # count — only lines that START with the marker are real marimo cell failures.
    cfg, ws = _mk(tmp_path)
    stderr = (
        "MarimoExceptionRaisedError: boom\n"
        "some cell printed: look a MarimoExceptionRaisedError in my logs\n"
    )
    monkeypatch.setattr(verify_run, "_run_export", _fake_export(1, stderr))

    result = verify_run.verify_notebook(cfg, "sales.py")
    assert result.cells_failed == 1


def test_the_value_bearing_render_is_deleted(tmp_path, monkeypatch):
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(verify_run, "_run_export", _fake_export(1, "boom"))

    verify_run.verify_notebook(cfg, "sales.py")

    # The rendered HTML embeds real values; it must not survive the run on disk.
    assert not verify.render_target(ws, "sales.py").is_file()
    assert list(verify.verify_dir(ws).glob("*.html")) == []


def test_environment_failure_is_not_blamed_on_the_notebook(tmp_path, monkeypatch):
    # marimo never produced a render (e.g. a stale uv.lock failed dependency resolution
    # before the notebook ran). That's an environment fault, not the notebook's — it
    # must surface as a VerifyError, NOT a recorded "failed to run" receipt.
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(verify_run, "_run_export", _fake_export(1, "no such dependency", produce=False))

    with pytest.raises(verify_run.VerifyError):
        verify_run.verify_notebook(cfg, "sales.py")
    assert verify.read_results(ws) == {}  # nothing recorded


def test_nonzero_exit_with_render_but_no_markers_is_unknown_not_zero(tmp_path, monkeypatch):
    # A module-level error DOES still produce a render (marimo ran) but emits no per-cell
    # markers — report "unknown" (None), never "0 cells failed" (which reads as clean).
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(verify_run, "_run_export", _fake_export(1, "SyntaxError: bad"))

    result = verify_run.verify_notebook(cfg, "sales.py")
    assert result.passed is False
    assert result.cells_failed is None


def test_receipt_keys_to_pre_run_bytes_so_a_mid_run_edit_auto_clears(tmp_path, monkeypatch):
    # THE trust-integrity guard (the review's headline finding): the SHA is captured
    # BEFORE the run, so an edit saved while marimo is running keys the receipt to bytes
    # that no longer match the file — read_results drops it (fail-safe), never a green
    # badge over code the run never executed.
    cfg, ws = _mk(tmp_path)

    def _edit_midrun():
        (ws / "sales.py").write_text(NOTEBOOK + "\n# edited mid-run\n", encoding="utf-8")

    monkeypatch.setattr(verify_run, "_run_export", _fake_export(0, before=_edit_midrun))
    result = verify_run.verify_notebook(cfg, "sales.py")

    assert result.passed is True  # the run itself passed (on the old bytes)
    # ...but the badge does NOT show, because the file changed out from under it.
    assert verify.read_results(ws) == {}


def test_records_a_value_free_activity_entry(tmp_path, monkeypatch):
    cfg, ws = _mk(tmp_path)
    monkeypatch.setattr(verify_run, "_run_export", _fake_export(0))

    verify_run.verify_notebook(cfg, "sales.py")

    entries = activity.read(ws)
    assert entries and entries[0]["op"] == "verify"
    assert entries[0]["path"] == "sales.py"


def test_refuses_a_plain_module(tmp_path):
    cfg, ws = _mk(tmp_path, "helpers.py", "def f():\n    return 1\n")
    with pytest.raises(verify_run.VerifyError):
        verify_run.verify_notebook(cfg, "helpers.py")


def test_timeout_surfaces_verify_error_and_cleans_up(tmp_path, monkeypatch):
    cfg, ws = _mk(tmp_path)

    def _slow(cmd, cwd, env):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(verify_run, "_run_export", _slow)
    with pytest.raises(verify_run.VerifyError):
        verify_run.verify_notebook(cfg, "sales.py")
    assert not verify.render_target(ws, "sales.py").is_file()


def test_launch_oserror_surfaces_verify_error(tmp_path, monkeypatch):
    # A locked/read-only dir or a bad executable raises a non-FileNotFoundError OSError;
    # it must become a clean VerifyError, not an unhandled 500/traceback.
    cfg, ws = _mk(tmp_path)

    def _boom(cmd, cwd, env):
        raise PermissionError("access denied")

    monkeypatch.setattr(verify_run, "_run_export", _boom)
    with pytest.raises(verify_run.VerifyError):
        verify_run.verify_notebook(cfg, "sales.py")


def test_rejects_a_path_escaping_the_workspace(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.workspace().mkdir(parents=True)
    with pytest.raises(ValueError):
        verify_run.verify_notebook(cfg, "../secret.py")


def test_describe_result_wording():
    R = verify_run.VerifyResult
    assert "ran clean" in verify_run.describe_result(R("a.py", True, None, "t"))
    assert "1 cell failed" in verify_run.describe_result(R("a.py", False, 1, "t"))
    assert "2 cells failed" in verify_run.describe_result(R("a.py", False, 2, "t"))
    assert "failed to run" in verify_run.describe_result(R("a.py", False, None, "t"))


def test_cli_verify_clear_removes_receipts(tmp_path):
    # The `--clear` wiring makes verify.clear reachable from the product (the review
    # flagged it as otherwise dead code). A bare `--clear` clears everything.
    cfg, ws = _mk(tmp_path)
    sha = gitsha.local_blob_sha(ws / "sales.py", "sales.py")
    verify.record(ws, "sales.py", passed=True, sha=sha, cells_failed=None, ran_at="t")
    assert verify.read_results(ws)  # present

    rc = cli.cmd_verify(cfg, argparse.Namespace(path=None, clear=True))
    assert rc == 0
    assert verify.read_results(ws) == {}


def test_cli_verify_requires_a_path_when_not_clearing(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.workspace().mkdir(parents=True)
    with pytest.raises(SystemExit):
        cli.cmd_verify(cfg, argparse.Namespace(path=None, clear=False))
