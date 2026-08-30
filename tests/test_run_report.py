"""The copilot's runtime-error loop: run the notebook after an Apply, report value-safely.

Everything a weak model actually gets wrong — a column that isn't there, an API called the
wrong way, a name that only resolves at runtime — is invisible to mooring's static checks,
because seeing it means RUNNING the cell and mooring never opens a marimo websocket. This is
the one route back: an explicit click runs the existing verify smoke path and hands the
assistant a sanitised failure summary.

The marimo export subprocess is faked at the shared runner's ``_exec`` seam (a real one
spawns a kernel), so these exercise the WHOLE chain — stderr -> ``notebook_run.failure_lines``
-> ``ChatSessionBase.run_failure_report`` -> ``egress.sanitize_traceback`` -> the session —
rather than a stub of it. That matters for the leak tests below: the thing being proved is
that a value on marimo's stderr has no path to the model, and only the real chain can prove
it.

The critical fact these are built around, verified against a real ``marimo export html``:
**marimo's stderr is not a log.** The exporter echoes every cell's own ``print`` output onto
it — a printed dataframe lands there in full, values and all — interleaved with one
``<MarimoErrorClass>: <message>`` line per failed cell. Handing that stream to the traceback
sanitiser would leak it wholesale (the sanitiser rewrites only DETECTED traceback blocks and
leaves surrounding prose untouched), so the console half is dropped before anything is
composed, and the message half is rewritten.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mooring import config, paths, policy, verify, workspace_config
from mooring.app import notebook_run, run_report
from mooring.hub.server import Hub, create_app

SECRET = "SECRET_VALUE_DO_NOT_LEAK"

# A real marimo notebook (cellwrite/verify both parse it) whose source mentions `revenue`,
# so a `revenue` token in an error message is one the model has already been shown.
NOTEBOOK = (
    "import marimo\n\n"
    '__generated_with = "0.23.9"\n'
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    revenue = 1\n"
    "    return (revenue,)\n\n\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)

# What the exporter really writes when a cell prints a dataframe and a later cell raises.
STDERR_WITH_CONSOLE = (
    "shape: (1, 2)\n"
    "| cust                     | amt              |\n"
    f"| {SECRET} | 4012888888881881 |\n"
    f"MarimoExceptionRaisedError: '{SECRET}'\n"
    "Error: Export was successful, but some cells failed to execute.\n"
)


def _out_of(cmd):
    for i, tok in enumerate(cmd):
        if tok == "-o":
            return Path(cmd[i + 1])
    return None


def _fake_export(returncode, stderr="", *, produce=True):
    """Stand in for ``notebook_run._exec`` — marimo writes its render whenever it actually
    ran, even on a cell failure. ``produce=False`` is the environment failure."""

    def _run(cmd, cwd, env, timeout, cancel=None):
        if produce:
            out = _out_of(cmd)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f"<html>{SECRET}</html>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return _run


@pytest.fixture
def client_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    monkeypatch.delenv("MOORING_GITHUB_HOST", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(ws))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws"))
    with TestClient(create_app(hub)) as client:
        yield client, hub, ws


@pytest.fixture
def stub_chat(monkeypatch):
    """The no-LLM stub session, with the traceback guard ARMED (its default) so these
    also prove the report does not trip the paste guard's hold."""
    from mooring.ai.chat import StubChatSession

    monkeypatch.setattr(
        Hub,
        "_make_chat_session",
        lambda self, ctx, ws, nb, **kw: StubChatSession(
            system_context=ctx, traceback_guard=True, workspace=ws, notebook_rel=nb
        ),
    )


def _open(client, ws, rel="nb.py", source=NOTEBOOK):
    (ws / rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / rel).write_text(source, encoding="utf-8")
    return client.post("/api/ai/chat/open", json={"notebook": rel}).json()["sid"]


# -- notebook_run.failure_lines: the narrow slice -------------------------------


