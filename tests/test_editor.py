"""Editor launch-backend selection (frozen bundle vs the team's uv project)."""

import socket
import subprocess

import pytest

from mooring import editor as editor_mod
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


def _capture_spawn(monkeypatch):
    """Stub out everything ensure_started() does except the Popen call, returning a
    dict that records the kwargs marimo was spawned with."""
    captured: dict = {}

    class _FakeProc:
        def poll(self):
            return None

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(editor_mod.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(EditorServer, "_wait_ready", lambda self: None)
    monkeypatch.setattr(EditorServer, "_ensure_marimo_config", lambda self: None)
    return captured


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"), reason="Windows-only flag")
def test_windows_spawns_marimo_in_new_process_group(tmp_path, monkeypatch):
    # The Ctrl+C fix: marimo must launch in its own process group so a console Ctrl+C
    # isn't broadcast to it (and its kernel children) alongside mooring.
    captured = _capture_spawn(monkeypatch)
    monkeypatch.setattr(editor_mod.sys, "platform", "win32")
    EditorServer(tmp_path).ensure_started()
    flags = captured["kwargs"].get("creationflags", 0)
    flag = subprocess.CREATE_NEW_PROCESS_GROUP  # ty: ignore[unresolved-attribute]  # Windows-only
    assert flags & flag


def test_posix_spawns_marimo_without_creationflags(tmp_path, monkeypatch):
    captured = _capture_spawn(monkeypatch)
    monkeypatch.setattr(editor_mod.sys, "platform", "linux")
    EditorServer(tmp_path).ensure_started()
    assert "creationflags" not in captured["kwargs"]


def test_bind_or_free_prefers_the_requested_port():
    # A free port stays free between free_port() closing its probe and bind_or_free
    # rebinding it, so the hub gets the SAME stable origin each launch — which is
    # what keeps the browser's per-origin localStorage (checklist, watch set) alive.
    preferred = editor_mod.free_port()
    assert editor_mod.bind_or_free(preferred) == preferred


def test_bind_or_free_falls_back_when_port_taken():
    # Hold a port open so the preferred bind fails and bind_or_free must yield a
    # different, genuinely-free port rather than crashing the launch.
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        got = editor_mod.bind_or_free(taken)
        assert got != taken
    with socket.socket() as check:
        check.bind(("127.0.0.1", got))  # the fallback port is real and bindable
