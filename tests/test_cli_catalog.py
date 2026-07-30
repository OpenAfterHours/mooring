"""`mooring catalog` — the offline "has someone already built this?" search.

Runs off the local workspace only: it parses each notebook with ``ast``, never executes
one, and doubles as the preview of what the copilot's catalog tools can see.
"""

import pytest

from mooring import cli, paths

SECRET = "SECRET_VALUE_DO_NOT_LEAK"

NOTEBOOK = (
    "import marimo\n\napp = marimo.App()\n\n"
    "@app.cell\ndef _():\n"
    "    import marimo as mo\n"
    "    import mooring_inputs as mi\n"
    '    mo.md("""# Month End Recon\n\n    Ties the ledger to the GL feed.""")\n'
    '    mi.fingerprint(df, "ledger", path="data/gl_ledger.parquet")\n'
    '    hush = "SECRET_VALUE_DO_NOT_LEAK"\n'
    "    return\n"
)


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
    monkeypatch.delenv("MOORING_AI_NOTEBOOK_CATALOG", raising=False)
    return ws


def _write(ws, rel, text):
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8", newline="\n")


def test_catalog_lists_every_notebook_with_its_title(workspace, capsys):
    _write(workspace, "notebooks/recon.py", NOTEBOOK)
    _write(workspace, "notebooks/helpers.py", "def clean(df):\n    return df\n")  # a module
    assert cli.main(["catalog"]) == 0
    out = capsys.readouterr().out
    assert "notebooks/recon.py — Month End Recon" in out
    assert "1 input(s)" in out
    assert "helpers.py" not in out  # a plain module is not a notebook


def test_catalog_searches_content_not_just_the_filename(workspace, capsys):
    _write(workspace, "notebooks/q3_v2.py", NOTEBOOK)
    assert cli.main(["catalog", "gl_ledger"]) == 0
    assert "notebooks/q3_v2.py" in capsys.readouterr().out
    assert cli.main(["catalog", "ties", "the", "ledger"]) == 0
    assert "notebooks/q3_v2.py" in capsys.readouterr().out


def test_catalog_full_shows_the_declared_inputs(workspace, capsys):
    _write(workspace, "notebooks/recon.py", NOTEBOOK)
    assert cli.main(["catalog", "--full"]) == 0
    out = capsys.readouterr().out
    assert "fingerprints inputs: ledger (data/gl_ledger.parquet)" in out
    assert "about: Ties the ledger to the GL feed." in out


def test_catalog_output_is_value_free(workspace, capsys):
    _write(workspace, "notebooks/recon.py", NOTEBOOK)
    assert cli.main(["catalog", "--full"]) == 0
    assert SECRET not in capsys.readouterr().out  # a cell-body value never surfaces


def test_catalog_reports_a_miss_and_an_empty_workspace(workspace, capsys):
    workspace.mkdir(parents=True, exist_ok=True)
    assert cli.main(["catalog"]) == 0
    assert "No notebooks found" in capsys.readouterr().out
    _write(workspace, "notebooks/recon.py", NOTEBOOK)
    assert cli.main(["catalog", "payroll"]) == 1
    assert "No notebooks match" in capsys.readouterr().out


def test_catalog_notes_when_the_copilot_flag_is_off(workspace, monkeypatch, capsys):
    monkeypatch.setenv("MOORING_AI_NOTEBOOK_CATALOG", "0")
    _write(workspace, "notebooks/recon.py", NOTEBOOK)
    assert cli.main(["catalog"]) == 0
    assert "[ai] notebook_catalog is OFF" in capsys.readouterr().out


def test_catalog_reports_an_unparseable_notebook_without_its_source(workspace, capsys):
    _write(workspace, "notebooks/recon.py", NOTEBOOK)
    _write(
        workspace,
        "notebooks/wip.py",
        f'import marimo\napp = marimo.App()\n@app.cell\ndef _(:\n    x = "{SECRET}"\n',
    )
    assert cli.main(["catalog"]) == 0
    out = capsys.readouterr().out
    assert "notebooks/wip.py: SyntaxError@" in out
    assert SECRET not in out  # never str(exc) — its message embeds the offending line