def test_failure_lines_keeps_only_marimo_error_lines():
    pairs = notebook_run.failure_lines(STDERR_WITH_CONSOLE)
    assert pairs == [("MarimoExceptionRaisedError", f"'{SECRET}'")]
    # The console echo — the half that carries real values — has no representation at all.
    assert not any(SECRET in kind for kind, _ in pairs)


def test_failure_lines_kind_is_a_constant_not_lifted_text():
    # A crafted "kind" cannot ride out of stderr: only names in the closed table match,
    # and the constant is what's returned.
    pairs = notebook_run.failure_lines(f"{SECRET}Error: boom\nMarimoSyntaxError: invalid syntax\n")
    assert pairs == [("MarimoSyntaxError", "invalid syntax")]


def test_failure_lines_is_empty_for_a_clean_stream():
    assert notebook_run.failure_lines("") == []
    assert notebook_run.failure_lines("shape: (1, 2)\nall good\n") == []


def test_run_passes_failures_only_when_asked(tmp_path, monkeypatch):
    # Rule 3 unchanged by default: a caller that does not opt in learns only the count.
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, STDERR_WITH_CONSOLE))
    ws = tmp_path / "ws"
    (ws / "nb.py").parent.mkdir(parents=True, exist_ok=True)
    (ws / "nb.py").write_text(NOTEBOOK, encoding="utf-8")
    out = ws / ".mooring" / "verify" / "nb.html"

    seen: list = []
    outcome = notebook_run.run(ws, "nb.py", out, keep_on_success=False)
    assert outcome.cells_failed == 1 and not seen

    notebook_run.run(ws, "nb.py", out, keep_on_success=False, on_failures=seen.extend)
    assert seen == [("MarimoExceptionRaisedError", f"'{SECRET}'")]


# -- the endpoint ---------------------------------------------------------------


def test_run_report_on_a_clean_run_sends_nothing(client_hub, stub_chat, monkeypatch):
    client, hub, ws = client_hub
    sid = _open(client, ws)
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(0))

    resp = client.post("/api/ai/chat/run-report", json={"sid": sid})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ran_clean"] is True and body["sent"] == ""
    assert hub._chats[sid].last_sent == ""  # the model is told nothing on success
    # It IS a verify: the ordinary receipt is recorded, so the trust badge follows.
    assert verify.read_results(ws)["nb.py"]["passed"] is True


def test_run_report_reaches_the_model_with_the_failure(client_hub, stub_chat, monkeypatch):
    client, hub, ws = client_hub
    sid = _open(client, ws)
    stderr = "MarimoExceptionRaisedError: division by zero\n"
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, stderr))

    resp = client.post("/api/ai/chat/run-report", json={"sid": sid})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ran_clean"] is False and body["cells_failed"] == 1
    sent = hub._chats[sid].last_sent
    # A fixed interpreter message is on the sanitiser's allowlist, so it survives whole —
    # which is the point: this is the text that lets the model actually fix the cell.
    assert "MarimoExceptionRaisedError: division by zero" in sent
    assert body["sent"] == sent  # what the analyst is shown IS what was sent
    # The failing run badges the row, exactly as a hand-run Verify would.
    assert verify.read_results(ws)["nb.py"]["passed"] is False


def test_run_report_never_sends_a_value_from_the_error_message(client_hub, stub_chat, monkeypatch):
    client, hub, ws = client_hub
    sid = _open(client, ws)
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, STDERR_WITH_CONSOLE))

    resp = client.post("/api/ai/chat/run-report", json={"sid": sid})

    sent = hub._chats[sid].last_sent
    assert sent  # something was reported
    # The RAW message never reaches the model, and neither does the console echo that
    # shared the stream with it (the printed dataframe, and the card number in it).
    assert SECRET not in sent
    assert "4012888888881881" not in sent
    assert "shape: (1, 2)" not in sent
    assert SECRET not in resp.text and "4012888888881881" not in resp.text
    # What survives is the kind plus a shape-preserving placeholder.
    assert "MarimoExceptionRaisedError: <redacted:" in sent
    assert resp.json()["redactions"]  # and the withholding is reported, value-free
    assert all(SECRET not in f["kind"] for f in resp.json()["redactions"])


