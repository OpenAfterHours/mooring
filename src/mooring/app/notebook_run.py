"""The ONE hardened way to execute a notebook headlessly and learn only value-free facts.

Every caller that runs a whole notebook — the attended **Verify** (:mod:`mooring.app.verify_run`),
the **scheduled refresh** (:mod:`mooring.app.refresh`), and the attended **parameterised run**
(:mod:`mooring.app.param_runs`) — sits on this. The four rules below are subtle, were hard-won
in verify, and are exactly the kind a second caller would have been tempted to copy and get
subtly wrong:

1. **The render is value-bearing; its lifetime is managed here.** ``marimo export html``
   captures cell OUTPUTS, so the file it writes embeds real data. It is written only into a
   sync-excluded location the caller nominates, and is deleted on EVERY path that is not an
   explicit, successful ``keep_on_success`` — including timeouts and OS errors.
2. **The process TREE is killed on timeout.** On the uv path marimo runs as a GRANDCHILD under
   ``uv run``; a plain ``subprocess.run`` timeout on Windows terminates only ``uv``, leaving
   the marimo kernel alive to finish and re-write the value-bearing HTML *after* cleanup. A new
   process group plus ``taskkill /T`` tears the kernel down before anyone unlinks.
3. **stderr is never stored, only counted.** marimo's stderr can quote a data value inside a
   cell's error message, so the only thing read from it is the COUNT of marker-anchored lines.
   The text never reaches a receipt, the activity ledger, telemetry, or the AI.
4. **"Did not run" is distinguished from "ran and failed".** marimo writes its render iff it
   actually executed the notebook. A non-zero exit with NO render at all is an ENVIRONMENT
   failure (a stale ``uv.lock``, an unresolvable dependency) and must not badge a good notebook
   red; that is raised as :class:`RunError` rather than reported as a failing run.

**Cancel rides rule 2, it does not get its own mechanism.** ``cancel`` is an ordinary
:class:`threading.Event` a caller may fire from another thread; a watchdog turns it into the
SAME ``taskkill /T`` process-tree kill the timeout path uses, so a cancelled run cannot leave
a live marimo kernel behind to re-write the value-bearing render after cleanup. Cancelling
therefore has exactly the safety properties a timeout has, by construction.

Nothing here reaches the AI copilot, and nothing here writes outside the path it is given.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from mooring import editor

# marimo EXECUTES every cell, so bound the wait generously.
RUN_TIMEOUT = 300

# The marker marimo prints (at the start of a stderr line) once per failed cell. Its COUNT is
# value-free; the rest of the line can quote a data value, so only marker-anchored lines are
# counted and the text is never read.
_FAIL_MARKER = "MarimoExceptionRaisedError"

# How often the cancel watchdog wakes while a run is in flight. Short enough that "Cancel"
# feels immediate, long enough to cost nothing over a run measured in minutes.
_CANCEL_POLL_S = 0.25


class RunError(Exception):
    """The notebook could not be RUN at all (renderer missing, timed out, environment broken).
    ``str(exc)`` is the user-facing reason. A notebook that RUNS but has a failing cell is NOT
    an error — that is a completed run with ``ok`` False."""


class RunCancelled(RunError):
    """The caller fired its cancel event and the process tree was killed.

    A subclass of :class:`RunError` on purpose: a caller that does not pass a cancel event can
    never see this, and one that predates cancellation still handles it safely (as "could not
    run") rather than letting it escape as an unhandled exception."""


@dataclass(frozen=True)
class RunOutcome:
    returncode: int
    produced: bool  # marimo actually wrote its render, i.e. the notebook executed
    cells_failed: int | None  # value-free count, or None when unknown

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.produced


def run(
    workspace: Path,
    rel_posix: str,
    out_path: Path,
    *,
    keep_on_success: bool,
    include_code: bool = False,
    timeout: int = RUN_TIMEOUT,
    env_extra: dict[str, str] | None = None,
    cancel: threading.Event | None = None,
) -> RunOutcome:
    """Execute ``rel_posix`` top to bottom, rendering to ``out_path``.

    ``out_path`` MUST be inside the sync-excluded ``.mooring`` tree (the render embeds data
    values). It is removed on every path except a completed, successful run with
    ``keep_on_success`` — so a caller that wants the artifact gets it only when there is a
    real one to get, and a caller that wants only the receipt never leaves values on disk.

    ``env_extra`` overlays environment variables onto the run (the channel a parameterised
    run hands its value to the kernel on — see :mod:`mooring.params`). It never changes the
    COMMAND, so both launch backends behave identically. ``cancel`` is an event another
    thread may set to stop the run; it becomes the same process-tree kill the timeout uses
    and raises :class:`RunCancelled`.

    Raises :class:`RunError` when the notebook could not be run at all."""
    editor.ensure_runtime_config(workspace)
    cmd, env = editor.export_html_command(
        workspace, rel_posix, out_path, include_code=include_code
    )
    if env_extra:
        # export_html_command returns None to mean "inherit", so materialise the parent
        # environment before overlaying rather than handing marimo a two-variable env.
        env = {**(env if env is not None else os.environ), **env_extra}
    produced = False
    proc: subprocess.CompletedProcess | None = None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Clear any stale render so `produced` reflects THIS run only.
        _unlink(out_path)
        proc = _exec(cmd, str(workspace), env, timeout, cancel)
        produced = out_path.is_file()  # marimo writes the render iff it actually ran
    except OSError as exc:  # marimo/uv absent, a locked/read-only dir, a bad executable
        _unlink(out_path)
        raise RunError(f"Could not run the notebook: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # _exec has already killed the whole process tree, so no orphaned kernel can
        # re-create the render after this unlink.
        _unlink(out_path)
        raise RunError("The run timed out — the notebook took too long to run.") from exc

    if cancel is not None and cancel.is_set():
        # The tree is already dead (the watchdog killed it), so nothing can re-create the
        # render after this unlink — the same guarantee the timeout path relies on. A
        # half-rendered file must never be promoted to an artifact.
        _unlink(out_path)
        raise RunCancelled("Cancelled — the notebook run was stopped.")

    if not produced:
        # marimo never wrote its output: the environment/tooling failed BEFORE the notebook
        # ran. Not the notebook's fault. (A notebook-level syntax/import error DOES still
        # produce a render, so it is correctly reported as a failing run below.)
        _unlink(out_path)
        raise RunError(
            "Could not run the notebook — check that its dependencies are installed "
            "(the environment failed before the notebook ran)."
        )

    outcome = RunOutcome(
        returncode=proc.returncode,
        produced=True,
        cells_failed=_count_failed_cells(proc) if proc.returncode != 0 else None,
    )
    if not (outcome.ok and keep_on_success):
        _unlink(out_path)
    return outcome


def _count_failed_cells(proc: subprocess.CompletedProcess) -> int | None:
    """How many cells failed, read VALUE-FREE: count the marker-anchored stderr lines and
    never read the message text. Zero markers on a non-zero exit (e.g. a module-level error
    before any cell ran) means "unknown", not "0 cells failed"."""
    count = sum(
        1 for line in (proc.stderr or "").splitlines() if line.lstrip().startswith(_FAIL_MARKER)
    )
    return count or None


def _exec(
    cmd: list[str],
    cwd: str,
    env: dict[str, str] | None,
    timeout: int,
    cancel: threading.Event | None = None,
) -> subprocess.CompletedProcess:
    """Run the export subprocess, killing the whole process TREE on timeout (rule 2) — and
    on cancel, through the very same kill."""
    kwargs: dict = {}
    if sys.platform == "win32":
        # New process group so the tree kill (taskkill /T) can reach the marimo kernel.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs
    )
    # A watchdog rather than a polling communicate(): the wait stays ONE blocking call with
    # the original timeout semantics, and cancelling simply kills the tree out from under
    # it, after which communicate returns on its own. Nothing about rules 1-4 moves.
    finished = threading.Event()
    watchdog: threading.Thread | None = None
    if cancel is not None:
        watchdog = threading.Thread(
            target=_watch_cancel, args=(proc, cancel, finished), daemon=True
        )
        watchdog.start()
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        with contextlib.suppress(OSError, ValueError):
            proc.communicate(timeout=10)  # reap the killed tree
        raise
    finally:
        finished.set()
        if watchdog is not None:
            watchdog.join(timeout=5)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _watch_cancel(
    proc: subprocess.Popen, cancel: threading.Event, finished: threading.Event
) -> None:
    """Turn a set cancel event into the process-TREE kill, until the run finishes."""
    while not finished.is_set():
        if cancel.wait(_CANCEL_POLL_S):
            with contextlib.suppress(OSError, ValueError):
                _kill_tree(proc)
            return


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        # taskkill /T walks the PID tree, so it reaches the marimo kernel uv spawned.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    else:
        proc.kill()


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
