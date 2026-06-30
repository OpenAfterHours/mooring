"""marimo's own built-in AI must be disabled in every editor mooring spawns.

These inspect the workspace config the editor writes; no marimo subprocess runs.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from mooring.editor import EditorServer


def _read(ws):
    return tomllib.loads((ws / ".marimo.toml").read_text("utf-8"))


def test_disables_ai_and_enables_autorun(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    EditorServer(ws)._ensure_marimo_config()
    data = _read(ws)
    assert data["ai"]["enabled"] is False
    assert data["completion"]["copilot"] is False
    assert data["runtime"]["watcher_on_save"] == "autorun"  # applied cells run
    assert data["display"]["theme"] == "system"  # default appearance written


def test_writes_the_configured_theme(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    EditorServer(ws, theme="dark")._ensure_marimo_config()
    assert _read(ws)["display"]["theme"] == "dark"  # mooring owns the notebook theme


def test_merges_without_clobbering_unrelated_settings(tmp_path):
    # mooring now OWNS display.theme (the hub is the single control point), but
    # must still preserve marimo settings it does not manage.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".marimo.toml").write_text(
        '[display]\ntheme = "light"\ncode_editor_font_size = 20\n\n[ai]\nenabled = true\n',
        encoding="utf-8",
    )
    EditorServer(ws, theme="dark")._ensure_marimo_config()
    data = _read(ws)
    assert data["display"]["code_editor_font_size"] == 20  # unrelated key preserved
    assert data["display"]["theme"] == "dark"  # overridden by the hub's theme
    assert data["ai"]["enabled"] is False  # forced off


def test_apply_theme_rewrites_existing_config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    editor = EditorServer(ws, theme="light")
    editor._ensure_marimo_config()
    assert _read(ws)["display"]["theme"] == "light"
    editor.apply_theme("dark")  # the hub switched the toggle while running
    assert editor.theme == "dark"
    assert _read(ws)["display"]["theme"] == "dark"


def test_writes_workspace_root_to_pythonpath(tmp_path):
    # The workspace root goes on the notebook kernel's sys.path so a notebook in any
    # sub-folder can import the repo's helper modules (`from lib import helpers`). It
    # must be ABSOLUTE — marimo doesn't resolve a .marimo.toml pythonpath entry.
    ws = tmp_path / "ws"
    ws.mkdir()
    EditorServer(ws)._ensure_marimo_config()
    assert _read(ws)["runtime"]["pythonpath"] == [str(ws.resolve())]


def test_pythonpath_is_absolute_for_a_relative_workspace(tmp_path, monkeypatch):
    # A relative workspace path must still produce an ABSOLUTE pythonpath entry, or the
    # kernel's relative sys.path entry would resolve against the wrong cwd.
    monkeypatch.chdir(tmp_path)
    ws = Path("relws")
    ws.mkdir()
    EditorServer(ws)._ensure_marimo_config()
    entry = _read(ws)["runtime"]["pythonpath"][0]
    assert Path(entry).is_absolute()
    assert Path(entry) == (tmp_path / "relws").resolve()


def test_pythonpath_preserves_existing_entries_with_root_first(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".marimo.toml").write_text(
        '[runtime]\npythonpath = ["/some/other/dir"]\n', encoding="utf-8"
    )
    EditorServer(ws)._ensure_marimo_config()
    assert _read(ws)["runtime"]["pythonpath"] == [str(ws.resolve()), "/some/other/dir"]


def test_idempotent(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    editor = EditorServer(ws)
    editor._ensure_marimo_config()
    before = (ws / ".marimo.toml").read_text("utf-8")
    editor._ensure_marimo_config()  # second call is a no-op rewrite
    assert (ws / ".marimo.toml").read_text("utf-8") == before


def test_survives_a_malformed_existing_config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".marimo.toml").write_text("this is not valid toml = = =", encoding="utf-8")
    # best-effort: must not raise
    EditorServer(ws)._ensure_marimo_config()


def test_invocation_includes_watch(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    editor = EditorServer(ws)
    editor.port = 12345
    cmd, _env = editor._invocation()
    assert "--watch" in cmd
