"""The ONE hardened way to execute a notebook headlessly and learn only value-free facts.

Both callers that run a whole notebook — the attended **Verify** (:mod:`mooring.app.verify_run`)
and the **scheduled refresh** (:mod:`mooring.app.refresh`) — sit on this. The four rules below
are subtle, were hard-won in verify, and are exactly the kind a second caller would have been
tempted to copy and get subtly wrong:

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

Nothing here reaches the AI copilot, and nothing here writes outside the path it is given.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mooring import editor

# marimo EXECUTES every cell, so bound the wait generously.
RUN_TIMEOUT = 300

# The marker marimo prints (at the start of a stderr line) once per failed cell. Its COUNT is
# value-free; the rest of the line can quote a data value, so only marker-anchored lines are
# counted and the text is never read.
_FAIL_MARKER = "MarimoExceptionRaisedError"


class RunError(Exception):
    """The notebook could not be RUN at all (renderer missing, timed out, environment broken).
    ``str(exc)`` is the user-facing reason. A notebook that RUNS but has a failing cell is NOT
    an error — that is a completed run with ``ok`` False."""


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
) -> RunOutcome:
    """Execute ``rel_posix`` top to bottom, rendering to ``out_path``.

    ``out_path`` MUST be inside the sync-excluded ``.mooring`` tree (the render embeds data
    values). It is removed on every path except a completed, successful run with
    ``keep_on_success`` — so a caller that wants the artifact gets it only when there is a
    real one to get, and a caller that wants only the receipt never leaves values on disk.

    ``env_extra`` adds environment variables for the run — the one channel from mooring INTO
    the kernel, used by the Excel delivery to name its target and pass the provenance facts
    only mooring knows (see :mod:`mooring.workbook`). It layers over the launch environment
    rather than replacing it, so the uv/frozen backend choice is untouched.

    Raises :class:`RunError` when the notebook could not be run at all."""
    editor.ensure_runtime_config(workspace)
    cmd, env = editor.export_html_command(
        workspace, rel_posix, out_path, include_code=include_code
    )
    if env_extra:
        # env is None when the launch inherits ours, so materialise it before layering.
        env = {**(os.environ if env is None else env), **env_extra}
    produced = False
    proc: subprocess.CompletedProcess | None = None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Clear any stale render so `produced` reflects THIS run only.
        _unlink(out_path)
        proc = _exec(cmd, str(workspace), env, timeout)
        produced = out_path.is_file()  # marimo writes the render iff it actually ran
    except OSError as exc:  # marimo/uv absent, a locked/read-only dir, a bad executable
        _unlink(out_path)
        raise RunError(f"Could not run the notebook: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # _exec has already killed the whole process tree, so no orphaned kernel can
        # re-create the render after this unlink.
        _unlink(out_path)
        raise RunError("The run timed out — the notebook took too long to run.") from exc

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
    cmd: list[str], cwd: str, env: dict[str, str] | None, timeout: int
) -> subprocess.CompletedProcess:
    """Run the export subprocess, killing the whole process TREE on timeout (rule 2)."""
    kwargs: dict = {}
    if sys.platform == "win32":
        # New process group so the tree kill (taskkill /T) can reach the marimo kernel.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        with contextlib.suppress(OSError, ValueError):
            proc.communicate(timeout=10)  # reap the killed tree
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


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
