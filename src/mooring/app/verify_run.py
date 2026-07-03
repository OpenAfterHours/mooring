"""Smoke-run a notebook locally and record a value-free trust receipt.

"Verify" is the pre-share trust step: does this notebook actually run, top to bottom,
in the team's real environment? :func:`verify_notebook` runs all its cells via
``marimo export html`` — the SAME headless invocation Deliver uses, so it executes in
the locked uv / frozen env and honours the kernel import path (``runtime.pythonpath``,
so cross-folder imports and ``import mooring_checks`` resolve) — reads only the process
EXIT CODE for pass/fail, and records a value-free receipt (see :mod:`mooring.verify`).

The receipt is keyed to the notebook's content SHA, captured **before** the run — so an
edit saved mid-run keys the receipt to bytes that no longer match the file, and the
badge auto-clears rather than vouching for code the run never executed. (Hashing after
the run would key a "ran clean" receipt to the edited-and-maybe-broken bytes — the exact
false-green the SHA rule exists to prevent.)

Two value-safety rules make this safe to run on financial notebooks:

* The rendered HTML EMBEDS data values (it captures cell outputs), and marimo's stderr
  can quote a value inside a cell's error message — so the HTML is rendered into the
  sync-excluded ``.mooring/verify/`` dir and DELETED on every path, the whole process
  TREE is killed on timeout (so an orphaned kernel can't re-write it after cleanup), and
  the stderr is NEVER stored: only a value-free COUNT of failed-cell markers is kept.
* No channel here reaches the AI copilot; the receipt is local-only and never synced.

"Ran clean" requires BOTH a zero exit AND that marimo actually produced its render — a
non-zero exit with no render at all is an ENVIRONMENT failure (e.g. a stale ``uv.lock``),
not the notebook's fault, and is surfaced as a :class:`VerifyError` rather than badging a
good notebook red. This is an ATTENDED action (a **Verify** click / ``mooring verify``) —
never a scheduler, and it never commits refreshed outputs.

Mirrors :mod:`mooring.app.deliver` (same resolver, same subprocess shape).
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mooring import activity, editor, gitsha, verify
from mooring.app import notebooks
from mooring.config import Config

# marimo EXECUTES every cell, so bound the wait generously (matches deliver's export).
_RUN_TIMEOUT = 300
# The marker marimo prints (at the start of a stderr line) once per failed cell. Its
# COUNT is value-free; the rest of the line can quote a data value, so only the count of
# marker-anchored lines is ever read, never the text.
_FAIL_MARKER = "MarimoExceptionRaisedError"


class VerifyError(Exception):
    """The notebook could not be RUN at all (renderer missing, timed out, environment
    broken, or the target is not a notebook); ``str(exc)`` is the user-facing reason. A
    notebook that RUNS but has a failing cell is NOT an error — it is a recorded
    ``passed=False`` receipt."""


@dataclass
class VerifyResult:
    notebook_rel: str
    passed: bool
    cells_failed: int | None  # value-free count of failed cells, or None if unknown
    ran_at: str


def verify_notebook(cfg: Config, rel_path: str) -> VerifyResult:
    """Run ``rel_path``'s cells locally and record a value-free trust receipt.

    Raises :class:`VerifyError` when the notebook cannot be run (missing/failing
    renderer, timeout, broken environment, non-notebook target) and ``ValueError`` /
    ``FileNotFoundError`` (from :func:`notebooks.ws_file`) for a bad path — the adapters
    translate these to their transport (a hub 4xx / a CLI message)."""
    workspace = cfg.workspace()
    target = notebooks.ws_file(workspace, rel_path, suffix=".py")
    try:
        kind = notebooks.openable_kind(target, rel_path)
    except notebooks.OpenRefused as exc:
        # A plain helper module (non-notebook .py): running it would execute a module
        # that was never a notebook. Refuse with the shared explanation.
        raise VerifyError(str(exc)) from exc
    if kind != "notebook":  # e.g. a .pbip project — open it in Power BI Desktop instead
        raise VerifyError("Only marimo notebooks can be verified.")

    rel_posix = rel_path.replace("\\", "/")
    # Set the kernel import path (workspace root + .mooring/pylib) so cross-folder
    # imports and any `import mooring_checks` resolve during the run — otherwise a
    # perfectly good notebook would falsely fail. theme=None preserves an open editor's.
    editor.ensure_runtime_config(workspace)

    # Capture the SHA of the bytes marimo is about to run BEFORE launching it, so an
    # edit landing mid-run keys the receipt to now-stale bytes and the badge auto-clears
    # (fail-safe) instead of vouching for code the run never executed.
    sha = gitsha.local_blob_sha(target, rel_posix)

    out_path = verify.render_target(workspace, rel_posix)
    cmd, env = editor.export_html_command(workspace, rel_posix, out_path, include_code=False)
    produced = False
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _unlink(out_path)  # clear any stale render so `produced` reflects THIS run only
        proc = _run_export(cmd, str(workspace), env)
        produced = out_path.is_file()  # marimo writes the render iff it actually ran
    except OSError as exc:  # marimo/uv absent, a locked/read-only dir, a bad executable
        raise VerifyError(f"Could not run the notebook: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VerifyError("Verification timed out — the notebook took too long to run.") from exc
    finally:
        # The rendered HTML embeds real values — never keep it on disk, on any path.
        # (_run_export has already killed the whole process tree on timeout, so no
        # orphaned kernel can re-create it after this unlink.)
        _unlink(out_path)

    if not produced:
        # marimo never wrote its output: the environment/tooling failed before the
        # notebook ran (e.g. a stale uv.lock or an unresolvable dependency), NOT a fault
        # of the notebook. Don't badge a good notebook red — surface it as a run error.
        # (A notebook-level syntax/import error DOES still produce a render, so it is
        # correctly recorded as a failing run below, not caught here.)
        raise VerifyError(
            "Could not run the notebook — check that its dependencies are installed "
            "(the environment failed before the notebook ran)."
        )

    passed = proc.returncode == 0
    cells_failed: int | None = None
    if not passed:
        # Value-free: COUNT the marker-anchored stderr lines; never read the message
        # text. Zero markers on a non-zero exit (e.g. a module-level error before any
        # cell ran) means "unknown", not "0 cells failed".
        count = sum(
            1 for line in (proc.stderr or "").splitlines() if line.lstrip().startswith(_FAIL_MARKER)
        )
        cells_failed = count or None
    ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    verify.record(
        workspace, rel_posix, passed=passed, sha=sha, cells_failed=cells_failed, ran_at=ran_at
    )
    activity.record(workspace, "verify", path=rel_posix, ok=passed)
    return VerifyResult(
        notebook_rel=rel_posix, passed=passed, cells_failed=cells_failed, ran_at=ran_at
    )


def describe_result(result: VerifyResult) -> str:
    """One human line summarising a run outcome — shared by both adapters so the hub
    toast and the CLI output can never drift (the pass / N-cells / unknown branching and
    the singular-plural rule live here once)."""
    if result.passed:
        return f"{result.notebook_rel} — ran clean."
    if result.cells_failed:
        cells = "cell" if result.cells_failed == 1 else "cells"
        return (
            f"{result.notebook_rel} — {result.cells_failed} {cells} failed to run "
            "(open the notebook to see which)."
        )
    return f"{result.notebook_rel} — it failed to run (open the notebook to see why)."


def _run_export(cmd: list[str], cwd: str, env: dict[str, str] | None) -> subprocess.CompletedProcess:
    """Run the export subprocess, killing the whole process TREE on timeout.

    marimo runs as a GRANDCHILD under ``uv run`` on the uv path; a plain
    ``subprocess.run`` timeout on Windows terminates only the direct child (``uv``),
    leaving the marimo kernel alive to finish and re-write the value-bearing HTML after
    we clean up. Popen + a tree kill (the editor.shutdown idiom) tears the kernel down
    before the caller unlinks, so the render can't reappear on disk."""
    kwargs: dict = {}
    if sys.platform == "win32":
        # New process group so the tree kill (taskkill /T) can reach the marimo kernel.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs
    )
    try:
        stdout, stderr = proc.communicate(timeout=_RUN_TIMEOUT)
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
