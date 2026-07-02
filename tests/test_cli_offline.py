"""Offline mode at the CLI: a cached OFFLINE status and one-line exits — never
a traceback when GitHub is unreachable."""

import pytest
from conftest import FakeClient

from mooring import cli, paths
from mooring.github import Unreachable


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


def _go_offline(fake, monkeypatch):
    def boom(*args, **kwargs):
        raise Unreachable("GitHub is unreachable — check your network connection and try again.")

    monkeypatch.setattr(fake, "get_branch_head", boom)


def test_status_offline_prints_cached_rows_under_a_loud_header(workspace, monkeypatch, capsys):
    fake = FakeClient({"notebooks/a.py": b"v1\n"})
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    assert cli.main(["pull"]) == 0  # primes the workspace AND the remote cache
    _go_offline(fake, monkeypatch)
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OFFLINE — GitHub unreachable; showing sync state as of ")
    assert "notebooks/a.py" in out
    assert "synced" in out


def test_status_offline_without_a_cache_exits_with_the_classified_line(workspace, monkeypatch):
    fake = FakeClient({"notebooks/a.py": b"v1\n"})
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    _go_offline(fake, monkeypatch)  # never synced: nothing cached to show
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["status"])
    assert "GitHub is unreachable" in str(exc_info.value.code)


def test_push_offline_exits_one_line_never_a_traceback(workspace, monkeypatch, capsys):
    fake = FakeClient({"notebooks/a.py": b"v1\n"})
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    assert cli.main(["pull"]) == 0
    (workspace / "notebooks" / "a.py").write_text("mine\n", "utf-8", newline="\n")
    _go_offline(fake, monkeypatch)
    # main() classifies the outage into sys.exit(message) — the argparse-style
    # one-liner — instead of re-raising into a traceback.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["push"])
    assert str(exc_info.value.code) == (
        "GitHub is unreachable — check your network connection and try again."
    )
    assert (workspace / "notebooks" / "a.py").read_text("utf-8") == "mine\n"
