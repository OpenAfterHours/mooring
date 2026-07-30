"""The capability ladder: resolving a durable command, the task XML, and honest demotion.

Nothing here registers a real task or writes to the real Startup folder — ``schtasks`` and
the Startup directory are faked. What is pinned is the reasoning: an ephemeral install is
REFUSED rather than registered (a task pointing at a uvx cache breaks silently, which is the
one failure this feature cannot afford), the XML carries the four settings that decide
whether a laptop actually runs it, and a blocked Task Scheduler DEMOTES to the sign-in agent
with a stated reason instead of failing or silently doing nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mooring import schedule_os

WS = Path("C:/ws") if sys.platform == "win32" else Path("/ws")


# -- is the install durable? --------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "C:/Users/x/AppData/Local/uv/cache/environments-v2/abc/Scripts/mooring.exe",
        "C:/Users/x/AppData/Local/uv/cache/archive-v0/xyz/mooring.exe",
        "/home/x/.cache/uv/environments-v2/abc/bin/mooring",
    ],
)
def test_ephemeral_uv_locations_are_not_stable(path):
    # THE guard: a background task registered against a uvx cache breaks silently within a
    # fortnight, which is worse than having no background refresh at all.
    assert schedule_os.is_stable(path) is False


def test_the_temp_dir_is_not_stable(tmp_path):
    import tempfile

    assert schedule_os.is_stable(Path(tempfile.gettempdir()) / "mooring.exe") is False


def test_an_ordinary_install_is_stable():
    assert schedule_os.is_stable("C:/Users/x/.local/bin/mooring.exe") is True
    assert schedule_os.is_stable("/usr/local/bin/mooring") is True


def test_resolve_prefers_a_zipapp_then_the_shim_then_dash_m(monkeypatch, tmp_path):
    pyz = tmp_path / "mooring.pyz"
    pyz.write_text("", encoding="utf-8")
    python = tmp_path / ("python.exe" if sys.platform == "win32" else "python")
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(pyz)])
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(schedule_os, "is_stable", lambda p: True)
    assert schedule_os.resolve_command() == [str(python.resolve()), str(pyz.resolve())]

    # No zipapp: the console script sitting BESIDE this interpreter.
    monkeypatch.setattr(sys, "argv", ["mooring"])
    shim = tmp_path / ("mooring.exe" if sys.platform == "win32" else "mooring")
    shim.write_text("", encoding="utf-8")
    assert schedule_os.resolve_command() == [str(shim.resolve())]

    # No shim either: python -m mooring (which is why __main__.py exists).
    shim.unlink()
    assert schedule_os.resolve_command() == [str(python.resolve()), "-m", "mooring"]


def test_resolve_never_uses_a_bare_PATH_lookup(monkeypatch, tmp_path):
    # THE regression guard, from a real failure: shutil.which("mooring") found a leftover
    # `uv tool install` of version 0.4.15 beside the dev checkout. The task registered THAT
    # shim, which had no `refresh` command, so every background run died with an argparse
    # error nobody would ever see. Every candidate must be anchored to the running
    # interpreter instead.
    monkeypatch.setattr(
        schedule_os.shutil, "which", lambda name: pytest.fail("must not resolve mooring via PATH")
    )
    python = tmp_path / ("python.exe" if sys.platform == "win32" else "python")
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "argv", ["mooring"])
    monkeypatch.setattr(schedule_os, "is_stable", lambda p: True)  # tmp_path is under /tmp
    assert schedule_os.resolve_command() == [str(python.resolve()), "-m", "mooring"]


def test_an_entirely_ephemeral_install_is_refused_with_the_fix(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mooring"])
    monkeypatch.setattr(schedule_os, "is_stable", lambda p: False)
    with pytest.raises(schedule_os.UnstableInstall) as exc:
        schedule_os.resolve_command()
    assert "uv tool install mooring" in str(exc.value)


def test_python_m_mooring_actually_works():
    # resolve_command's last resort depends on the package being runnable that way.
    proc = subprocess.run(
        [sys.executable, "-m", "mooring", "version"], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0 and "mooring" in proc.stdout


# -- the task definition ------------------------------------------------------


def _xml():
    return schedule_os.task_xml(["C:/tools/mooring.exe", "refresh", "--due"], WS, "nbs")


def test_the_xml_carries_the_settings_a_laptop_actually_needs():
    xml = _xml()
    # DisallowStartIfOnBatteries DEFAULTS TO TRUE and analysts live on battery — leaving it
    # is the likeliest cause of a schedule that silently never runs.
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
    # A laptop asleep at 07:30 catches up on wake instead of skipping the day...
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    # ...but is never WOKEN to render a report.
    assert "<WakeToRun>false</WakeToRun>" in xml
    # A long run overlapping the next tick is dropped, not stacked.
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    # An OS-level backstop behind mooring's own run timeout.
    assert f"<ExecutionTimeLimit>{schedule_os.TASK_TIME_LIMIT}</ExecutionTimeLimit>" in xml


def test_the_task_needs_no_admin_and_no_stored_password():
    xml = _xml()
    # InteractiveToken == "only when the user is signed in", which needs no stored password
    # and no elevation — the only variant that survives corporate policy.
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "Password" not in xml


def test_the_task_runs_below_normal_priority_and_catches_up_at_logon():
    xml = _xml()
    assert "<Priority>7</Priority>" in xml  # don't fight the user's foreground work
    assert "<LogonTrigger>" in xml  # the tier-3 equivalent of catch-up-on-open
    assert f"<Interval>{schedule_os.TASK_INTERVAL}</Interval>" in xml


def test_the_xml_escapes_its_inputs():
    xml = schedule_os.task_xml(["C:/a & b/mooring.exe", "refresh"], WS, "a<b>")
    assert "a &amp; b" in xml and "a&lt;b&gt;" in xml


def test_one_task_per_repo():
    assert schedule_os.task_name("nbs") != schedule_os.task_name("other")
    assert "nbs" in schedule_os.task_name("nbs")


# -- the ladder ---------------------------------------------------------------


def _fake_schtasks(monkeypatch, *, present=True, create_rc=0, output=""):
    monkeypatch.setattr(schedule_os, "_schtasks", lambda: "schtasks" if present else None)

    def _run(args):
        return subprocess.CompletedProcess(args, create_rc, output, "")

    monkeypatch.setattr(schedule_os, "_run", _run)


def test_enable_prefers_the_os_task(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule_os, "resolve_command", lambda: ["mooring.exe"])
    _fake_schtasks(monkeypatch)
    monkeypatch.setattr(schedule_os, "remove_agent", lambda alias: False)
    installed = schedule_os.enable(tmp_path, "nbs")
    assert installed.tier == schedule_os.TIER_OS_TASK
    assert installed.reason == ""


def test_a_blocked_task_scheduler_demotes_to_the_agent_and_says_why(monkeypatch, tmp_path):
    # THE managed-laptop case, and the reason the ladder exists: policy refuses the task, so
    # mooring falls back to something needing no admin AND states what happened.
    monkeypatch.setattr(schedule_os, "resolve_command", lambda: ["python.exe", "-m", "mooring"])
    _fake_schtasks(monkeypatch, create_rc=1, output="ERROR: Access is denied.")
    startup = tmp_path / "Startup"
    startup.mkdir()
    monkeypatch.setattr(schedule_os, "startup_dir", lambda: startup)

    installed = schedule_os.enable(tmp_path, "nbs")

    assert installed.tier == schedule_os.TIER_LOGON_AGENT
    assert "access denied" in installed.reason.lower()
    script = next(startup.glob("*.cmd"))
    assert "refresh" in script.read_text("utf-8") and "--agent" in script.read_text("utf-8")


def test_with_neither_available_it_refuses_rather_than_pretending(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule_os, "resolve_command", lambda: ["mooring.exe"])
    _fake_schtasks(monkeypatch, present=False)
    monkeypatch.setattr(schedule_os, "startup_dir", lambda: None)
    with pytest.raises(schedule_os.UnstableInstall) as exc:
        schedule_os.enable(tmp_path, "nbs")
    # The user is told refreshes still work via the hub — the ladder's floor, not a dead end.
    assert "hub is open" in str(exc.value)


def test_the_agent_launcher_is_windowless_when_pythonw_is_available(monkeypatch, tmp_path):
    # A resident agent with a console window open all session would be intolerable.
    (tmp_path / "python.exe").write_text("", encoding="utf-8")
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    cmd = schedule_os.agent_command([str(tmp_path / "python.exe"), "-m", "mooring"], "nbs")
    assert cmd[0].endswith("pythonw.exe")
    assert cmd[-3:] == ["--agent", "--repo", "nbs"]


def test_the_agent_launcher_falls_back_when_there_is_no_pythonw(tmp_path):
    exe = str(tmp_path / "mooring.exe")
    assert schedule_os.agent_command([exe], "nbs")[0] == exe


def test_the_startup_folder_is_used_not_the_run_registry_key():
    # Run-key persistence is textbook malware behaviour and EDR flags it; a file in the
    # Startup folder is unremarkable. Pinned so nobody "simplifies" it later — by parsing,
    # so the module's own prose EXPLAINING the choice doesn't trip its own guard.
    import ast

    tree = ast.parse(Path(schedule_os.__file__).read_text("utf-8"))
    imported = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "winreg" not in imported  # the only in-process way to write the Run key

    docstrings = {
        text
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for text in [ast.get_docstring(node, clean=False)]
        if text
    }
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]
    # ...and no shell-out to reg.exe either.
    assert not any("CurrentVersion" in s or "reg add" in s.lower() for s in literals)


def test_current_tier_reports_what_is_actually_registered(monkeypatch):
    monkeypatch.setattr(schedule_os, "task_exists", lambda alias: True)
    assert schedule_os.current_tier("nbs") == schedule_os.TIER_OS_TASK
    monkeypatch.setattr(schedule_os, "task_exists", lambda alias: False)
    monkeypatch.setattr(schedule_os, "agent_installed", lambda alias: True)
    assert schedule_os.current_tier("nbs") == schedule_os.TIER_LOGON_AGENT
    monkeypatch.setattr(schedule_os, "agent_installed", lambda alias: False)
    # Nothing registered: the honest answer is the hub's own sweep, not "off".
    assert schedule_os.current_tier("nbs") == schedule_os.TIER_HUB_CATCHUP


def test_probe_never_creates_a_task(monkeypatch):
    # A speculative create/delete would litter the user's Task Scheduler with probe entries;
    # the authoritative answer comes from enable() instead.
    calls = []
    monkeypatch.setattr(schedule_os, "_run", lambda args: calls.append(args))
    monkeypatch.setattr(schedule_os, "resolve_command", lambda: ["mooring.exe"])
    schedule_os.probe()
    assert calls == []


def test_probe_explains_an_unstable_install(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        schedule_os, "resolve_command",
        lambda: (_ for _ in ()).throw(schedule_os.UnstableInstall("temporary location")),
    )
    capability = schedule_os.probe()
    assert capability.can_background is False
    assert "temporary location" in capability.reason


def test_probe_is_honest_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    capability = schedule_os.probe()
    assert capability.tier == schedule_os.TIER_HUB_TIMER
    assert "Windows-only" in capability.reason
