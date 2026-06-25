"""CLI `init` / `deps` / `build-requirements` — local, need no GitHub login."""

import pytest

from mooring import cli, paths
from mooring import pyproject_env as pe


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    ws = tmp_path / "ws"
    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")
    monkeypatch.setenv("MOORING_OWNER", "acme")
    monkeypatch.setenv("MOORING_REPO", "nbs")
    monkeypatch.setenv("MOORING_WORKSPACE", str(ws))
    monkeypatch.setenv("MOORING_TRUSTSTORE", "0")
    for var in (
        "MOORING_BRANCH",
        "MOORING_ACTIVE_REPO",
        "MOORING_GITHUB_HOST",
        "MOORING_FORCE_FROZEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return ws


def test_init_creates_minimal_pyproject(workspace, monkeypatch, capsys):
    monkeypatch.setattr(pe, "uv_available", lambda: False)  # skip the real lock
    assert cli.main(["init"]) == 0
    text = (workspace / "pyproject.toml").read_text(encoding="utf-8")
    assert "marimo>=0.23.9" in text
    assert "polars" not in text  # lean seed
    assert "Created" in capsys.readouterr().out


def test_init_is_idempotent(workspace, monkeypatch, capsys):
    monkeypatch.setattr(pe, "uv_available", lambda: False)
    cli.main(["init"])
    capsys.readouterr()
    assert cli.main(["init"]) == 0
    assert "already exists" in capsys.readouterr().out


def test_deps_list_reports_availability(workspace, monkeypatch, capsys):
    workspace.mkdir(parents=True, exist_ok=True)
    pe.pyproject_path(workspace).write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["marimo", "scipy"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(pe, "_is_installed", lambda n: n == "marimo")
    assert cli.main(["deps", "list"]) == 0
    out = capsys.readouterr().out
    assert "ok" in out and "missing" in out and "scipy" in out


def test_deps_add_without_uv_errors(workspace, monkeypatch):
    monkeypatch.setattr(pe, "uv_available", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["deps", "add", "polars"])
    assert "uv" in str(exc.value).lower()


def test_build_requirements_without_pyproject_errors(workspace):
    with pytest.raises(SystemExit) as exc:
        cli.main(["build-requirements"])
    assert "init" in str(exc.value)
