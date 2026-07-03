"""Smoke-run a notebook locally and record a value-free trust receipt.

"Verify" is the pre-share trust step: does this notebook actually run, top to bottom,
in the team's real environment? :func:`verify_notebook` runs all its cells via
``marimo export html`` — the SAME headless invocation Deliver uses, so it executes in
the locked uv / frozen env and honours the kernel import path (``runtime.pythonpath``,
so cross-folder imports and ``import mooring_checks`` resolve) — reads only the process
EXIT CODE for pass/fail, and records a value-free receipt (see :mod:`mooring.verify`)
keyed to the notebook's content SHA so the hub badge auto-clears when the file changes.

Two value-safety rules make this safe to run on financial notebooks:

* The rendered HTML EMBEDS data values (it captures cell outputs), and marimo's stderr
  can quote a value inside a cell's error message — so the HTML is rendered into the
  sync-excluded ``.mooring/verify/`` dir and DELETED immediately, and the stderr is
  NEVER stored: only a value-free COUNT of failed-cell markers is kept.
* No channel here reaches the AI copilot; the receipt is local-only and never synced.

This is an ATTENDED action (the analyst clicks **Verify** / runs ``mooring verify``) —
never a scheduler. A "certified" badge that could refresh unattended, or that commits
refreshed outputs, is explicitly out of scope (see the roadmap + docs); verify only
ever reads an exit code and writes a boolean.

Mirrors :mod:`mooring.app.deliver` (same resolver, same subprocess shape).
"""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mooring import activity, editor, gitsha, verify
from mooring.app import notebooks
from mooring.config import Config

# marimo EXECUTES every cell, so bound the wait generously (matches deliver's export).
_RUN_TIMEOUT = 300
# The marker marimo prints to stderr once per failed cell. Its COUNT is value-free; the
# lines around it can quote a data value, so only the count is ever read, never the text.
_FAIL_MARKER = "MarimoExceptionRaisedError"


class VerifyError(Exception):
    """The notebook could not be RUN at all (renderer missing, timed out, or the target
    is not a notebook); ``str(exc)`` is the user-facing reason. A notebook that runs but
    has a failing cell is NOT an error — it is a recorded ``passed=False`` receipt."""


@dataclass
class VerifyResult:
    notebook_rel: str
    passed: bool
    cells_failed: int | None  # value-free count of failed cells, or None if unknown
    ran_at: str


def verify_notebook(cfg: Config, rel_path: str) -> VerifyResult:
    """Run ``rel_path``'s cells locally and record a value-free trust receipt.

    Raises :class:`VerifyError` when the notebook cannot be run (missing renderer,
    timeout, non-notebook target) and ``ValueError`` / ``FileNotFoundError`` (from
    :func:`notebooks.ws_file`) for a bad path — the adapters translate these to their
    transport (a hub 4xx / a CLI message)."""
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

    out_path = verify.render_target(workspace, rel_posix)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd, env = editor.export_html_command(workspace, rel_posix, out_path, include_code=False)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT,
        )
    except FileNotFoundError as exc:  # marimo/uv not found on PATH
        raise VerifyError(f"Could not run the notebook: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        _unlink(out_path)
        raise VerifyError("Verification timed out — the notebook took too long to run.") from exc
    finally:
        # The rendered HTML embeds real values — never keep it on disk. (marimo writes
        # it even when a cell fails, so this runs on both the pass and fail paths.)
        _unlink(out_path)

    passed = proc.returncode == 0
    cells_failed: int | None = None
    if not passed:
        # Value-free: COUNT the failed-cell markers; never read the message text. Zero
        # markers on a non-zero exit (e.g. a module-level import/syntax error before any
        # cell ran) means "unknown", not "0 cells failed".
        count = (proc.stderr or "").count(_FAIL_MARKER)
        cells_failed = count or None
    ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sha = gitsha.local_blob_sha(target, rel_posix)
    verify.record(
        workspace, rel_posix, passed=passed, sha=sha, cells_failed=cells_failed, ran_at=ran_at
    )
    activity.record(workspace, "verify", path=rel_posix, ok=passed)
    return VerifyResult(
        notebook_rel=rel_posix, passed=passed, cells_failed=cells_failed, ran_at=ran_at
    )


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
