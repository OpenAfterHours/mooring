"""CLI `history` / `restore` — the time machine for support calls."""

import pytest
from conftest import FakeClient

from mooring import cli, paths


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


def _with_history(monkeypatch, workspace):
    fake = FakeClient({"notebooks/a.py": b"v1\n"})
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    assert cli.main(["pull"]) == 0
    old_head = fake.head
    (workspace / "notebooks/a.py").write_text("v2\n", "utf-8", newline="\n")
    assert cli.main(["push"]) == 0
    return fake, old_head


def test_history_prints_versions(workspace, monkeypatch, capsys):
    _with_history(monkeypatch, workspace)
    assert cli.main(["history", "notebooks/a.py"]) == 0
    out = capsys.readouterr().out
    assert "Update notebooks/a.py" in out
    assert "Seed notebooks/a.py" in out
    assert "mooring restore notebooks/a.py --at" in out


def test_restore_copy_writes_sibling(workspace, monkeypatch, capsys):
    _, old_head = _with_history(monkeypatch, workspace)
    assert cli.main(["restore", "notebooks/a.py", "--at", old_head, "--copy"]) == 0
    copy = workspace / f"notebooks/a.restored-{old_head[:7]}.py"
    assert copy.read_text("utf-8") == "v1\n"
    assert (workspace / "notebooks/a.py").read_text("utf-8") == "v2\n"  # untouched


def test_restore_over_refuses_non_interactive_without_yes(workspace, monkeypatch):
    _, old_head = _with_history(monkeypatch, workspace)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["restore", "notebooks/a.py", "--at", old_head])
    assert "--yes" in str(exc.value)
    assert (workspace / "notebooks/a.py").read_text("utf-8") == "v2\n"


def test_restore_over_with_yes_overwrites(workspace, monkeypatch, capsys):
    _, old_head = _with_history(monkeypatch, workspace)
    assert cli.main(["restore", "notebooks/a.py", "--at", old_head, "--yes"]) == 0
    assert (workspace / "notebooks/a.py").read_text("utf-8") == "v1\n"
    assert "restored notebooks/a.py" in capsys.readouterr().out


def test_restore_unknown_version_exits_nonzero(workspace, monkeypatch, capsys):
    _with_history(monkeypatch, workspace)
    assert cli.main(["restore", "notebooks/a.py", "--at", "head-999", "--yes"]) == 1
    assert "no version at" in capsys.readouterr().out