"""The diagnosis engine: probe outcomes, curated fixes, and the paste-safe report."""

from pathlib import Path

import pytest
import requests

from mooring import doctor
from mooring.config import Config
from mooring.github import AuthFailed, NotFound, RateLimited


@pytest.fixture
def cfg(tmp_path):
    return Config(client_id="cid", owner="acme", repo="nbs", workspace_path=str(tmp_path / "ws"))


# -- individual probes ---------------------------------------------------------


def test_python_probe_passes_on_this_interpreter(cfg):
    result = doctor._probe_python(cfg)
    assert result.status == doctor.PASS
    assert "Python 3." in result.detail


def test_reach_probe_sslerror_names_the_proxy_ca(cfg, monkeypatch):
    def boom(url, timeout):
        raise requests.exceptions.SSLError("bad handshake")

    monkeypatch.setattr(doctor.requests, "get", boom)
    result = doctor._probe_github_reach(cfg)
    assert result.status == doctor.FAIL
    assert "TLS" in result.detail
    assert "root CA" in result.fix  # the corporate-MITM wording, for the ticket


def test_reach_probe_connection_error(cfg, monkeypatch):
    def boom(url, timeout):
        raise requests.exceptions.ConnectionError("no route")

    monkeypatch.setattr(doctor.requests, "get", boom)
    result = doctor._probe_github_reach(cfg)
    assert result.status == doctor.FAIL
    assert "reach" in result.detail.lower()


def test_reach_probe_any_http_answer_is_reachable(cfg, monkeypatch):
    monkeypatch.setattr(doctor.requests, "get", lambda url, timeout: object())
    assert doctor._probe_github_reach(cfg).status == doctor.PASS


def _auth_probe_with(monkeypatch, cfg, exc):
    class FakeGH:
        def __init__(self, *a, **k):
            pass  # stub double: constructor args irrelevant

        def get_user(self):
            if exc is not None:
                raise exc
            return {"login": "phil"}

        def get_branch_head(self, branch):
            return "head-1"

    monkeypatch.setattr(doctor.auth, "get_token", lambda host=None: "tok")
    monkeypatch.setattr(doctor, "GitHubClient", FakeGH)
    return doctor._probe_github_auth(cfg)


def test_auth_probe_curated_fixes(cfg, monkeypatch):
    assert _auth_probe_with(monkeypatch, cfg, None).status == doctor.PASS
    expired = _auth_probe_with(monkeypatch, cfg, AuthFailed("401"))
    assert expired.status == doctor.FAIL and "mooring login" in expired.fix
    denied = _auth_probe_with(monkeypatch, cfg, NotFound("404"))
    assert denied.status == doctor.FAIL and "access" in denied.fix
    limited = _auth_probe_with(monkeypatch, cfg, RateLimited("429"))
    assert limited.status == doctor.WARN


def test_auth_probe_not_logged_in_and_not_configured(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.auth, "get_token", lambda host=None: "")
    assert doctor._probe_github_auth(cfg).status == doctor.WARN
    bare = Config(workspace_path=str(tmp_path / "w2"))
    assert doctor._probe_github_auth(bare).status == doctor.UNKNOWN


def test_config_probe_flags_corrupt_manifest_and_toml(cfg):
    ws = cfg.workspace()
    (ws / ".mooring").mkdir(parents=True)
    (ws / ".mooring" / "manifest.json").write_text("{corrupt", "utf-8")
    (ws / "mooring.toml").write_text("[guard\n", "utf-8")
    result = doctor._probe_config_files(cfg)
    assert result.status == doctor.FAIL
    assert "manifest" in result.detail
    assert "mooring.toml" in result.detail


def test_deps_probe_states(cfg):
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    assert doctor._probe_deps_lock(cfg).status == doctor.PASS  # no project at all
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n", "utf-8")
    missing_lock = doctor._probe_deps_lock(cfg)
    assert missing_lock.status == doctor.WARN
    assert "deps lock" in missing_lock.fix


def test_run_probes_never_raises(cfg, monkeypatch):
    def bomb(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "_PROBES", (bomb,))
    results = doctor.run_probes(cfg, extra_probes=[lambda: (_ for _ in ()).throw(ValueError())])
    assert [r.status for r in results] == [doctor.UNKNOWN, doctor.UNKNOWN]


# -- rendering and the paste-safe report ----------------------------------------


def test_render_lines_show_fix_only_on_non_pass():
    results = [
        doctor.ProbeResult("a", "Alpha", doctor.PASS, "Fine.", "should not show"),
        doctor.ProbeResult("b", "Beta", doctor.FAIL, "Broken.", "do the thing"),
    ]
    lines = doctor.render_lines(results)
    assert any("Alpha: Fine." in line for line in lines)
    assert not any("should not show" in line for line in lines)
    assert any("fix: do the thing" in line for line in lines)


def test_report_redaction_pins(tmp_path, monkeypatch):
    """The SECRET_VALUE_DO_NOT_LEAK-style pins that make "safe to paste" honest:
    the enterprise hostname, the OS username, and the home directory never
    appear in the copyable report."""
    cfg = Config(
        client_id="cid", owner="acme", repo="nbs",
        host="ghe.secret-corp.example", workspace_path=str(tmp_path / "ws"),
    )
    monkeypatch.setattr(doctor.getpass, "getuser", lambda: "SENTINEL_USER_X")
    home = str(Path.home())
    results = [
        doctor.ProbeResult(
            "x", "Probe", doctor.FAIL,
            f"Something at {home}\\ws failed against ghe.secret-corp.example "
            "for SENTINEL_USER_X.",
            "a fix line",
        )
    ]
    report = doctor.build_report(results, cfg)
    assert "ghe.secret-corp.example" not in report
    assert "SENTINEL_USER_X" not in report
    assert home not in report
    assert "~" in report  # collapsed, not dropped
    assert "1 fail" in report
    # Org/repo names identify the customer; workspace-path hints end in them.
    leaky = [doctor.ProbeResult("w", "Workspace", doctor.WARN, "Found acme and nbs in a path.")]
    scrubbed = doctor.build_report(leaky, cfg)
    assert "acme" not in scrubbed
    assert "nbs" not in scrubbed


def test_build_report_counts_header(cfg):
    results = [
        doctor.ProbeResult("a", "A", doctor.PASS, "ok"),
        doctor.ProbeResult("b", "B", doctor.WARN, "meh"),
    ]
    report = doctor.build_report(results, cfg)
    assert report.startswith("mooring doctor report")
    assert "1 pass, 1 warn, 0 fail, 0 unknown" in report