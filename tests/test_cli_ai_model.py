"""`mooring ai model check` — the offline Power BI semantic-model transparency
lint (the `ai dictionary check` idiom): per model it reports files read, what
the allowlist kept and excluded, and the egress scrubber's value-free findings.
No Copilot, no network, no GitHub client.
"""

import pytest

from mooring import cli, paths

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
VALID_CARD = "4012888888881881"  # Luhn-valid, not on any test-PAN list


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
    monkeypatch.delenv("MOORING_AI_SEMANTIC_MODEL", raising=False)
    return ws


def _write(ws, rel, text):
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8", newline="\n")


def _model(ws, *, measure_dax="SUM(Sales[Amount])"):
    _write(
        ws,
        "reports/Sales.SemanticModel/definition/tables/Sales.tmdl",
        "table Sales\n"
        f"\tmeasure 'Total Sales' = {measure_dax}\n"
        "\t\tformatString: #,0\n"
        "\tcolumn Amount\n"
        "\t\tdataType: decimal\n"
        "\tpartition Sales = m\n"
        "\t\tsource =\n"
        f'\t\t\t\tSql.Database("{SECRET}", "db")\n',
    )
    _write(
        ws,
        "reports/Sales.SemanticModel/definition/roles/R.tmdl",
        f"role R\n\ttablePermission Sales = {SECRET}\n",
    )


def test_check_reports_kept_and_excluded(workspace, capsys):
    _model(workspace)
    assert cli.main(["ai", "model", "check"]) == 0
    out = capsys.readouterr().out
    assert "reports/Sales.SemanticModel:" in out
    assert "read definition/tables/Sales.tmdl" in out
    assert "kept: 1 tables, 1 measures, 0 relationships" in out
    assert "1 partition/source block(s) (never captured)" in out
    assert "1 roles file(s) (never opened)" in out
    assert "scrub: clean" in out
    assert SECRET not in out  # the report itself stays value-free


def test_check_flags_checksum_pii_in_dax_and_exits_1(workspace, capsys):
    _model(workspace, measure_dax=f'IF(Sales[card] = "{VALID_CARD}", 1, 0)')
    assert cli.main(["ai", "model", "check"]) == 1
    out = capsys.readouterr().out
    assert "finding(s)" in out and "payment card" in out
    assert VALID_CARD not in out  # findings are value-free (line + kind only)


def test_check_no_models(workspace, capsys):
    workspace.mkdir(parents=True, exist_ok=True)
    assert cli.main(["ai", "model", "check"]) == 0
    assert "No Power BI semantic models" in capsys.readouterr().out


def test_check_notes_when_the_switch_is_off(workspace, monkeypatch, capsys):
    monkeypatch.setenv("MOORING_AI_SEMANTIC_MODEL", "0")
    _model(workspace)
    assert cli.main(["ai", "model", "check"]) == 0
    out = capsys.readouterr().out
    assert "[ai] semantic_model is OFF" in out


def test_check_notes_the_synced_opt_out(workspace, capsys):
    _model(workspace)
    _write(workspace, "mooring.toml", '[ai]\ndisabled_semantic_models = ["reports/Sales"]\n')
    assert cli.main(["ai", "model", "check"]) == 0
    assert "AI access: OFF for this model" in capsys.readouterr().out
