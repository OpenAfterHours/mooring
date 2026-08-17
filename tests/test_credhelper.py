"""Borrowing git's stored credential: the L0 leaf and auth's borrowed-token source.

The whole point of this method is that mooring NEVER stores what it borrows, so the
tests below check two things above all: that the credential is re-read from git rather
than cached anywhere durable, and that a background caller can never trip a helper into
opening a prompt nobody can see.
"""

import subprocess

import pytest

from mooring import auth, credhelper


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


@pytest.fixture(autouse=True)
def _no_borrow_cache():
    """auth's borrow cache is process-global; keep it from leaking between tests."""
    auth.forget_borrowed()
    yield
    auth.forget_borrowed()


@pytest.fixture
def git_present(monkeypatch):
    monkeypatch.setattr(credhelper.shutil, "which", lambda name: f"/usr/bin/{name}")


def _record(monkeypatch, replies):
    """Stub subprocess.run, recording every call. `replies` maps a marker in the
    argv (e.g. "fill") to the FakeProc to answer with."""
    calls = []

    def fake_run(argv, **kw):
        calls.append({"argv": argv, **kw})
        for marker, proc in replies.items():
            if marker in argv:
                return proc
        return FakeProc(returncode=1)

    monkeypatch.setattr(credhelper.subprocess, "run", fake_run)
    return calls


GHO = "username=phil\npassword=gho_abc123\n"


# -- the protocol --------------------------------------------------------------


def test_fill_returns_the_credential_git_hands_back(monkeypatch, git_present):
    _record(monkeypatch, {"fill": FakeProc(GHO)})
    cred = credhelper.fill("github.com")
    assert cred is not None
    assert (cred.username, cred.password, cred.host) == ("phil", "gho_abc123", "github.com")


