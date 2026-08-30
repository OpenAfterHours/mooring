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
3. **stderr is never stored, and never leaves this module.** marimo's stderr is not a log —
   the exporter echoes every cell's own ``print`` output onto it (verified: a printed
   dataframe lands there in full, values and all), interleaved with one
   ``<MarimoErrorClass>: <message>`` line per failed cell. So the only thing the default
   path reads from it is the COUNT of marker-anchored lines, and the text never reaches a
   receipt, the activity ledger, telemetry, or the AI. A caller that must learn *why* a cell
   failed opts in with ``on_failures`` and receives :func:`failure_lines`' narrow slice — a
   KIND from marimo's own closed error taxonomy plus that one line's raw message — never the
   stderr text, so a printed value has no path out. Its one caller
   (:mod:`mooring.app.run_report`) hands the message straight to the traceback sanitiser.
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
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mooring import editor, params

# Environment variables mooring itself sets on a run (its "run channels"). They are SCRUBBED
# from the inherited environment whenever the current run does not supply them — see
# :func:`_child_env` for why inheriting one is a mislabelled-artifact bug.
_RUN_CHANNELS = (params.ENV_VAR,)

# marimo EXECUTES every cell, so bound the wait generously.
RUN_TIMEOUT = 300

# The marker marimo prints (at the start of a stderr line) once per failed cell. Its COUNT is
# value-free; the rest of the line can quote a data value, so only marker-anchored lines are
# counted and the text is never read.
_FAIL_MARKER = "MarimoExceptionRaisedError"

# Every error class name marimo's exporter can print at the start of a stderr line — its own
# closed taxonomy (``marimo/_messaging/errors.py``; the exporter emits
# ``f"{err.__class__.__name__}: {err.describe()}"``, see marimo/_server/export/__init__.py).
#
# A CLOSED set is the point, not a convenience. :func:`failure_lines` returns the constant it
# matched rather than the substring it found, so the KIND half of a failure can only ever be
# one of these fixed strings — nothing a cell PRINTED onto the same stream can ride out on it.
# A name marimo adds later simply is not recognised, which loses a report and leaks nothing.
_ERROR_KINDS = (
    "CycleError",
    "ImportStarError",
    "MarimoAncestorPreventedError",
    "MarimoAncestorStoppedError",
    "MarimoExceptionRaisedError",
    "MarimoInternalError",
    "MarimoInterruptionError",
    "MarimoSQLError",
    "MarimoStrictExecutionError",
    "MarimoSyntaxError",
    "MultipleDefinitionError",
    "SetupRootError",
    "UnknownError",
)
_ERROR_LINE_RE = re.compile(rf"^(?P<kind>{'|'.join(_ERROR_KINDS)}):[ \t]?(?P<msg>.*)$")

# How often the cancel watchdog wakes while a run is in flight. Short enough that "Cancel"
# feels immediate, long enough to cost nothing over a run measured in minutes.
_CANCEL_POLL_S = 0.25


# -- the one workspace run lock ----------------------------------------------
#
# One whole-notebook run per WORKSPACE, across processes. It lives here, in the module whose
# whole subject is running a notebook, rather than in any one feature: every caller that
# starts a kernel needs it for the same reasons, and a lock owned by one feature is a lock
# the next feature forgets to take. (It began life in app/refresh.py, which still re-exports
# it; Verify, Deliver and the parameterised fan-out were all running unguarded.)
_LOCK_NAME = "refresh.lock"
# A held lock older than this belongs to a process that died mid-run (a hard reboot, a killed
# agent) and is stolen. The bound is on the HEARTBEAT below, not on how long a run may take:
# a fan-out can legitimately hold this for MAX_VALUES x RUN_TIMEOUT, many times any run
# timeout, so deriving it from a single run's length was wrong and let a live fan-out's lock
# be stolen out from under it.
_LOCK_STALE_S = 900
# How often a live holder touches its lockfile. Two orders of magnitude inside the stale
# window, so a missed beat or two proves nothing and only a genuinely dead process ages out.
_LOCK_BEAT_S = 60

_BUSY = (
    "This workspace is already running a notebook (a verify, a delivery, a scheduled "
    "refresh, or a parameterised run) — wait for it to finish."
)


class RunBusy(Exception):
    """Another whole-notebook run already holds this workspace, in this process or another.

    Not necessarily an error to record — a scheduled sweep simply steps aside, because
    whatever holds the lock is doing the work. Attended callers translate it into their own
    refusal (``VerifyError`` / ``DeliverError`` / a hub 409)."""


