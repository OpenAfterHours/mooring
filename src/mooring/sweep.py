"""The catalog-wide verify sweep: its value-free report, and the dependency-change gate.

``Verify`` answers "does THIS notebook run"; a **sweep** asks it of every notebook in the
workspace at once. This module owns the sweep's *record* — what was covered, how each
notebook came out, and which dependency set it was run against — plus the push-time gate
that keeps a lock change from landing on the team unexamined. The runner that actually
executes the notebooks lives in :mod:`mooring.app.sweep_run` (it needs marimo); this half
is a lean L2 leaf, so the push gate can be read anywhere without dragging the editor in.

Three properties are load-bearing:

1. **The aggregate inherits the per-notebook auto-clear.** A verify receipt is keyed to the
   notebook's content SHA and vanishes the moment the file changes (:mod:`mooring.verify`).
   An aggregate "10 ran clean" would otherwise freeze a number that outlives the code it
   described, so :func:`read` re-hashes every covered notebook and moves any whose bytes
   moved into ``stale`` — out of every count. The claim shrinks on the next edit rather
   than lying; there is no invalidation logic to forget to run.
2. **The claim is bound to the dependency set it was made under.** A verify receipt says
   nothing about ``uv.lock``: change the lock and every receipt stays SHA-valid while the
   environment underneath it moved. So the report also stores a fingerprint of the lock it
   ran against, and :func:`dependency_findings` refuses to let a report vouch for different
   lock bytes.
3. **Value-free throughout.** Booleans, counts, timestamps, content hashes, and curated
   reasons — never marimo's stderr, never a data value. The report lives in the
   sync-excluded ``.mooring/`` tree, is never pushed, and never reaches the AI copilot.

**A green sweep means each notebook executed — not that its numbers are right.** That is
the same line ``mooring verify`` draws (see ``docs/users/daily-workflow.md``); nothing here
may imply more.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from mooring import gitsha, pyproject_env
from mooring.paths import safe_write_bytes

STATE_DIR = ".mooring"
SWEEP_NAME = "sweep.json"

# Per-notebook outcomes. CLEAN/FAILED mirror the verify receipt's `passed` (the notebook
# RAN); BLOCKED is the runner's "could not be run at all" — an environment fault that must
# not badge a good notebook red (see app/notebook_run.py rule 4) but is still a reason the
# team's work is not currently runnable. SKIPPED is "never attempted" (cancelled).
CLEAN = "clean"
FAILED = "failed"
BLOCKED = "blocked"
SKIPPED = "skipped"

# The one line every surface repeats about what a green sweep proves.
HONESTY_NOTE = (
    "A clean sweep means each notebook ran — not that its numbers are right."
)


def sweep_path(workspace: Path | str) -> Path:
    """The sync-excluded file holding the last sweep's report.

    Deliberately NOT inside ``.mooring/verify/``: that directory is globbed wholesale by
    :func:`mooring.verify.read_results` and :func:`mooring.verify.clear`, and an aggregate
    report living among the per-notebook receipts would be miscounted by one and deleted
    by a ``verify --clear``."""
    return Path(workspace) / STATE_DIR / SWEEP_NAME


# -- the environment fingerprint ---------------------------------------------


def fingerprint(data: bytes | None) -> str:
    """A short content hash of the dependency lock, or "" for absent/empty."""
    return hashlib.sha256(data).hexdigest()[:16] if data else ""


def lock_fingerprint(workspace: Path | str) -> str:
    """Fingerprint the workspace's ``uv.lock`` as it stands (best-effort, "" when absent).

    This is what makes a sweep's claim expire when the environment moves: the notebooks'
    own SHAs are untouched by ``mooring deps add``, so without it every receipt would keep
    vouching across a lock change that may well have broken them."""
    try:
        return fingerprint(pyproject_env.lock_path(Path(workspace)).read_bytes())
    except OSError:
        return ""


def is_lock(rel_path: str) -> bool:
    """Whether ``rel_path`` is the repo-root ``uv.lock`` — the one file whose push changes
    what every teammate's notebooks run against."""
    return rel_path.replace("\\", "/") == pyproject_env.LOCK_NAME


