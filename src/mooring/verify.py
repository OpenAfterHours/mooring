"""Record and read value-free notebook run-verification receipts (the trust badge).

A "verify" is an opt-in, ATTENDED, LOCAL smoke re-run: mooring runs all a notebook's
cells in the real marimo runtime (see :mod:`mooring.app.verify_run`) and records a
VALUE-FREE receipt of whether it ran clean, keyed to the notebook's exact content SHA.
The hub badges the row green ("ran clean") only while that SHA still matches the file
on disk, so the badge auto-CLEARS the instant the notebook changes — a stale
"verified" can never linger over edited code. That auto-clear is the load-bearing rule
for a trust signal, and it is enforced HERE at read time (:func:`read_results`), not by
hoping something deletes the receipt on edit.

Receipts live in the sync-excluded ``.mooring/verify/`` dir. Each is value-free — a
boolean, a content hash, a failed-cell COUNT, and a timestamp, never a data value or an
error message — local-only, never pushed, never handed to the AI copilot.

Lean-core leaf: imports only :mod:`mooring.gitsha`, :mod:`mooring.paths`, and the
standard library, so it carries no path to marimo / the Copilot SDK / spaCy (locked by
the ``frozen-core-is-lean`` import contract). This mirrors :mod:`mooring.checks`.
"""

from __future__ import annotations

import json
from pathlib import Path

from mooring import gitsha
from mooring.paths import safe_write_bytes

STATE_DIR = ".mooring"
VERIFY_DIRNAME = "verify"


def verify_dir(workspace: Path | str) -> Path:
    """The sync-excluded dir holding the per-notebook verification receipts (and the
    throwaway HTML the run renders into and deletes)."""
    return Path(workspace) / STATE_DIR / VERIFY_DIRNAME


def _slug(rel_posix: str) -> str:
    """A filesystem-safe, INJECTIVE receipt name: escape ``_`` first, THEN map ``/``
    to ``__``, so two different paths (``a_b.py`` vs ``a/b.py``) can never collide on
    one receipt. Same scheme as the checks runtime's slug."""
    return rel_posix.replace("_", "_u").replace("/", "__")


def render_target(workspace: Path | str, rel_posix: str) -> Path:
    """The throwaway ``.html`` path a verify run renders into before deleting it.

    Kept in the sync-excluded verify dir (never a synced location — the render embeds
    data values) and named per-notebook so repeated runs reuse one temp file rather
    than accumulating."""
    return verify_dir(workspace) / f"{_slug(rel_posix)}.html"


def record(
    workspace: Path | str,
    rel: str,
    *,
    passed: bool,
    sha: str,
    cells_failed: int | None,
    ran_at: str,
) -> None:
    """Write ``rel``'s verification receipt (value-free), keyed to content ``sha``.

    Best-effort; never raises — a failed write just means no badge appears (a badge is
    a nicety, never worth breaking the caller). One file per notebook, written
    atomically, so two concurrent verifies never corrupt a shared file."""
    rel_posix = rel.replace("\\", "/")
    receipt = {
        "notebook": rel_posix,
        "sha": sha,
        "passed": bool(passed),
        "cells_failed": cells_failed if isinstance(cells_failed, int) else None,
        "ran_at": ran_at,
    }
    target = verify_dir(workspace) / f"{_slug(rel_posix)}.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_write_bytes(target, json.dumps(receipt).encode("utf-8"))
    except OSError:
        pass


def read_results(
    workspace: Path | str, local_shas: dict[str, str] | None = None
) -> dict[str, dict]:
    """Map notebook rel-path -> ``{passed, cells_failed, ran_at}`` — but ONLY for
    receipts still valid for the file on disk.

    A receipt is surfaced only when its notebook still exists AND its stored content
    SHA still equals the file's current blob SHA. A notebook edited since it was
    verified has a different SHA, so its receipt is dropped here and the badge
    disappears — the "auto-clear the instant the SHA advances" rule.

    ``local_shas`` (rel -> current blob SHA) lets the caller pass SHAs it already
    computed — the hub's sync StatusReport carries ``local_sha`` for every file, so the
    per-poll comparison costs nothing there. A rel absent from the map (or a ``None``
    entry, as in no-repo mode) falls back to hashing the file. When ``local_shas`` is
    None every match is computed here (the CLI / test path).

    Value-free; best-effort (unreadable / foreign / corrupt receipts are skipped)."""
    out: dict[str, dict] = {}
    ws = Path(workspace)
    directory = verify_dir(workspace)
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        rel = data.get("notebook")
        sha = data.get("sha")
        if not isinstance(rel, str) or not isinstance(sha, str):
            continue
        current = local_shas.get(rel) if local_shas is not None else None
        if current is None:
            # Not supplied (or no map) — hash it ourselves, dropping a gone file.
            target = ws / rel
            try:
                if not target.is_file():
                    continue  # notebook deleted — never badge a file that's gone
                current = gitsha.local_blob_sha(target, rel)
            except OSError:
                continue
        if current != sha:
            continue  # edited since verify — the badge auto-clears
        cells = data.get("cells_failed")
        out[rel] = {
            "passed": bool(data.get("passed")),
            "cells_failed": cells if isinstance(cells, int) else None,
            "ran_at": data.get("ran_at", ""),
        }
    return out


def clear(workspace: Path | str, rel: str | None = None) -> int:
    """Delete verification receipts — all of them, or just ``rel``'s. Returns the
    number removed. Lets an analyst reset a badge by hand. Best-effort; never raises.

    (Normal editing already clears a badge via :func:`read_results`' SHA check — this
    is for the rarer "forget it entirely" case.)"""
    directory = verify_dir(workspace)
    removed = 0
    try:
        files = list(directory.glob("*.json"))
    except OSError:
        return 0
    want = rel.replace("\\", "/") if rel is not None else None
    for path in files:
        if want is not None:
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if not (isinstance(data, dict) and data.get("notebook") == want):
                continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
