"""Smoke-run a notebook locally and record a value-free trust receipt.

"Verify" is the pre-share trust step: does this notebook actually run, top to bottom,
in the team's real environment? :func:`verify_notebook` runs all its cells through the
shared hardened runner (:mod:`mooring.app.notebook_run` — which owns the render-lifetime,
process-tree-kill, value-free-stderr and did-it-run-at-all rules), reads only the process
EXIT CODE for pass/fail, and records a value-free receipt (see :mod:`mooring.verify`).

The receipt is keyed to the notebook's content SHA, captured **before** the run — so an
edit saved mid-run keys the receipt to bytes that no longer match the file, and the
badge auto-clears rather than vouching for code the run never executed. (Hashing after
the run would key a "ran clean" receipt to the edited-and-maybe-broken bytes — the exact
false-green the SHA rule exists to prevent.)

Two value-safety rules make this safe to run on financial notebooks, and both are enforced
in :mod:`mooring.app.notebook_run`: the value-bearing HTML render is written into the
sync-excluded ``.mooring/verify/`` dir and deleted on every path, and marimo's stderr is
never stored — only a value-free COUNT of failed-cell markers. No channel here reaches the
AI copilot; the receipt is local-only and never synced.

"Ran clean" requires BOTH a zero exit AND that marimo actually produced its render — a
non-zero exit with no render at all is an ENVIRONMENT failure (e.g. a stale ``uv.lock``),
not the notebook's fault, and is surfaced as a :class:`VerifyError` rather than badging a
good notebook red.

**Attended vs scheduled.** This entry point backs an attended action (a **Verify** click /
``mooring verify``). :mod:`mooring.app.refresh` runs the same notebook on a cadence and
records the same receipt. What stays attended-only is everything that LEAVES the machine —
push, propose, and sending a delivered artifact — never the local run itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from mooring import activity, gitsha, verify
from mooring.app import notebook_run, notebooks
from mooring.config import Config


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
    rel_posix = ensure_runnable(workspace, rel_path, VerifyError)
    target = workspace / rel_posix

    # Capture the SHA of the bytes marimo is about to run BEFORE launching it, so an
    # edit landing mid-run keys the receipt to now-stale bytes and the badge auto-clears
    # (fail-safe) instead of vouching for code the run never executed.
    sha = gitsha.local_blob_sha(target, rel_posix)

    try:
        outcome = notebook_run.run(
            workspace, rel_posix, verify.render_target(workspace, rel_posix), keep_on_success=False
        )
    except notebook_run.RunError as exc:
        raise VerifyError(str(exc)) from exc

    ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record_receipt(workspace, rel_posix, outcome, sha=sha, ran_at=ran_at)
    activity.record(workspace, "verify", path=rel_posix, ok=outcome.ok)
    return VerifyResult(
        notebook_rel=rel_posix,
        passed=outcome.ok,
        cells_failed=outcome.cells_failed,
        ran_at=ran_at,
    )


def ensure_runnable(workspace, rel_path: str, error: type[Exception]) -> str:
    """Resolve ``rel_path`` to a runnable notebook's POSIX rel-path, or raise ``error``.

    Shared with :mod:`mooring.app.refresh` so both entry points refuse the same things for
    the same reasons: a plain helper module (running it would execute something that was
    never a notebook) and a non-notebook target such as a ``.pbip`` project."""
    target = notebooks.ws_file(workspace, rel_path, suffix=".py")
    try:
        kind = notebooks.openable_kind(target, rel_path)
    except notebooks.OpenRefused as exc:
        raise error(str(exc)) from exc
    if kind != "notebook":  # e.g. a .pbip project — open it in Power BI Desktop instead
        raise error("Only marimo notebooks can be run.")
    return rel_path.replace("\\", "/")


def record_receipt(
    workspace, rel_posix: str, outcome: notebook_run.RunOutcome, *, sha: str, ran_at: str
) -> None:
    """Write the value-free trust receipt for a completed run.

    Shared with :mod:`mooring.app.refresh`, so a scheduled run keeps the trust badge current
    instead of letting it lapse — which matters because a lapsed verification is what drops a
    schedule to a one-strike failure budget."""
    verify.record(
        workspace,
        rel_posix,
        passed=outcome.ok,
        sha=sha,
        cells_failed=outcome.cells_failed,
        ran_at=ran_at,
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
