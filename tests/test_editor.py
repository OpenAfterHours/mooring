"""Editor launch-backend selection (frozen bundle vs the team's uv project)."""

from mooring import pyproject_env as pe
from mooring.editor import EditorServer


def _server(workspace):
    srv = EditorServer(workspace)
    srv.port = 9999
    return srv


def _write_pyproject(path, deps):
    body = ", ".join(f'"{d}"' for d in deps)
    pe.pyproject_path(path).write_text(
        f'[project]\nname = "x"\nversion = "0"\ndependencies = [{body}]\n',
        encoding="utf-8",
    )


def test_frozen_path_without_pyproject(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "uv_available", lambda: True)
    monkeypatch.delenv("MOORING_FORCE_FROZEN", raising=False)
    srv = _server(tmp_path)
    assert srv.use_uv() is False  # uv present but no repo pyproject
    cmd, env = srv._invocation()
    assert cmd[1:3] == ["-m", "marimo"]
    assert env is None


def test_uv_path_with_lock_and_marimo_strips_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "uv_available", lambda: True)
    monkeypatch.delenv("MOORING_FORCE_FROZEN", raising=False)
    monkeypatch.setenv("PYTHONPATH", "/bundled/site-packages")
    _write_pyproject(tmp_path, ["marimo>=0.13"])
    pe.lock_path(tmp_path).write_text("version = 1\n", encoding="utf-8")
    srv = _server(tmp_path)
    assert srv.use_uv() is True
    cmd, env = srv._invocation()
    assert cmd[:2] == ["uv", "run"]
    assert "--frozen" in cmd  # lock present
    assert "--project" in cmd
    assert "edit" in cmd
    assert "--with" not in cmd  # marimo is declared
    assert env is not None and "PYTHONPATH" not in env


def test_uv_path_without_lock_or_marimo_adds_safety_net(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "uv_available", lambda: True)
    monkeypatch.delenv("MOORING_FORCE_FROZEN", raising=False)
    _write_pyproject(tmp_path, ["polars"])  # marimo not declared, no lock file
    srv = _server(tmp_path)
    cmd, _ = srv._invocation()
    assert "--frozen" not in cmd
    assert cmd[cmd.index("--with") + 1] == "marimo"


def test_force_frozen_overrides_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "uv_available", lambda: True)
    _write_pyproject(tmp_path, ["marimo>=0.13"])
    monkeypatch.setenv("MOORING_FORCE_FROZEN", "1")
    srv = _server(tmp_path)
    assert srv.use_uv() is False
    cmd, env = srv._invocation()
    assert cmd[1:3] == ["-m", "marimo"]
    assert env is None
