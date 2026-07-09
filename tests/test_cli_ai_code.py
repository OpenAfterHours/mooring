"""`mooring ai code check` — the offline "what helpers can the copilot reuse?" diagnostic.

The answer to "the AI can't see my .py files": it shows which importable modules under the
synced folders yield reusable helpers, and WHY the rest were skipped. No Copilot/network.
"""

import pytest

from mooring import cli, paths

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


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
    monkeypatch.delenv("MOORING_AI_CODE_INDEX", raising=False)
    return ws


def _write(ws, rel, text):
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8", newline="\n")


def test_check_distinguishes_helpers_notebooks_and_scripts(workspace, monkeypatch, capsys):
    monkeypatch.setenv("MOORING_AI_CODE_INDEX", "1")
    _write(workspace, "notebooks/helpers.py", 'def clean(df, cols):\n    """Norm."""\n    return df\n')
    _write(workspace, "notebooks/nb.py", "import marimo\napp = marimo.App()\n@app.cell\ndef _():\n    return 1\n")
    _write(workspace, "notebooks/script.py", 'print("hi")\nX = 1\n')
    assert cli.main(["ai", "code", "check"]) == 0
    out = capsys.readouterr().out
    assert "notebooks/helpers.py: 1 function(s)" in out
    assert "notebooks/nb.py: marimo notebook" in out
    assert "notebooks/script.py: no reusable helpers" in out
    assert "1 module(s) the copilot can reuse" in out
    assert "clean(df, cols)" in out  # the sample list_helpers view


def test_check_notes_when_the_flag_is_off(workspace, capsys):
    _write(workspace, "notebooks/helpers.py", "def h(): pass\n")
    assert cli.main(["ai", "code", "check"]) == 0
    assert "[ai] code_index is OFF" in capsys.readouterr().out


def test_check_no_python_files(workspace, capsys):
    workspace.mkdir(parents=True, exist_ok=True)
    assert cli.main(["ai", "code", "check"]) == 0
    assert "No .py files found" in capsys.readouterr().out


def test_check_output_is_value_free(workspace, monkeypatch, capsys):
    monkeypatch.setenv("MOORING_AI_CODE_INDEX", "1")
    _write(
        workspace,
        "notebooks/helpers.py",
        f'def clean(df):\n    key = "{SECRET}"\n    return key\n',  # a body literal
    )
    assert cli.main(["ai", "code", "check"]) == 0
    assert SECRET not in capsys.readouterr().out  # the body value never surfaces
