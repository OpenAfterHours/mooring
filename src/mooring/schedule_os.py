"""Registering mooring's refresh clock with the OS — the top of the capability ladder.

Scheduled refresh works for everyone through the hub (tiers 0 and 1: catch up when the hub
opens, then tick while it is open). This module adds the two tiers that fire when the hub is
NOT open, and the whole design assumption is that **neither may be required**:

===== ==================================== ================================================
Tier  Mechanism                            Needs
===== ==================================== ================================================
2     a ``.cmd`` in the per-user Startup   write access to ``%APPDATA%\\…\\Startup``
      folder launching a resident agent    (no admin, no installer, no registry)
3     a Windows Task Scheduler task        ``schtasks.exe`` permitted by policy
===== ==================================== ================================================

Tier 3 is better and is tried first: the OS owns the schedule, there is no resident process,
and a missed run catches up on wake. Tier 2 is the fallback for the very common managed-laptop
case where ``schtasks`` is blocked by AppLocker or Group Policy.

**Capability is probed, never assumed — in either direction.** A logon-context task
(``InteractiveToken`` + ``LeastPrivilege``, no stored password) is often permitted for
standard users; it is the "run whether the user is logged on or not" variant that reliably
needs elevation. So this never says "you need admin" without asking, and never registers a
task without checking the answer. A demotion is a normal outcome that records WHY, so
``mooring doctor`` can say *"Task Scheduler is blocked by policy — refreshes run when you
open mooring instead"* rather than leaving a user to wonder.

**The invocation must be stable.** A task pointing at an ephemeral ``uvx`` cache breaks
silently within a fortnight, which is precisely the failure mode this feature cannot afford.
:func:`resolve_command` refuses rather than registering one (see :class:`UnstableInstall`).

L1 leaf: stdlib only, so it stays importable from the CLI, the hub, and ``doctor.py`` alike.
Nothing here runs a notebook — it only arranges for ``mooring refresh`` to be invoked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Ladder rungs. 0/1 are the hub's own sweep (no OS involvement); this module owns 2 and 3.
TIER_HUB_CATCHUP = 0
TIER_HUB_TIMER = 1
TIER_LOGON_AGENT = 2
TIER_OS_TASK = 3

TIER_NAMES = {
    TIER_HUB_CATCHUP: "when you open the hub",
    TIER_HUB_TIMER: "while the hub is open",
    TIER_LOGON_AGENT: "whenever you are signed in",
    TIER_OS_TASK: "in the background, even with the hub closed",
}

# How often the OS task fires. mooring itself decides what is actually due, so this is only
# the resolution of the clock — 30 minutes keeps a 07:30 daily schedule punctual enough
# without waking the machine or churning.
TASK_INTERVAL = "PT30M"
# A backstop the OS enforces even if mooring's own timeout is somehow not reached.
TASK_TIME_LIMIT = "PT1H"

# Paths that mean "this install can vanish": uv's ephemeral tool/environment caches (what
# `uvx mooring` runs from) and the OS temp dir.
_EPHEMERAL_MARKERS = ("/uv/cache/", "/environments-v", "/archive-v", "/.cache/uv/")

_STARTUP_REL = ("Microsoft", "Windows", "Start Menu", "Programs", "Startup")


class UnstableInstall(Exception):
    """mooring is running from a location that will not still be there tomorrow (an ephemeral
    ``uvx`` cache), so no durable command can be registered. ``str(exc)`` is the user-facing
    reason and carries the fix."""


@dataclass(frozen=True)
class Capability:
    """What background tier this machine can support, and why not a higher one."""

    tier: int
    reason: str = ""  # curated, value-free; "" when nothing is holding it back
    command: tuple[str, ...] = ()  # the stable invocation, or () when unresolvable

    @property
    def can_background(self) -> bool:
        return self.tier >= TIER_LOGON_AGENT

    def describe(self) -> str:
        base = f"Refreshes run {TIER_NAMES[self.tier]}."
        return f"{base} {self.reason}".strip()


@dataclass(frozen=True)
class Installed:
    """The outcome of enabling background refresh."""

    tier: int
    detail: str  # what was registered, in human terms
    reason: str = ""  # why not a higher tier


# -- resolving a durable command ---------------------------------------------


def _normalise(path: str | Path) -> str:
    try:
        return str(Path(path).resolve()).replace("\\", "/").lower()
    except OSError:
        return str(path).replace("\\", "/").lower()


def is_stable(path: str | Path) -> bool:
    """Whether ``path`` will still exist next week.

    Rejects the OS temp dir and uv's ephemeral caches. This is the check that stops a
    background task being registered against a ``uvx`` run that uv may garbage-collect —
    a task that breaks silently is worse than no task at all."""
    text = _normalise(path)
    temp = _normalise(tempfile.gettempdir())
    if text.startswith(temp + "/"):
        return False
    return not any(marker in text for marker in _EPHEMERAL_MARKERS)


def _interpreter_shim() -> Path | None:
    """The ``mooring`` console script belonging to the RUNNING interpreter, if it has one."""
    base = Path(sys.executable).resolve().parent
    name = "mooring.exe" if sys.platform == "win32" else "mooring"
    # A venv puts python and its scripts in the same dir (Scripts/ on Windows, bin/ on
    # POSIX); a system install keeps scripts in a sibling.
    for directory in (base, base / "Scripts", base / "bin"):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def resolve_command() -> list[str]:
    """The durable command that runs **this** mooring on this machine.

    Tried in order of directness: a frozen ``.exe``, a ``.pyz`` zipapp, the console script
    belonging to the running interpreter, then ``python -m mooring`` on that same
    interpreter. Raises :class:`UnstableInstall` when every candidate lives somewhere
    ephemeral.

    **Never a bare PATH lookup.** ``shutil.which("mooring")`` finds whatever ``mooring``
    happens to mean on PATH, which is frequently a *different installation* — a leftover
    ``uv tool install`` beside a dev checkout, or a half-finished upgrade. Registering that
    silently binds the background clock to another (often older) mooring: observed in
    testing as a task that ran a 0.4.15 shim, which had no ``refresh`` command at all and
    failed with an argparse error nobody would ever see. Every candidate here is anchored to
    the interpreter actually executing this code, so the task runs the mooring that
    registered it."""
    if getattr(sys, "frozen", False):  # a packaged single-file build
        exe = Path(sys.executable).resolve()
        if is_stable(exe):
            return [str(exe)]

    argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.suffix.lower() == ".pyz" and argv0.is_file():
        if is_stable(argv0) and is_stable(sys.executable):
            return [str(Path(sys.executable).resolve()), str(argv0)]

    shim = _interpreter_shim()
    if shim is not None and is_stable(shim):
        return [str(shim)]

    if is_stable(sys.executable):
        # Works because mooring ships a __main__.py; needs only an importable install, and
        # runs THIS interpreter's copy of the package.
        return [str(Path(sys.executable).resolve()), "-m", "mooring"]

    raise UnstableInstall(
        "mooring is running from a temporary location (a uvx cache), which may be cleaned up "
        "at any time — a background refresh registered against it would stop working "
        "silently. Install it durably first: `uv tool install mooring` (or `pip install "
        "mooring`), then enable background refresh again."
    )


# -- probing ------------------------------------------------------------------


def startup_dir() -> Path | None:
    """The per-user Startup folder — writable by any user, no admin and no registry.

    Deliberately NOT ``HKCU\\…\\CurrentVersion\\Run``, which is equally permission-free but is
    textbook malware persistence and is routinely flagged by EDR. A file in the Startup
    folder is unremarkable by comparison."""
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata).joinpath(*_STARTUP_REL)


def _schtasks() -> str | None:
    return shutil.which("schtasks")


def _startup_writable() -> bool:
    directory = startup_dir()
    if directory is None:
        return False
    return os.access(directory, os.W_OK) if directory.is_dir() else False


def probe() -> Capability:
    """The highest tier this machine looks able to support, with a curated reason if lower.

    Cheap and NON-INTRUSIVE: it checks the platform, that a durable command exists, that
    ``schtasks.exe`` is present, and that the Startup folder is writable. It deliberately does
    NOT speculatively create a task to find out whether policy permits it — that would litter
    the user's Task Scheduler with probe entries. The authoritative answer comes from
    :func:`install`, which reports what it actually managed to register."""
    if sys.platform != "win32":
        return Capability(
            TIER_HUB_TIMER,
            "Background refresh is Windows-only for now; on this platform mooring refreshes "
            "while the hub is open.",
        )
    try:
        command = tuple(resolve_command())
    except UnstableInstall as exc:
        return Capability(TIER_HUB_TIMER, str(exc))
    if _schtasks():
        return Capability(TIER_OS_TASK, "", command)
    if _startup_writable():
        return Capability(
            TIER_LOGON_AGENT,
            "Task Scheduler is not available on this machine, so mooring uses a sign-in "
            "agent instead.",
            command,
        )
    return Capability(
        TIER_HUB_TIMER,
        "Neither Task Scheduler nor the Startup folder is available on this machine.",
        command,
    )


# -- tier 3: the Task Scheduler task -----------------------------------------


def task_name(alias: str) -> str:
    """One task per repo. Flat (no ``\\mooring\\`` folder): creating a Task Scheduler folder
    can itself need permissions the user does not have, and the whole point here is to need
    as little as possible."""
    return f"mooring refresh - {alias or 'default'}"


def _user_id() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain and user else user


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def task_xml(command: list[str], workspace: Path, alias: str) -> str:
    """The task definition.

    ``/Create /XML`` rather than the flag form because the flags cannot express the four
    settings that decide whether this works on a real laptop:

    * ``DisallowStartIfOnBatteries`` — **the default is true**, and analysts run on battery
      constantly. Leaving it is the single likeliest cause of a schedule that silently never
      runs, which is the exact failure this feature cannot afford.
    * ``StartWhenAvailable`` — a laptop asleep at 07:30 catches up when it wakes instead of
      skipping the day.
    * ``MultipleInstancesPolicy=IgnoreNew`` — a long run overlapping the next tick is dropped,
      not stacked.
    * ``ExecutionTimeLimit`` — an OS-level backstop behind mooring's own run timeout.

    ``WakeToRun`` stays false on purpose: waking a laptop to render a report is user-hostile,
    and ``StartWhenAvailable`` already covers the miss. Priority 7 (below normal) keeps a
    refresh from fighting the user's foreground work.
    """
    exe = _xml_escape(command[0])
    args = " ".join(f'"{a}"' if " " in a else a for a in command[1:])
    user = _xml_escape(_user_id())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Refreshes mooring notebooks that are due for {_xml_escape(alias)}. Runs locally; never pushes.</Description>
    <Author>{user}</Author>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
    <CalendarTrigger>
      <StartBoundary>2020-01-01T00:00:00</StartBoundary>
      <Repetition>
        <Interval>{TASK_INTERVAL}</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
      <Enabled>true</Enabled>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{TASK_TIME_LIMIT}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>{_xml_escape(args)}</Arguments>
      <WorkingDirectory>{_xml_escape(str(workspace))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=30)


def install_task(command: list[str], workspace: Path, alias: str) -> None:
    """Register (or replace) the Task Scheduler task. Raises OSError on refusal.

    The XML is written as UTF-16: ``schtasks /Create /XML`` rejects a UTF-8 file on several
    Windows builds with a bare "the task XML is malformed", which is a miserable thing to
    debug from a support ticket."""
    tool = _schtasks()
    if tool is None:
        raise OSError("schtasks.exe is not available on this machine.")
    xml = task_xml(command, workspace, alias)
    handle, temp_path = tempfile.mkstemp(suffix=".xml", prefix="mooring-task-")
    try:
        with os.fdopen(handle, "w", encoding="utf-16") as fh:
            fh.write(xml)
        proc = _run([tool, "/Create", "/TN", task_name(alias), "/XML", temp_path, "/F"])
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    if proc.returncode != 0:
        raise OSError(_task_error(proc))
    return None


def _task_error(proc: subprocess.CompletedProcess) -> str:
    """A curated one-liner from a schtasks failure. The raw output is a localised, chatty
    blob; the access-denied case is the one that matters and gets its own wording so the
    fallback to tier 2 can be explained rather than just happening."""
    blob = f"{proc.stdout or ''} {proc.stderr or ''}".strip()
    lowered = blob.lower()
    if "access is denied" in lowered or "5)" in lowered or "denied" in lowered:
        return "Task Scheduler refused the task (access denied — usually a policy on managed machines)."
    tail = next((line.strip() for line in reversed(blob.splitlines()) if line.strip()), "")
    return f"Task Scheduler refused the task.{(' ' + tail) if tail else ''}"


def task_exists(alias: str) -> bool:
    tool = _schtasks()
    if tool is None:
        return False
    try:
        return _run([tool, "/Query", "/TN", task_name(alias)]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def remove_task(alias: str) -> bool:
    """Delete the task. Returns whether one was there. Never raises."""
    tool = _schtasks()
    if tool is None:
        return False
    try:
        return _run([tool, "/Delete", "/TN", task_name(alias), "/F"]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# -- tier 2: the sign-in agent ------------------------------------------------


def agent_script(alias: str) -> Path | None:
    directory = startup_dir()
    return None if directory is None else directory / f"mooring-refresh-{alias or 'default'}.cmd"


def _windowless(command: list[str]) -> list[str]:
    """Swap ``python.exe`` for ``pythonw.exe`` so the resident agent carries no console
    window for the whole session. Falls back to the original when there is no pythonw
    beside it (a frozen build, or an unusual layout)."""
    exe = Path(command[0])
    if exe.name.lower() == "python.exe":
        quiet = exe.with_name("pythonw.exe")
        if quiet.is_file():
            return [str(quiet), *command[1:]]
    return command


def agent_command(command: list[str], alias: str) -> list[str]:
    return [*_windowless(command), "refresh", "--agent", "--repo", alias]


def install_agent(command: list[str], workspace: Path, alias: str) -> Path:
    """Write the Startup-folder launcher for the resident agent. Raises OSError on refusal.

    A plain ``.cmd``: creating a ``.lnk`` needs COM or a PowerShell shell-out, and a ``.vbs``
    — the other windowless trick — is exactly the shape EDR flags. The cost is a brief
    console flash at sign-in while ``cmd.exe`` starts the agent detached; ``pythonw`` keeps
    the agent itself windowless thereafter. Tier 3 avoids the flash entirely, which is one
    more reason it is tried first."""
    script = agent_script(alias)
    if script is None:
        raise OSError("This machine has no per-user Startup folder.")
    args = agent_command(command, alias)
    quoted = " ".join(f'"{a}"' if " " in a else a for a in args)
    body = (
        "@echo off\r\n"
        "rem Written by mooring (schedule_os.install_agent). Starts the background refresh\r\n"
        "rem agent at sign-in. Delete this file, or run `mooring schedule background disable`,\r\n"
        "rem to stop it. It never pushes; it only refreshes notebooks that are due.\r\n"
        f'cd /d "{workspace}"\r\n'
        f"start \"mooring refresh\" /b {quoted}\r\n"
    )
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body, encoding="utf-8")
    return script


def agent_installed(alias: str) -> bool:
    script = agent_script(alias)
    return bool(script and script.is_file())


def remove_agent(alias: str) -> bool:
    script = agent_script(alias)
    if not (script and script.is_file()):
        return False
    try:
        script.unlink()
        return True
    except OSError:
        return False


# -- the ladder ---------------------------------------------------------------


def current_tier(alias: str) -> int:
    """What is actually registered right now — the honest answer for the hub and the doctor.

    Falls back to :data:`TIER_HUB_CATCHUP` rather than guessing: with nothing registered, the
    hub's own sweep is genuinely all that runs."""
    if task_exists(alias):
        return TIER_OS_TASK
    if agent_installed(alias):
        return TIER_LOGON_AGENT
    return TIER_HUB_CATCHUP


