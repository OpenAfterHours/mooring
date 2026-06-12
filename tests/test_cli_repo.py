"""CLI repo-management commands driven through cli.main()."""

import tomllib

import pytest

from mooring import cli, paths


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    for var in ("MOORING_CLIENT_ID", "MOORING_OWNER", "MOORING_REPO",
                "MOORING_BRANCH", "MOORING_WORKSPACE", "MOORING_ACTIVE_REPO",
                "MOORING_GITHUB_HOST"):
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
