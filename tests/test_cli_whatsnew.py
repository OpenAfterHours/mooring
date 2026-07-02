"""CLI `whatsnew` — the pull digest at the terminal."""

import pytest
from conftest import FakeClient

from mooring import cli, paths


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


def test_whatsnew_prints_the_pending_digest(workspace, monkeypatch, capsys):
    fake = FakeClient({"notebooks/a.py": b"v1\n"})
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    assert cli.main(["pull"]) == 0
    fake.seed("notebooks/a.py", b"v2\n")  # a teammate pushes
    capsys.readouterr()
    assert cli.main(["whatsnew"]) == 0
    out = capsys.readouterr().out
    assert "notebooks/a.py" in out
    assert "remote changed" in out
    assert "phil" in out  # who
    assert "Seed notebooks/a.py" in out  # why (the commit message)
    assert "mooring pull" in out  # the next step


def test_whatsnew_reports_nothing_new(workspace, monkeypatch, capsys):
    fake = FakeClient({"notebooks/a.py": b"v1\n"})
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    assert cli.main(["pull"]) == 0
    capsys.readouterr()
    assert cli.main(["whatsnew"]) == 0
    assert "Nothing new" in capsys.readouterr().out