def enable(workspace: Path, alias: str) -> Installed:
    """Register background refresh at the best tier this machine actually permits.

    Tries the OS task first and falls back to the sign-in agent when Task Scheduler refuses —
    which on a managed laptop is a routine outcome, not an error. The reason for any demotion
    is carried back so the UI can state it plainly instead of silently doing something else
    than the user asked for."""
    command = resolve_command()  # raises UnstableInstall, which the adapters surface
    refresh_cmd = [*command, "refresh", "--due", "--repo", alias]
    demotion = ""
    if _schtasks() is not None:
        try:
            install_task(refresh_cmd, workspace, alias)
            remove_agent(alias)  # never leave both rungs registered
            return Installed(
                TIER_OS_TASK,
                f"Registered the Windows task {task_name(alias)!r} (every 30 minutes and at "
                "sign-in).",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            demotion = str(exc)
    else:
        demotion = "Task Scheduler is not available on this machine."
    try:
        script = install_agent(command, workspace, alias)
    except OSError as exc:
        raise UnstableInstall(
            f"{demotion} A sign-in agent could not be installed either ({exc}). Refreshes "
            "will keep running whenever the hub is open."
        ) from exc
    return Installed(
        TIER_LOGON_AGENT,
        f"Installed a sign-in agent at {script}.",
        demotion,
    )


def disable(alias: str) -> bool:
    """Remove whatever is registered. Returns whether anything was. Never raises."""
    removed_task = remove_task(alias)
    removed_agent = remove_agent(alias)
    return removed_task or removed_agent