def test_run_report_rescues_a_message_the_model_has_already_seen(client_hub, stub_chat, monkeypatch):
    # The whole point of the loop: a wrong COLUMN name is in the notebook source the model
    # was already shown, so restating it reveals nothing new and the model gets a usable
    # error. A value it has never seen does not get the same rescue (test above).
    client, hub, ws = client_hub
    sid = _open(client, ws)
    monkeypatch.setattr(
        notebook_run, "_exec", _fake_export(1, "MarimoExceptionRaisedError: 'revenue'\n")
    )

    client.post("/api/ai/chat/run-report", json={"sid": sid})

    assert "MarimoExceptionRaisedError: 'revenue'" in hub._chats[sid].last_sent


def test_run_report_does_not_trip_the_traceback_hold(client_hub, stub_chat, monkeypatch):
    # The summary is composed from a synthetic traceback, but the synthetic header is
    # dropped — so the outbound text carries no traceback block, the paste guard does not
    # hold it, and the analyst is not asked to confirm text they never wrote.
    client, hub, ws = client_hub
    sid = _open(client, ws)
    monkeypatch.setattr(
        notebook_run, "_exec", _fake_export(1, "MarimoExceptionRaisedError: division by zero\n")
    )

    client.post("/api/ai/chat/run-report", json={"sid": sid})

    sent = hub._chats[sid].last_sent
    assert sent and "Traceback (most recent call last):" not in sent
    assert 'File "' not in sent  # no frame line: nothing for a source re-read to key off


def test_run_report_caps_how_many_failures_it_names(client_hub, stub_chat, monkeypatch):
    client, hub, ws = client_hub
    sid = _open(client, ws)
    stderr = "".join("MarimoExceptionRaisedError: division by zero\n" for _ in range(12))
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, stderr))

    client.post("/api/ai/chat/run-report", json={"sid": sid})

    sent = hub._chats[sid].last_sent
    assert sent.count("MarimoExceptionRaisedError") == 8
    assert "and 4 more failing cell(s)" in sent
    assert "12 cells failed" in sent


def test_run_report_on_a_failure_with_no_readable_reason(client_hub, stub_chat, monkeypatch):
    # Non-zero exit, render written, but no line mooring recognises: report the run to the
    # ANALYST and send the model nothing rather than inventing a summary.
    client, hub, ws = client_hub
    sid = _open(client, ws)
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, "something went wrong\n"))

    body = client.post("/api/ai/chat/run-report", json={"sid": sid}).json()

    assert body["ran_clean"] is False and body["sent"] == ""
    assert hub._chats[sid].last_sent == ""


def test_run_report_environment_failure_is_502(client_hub, stub_chat, monkeypatch):
    client, hub, ws = client_hub
    sid = _open(client, ws)
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, "", produce=False))

    resp = client.post("/api/ai/chat/run-report", json={"sid": sid})

    assert resp.status_code == 502 and "dependencies" in resp.json()["error"]
    assert hub._chats[sid].last_sent == ""


def test_run_report_unknown_sid_is_404(client_hub, stub_chat):
    client, _hub, _ws = client_hub
    assert client.post("/api/ai/chat/run-report", json={"sid": "nope"}).status_code == 404


def test_run_report_refuses_a_disabled_notebook(client_hub, stub_chat, monkeypatch):
    client, hub, ws = client_hub
    sid = _open(client, ws)
    ran = []
    monkeypatch.setattr(notebook_run, "_exec", lambda *a, **k: ran.append(1))
    workspace_config.set_ai_disabled(ws, "nb.py", True)

    resp = client.post("/api/ai/chat/run-report", json={"sid": sid})

    assert resp.status_code == 403 and resp.json()["reason"] == "notebook_disabled"
    assert not ran  # refused BEFORE the run: a disabled notebook is not even executed
    assert sid not in hub._chats  # and the session is torn down, as on send/apply