def test_fill_sends_a_well_formed_request_and_never_prompts(monkeypatch, git_present):
    calls = _record(monkeypatch, {"fill": FakeProc(GHO)})
    credhelper.fill("acme.ghe.com", "acme/notebooks")
    argv, payload, env = calls[0]["argv"], calls[0]["input"], calls[0]["env"]
    # The record git expects, terminated by a blank line.
    assert payload == "protocol=https\nhost=acme.ghe.com\npath=acme/notebooks\n\n"
    # Prompting off three ways: a helper honouring ANY of them cannot open a dialog
    # behind the hub, where nobody would ever see it to answer.
    assert argv[:3] == ["git", "-c", "credential.interactive=false"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never"


def test_interactive_fill_opts_back_in(monkeypatch, git_present):
    calls = _record(monkeypatch, {"fill": FakeProc(GHO)})
    credhelper.fill("github.com", interactive=True)
    assert "credential.interactive=false" not in calls[0]["argv"]
    assert "GIT_TERMINAL_PROMPT" not in calls[0]["env"]


def test_no_password_is_no_credential(monkeypatch, git_present):
    # git with prompting disabled and no helper answers with no password rather
    # than asking a human — that is "nothing to borrow", not an error.
    _record(monkeypatch, {"fill": FakeProc("username=phil\n")})
    assert credhelper.fill("github.com") is None


def test_failed_helper_is_no_credential(monkeypatch, git_present):
    _record(monkeypatch, {"fill": FakeProc("", returncode=128)})
    assert credhelper.fill("github.com") is None


def test_bearer_credential_form_is_accepted(monkeypatch, git_present):
    # git 2.46+ helpers may answer with authtype/credential instead of password.
    _record(monkeypatch, {"fill": FakeProc("authtype=Bearer\ncredential=gho_xyz\n")})
    assert credhelper.fill("github.com").password == "gho_xyz"


def test_basic_credential_form_is_refused(monkeypatch, git_present):
    # A Basic `credential` is base64 user:password — sending it as a bearer token
    # would be wrong, so it must read as "nothing to borrow".
    _record(monkeypatch, {"fill": FakeProc("authtype=Basic\ncredential=cGhpbDpodW50ZXIy\n")})
    assert credhelper.fill("github.com") is None


def test_parsing_stops_at_the_blank_line(monkeypatch, git_present):
    _record(monkeypatch, {"fill": FakeProc("password=gho_a\n\npassword=gho_LATER\n")})
    assert credhelper.fill("github.com").password == "gho_a"


# -- never raising -------------------------------------------------------------


@pytest.mark.parametrize("boom", [OSError("no git"), subprocess.TimeoutExpired("git", 20)])
def test_a_broken_helper_is_none_not_an_exception(monkeypatch, git_present, boom):
    # These run inside hub routes and sync steps; a raise here would 500 the page.
    def explode(*a, **k):
        raise boom

    monkeypatch.setattr(credhelper.subprocess, "run", explode)
    assert credhelper.fill("github.com") is None
    assert credhelper.borrow("github.com") is None
    credhelper.reject(credhelper.Credential("github.com", "phil", "gho_a"))  # no raise


def test_no_git_means_nothing_to_borrow(monkeypatch):
    monkeypatch.setattr(credhelper.shutil, "which", lambda name: None)
    assert credhelper.available() is False
    assert credhelper.fill("github.com") is None
    assert credhelper.borrow("github.com") is None


def test_empty_host_is_refused(monkeypatch, git_present):
    _record(monkeypatch, {"fill": FakeProc(GHO)})
    assert credhelper.fill("") is None


# -- borrow strategy -----------------------------------------------------------


def test_borrow_tries_the_path_first_then_the_bare_host(monkeypatch, git_present):
    # A helper with credential.useHttpPath files per repository; one without files
    # per host. Trying the path first covers both, in that order.
    def fake_run(argv, **kw):
        if "path=acme/nbs" in kw["input"]:
            return FakeProc("", returncode=1)
        return FakeProc(GHO)

    monkeypatch.setattr(credhelper.subprocess, "run", fake_run)
    assert credhelper.borrow("github.com", "acme/nbs").password == "gho_abc123"


def test_borrow_falls_back_to_the_github_cli(monkeypatch, git_present):
    def fake_run(argv, **kw):
        if argv[0] == "gh":
            assert argv == ["gh", "auth", "token", "--hostname", "acme.ghe.com"]
            return FakeProc("gho_from_gh\n")
        return FakeProc("", returncode=1)

    monkeypatch.setattr(credhelper.subprocess, "run", fake_run)
    cred = credhelper.borrow("acme.ghe.com")
    assert cred.password == "gho_from_gh"


def test_reject_hands_the_credential_back_so_the_helper_renews_it(monkeypatch, git_present):
    calls = _record(monkeypatch, {"reject": FakeProc()})
    credhelper.reject(credhelper.Credential("acme.ghe.com", "phil", "gho_abc123"))
    assert "reject" in calls[0]["argv"]
    # The helper needs the whole record to know WHICH credential to drop.
    assert calls[0]["input"] == (
        "protocol=https\nhost=acme.ghe.com\nusername=phil\npassword=gho_abc123\n\n"
    )


# -- value-free reporting ------------------------------------------------------


@pytest.mark.parametrize(
    "secret,kind,refreshable",
    [
        ("gho_abc", "gho_", True),   # OAuth app token: the helper can refresh it
        ("ghu_abc", "ghu_", True),   # GitHub App user token: ditto
        ("ghp_abc", "ghp_", False),  # classic PAT: an enterprise lifetime cap bites
        ("github_pat_abc", "github_pat_", False),
        ("mystery", "", False),
    ],
)
def test_token_kind_names_the_type(secret, kind, refreshable):
    cred = credhelper.Credential("github.com", "phil", secret)
    assert cred.kind == kind
    assert cred.refreshable is refreshable


def test_probe_reports_the_type_and_never_the_secret(monkeypatch, git_present):
    _record(monkeypatch, {"fill": FakeProc("username=phil\npassword=gho_SECRET\n")})
    probe = credhelper.probe("acme.ghe.com")
    assert probe.found and probe.kind == "gho_" and probe.refreshable
    blob = repr(probe) + probe.summary
    assert "gho_SECRET" not in blob and "SECRET" not in blob


def test_probe_without_git_explains_itself(monkeypatch):
    monkeypatch.setattr(credhelper.shutil, "which", lambda name: None)
    probe = credhelper.probe("github.com")
    assert not probe.git_present and not probe.found
    assert "git isn't on PATH" in probe.summary


# -- auth's borrowed source ----------------------------------------------------


def test_token_for_git_method_borrows(monkeypatch, git_present):
    _record(monkeypatch, {"fill": FakeProc(GHO)})
    assert auth.token_for(("github.com", "phil"), env={}, method="git") == "gho_abc123"


def test_token_for_defaults_to_stored_tokens(monkeypatch, git_present):
    """Fail-closed: a caller that forgets the method gets None for a borrowed
    account — "not signed in" — rather than a credential from another source."""
    _record(monkeypatch, {"fill": FakeProc(GHO)})
    monkeypatch.setattr(auth, "get_token", lambda **kw: None)
    assert auth.token_for(("github.com", "phil"), env={}) is None


def test_a_slotless_account_cannot_borrow(monkeypatch, git_present):
    # An account that never finished signing in resolves to token_slot None. That
    # must stay fail-closed for EVERY method, borrowed included.
    _record(monkeypatch, {"fill": FakeProc(GHO)})
    assert auth.token_for(None, env={}, method="git") is None


def test_mooring_token_overrides_a_borrowed_credential(monkeypatch, git_present):
    _record(monkeypatch, {"fill": FakeProc(GHO)})
    env = {"MOORING_TOKEN": "from_env"}
    assert auth.token_for(("github.com", "phil"), env=env, method="git") == "from_env"


def test_the_credential_is_cached_briefly_then_re_read(monkeypatch, git_present):
    calls = _record(monkeypatch, {"fill": FakeProc(GHO)})
    now = [1000.0]
    auth.borrowed_credential("github.com", clock=lambda: now[0])
    auth.borrowed_credential("github.com", clock=lambda: now[0])
    assert len(calls) == 1, "a hub poll must not spawn git every time"
    now[0] += auth.BORROW_TTL + 1
    auth.borrowed_credential("github.com", clock=lambda: now[0])
    assert len(calls) == 2, "and it must go stale, so a renewed credential is picked up"


def test_the_helpers_own_expiry_shortens_the_cache(monkeypatch, git_present):
    """A cache entry must never outlive what the helper said it was good for."""
    import time as time_mod

    expiry = int(time_mod.time()) + 5  # the helper promises 5s; BORROW_TTL is 60
    calls = _record(
        monkeypatch, {"fill": FakeProc(f"password=gho_a\npassword_expiry_utc={expiry}\n")}
    )
    now = [1000.0]
    auth.borrowed_credential("github.com", clock=lambda: now[0])
    good_until = auth._borrowed["github.com"][0]
    assert good_until < now[0] + auth.BORROW_TTL

    now[0] += 6  # past the helper's expiry, well inside the plain TTL
    auth.borrowed_credential("github.com", clock=lambda: now[0])
    assert len(calls) == 2


def test_forget_borrowed_forces_a_re_read(monkeypatch, git_present):
    calls = _record(monkeypatch, {"fill": FakeProc(GHO)})
    auth.borrowed_token("github.com")
    auth.forget_borrowed("github.com")
    auth.borrowed_token("github.com")
    assert len(calls) == 2


def test_reject_borrowed_tells_git_and_drops_the_cache(monkeypatch, git_present):
    calls = _record(monkeypatch, {"fill": FakeProc(GHO), "reject": FakeProc()})
    auth.borrowed_token("github.com")           # populate the cache
    auth.reject_borrowed("github.com")
    assert any("reject" in c["argv"] for c in calls)
    assert "github.com" not in auth._borrowed


def test_nothing_borrowed_is_ever_written_to_disk(monkeypatch, git_present, tmp_path):
    """The load-bearing guarantee: a borrowed credential leaves no copy behind.

    A copy in mooring's own store would go stale with nothing able to refresh it —
    exactly the failure this method exists to avoid.
    """
    from mooring import paths

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    _record(monkeypatch, {"fill": FakeProc(GHO)})
    auth.borrowed_token("acme.ghe.com")
    written = list((tmp_path / "appdata").rglob("*")) if (tmp_path / "appdata").exists() else []
    assert not [p for p in written if p.is_file()]
    assert auth.get_token(env={}, host="acme.ghe.com", login="phil") is None


# -- discovery: finding hosts worth asking about -------------------------------
# The credential protocol has no "list" verb, so which hosts exist at all has to be
# inferred from where their NAMES show up in plain sight. These pin the parsing and,
# above all, that discovery stays a guess that is always confirmed by a real probe.

CONFIG_OUT = (
    "credential.helper manager\n"
    "credential.https://dev.azure.com.usehttppath true\n"
    "credential.https://ghe.service.group.helper manager\n"
    "credential.https://phil@ghe.service.group/acme/notebooks.helper manager\n"
)


def _cmd(argv, marker):
    return marker in argv or any(marker in str(a) for a in argv)


def _replies(monkeypatch, *, config_out="", gh_out="", fills=()):
    """Stub subprocess.run for the three commands discovery runs."""
    calls = []
    filled = list(fills)

    def fake_run(argv, **kw):
        calls.append(argv)
        if "config" in argv:
            return FakeProc(config_out)
        if "auth" in argv:
            return FakeProc(gh_out)
        if "fill" in argv:
            return filled.pop(0) if filled else FakeProc(returncode=1)
        return FakeProc(returncode=1)

    monkeypatch.setattr(credhelper.subprocess, "run", fake_run)
    return calls


def test_config_hosts_reads_the_url_out_of_a_credential_key(monkeypatch, git_present):
    _replies(monkeypatch, config_out=CONFIG_OUT)
    hosts = credhelper._config_hosts()
    # The bare `credential.helper` has no URL and is skipped; the rest keep only the
    # host, with the user and the repo path stripped.
    assert hosts == ["dev.azure.com", "ghe.service.group", "ghe.service.group"]


def test_config_hosts_survives_junk_entries(monkeypatch, git_present):
    _replies(monkeypatch, config_out="credential.helper x\ncredential..helper y\ngarbage\n")
    assert credhelper._config_hosts() == []


def test_gh_hosts_takes_the_unindented_lines(monkeypatch, git_present):
    gh = (
        "github.com\n"
        "  ✓ Logged in to github.com account phil (keyring)\n"
        "ghe.service.group\n"
        "  ✓ Logged in to ghe.service.group account a.harrison\n"
    )
    _replies(monkeypatch, gh_out=gh)
    assert credhelper._gh_hosts() == ["github.com", "ghe.service.group"]


def test_gh_hosts_is_empty_without_gh(monkeypatch):
    monkeypatch.setattr(credhelper.shutil, "which", lambda name: None)
    assert credhelper._gh_hosts() == []


def test_candidate_hosts_puts_the_callers_facts_first_and_dedupes(monkeypatch, git_present):
    _replies(monkeypatch, config_out=CONFIG_OUT, gh_out="github.com\n")
    hosts = credhelper.candidate_hosts(["ghe.service.group"])
    assert hosts[0] == "ghe.service.group"          # the caller's known host leads
    assert hosts.count("ghe.service.group") == 1    # and is not probed twice
    assert set(hosts) == {"ghe.service.group", "dev.azure.com", "github.com"}


def test_discover_returns_only_hosts_that_actually_have_a_credential(monkeypatch, git_present):
    # ghe answers, dev.azure.com does not — and gh_token has nothing either.
    _replies(monkeypatch, fills=[FakeProc(GHO), FakeProc(returncode=1)])
    found = credhelper.discover(["ghe.service.group", "dev.azure.com"])
    assert [p.host for p in found] == ["ghe.service.group"]
    assert found[0].kind == "gho_" and found[0].refreshable


def test_discover_is_bounded(monkeypatch, git_present):
    calls = _replies(monkeypatch, fills=[FakeProc(GHO)] * 20)
    credhelper.discover([f"h{i}.example.com" for i in range(20)], limit=3)
    assert len([c for c in calls if "fill" in c]) == 3


def test_discovery_never_reports_a_secret(monkeypatch, git_present):
    """A Probe carries the token's TYPE and nothing else — the same guarantee the
    single-host probe makes, held across the sweep."""
    _replies(monkeypatch, fills=[FakeProc("username=phil\npassword=gho_SECRET_VALUE\n")])
    found = credhelper.discover(["ghe.service.group"])
    assert "SECRET_VALUE" not in repr(found)
    assert "SECRET_VALUE" not in found[0].summary