@contextlib.contextmanager
def workspace_guard(workspace: Path):
    """Hold the cross-process run lock for ``workspace``, or raise :class:`RunBusy`.

    ``O_CREAT | O_EXCL`` is atomic on Windows and POSIX alike, so the file's existence IS the
    lock. A stale lock is stolen rather than deadlocking forever — a background agent killed
    mid-run must not wedge every future run, which would be a silent stop of exactly the kind
    the scheduled-refresh feature exists to prevent.

    While held, the lockfile's mtime is HEARTBEATEN (:data:`_LOCK_BEAT_S`). Without that,
    "stale" meant "older than one run", which a parameterised fan-out exceeds by design — and
    a second run would take the lock out from under a live one, put two kernels on the same
    CPU and the same throwaway render path, and let one run's render be promoted as another
    value's artifact."""
    path = workspace / ".mooring" / _LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _stale(path):
            raise RunBusy(_BUSY) from None
        # Steal it: best-effort unlink then one retry. Losing the retry means another process
        # got there first, which is a perfectly good outcome — it is doing the work.
        with contextlib.suppress(OSError):
            path.unlink()
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            raise RunBusy(_BUSY) from None
    except OSError as exc:
        # A read-only or otherwise unusable state dir: refuse rather than running unguarded.
        raise RunBusy(f"Could not take the workspace run lock: {exc}") from exc
    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat, args=(path, stop), daemon=True)
    beat.start()  # STARTED before the try, so the finally's join always has a live thread
    try:
        with contextlib.suppress(OSError):
            os.write(handle, str(os.getpid()).encode("ascii"))
        with contextlib.suppress(OSError):
            os.close(handle)
        yield
    finally:
        stop.set()
        beat.join(timeout=5)
        with contextlib.suppress(OSError):
            path.unlink()


def _heartbeat(path: Path, stop: threading.Event) -> None:
    """Keep a held lock's mtime current, so a long legitimate hold is never mistaken for a
    dead process's abandoned one."""
    while not stop.wait(_LOCK_BEAT_S):
        with contextlib.suppress(OSError):
            os.utime(path, None)


