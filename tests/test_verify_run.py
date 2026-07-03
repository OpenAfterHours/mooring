"""The verify runner: run a notebook, keep only a value-free receipt, delete the render.

The marimo export subprocess is faked (a real one spawns a kernel); these pin the
pass/fail-from-exit-code contract, the value-free failed-cell count, that the
value-bearing HTML render is deleted, and the activity receipt.
"""

from __future__ import annotations

import subprocess

import pytest

from mooring import activity, verify
from mooring.app import verify_run
from mooring.config import Config

NOTEBOOK = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"


def _cfg(tmp_path):
    return Config(client_id="cid", owner="acme", repo="nbs", workspace_path=str(tmp_path / "ws"))


def _fake_run(returncode, stderr="", *, write_html=True):
    """Stand in for `marimo export html`: optionally write an HTML at the `-o` target
    (marimo writes one even on a cell failure) and return the given exit code."""

    def _run(cmd, **kwargs):
        if write_html:
            out = None
            for i, tok in enumerate(cmd):
                if tok == "-o":
                    out = cmd[i + 1]
                    break
            if out is not None:
                from pathlib import Path

                path = Path(out)
                path.parent.mkdir(parents=True, exist_ok=True)
                # The real render embeds data values; we plant one to prove it's deleted.
                path.write_text("<html>SECRET_VALUE_DO_NOT_LEAK</html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    return _run


def test_clean_run_records_a_passing_receipt(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    (ws / "notebooks").mkdir(parents=True)
    (ws / "notebooks" / "sales.py").write_text(NOTEBOOK, encoding="utf-8")
    monkeypatch.setattr(verify_run.subprocess, "run", _fake_run(0))

    result = verify_run.verify_notebook(cfg, "notebooks/sales.py")

    assert result.passed is True
    assert result.cells_failed is None
    assert verify.read_results(ws)["notebooks/sales.py"]["passed"] is True


def test_failing_run_counts_cells_value_free(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")
    # stderr quotes a value in the message — we must count markers, never store the text.
    stderr = (
        "MarimoExceptionRaisedError: division by zero\n"
        "MarimoExceptionRaisedError: 'SECRET_VALUE_DO_NOT_LEAK'\n"
        "Error: Export was successful, but some cells failed to execute.\n"
    )
    monkeypatch.setattr(verify_run.subprocess, "run", _fake_run(1, stderr))

    result = verify_run.verify_notebook(cfg, "sales.py")

    assert result.passed is False
    assert result.cells_failed == 2
    receipt = verify.read_results(ws)["sales.py"]
    assert receipt["passed"] is False
    assert receipt["cells_failed"] == 2


def test_the_value_bearing_render_is_deleted(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")
    monkeypatch.setattr(verify_run.subprocess, "run", _fake_run(1, "boom"))

    verify_run.verify_notebook(cfg, "sales.py")

    # The rendered HTML embeds real values; it must not survive the run on disk.
    assert not verify.render_target(ws, "sales.py").is_file()
    # And no leftover .html anywhere under the verify dir.
    assert list(verify.verify_dir(ws).glob("*.html")) == []


def test_nonzero_exit_without_markers_is_unknown_not_zero(tmp_path, monkeypatch):
    # A module-level import/syntax error fails before any cell runs — 0 markers. Report
    # "unknown" (None), never "0 cells failed" (which would read as a clean run).
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")
    monkeypatch.setattr(verify_run.subprocess, "run", _fake_run(1, "ModuleNotFoundError: no pandas"))

    result = verify_run.verify_notebook(cfg, "sales.py")
    assert result.passed is False
    assert result.cells_failed is None


def test_records_a_value_free_activity_entry(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")
    monkeypatch.setattr(verify_run.subprocess, "run", _fake_run(0))

    verify_run.verify_notebook(cfg, "sales.py")

    entries = activity.read(ws)
    assert entries and entries[0]["op"] == "verify"
    assert entries[0]["path"] == "sales.py"


def test_refuses_a_plain_module(tmp_path):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "helpers.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    with pytest.raises(verify_run.VerifyError):
        verify_run.verify_notebook(cfg, "helpers.py")


def test_timeout_surfaces_verify_error_and_cleans_up(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "sales.py").write_text(NOTEBOOK, encoding="utf-8")

    def _slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(verify_run.subprocess, "run", _slow)
    with pytest.raises(verify_run.VerifyError):
        verify_run.verify_notebook(cfg, "sales.py")
    assert not verify.render_target(ws, "sales.py").is_file()


def test_rejects_a_path_escaping_the_workspace(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.workspace().mkdir(parents=True)
    with pytest.raises(ValueError):
        verify_run.verify_notebook(cfg, "../secret.py")
