"""CLI `rollback` command — restore a notebook to its last synced version.

Unlike `delete` (local-only), rollback fetches the last-synced bytes from GitHub,
so it needs a configured repo + login. The client is faked here so the tests stay
offline; the manifest base is seeded with a real `pull`.
"""

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


def _with_pulled_file(monkeypatch, contents=b"v1\n"):
    """Fake the GitHub client and seed the manifest base via a real pull."""
    fake = FakeClient({"notebooks/a.py": contents})
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    assert cli.main(["pull"]) == 0
    return fake


def test_rollback_restores_last_synced_version(workspace, monkeypatch, capsys):
    _with_pulled_file(monkeypatch)
    (workspace / "notebooks/a.py").write_text("mine\n", "utf-8", newline="\n")
    assert cli.main(["rollback", "notebooks/a.py", "--yes"]) == 0
    assert (workspace / "notebooks/a.py").read_text("utf-8") == "v1\n"
    assert "reverted notebooks/a.py" in capsys.readouterr().out


def test_rollback_refuses_without_confirmation_when_non_interactive(workspace, monkeypatch):
    _with_pulled_file(monkeypatch)
    (workspace / "notebooks/a.py").write_text("mine\n", "utf-8", newline="\n")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["rollback", "notebooks/a.py"])
    assert "Re-run with --yes" in str(exc.value)
    assert (workspace / "notebooks/a.py").read_text("utf-8") == "mine\n"  # untouched


def test_rollback_interactive_prompt_no_cancels(workspace, monkeypatch, capsys):
    _with_pulled_file(monkeypatch)
    (workspace / "notebooks/a.py").write_text("mine\n", "utf-8", newline="\n")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert cli.main(["rollback", "notebooks/a.py"]) == 0
    assert (workspace / "notebooks/a.py").read_text("utf-8") == "mine\n"
    assert "Cancelled" in capsys.readouterr().out


def test_rollback_snapshots_so_it_is_undoable(workspace, monkeypatch):
    from mooring import notebook_undo

    _with_pulled_file(monkeypatch)
    (workspace / "notebooks/a.py").write_text("mine\n", "utf-8", newline="\n")
    assert cli.main(["rollback", "notebooks/a.py", "--yes"]) == 0
    # the pre-revert bytes are on the local undo stack (the hub's Undo can restore them)
    latest = notebook_undo.peek_latest(workspace, "notebooks/a.py")
    assert latest is not None
    assert latest[1] == b"mine\n"


def test_rollback_needs_login(workspace, monkeypatch, capsys):
    # No token: rollback fails fast with the login hint (unlike delete, which is
    # local-only). Force get_token to None so a real token on the dev machine's
    # keyring doesn't leak in and turn this into a live network call.
    monkeypatch.setattr("mooring.auth.get_token", lambda host=None: None)
    (workspace / "notebooks").mkdir(parents=True)
    (workspace / "notebooks/a.py").write_text("mine\n", "utf-8", newline="\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["rollback", "notebooks/a.py", "--yes"])
    assert "Not logged in" in str(exc.value)