def test_run_report_respects_the_policy_ai_off_glob(client_hub, stub_chat, monkeypatch):
    # policy.ai_gate unions the per-notebook opt-out with the admin's ai_off globs, and
    # this route is gated by the same one call apply/rollback use.
    client, hub, ws = client_hub
    sid = _open(client, ws, rel="reports/board.py")
    ran = []
    monkeypatch.setattr(notebook_run, "_exec", lambda *a, **k: ran.append(1))
    (ws / "mooring.toml").write_text('[policy]\nai_off = ["reports/**"]\n', encoding="utf-8")
    assert policy.ai_disabled(ws, "reports/board.py")  # the fixture is really in force

    resp = client.post("/api/ai/chat/run-report", json={"sid": sid})

    assert resp.status_code == 403 and resp.json()["reason"] == "notebook_disabled"
    assert not ran


def test_run_report_disabled_mid_run_is_refused_before_the_send(client_hub, stub_chat, monkeypatch):
    # The run takes minutes; the send is the egress. A disable landing inside that window
    # (a teammate's sync, a hub toggle) must stop the report, not merely the next one.
    client, hub, ws = client_hub
    sid = _open(client, ws)

    def _disable_then_fail(cmd, cwd, env, timeout, cancel=None):
        out = _out_of(cmd)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html/>", encoding="utf-8")
        workspace_config.set_ai_disabled(ws, "nb.py", True)
        return subprocess.CompletedProcess(
            cmd, 1, "", "MarimoExceptionRaisedError: division by zero\n"
        )

    session = hub._chats[sid]
    monkeypatch.setattr(notebook_run, "_exec", _disable_then_fail)

    resp = client.post("/api/ai/chat/run-report", json={"sid": sid})

    assert resp.status_code == 403 and resp.json()["reason"] == "notebook_disabled"
    assert session.last_sent == ""  # nothing left the machine
    assert sid not in hub._chats


def test_run_report_deletes_the_value_bearing_render(client_hub, stub_chat, monkeypatch):
    # Rule 1 of the shared runner, inherited rather than re-implemented: the HTML embeds
    # real outputs and is gone on every path.
    client, hub, ws = client_hub
    sid = _open(client, ws)
    monkeypatch.setattr(notebook_run, "_exec", _fake_export(1, STDERR_WITH_CONSOLE))

    client.post("/api/ai/chat/run-report", json={"sid": sid})

    assert list(verify.verify_dir(ws).glob("*.html")) == []


# -- the app service, driven directly -------------------------------------------


def test_run_and_report_raises_permission_error_when_disabled_before_the_send(
    tmp_path, monkeypatch
):
    from mooring.ai.chat import StubChatSession

    cfg = config.Config(client_id="cid", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "nb.py").write_text(NOTEBOOK, encoding="utf-8")
    workspace_config.set_ai_disabled(ws, "nb.py", True)
    monkeypatch.setattr(
        notebook_run, "_exec", _fake_export(1, "MarimoExceptionRaisedError: division by zero\n")
    )
    session = StubChatSession(traceback_guard=True, workspace=ws, notebook_rel="nb.py")

    with pytest.raises(PermissionError):
        run_report.run_and_report(session, cfg, "nb.py")
    assert session.last_sent == ""


def test_run_and_report_refuses_a_non_notebook(tmp_path, monkeypatch):
    from mooring.ai.chat import StubChatSession

    cfg = config.Config(client_id="cid", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    ws = cfg.workspace()
    ws.mkdir(parents=True)
    (ws / "helper.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    session = StubChatSession()

    with pytest.raises(run_report.ReportError):
        run_report.run_and_report(session, cfg, "helper.py")
