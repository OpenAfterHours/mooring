"""CLI shadow guard: `mooring shadow ignore/unignore`, status + open warnings.

All local — no GitHub login needed (the ignore command and the warning helper read
the workspace off disk).
"""

from dataclasses import replace

import pytest

from mooring import cli, config, paths, workspace_config


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    ws = tmp_path / "ws"
    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")
    monkeypatch.setenv("MOORING_OWNER", "acme")
    monkeypatch.setenv("MOORING_REPO", "nbs")
    monkeypatch.setenv("MOORING_WORKSPACE", str(ws))
    monkeypatch.setenv("MOORING_TRUSTSTORE", "0")
    for var in ("MOORING_BRANCH", "MOORING_ACTIVE_REPO", "MOORING_GITHUB_HOST", "MOORING_FORCE_FROZEN"):
        monkeypatch.delenv(var, raising=False)
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    return ws


def _cfg(ws):
    return config.Config(workspace_path=str(ws))


def test_shadow_ignore_unignore_round_trip(workspace, capsys):
    assert cli.main(["shadow", "ignore", "notebooks/polars.py"]) == 0
    assert workspace_config.shadow_ignored(workspace) == {"notebooks/polars.py"}
    assert "now ignored" in capsys.readouterr().out

    assert cli.main(["shadow", "unignore", "notebooks/polars.py"]) == 0
    assert workspace_config.shadow_ignored(workspace) == set()
    assert "no longer ignored" in capsys.readouterr().out


def test_status_warning_helper_flags_polars(workspace):
    (workspace / "notebooks" / "polars.py").write_text("import marimo\n", "utf-8")
    lines = cli._shadow_warning_lines(_cfg(workspace), ["notebooks/polars.py"])
    assert any("polars" in line for line in lines)


def test_status_warning_helper_respects_toggle(workspace):
    (workspace / "notebooks" / "polars.py").write_text("import marimo\n", "utf-8")
    cfg = replace(_cfg(workspace), warn_shadowed_notebooks=False)
    assert cli._shadow_warning_lines(cfg, ["notebooks/polars.py"]) == []


def test_status_warning_helper_respects_ignore(workspace):
    (workspace / "notebooks" / "polars.py").write_text("import marimo\n", "utf-8")
    workspace_config.set_shadow_ignored(workspace, "notebooks/polars.py", True)
    assert cli._shadow_warning_lines(_cfg(workspace), ["notebooks/polars.py"]) == []


def test_open_warns_before_launch(workspace, monkeypatch, capsys):
    from mooring import editor as editor_mod

    (workspace / "notebooks" / "polars.py").write_text(
        "import marimo\napp = marimo.App()\n", "utf-8"
    )

    class FakeEditor:
        def __init__(self, ws, **kw):
            pass

        def use_uv(self):
            return True

        def ensure_started(self):
            pass

        def url_for(self, rel):
            return "http://127.0.0.1:9999/edit"

        def wait(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(editor_mod, "EditorServer", FakeEditor)
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    assert cli.main(["open", "notebooks/polars.py"]) == 0
    out = capsys.readouterr().out
    assert "shadow" in out.lower() and "polars" in out


def test_open_refuses_an_empty_init_py(workspace):
    # An empty __init__.py must not open in marimo (it would be rewritten into notebook
    # form and break the package) — the CLI open guard refuses it like the hub does.
    (workspace / "notebooks" / "__init__.py").write_text("", "utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["open", "notebooks/__init__.py"])
    assert "module" in str(exc.value).lower()
