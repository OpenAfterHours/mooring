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


# -- [ai] apply_runs: does an applied cell run, or arrive staged? -------------
#
# marimo types runtime.watcher_on_save as Literal["lazy", "autorun"] and only issues
# run ids on "autorun" (see marimo._session.file_change_handler), so "lazy" is THE
# value that means "reload the cell, mark it stale, run nothing".


def test_apply_runs_false_stages_the_cell_instead_of_running_it(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    EditorServer(ws, apply_runs=False)._ensure_marimo_config()
    data = _read(ws)
    assert data["runtime"]["watcher_on_save"] == "lazy"
    # ...and the value-blindness guarantee is untouched by the run mode.
    assert data["ai"]["enabled"] is False
    assert data["completion"]["copilot"] is False


def test_apply_runs_true_is_todays_autorun(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    EditorServer(ws, apply_runs=True)._ensure_marimo_config()
    data = _read(ws)
    assert data["runtime"]["watcher_on_save"] == "autorun"
    assert data["ai"]["enabled"] is False
    assert data["completion"]["copilot"] is False


def test_idempotent_in_lazy_mode(tmp_path):
    """The `already` short-circuit compares against the mode ACTUALLY wanted. Hard-coding
    "autorun" there would make every staged workspace look stale and rewrite on each call."""
    ws = tmp_path / "ws"
    ws.mkdir()
    editor = EditorServer(ws, apply_runs=False)
    editor._ensure_marimo_config()
    before = (ws / ".marimo.toml").read_text("utf-8")
    editor._ensure_marimo_config()
    assert (ws / ".marimo.toml").read_text("utf-8") == before


def test_apply_run_mode_rewrites_a_running_editors_config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    editor = EditorServer(ws, apply_runs=True)
    editor._ensure_marimo_config()
    assert _read(ws)["runtime"]["watcher_on_save"] == "autorun"
    editor.apply_run_mode(False)  # the setting (or a policy) changed mid-session
    assert editor.apply_runs is False
    assert _read(ws)["runtime"]["watcher_on_save"] == "lazy"


def test_a_caller_without_a_config_preserves_the_staged_mode(tmp_path):
    """A Deliver export / scheduled run only wants the import path. If it re-armed
    autorun it would silently undo a team policy underneath an open editor."""
    from mooring import editor as editor_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    EditorServer(ws, apply_runs=False)._ensure_marimo_config()
    editor_mod.ensure_runtime_config(ws)  # no theme, no apply_runs — the export's call
    assert _read(ws)["runtime"]["watcher_on_save"] == "lazy"
    assert _read(ws)["display"]["theme"] == "system"  # and the theme is left alone too


def test_a_fresh_workspace_defaults_to_autorun(tmp_path):
    """Preserving must not mean "write nothing": a workspace with no .marimo.toml yet
    still gets today's behaviour."""
    from mooring import editor as editor_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    editor_mod.ensure_runtime_config(ws)
    assert _read(ws)["runtime"]["watcher_on_save"] == "autorun"


def test_writes_workspace_root_to_pythonpath(tmp_path):
    # The workspace root goes on the notebook kernel's sys.path so a notebook in any
    # sub-folder can import the repo's helper modules (`from lib import helpers`), and
    # the .mooring/pylib dir so a notebook can `import mooring_checks`. Both ABSOLUTE —
    # marimo doesn't resolve a .marimo.toml pythonpath entry.
    ws = tmp_path / "ws"
    ws.mkdir()
    EditorServer(ws)._ensure_marimo_config()
    assert _read(ws)["runtime"]["pythonpath"] == [
        str(ws.resolve()),
        str((ws / ".mooring" / "pylib").resolve()),
    ]


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
    assert _read(ws)["runtime"]["pythonpath"] == [
        str(ws.resolve()),
        str((ws / ".mooring" / "pylib").resolve()),
        "/some/other/dir",
    ]


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
