"""`mooring ai context add/remove/list` — curating the team's AI context OFFER from the CLI.

The offer is folded straight into ``cfg.folders`` (mooring.app.context_folders.sync_dirs), so
the CLI must refuse an escaping folder exactly as the hub route does. Nesting is not an escape:
a folder at any DEPTH is a legitimate offer. No Copilot, no network, no GitHub client.
"""

import pytest

from mooring import cli, paths, workspace_config


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")
    monkeypatch.setenv("MOORING_OWNER", "acme")
    monkeypatch.setenv("MOORING_REPO", "nbs")
    monkeypatch.setenv("MOORING_WORKSPACE", str(ws))
    monkeypatch.setenv("MOORING_TRUSTSTORE", "0")
    for var in ("MOORING_BRANCH", "MOORING_ACTIVE_REPO", "MOORING_GITHUB_HOST", "MOORING_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return ws


def test_add_accepts_a_nested_folder(workspace, capsys):
    assert cli.main(["ai", "context", "add", "reports/2026/finance"]) == 0
    assert workspace_config.context_folders(workspace) == ("reports/2026/finance",)
    assert "reports/2026/finance" in capsys.readouterr().out


def test_add_normalizes_a_windows_path(workspace):
    assert cli.main(["ai", "context", "add", "reports\\finance\\"]) == 0
    assert workspace_config.context_folders(workspace) == ("reports/finance",)


@pytest.mark.parametrize("bad", ["..", "../outside", "a/../../b", "C:/secrets", "/", "."])
def test_add_refuses_an_escaping_folder(workspace, bad):
    # The CLI's half of the escape check the hub route does with resolve()/relative_to.
    with pytest.raises(SystemExit):
        cli.main(["ai", "context", "add", bad])
    assert workspace_config.context_folders(workspace) == ()
    assert not (workspace / "mooring.toml").exists()


def test_remove_a_nested_folder_round_trips(workspace):
    assert cli.main(["ai", "context", "add", "reports/finance"]) == 0
    assert cli.main(["ai", "context", "remove", "reports/finance"]) == 0
    assert workspace_config.context_folders(workspace) == ()


def test_list_shows_a_nested_offer(workspace, capsys):
    cli.main(["ai", "context", "add", "reports/finance"])
    capsys.readouterr()
    assert cli.main(["ai", "context", "list"]) == 0
    assert "reports/finance" in capsys.readouterr().out