# -- the report --------------------------------------------------------------


@dataclass(frozen=True)
class SweepItem:
    """One notebook's outcome, keyed to the bytes that were actually run."""

    notebook: str
    outcome: str
    sha: str = ""
    cells_failed: int | None = None  # value-free count, or None when unknown
    reason: str = ""  # curated + value-free; "" when there is nothing to say

    def to_dict(self) -> dict:
        return {
            "notebook": self.notebook,
            "outcome": self.outcome,
            "sha": self.sha,
            "cells_failed": self.cells_failed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SweepReport:
    """What one sweep covered, and how much of it still applies.

    ``stale`` is NOT stored — it is recomputed by :func:`read` against the files on disk,
    which is what stops an aggregate claim outliving an edit to a notebook it covered."""

    items: tuple[SweepItem, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    cancelled: bool = False
    lock: str = ""  # the uv.lock fingerprint these runs happened under
    stale: tuple[str, ...] = ()

    # -- counts (every one of them excludes the stale) --

    def _live(self) -> tuple[SweepItem, ...]:
        gone = set(self.stale)
        return tuple(i for i in self.items if i.notebook not in gone)

    def of(self, outcome: str) -> tuple[SweepItem, ...]:
        return tuple(i for i in self._live() if i.outcome == outcome)

    @property
    def clean(self) -> int:
        return len(self.of(CLEAN))

    @property
    def failed(self) -> int:
        return len(self.of(FAILED))

    @property
    def blocked(self) -> int:
        return len(self.of(BLOCKED))

    @property
    def skipped(self) -> int:
        return len(self.of(SKIPPED))

    @property
    def broken(self) -> tuple[SweepItem, ...]:
        """The notebooks this sweep says are not currently runnable — a failing cell and
        a run that could not start are different diagnoses but the same answer to "can my
        team still use this repo"."""
        return self.of(FAILED) + self.of(BLOCKED)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def covered(self) -> int:
        """How many of the covered notebooks the report still describes."""
        return len(self._live())

    def to_dict(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancelled": self.cancelled,
            "lock": self.lock,
        }


def headline(report: SweepReport) -> str:
    """The one human summary line, shared by the CLI and the hub so the two can never
    word the same result differently. Zero buckets are omitted — "12 notebooks: 12 ran
    clean." reads better than four zeroes."""
    if not report.items:
        return "No notebooks to check."
    parts = [f"{report.clean} ran clean"]
    if report.failed:
        parts.append(f"{report.failed} failed")
    if report.blocked:
        parts.append(f"{report.blocked} could not run")
    if report.skipped:
        parts.append(f"{report.skipped} skipped")
    if report.stale:
        parts.append(f"{len(report.stale)} edited since (no longer covered)")
    noun = "notebook" if report.total == 1 else "notebooks"
    line = f"{report.total} {noun}: " + ", ".join(parts) + "."
    if report.cancelled:
        line += " (cancelled)"
    return line


# -- persistence -------------------------------------------------------------


def record(workspace: Path | str, report: SweepReport) -> None:
    """Store ``report`` (value-free) in the sync-excluded state dir.

    Best-effort; never raises — a failed write costs the push gate its input, which is a
    missing warning, never a broken sweep."""
    target = sweep_path(workspace)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_write_bytes(target, json.dumps(report.to_dict()).encode("utf-8"))
    except OSError:
        pass


def read(workspace: Path | str) -> SweepReport | None:
    """The last sweep, RE-VALIDATED against the notebooks on disk (None when absent).

    Every covered notebook is re-hashed; one whose blob SHA has moved (or that is gone)
    lands in ``stale`` and drops out of every count. So "10 ran clean" becomes "9 ran
    clean, 1 edited since" the instant somebody touches a file — the aggregate inherits
    the per-notebook badge's auto-clear instead of freezing a number.

    Best-effort: an unreadable or corrupt report reads as "no sweep"."""
    ws = Path(workspace)
    try:
        data = json.loads(sweep_path(ws).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    items: list[SweepItem] = []
    for raw in data.get("items", []):
        if not isinstance(raw, dict):
            continue
        rel = raw.get("notebook")
        outcome = raw.get("outcome")
        if not isinstance(rel, str) or not isinstance(outcome, str):
            continue
        cells = raw.get("cells_failed")
        items.append(
            SweepItem(
                notebook=rel,
                outcome=outcome,
                sha=str(raw.get("sha") or ""),
                cells_failed=cells if isinstance(cells, int) else None,
                reason=str(raw.get("reason") or ""),
            )
        )
    report = SweepReport(
        items=tuple(items),
        started_at=str(data.get("started_at") or ""),
        finished_at=str(data.get("finished_at") or ""),
        cancelled=bool(data.get("cancelled")),
        lock=str(data.get("lock") or ""),
    )
    return replace(report, stale=tuple(i.notebook for i in items if _moved(ws, i)))


def _moved(workspace: Path, item: SweepItem) -> bool:
    """Whether ``item``'s notebook no longer holds the bytes the sweep ran.

    Fail-safe in both directions: an unreadable file or a missing recorded SHA counts as
    moved, so doubt always shrinks the claim rather than propping it up."""
    if not item.sha:
        return True
    target = workspace / item.notebook
    try:
        if not target.is_file():
            return True
        return gitsha.local_blob_sha(target, item.notebook) != item.sha
    except OSError:
        return True


def clear(workspace: Path | str) -> bool:
    """Forget the stored sweep. Best-effort; True when something was removed."""
    try:
        sweep_path(workspace).unlink()
        return True
    except OSError:
        return False


# -- the dependency-change gate ----------------------------------------------


def dependency_findings(workspace: Path | str, rel_path: str, data: bytes) -> list[tuple[int, str]]:
    """Value-free ``(line, kind)`` findings for ONE outgoing file — the push-time gate on
    dependency changes. Empty for everything that is not the repo's ``uv.lock``.

    ``mooring deps add`` rewrites ``uv.lock`` **for the whole team**, and because mooring is
    the only road into the repo a check here covers 100% of the pushes that could break
    everyone at once. The finding is derived from the stored sweep, and the sweep only
    counts when it was run against *these exact* lock bytes — the report cannot vouch for a
    dependency set it never saw.

    This WARNS; it does not block. The adapters render it through the push guard's
    warn-and-confirm flow, where the confirm token binds the exact bytes to the exact
    findings — so a run that has since broken one more notebook changes the finding, and a
    stale acknowledgement stops matching."""
    if not is_lock(rel_path):
        return []
    report = read(workspace)
    if report is None:
        return [(1, "dependency change not checked — `mooring verify --all` runs "
                    "every notebook against it first")]
    if report.lock != fingerprint(data):
        return [(1, "these dependencies have not been checked — the last sweep ran "
                    "against a different uv.lock")]
    broken = report.broken
    if broken:
        noun = "notebook" if len(broken) == 1 else "notebooks"
        return [(1, f"this dependency change breaks {len(broken)} {noun} — "
                    f"they no longer run clean")]
    if report.stale:
        noun = "notebook" if len(report.stale) == 1 else "notebooks"
        return [(1, f"{len(report.stale)} {noun} changed since the sweep — "
                    f"its result no longer covers them")]
    if report.skipped:
        # A cancelled sweep always leaves its remainder recorded as SKIPPED, so this is
        # also the "it was cancelled" case — phrased by what it means rather than by how
        # it happened: some notebooks were never checked against these dependencies.
        noun = "notebook" if report.skipped == 1 else "notebooks"
        return [(1, f"the sweep was cancelled — {report.skipped} {noun} were never checked")]
    return []