def _stale(path: Path) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) > _LOCK_STALE_S
    except OSError:
        return False


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
    on_failures: Callable[[list[tuple[str, str]]], None] | None = None,
) -> RunOutcome:
    """Execute ``rel_posix`` top to bottom, rendering to ``out_path``.

    ``out_path`` MUST be inside the sync-excluded ``.mooring`` tree (the render embeds data
    values). It is removed on every path except a completed, successful run with
    ``keep_on_success`` — so a caller that wants the artifact gets it only when there is a
    real one to get, and a caller that wants only the receipt never leaves values on disk.

    ``env_extra`` adds environment variables for the run — the one channel from mooring INTO
    the kernel, used by the Excel delivery to name its target and pass the provenance facts
    only mooring knows (see :mod:`mooring.workbook`), and by a parameterised run to hand the
    kernel its value (see :mod:`mooring.params`). It layers over the launch environment
    rather than replacing it, so the uv/frozen backend choice is untouched, and it never
    changes the COMMAND, so both launch backends behave identically.

    ``cancel`` is an event another thread may set to stop the run; it becomes the same
    process-tree kill the timeout uses and raises :class:`RunCancelled`.

    ``on_failures`` is the ONE opt-in past rule 3. It is called once, on a completed run,
    with :func:`failure_lines`' ``(KIND, message)`` pairs — never with the stderr text, and
    never at all unless a caller asked. It fires AFTER the render has been dealt with, so a
    sink that raises can never leave a value-bearing HTML behind.

    Raises :class:`RunError` when the notebook could not be run at all."""
    editor.ensure_runtime_config(workspace)
    cmd, env = editor.export_html_command(
        workspace, rel_posix, out_path, include_code=include_code
    )
    env = _child_env(env, env_extra)
    produced = False
    proc: subprocess.CompletedProcess | None = None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Clear any stale render so `produced` reflects THIS run only.
        _unlink(out_path)
        # Passed ONLY when there is one. A run nobody can cancel must reach the seam with
        # exactly the arguments it reached it with before this feature existed — which is
        # the strongest available form of "an unparameterised run is unchanged", and keeps
        # the dozen `_exec` fakes across the suite honest characterizations rather than
        # signatures that have to be chased every time this seam grows.
        extra = {"cancel": cancel} if cancel is not None else {}
        proc = _exec(cmd, str(workspace), env, timeout, **extra)
        produced = out_path.is_file()  # marimo writes the render iff it actually ran
    except OSError as exc:  # marimo/uv absent, a locked/read-only dir, a bad executable
        _unlink(out_path)
        raise RunError(f"Could not run the notebook: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # _exec has already killed the whole process tree, so no orphaned kernel can
        # re-create the render after this unlink.
        _unlink(out_path)
        raise RunError("The run timed out — the notebook took too long to run.") from exc
    except BaseException:
        # Ctrl+C, or any other unwind. This clause is rule 1 applied to the ONE path that
        # was missing it: a console Ctrl+C reaches mooring but NOT marimo (the child runs in
        # its own process group precisely so it ignores the keystroke), so without _exec's
        # matching kill the kernel would run on and re-write the value-bearing render after
        # this unlink — with the workspace lock already released. _exec kills the tree before
        # re-raising, so by the time we get here nothing can re-create the file.
        _unlink(out_path)
        raise

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
    if on_failures is not None:
        on_failures(failure_lines(proc.stderr or ""))
    return outcome


def failure_lines(stderr: str) -> list[tuple[str, str]]:
    """The ``(KIND, message)`` pair for each marimo error line in ``stderr`` — the narrowest
    slice of a failed run that can answer "why did it fail", and the ONLY thing a caller may
    learn beyond the value-free count.

    ``KIND`` is one of the :data:`_ERROR_KINDS` constants (returned as the constant, never as
    text lifted from the stream). ``message`` is marimo's own error message, **raw and
    value-BEARING** — ``KeyError: 'ACME Ltd'`` is a real shape here — so it is the caller's
    job to make it value-safe; the one caller in the tree hands it straight to
    ``egress.sanitize_traceback`` (:mod:`mooring.app.run_report`).

    Everything else on the stream — above all the cell ``print`` output the exporter echoes
    there — is dropped, so no caller can reach it. A message never spans lines: this is
    line-based by construction, so a multi-line message contributes only its first line.
    """
    out: list[tuple[str, str]] = []
    for line in (stderr or "").splitlines():
        match = _ERROR_LINE_RE.match(line.lstrip())
        if match:
            out.append((match.group("kind"), match.group("msg")))
    return out


def _count_failed_cells(proc: subprocess.CompletedProcess) -> int | None:
    """How many cells failed, read VALUE-FREE: count the marker-anchored stderr lines and
    never read the message text. Zero markers on a non-zero exit (e.g. a module-level error
    before any cell ran) means "unknown", not "0 cells failed"."""
    count = sum(
        1 for line in (proc.stderr or "").splitlines() if line.lstrip().startswith(_FAIL_MARKER)
    )
    return count or None


def _child_env(env: dict[str, str] | None, env_extra: dict[str, str] | None) -> dict | None:
    """The environment the kernel actually gets: the launch environment, plus ``env_extra``,
    minus any stale RUN CHANNEL this run is not itself setting.

    The scrub is the load-bearing half. ``MOORING_PARAMS`` is how a parameterised run tells
    the notebook which value it is — so one left in mooring's OWN environment (exported in a
    shell, or set for an earlier `mooring run`) would silently parameterise every ordinary
    run after it: a scheduled refresh would write ``board-20260731.html``, with no value in
    the name, holding APAC's numbers. A channel mooring owns must therefore be set by
    mooring or not present at all — never inherited."""
    base = os.environ if env is None else env
    stale = [k for k in _RUN_CHANNELS if k not in (env_extra or {}) and k in base]
    if not (env_extra or stale):
        return env  # nothing to change; keep None meaning "inherit" exactly as before
    merged = {**base, **(env_extra or {})}
    for key in stale:
        merged.pop(key, None)
    return merged


def _exec(
    cmd: list[str],
    cwd: str,
    env: dict[str, str] | None,
    timeout: int,
    *,
    cancel: threading.Event | None = None,
) -> subprocess.CompletedProcess:
    """Run the export subprocess, killing the whole process TREE on timeout (rule 2) — and
    on cancel or a Ctrl+C unwind, through the very same kill.

    ``cancel`` is keyword-only: this seam is faked in a dozen tests, and a keyword-only
    extension can be added without touching a single one of them."""
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
    except BaseException:
        # EVERY abnormal exit from the wait kills the tree, not just the timeout. A console
        # Ctrl+C is the case that made this a BaseException clause: CREATE_NEW_PROCESS_GROUP
        # means the child never sees the keystroke, so a KeyboardInterrupt that merely
        # unwound past here would leave the marimo kernel running — free to re-write the
        # value-bearing render after the caller's cleanup and after the workspace lock was
        # released. That is exactly what rules 1 and 2 exist to prevent.
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
