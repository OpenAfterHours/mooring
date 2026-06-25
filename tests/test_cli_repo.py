"""CLI repo-management commands driven through cli.main()."""

import json
import tomllib

import pytest

from mooring import cli, paths, telemetry


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    for var in (
        "MOORING_CLIENT_ID",
        "MOORING_OWNER",
        "MOORING_REPO",
        "MOORING_BRANCH",
        "MOORING_WORKSPACE",
        "MOORING_ACTIVE_REPO",
        "MOORING_GITHUB_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    # main() injects truststore into global ssl; keep the test process hermetic.
    monkeypatch.setenv("MOORING_TRUSTSTORE", "0")
    return tmp_path


def test_repo_add_and_list(capsys):
    assert cli.main(["repo", "add", "acme/nbs"]) == 0
    assert cli.main(["repo", "add", "acme/lab", "--alias", "lab", "--no-use"]) == 0
    out = capsys.readouterr().out
    assert "Registered acme/nbs as 'nbs' (now active)." in out

    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["repos"]["active"] == "nbs"
    assert data["repos"]["lab"]["repo"] == "lab"

    assert cli.main(["repo", "list"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any(line.lstrip().startswith("* nbs") for line in lines)
    assert any("acme/lab" in line and not line.lstrip().startswith("*") for line in lines)


def test_repo_use_and_remove(capsys):
    cli.main(["repo", "add", "acme/nbs"])
    cli.main(["repo", "add", "acme/lab", "--no-use"])
    assert cli.main(["repo", "use", "lab"]) == 0
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["repos"]["active"] == "lab"

    assert cli.main(["repo", "remove", "lab"]) == 0
    out = capsys.readouterr().out
    assert "Workspace folder" in out
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert "lab" not in data["repos"]
    assert data["repos"]["active"] == "nbs"  # fell back to the remaining repo


def test_repo_use_unknown_alias_exits():
    cli.main(["repo", "add", "acme/nbs"])
    with pytest.raises(SystemExit) as exc:
        cli.main(["repo", "use", "nope"])
    assert "Unknown repo alias" in str(exc.value)


def test_cleared_registry_with_legacy_github_shows_no_phantom_repo(capsys):
    """After clearing all repos, a still-populated legacy [github] section must
    not resurrect a phantom repo in 'repo list' or contradict 'repo remove'.

    Regression for the reported 'Unknown repo alias notebooks. Known: notebooks'
    self-contradiction: 'list' must report no repos, and 'remove' of the old
    name must report 'Known: (none)' rather than listing the phantom.
    """
    paths.user_config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.user_config_file().write_text(
        '[github]\nclient_id = "cid"\nowner = "ShipsAfterHours"\nrepo = "notebooks"\n'
        'branch = "master"\n[repos]\n',
        "utf-8",
    )
    assert cli.main(["repo", "list"]) == 0
    assert "No repos registered" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        cli.main(["repo", "remove", "notebooks"])
    assert "Known: (none)" in str(exc.value)
    assert "Known: notebooks" not in str(exc.value)


def test_repo_remove_all(capsys):
    cli.main(["repo", "add", "acme/nbs"])
    cli.main(["repo", "add", "acme/lab", "--no-use"])
    assert cli.main(["repo", "remove", "--all"]) == 0
    out = capsys.readouterr().out
    assert "Removed all 2 repo(s)" in out
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["repos"] == {}


def test_repo_remove_all_when_empty(capsys):
    assert cli.main(["repo", "remove", "--all"]) == 0
    assert "No repos registered." in capsys.readouterr().out


def test_repo_remove_requires_alias_or_all():
    cli.main(["repo", "add", "acme/nbs"])
    with pytest.raises(SystemExit) as exc:
        cli.main(["repo", "remove"])
    assert "Specify a repo alias" in str(exc.value)


def test_login_with_host_persists_and_uses_it(capsys, monkeypatch):
    from mooring import auth, github

    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")
    seen = {}

    def fake_start(client_id, host="github.com", **kw):
        seen["host"] = host
        return auth.DeviceCode("d", "ABCD-1234", "https://x/login/device", 5, 900, host=host)

    monkeypatch.setattr(auth, "start_device_flow", fake_start)
    monkeypatch.setattr(auth, "poll_for_token", lambda *a, **k: "gho_tok")
    monkeypatch.setattr(auth, "save_token", lambda *a, **k: None)

    class FakeClient:
        def __init__(self, *a, **k):
            pass  # no-op: stub double, accepts and ignores constructor args

        def get_user(self):
            return {"login": "octo"}

    monkeypatch.setattr(github, "GitHubClient", FakeClient)

    assert cli.main(["login", "--host", "https://GHE.Example/"]) == 0
    assert seen["host"] == "ghe.example"  # normalized host passed to the flow
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["github"]["host"] == "ghe.example"  # and persisted
    out = capsys.readouterr().out
    assert "Saved GitHub host: ghe.example" in out
    assert "Requesting device code from ghe.example" in out


def test_login_failure_shows_enterprise_hint(monkeypatch):
    import requests

    from mooring import auth

    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")

    class Resp:
        status_code = 404

    def boom(*a, **k):
        err = requests.HTTPError("404 ...")
        err.response = Resp()  # ty: ignore[invalid-assignment]  # test stub Response
        raise err

    monkeypatch.setattr(auth, "start_device_flow", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main(["login"])  # no --host → default github.com
    msg = str(exc.value)
    assert "github.com" in msg
    assert "GitHub Enterprise" in msg


def test_repo_add_malformed_slug_exits():
    with pytest.raises(SystemExit):
        cli.main(["repo", "add", "just-a-name"])


def test_repo_add_with_host_persists_normalized_host():
    assert cli.main(["repo", "add", "acme/nbs", "--host", "https://GHE.Example/"]) == 0
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["github"]["host"] == "ghe.example"


def test_repo_add_with_invalid_host_exits():
    with pytest.raises(SystemExit) as exc:
        cli.main(["repo", "add", "acme/nbs", "--host", "not a host"])
    assert "Not a valid GitHub host" in str(exc.value)


def test_status_with_unknown_repo_alias_exits():
    cli.main(["repo", "add", "acme/nbs"])
    with pytest.raises(SystemExit) as exc:
        cli.main(["status", "--repo", "nope"])
    assert "Unknown repo alias" in str(exc.value)


def test_repo_list_when_empty(capsys):
    assert cli.main(["repo", "list"]) == 0
    assert "No repos registered" in capsys.readouterr().out


def test_telemetry_records_events_through_main(tmp_path, monkeypatch):
    """A baked path endpoint makes cli.main() emit app_start + the command event."""
    logdir = tmp_path / "telemetry"
    monkeypatch.setenv("MOORING_LOG_ENDPOINT", str(logdir))
    assert cli.main(["repo", "add", "acme/nbs"]) == 0
    telemetry.flush(2.0)
    files = list(logdir.glob("*.jsonl"))
    assert len(files) == 1
    events = [json.loads(line) for line in files[0].read_text("utf-8").splitlines() if line.strip()]
    by_name = {e["event"]: e for e in events}
    assert "app_start" in by_name and "repo_add" in by_name
    assert by_name["app_start"]["command"] == "repo"
    assert by_name["repo_add"]["alias"] == "nbs"
    assert by_name["app_start"]["ts"].endswith("Z")
    assert by_name["app_start"]["version"]  # identity stamped
