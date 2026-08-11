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


def test_workspace_commands_accept_the_repo_alias_flag():
    # duplicate/whatsnew were missing from the shared --repo loop, so targeting a
    # non-active repo exited 2 with "unrecognized arguments" while every sibling
    # workspace command (history/status/open/new/...) accepted it.
    parser = cli._build_parser()
    assert parser.parse_args(["duplicate", "notebooks/a.py", "--repo", "lab"]).repo == "lab"
    assert parser.parse_args(["whatsnew", "--repo", "lab"]).repo == "lab"
    assert parser.parse_args(["history", "notebooks/a.py", "--repo", "lab"]).repo == "lab"


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


def _stub_device_flow(monkeypatch, login="octo"):
    """Stub the whole flow: device code, poll, and the identity lookup."""
    from mooring import auth
    from mooring.app import accounts

    seen = {}

    def fake_start(client_id, session=None, host="github.com", account=""):
        seen["host"] = host
        seen["client_id"] = client_id
        seen["account"] = account
        return auth.DeviceCode(
            "d", "ABCD-1234", "https://x/login/device", 5, 900,
            host=host, client_id=client_id, account=account,
        )

    monkeypatch.setattr(auth, "start_device_flow", fake_start)
    monkeypatch.setattr(auth, "poll_for_token", lambda *a, **k: "gho_tok")
    monkeypatch.setattr(accounts, "resolve_login", lambda tok, host, session=None: login)
    return seen


def test_login_with_host_persists_and_uses_it(capsys, monkeypatch):
    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")
    seen = _stub_device_flow(monkeypatch)

    assert cli.main(["login", "--host", "https://GHE.Example/"]) == 0
    assert seen["host"] == "ghe.example"  # normalized host passed to the flow
    data = tomllib.loads(paths.user_config_file().read_text("utf-8"))
    assert data["github"]["host"] == "ghe.example"  # and persisted
    out = capsys.readouterr().out
    assert "Saved GitHub host: ghe.example" in out
    assert "Requesting device code from ghe.example" in out
    assert "Logged in as octo@ghe.example" in out


def test_login_on_an_unbound_repo_migrates_it_onto_a_real_account(capsys, monkeypatch):
    """Upgrades converge: a pre-accounts repo stops depending on the shared
    host-keyed slot the first time its owner signs in again."""
    from mooring import auth, config

    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")
    _stub_device_flow(monkeypatch, login="octo")
    cli.main(["repo", "add", "acme/nbs"])
    assert config.load_app_config().config_for("nbs").account == ""

    assert cli.main(["login"]) == 0

    cfg = config.load_app_config().config_for("nbs")
    assert cfg.account == "github"  # alias derived from the host
    assert cfg.account_login == "octo"
    assert cfg.token_slot == ("github.com", "octo")
    assert auth.get_token(env={}, host="github.com", login="octo") == "gho_tok"


def test_login_on_a_bound_repo_reauthenticates_that_account(capsys, monkeypatch):
    from mooring import config, config_store

    config_store.add_account("work", "ghe.example", login="a.h", client_id="cid_ghe")
    config_store.add_repo("team", "acme", "nbs", account="work")
    seen = _stub_device_flow(monkeypatch, login="a.h")

    assert cli.main(["login"]) == 0
    # The flow ran against the ACCOUNT's host and client id, not any global.
    assert (seen["host"], seen["client_id"], seen["account"]) == ("ghe.example", "cid_ghe", "work")
    assert config.load_app_config().config_for("team").token_slot == ("ghe.example", "a.h")


def test_login_host_flag_on_a_bound_repo_points_at_the_account_command(monkeypatch):
    from mooring import config_store

    config_store.add_account("work", "ghe.example", login="a.h", client_id="cid")
    config_store.add_repo("team", "acme", "nbs", account="work")
    with pytest.raises(SystemExit) as exc:
        cli.main(["login", "--host", "other.example"])
    assert "mooring account add work --host other.example" in str(exc.value)


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
