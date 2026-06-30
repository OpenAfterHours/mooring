"""CLI `adopt` command + the `status` adopt hint.

`adopt` brings repo folders that hold notebooks outside the standard synced folders
(notebooks/data/reports) into sync, by registering them in the synced mooring.toml and
pulling. The GitHub client is faked so the tests stay offline.
"""

import pytest
from conftest import FakeClient

from mooring import cli, paths, workspace_config


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
    return ws


def _fake(monkeypatch, files):
    fake = FakeClient(files)
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    return fake


def test_adopt_lists_candidates_without_args(workspace, monkeypatch, capsys):
    _fake(monkeypatch, {"notebooks/a.py": b"x\n", "analysis/q1.py": b"y\n", "lib/helpers.py": b"z\n"})
    assert cli.main(["adopt"]) == 0
    out = capsys.readouterr().out
    assert "analysis" in out and "lib" in out
    # Listing only — nothing registered, nothing pulled.
    assert workspace_config.extra_folders(workspace) == ()
    assert not (workspace / "analysis/q1.py").exists()


def test_adopt_registers_in_mooring_toml_and_pulls(workspace, monkeypatch):
    _fake(monkeypatch, {"analysis/q1.py": b"y\n", "lib/helpers.py": b"z\n"})
    assert cli.main(["adopt", "analysis", "lib"]) == 0
    assert set(workspace_config.extra_folders(workspace)) == {"analysis", "lib"}
    assert (workspace / "analysis/q1.py").read_text("utf-8") == "y\n"
    assert (workspace / "lib/helpers.py").read_text("utf-8") == "z\n"


def test_adopt_all(workspace, monkeypatch):
    _fake(monkeypatch, {"analysis/q1.py": b"y\n", "lib/helpers.py": b"z\n"})
    assert cli.main(["adopt", "--all"]) == 0
    assert set(workspace_config.extra_folders(workspace)) == {"analysis", "lib"}


def test_adopt_rejects_unknown_folder(workspace, monkeypatch):
    _fake(monkeypatch, {"analysis/q1.py": b"y\n"})
    with pytest.raises(SystemExit) as exc:
        cli.main(["adopt", "nope"])
    assert "Not adoptable" in str(exc.value)
    assert workspace_config.extra_folders(workspace) == ()  # nothing partially written


def test_adopt_nothing_to_do(workspace, monkeypatch, capsys):
    _fake(monkeypatch, {"notebooks/a.py": b"x\n"})  # only an in-scope file
    assert cli.main(["adopt"]) == 0
    assert "Nothing to adopt" in capsys.readouterr().out


def test_status_hints_unsynced_folders(workspace, monkeypatch, capsys):
    # The user's exact scenario: a new repo whose notebooks live only outside the
    # synced folders. status used to say "workspace empty"; it now points to adopt.
    _fake(monkeypatch, {"analysis/q1.py": b"y\n"})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "analysis" in out
    assert "mooring adopt" in out
    assert "Workspace empty" not in out


def test_status_no_hint_when_all_in_scope(workspace, monkeypatch, capsys):
    _fake(monkeypatch, {"notebooks/a.py": b"x\n"})
    assert cli.main(["status"]) == 0
    assert "mooring adopt" not in capsys.readouterr().out
