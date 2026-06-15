"""CLI `delete` command — local-only, needs no GitHub login."""

import pytest

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
    for var in ("MOORING_BRANCH", "MOORING_ACTIVE_REPO", "MOORING_GITHUB_HOST"):
        monkeypatch.delenv(var, raising=False)
    return ws


def write(ws, rel, text="x"):
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8")


def test_delete_with_yes_removes_file(workspace, capsys):
    write(workspace, "notebooks/a.py")
    assert cli.main(["delete", "notebooks/a.py", "--yes"]) == 0
    assert not (workspace / "notebooks/a.py").exists()
    assert "deleted notebooks/a.py" in capsys.readouterr().out


def test_delete_refuses_without_confirmation_when_non_interactive(workspace, monkeypatch):
    write(workspace, "notebooks/a.py")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["delete", "notebooks/a.py"])
    assert "Re-run with --yes" in str(exc.value)
    assert (workspace / "notebooks/a.py").exists()


def test_delete_interactive_prompt_yes(workspace, monkeypatch):
    write(workspace, "notebooks/a.py")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert cli.main(["delete", "notebooks/a.py"]) == 0
    assert not (workspace / "notebooks/a.py").exists()


def test_delete_interactive_prompt_no_cancels(workspace, monkeypatch, capsys):
    write(workspace, "notebooks/a.py")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert cli.main(["delete", "notebooks/a.py"]) == 0
    assert (workspace / "notebooks/a.py").exists()
    assert "Cancelled" in capsys.readouterr().out


def test_delete_unknown_path_exits(workspace):
    with pytest.raises(SystemExit) as exc:
        cli.main(["delete", "notebooks/nope.py", "--yes"])
    assert "No such notebook" in str(exc.value)


def test_delete_pbip_project(workspace):
    write(workspace, "reports/Sales.pbip", "{}")
    write(workspace, "reports/Sales.SemanticModel/model.tmdl", "m")
    assert cli.main(["delete", "reports/Sales.pbip", "--yes"]) == 0
    assert not (workspace / "reports/Sales.pbip").exists()
    assert not (workspace / "reports/Sales.SemanticModel").exists()
