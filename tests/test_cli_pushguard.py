"""CLI surface of the push guard: `scan`, guarded `push`, and `recall`.

The GitHub client is faked (offline); the guard runs for real over the
workspace files, so these cover the wiring end to end.
"""

import pytest
from conftest import FakeClient

from mooring import cli, paths

SECRETY = 'TOKEN = "ghp_' + "a" * 40 + '"\n'


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    ws = tmp_path / "ws"
    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")
    monkeypatch.setenv("MOORING_OWNER", "acme")
    monkeypatch.setenv("MOORING_REPO", "nbs")
    monkeypatch.setenv("MOORING_WORKSPACE", str(ws))
    monkeypatch.setenv("MOORING_TRUSTSTORE", "0")
    for var in ("MOORING_BRANCH", "MOORING_ACTIVE_REPO", "MOORING_GITHUB_HOST", "MOORING_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return ws


def _fake_client(monkeypatch, files=None):
    fake = FakeClient(files or {})
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    return fake


def _write(ws, rel, text):
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8", newline="\n")


def test_scan_reports_findings_and_exit_code(workspace, monkeypatch, capsys):
    _fake_client(monkeypatch)
    _write(workspace, "notebooks/leaky.py", SECRETY)
    assert cli.main(["scan"]) == 1
    out = capsys.readouterr().out
    assert "notebooks/leaky.py:1  GitHub token" in out
    assert "ghp_" + "a" * 40 not in out  # findings stay value-free


def test_scan_clean_exit_zero(workspace, monkeypatch, capsys):
    _fake_client(monkeypatch)
    _write(workspace, "notebooks/clean.py", "x = 1\n")
    assert cli.main(["scan"]) == 0
    assert "No findings" in capsys.readouterr().out


def test_push_withholds_then_acknowledge_pushes(workspace, monkeypatch, capsys):
    fake = _fake_client(monkeypatch)
    _write(workspace, "notebooks/leaky.py", SECRETY)
    assert cli.main(["push"]) == 1
    out = capsys.readouterr().out
    assert "withheld notebooks/leaky.py" in out
    assert "--acknowledge-findings" in out
    assert "notebooks/leaky.py" not in fake.tree
    assert cli.main(["push", "--acknowledge-findings"]) == 0
    assert "notebooks/leaky.py" in fake.tree


def test_block_mode_refuses_acknowledge(workspace, monkeypatch, capsys):
    fake = _fake_client(monkeypatch)
    _write(workspace, "mooring.toml", '[guard]\npush = "block"\n')
    _write(workspace, "notebooks/leaky.py", SECRETY)
    assert cli.main(["push", "--acknowledge-findings"]) == 1
    assert "notebooks/leaky.py" not in fake.tree
    assert "block" in capsys.readouterr().out


def test_recall_round_trip(workspace, monkeypatch, capsys):
    fake = _fake_client(monkeypatch, {"notebooks/a.py": b"v1\n"})
    assert cli.main(["pull"]) == 0
    _write(workspace, "notebooks/a.py", "v2\n")
    assert cli.main(["push"]) == 0
    assert cli.main(["recall", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "recalled notebooks/a.py" in out
    assert "history" in out  # the honest note
    assert fake.get_blob(fake.tree["notebooks/a.py"]) == b"v1\n"
