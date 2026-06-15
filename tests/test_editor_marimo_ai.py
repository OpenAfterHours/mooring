"""marimo's own built-in AI must be disabled in every editor mooring spawns.

These inspect the workspace config the editor writes; no marimo subprocess runs.
"""

from __future__ import annotations

import tomllib

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


def test_merges_without_clobbering_existing_settings(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".marimo.toml").write_text(
        '[display]\ntheme = "dark"\n\n[ai]\nenabled = true\n', encoding="utf-8"
    )
    EditorServer(ws)._ensure_marimo_config()
    data = _read(ws)
    assert data["display"]["theme"] == "dark"  # preserved
    assert data["ai"]["enabled"] is False  # forced off


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
