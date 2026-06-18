"""Local, pre-edit snapshots of a notebook ``.py`` so an AI Apply can be rolled back.

Every Apply (append / edit / rewrite) snapshots the notebook's current bytes BEFORE
writing; an Undo pops the most recent snapshot and restores it, and the editor's
``--watch`` reloads the prior content — exactly the channel an Apply itself rides.
So Undo is a pure, value-free LOCAL file restore: no marimo import, no AI egress, no
websocket. It is the symmetric counterpart to :mod:`mooring.ai.cellwrite`.

Snapshots live under ``<workspace>/.mooring/undo/<notebook>/`` — inside the
per-workspace ``.mooring`` state dir (``manifest.MANIFEST_DIR``), which sync, the
notebook listing, and deletion already ignore structurally (a ``.``-prefixed dir),
so a snapshot never reaches the team repo. The stack is bounded per notebook.

Caveat (cloud sync): like ``.mooring/manifest.json``, snapshots inherit the
workspace's fate under a cloud-sync provider (OneDrive/Dropbox/…), which can revert
files behind mooring's back — so they are a convenience, not durable history.

Caveat (open-tab refresh): the restore is byte-faithful, so it can equal a state the
running marimo editor itself last *saved*. marimo's ``--watch`` reload skips a file
change whose ``.strip()`` matches its own last save, so an Undo back to such a state
updates the file on disk (authoritative) but the OPEN tab may not repaint until the
next edit or a manual browser refresh. We keep the faithful restore (correct on disk)
rather than perturb the bytes to force a repaint.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from pathlib import Path

from mooring.paths import safe_write_bytes

# == manifest.MANIFEST_DIR; kept literal so this stays a dependency-free leaf. The
# per-workspace state dir is foundational ('.mooring') and structurally sync-excluded.
_STATE_DIR = ".mooring"
_UNDO_DIR = "undo"
_MAX_SNAPSHOTS = 25  # bounded undo depth per notebook; older snapshots are pruned


def _norm(notebook_rel: str) -> str:
    """The notebook's identity: rel-path with separators normalized to '/' (so a
    forward- and back-slashed form of the SAME path share one stack)."""
    return str(notebook_rel).replace("\\", "/").strip("/")


def _key(notebook_rel: str) -> str:
    """An INJECTIVE folder name for one notebook.

    A readable slug alone is not injective — e.g. ``a/b.py`` and ``a_b.py`` both slug
    to ``a_b.py`` — which would merge two distinct notebooks onto ONE undo stack and
    let an Undo restore the WRONG file. Appending a hash of the normalized rel-path
    keeps the folder readable while guaranteeing distinct notebooks never collide.
    """
    norm = _norm(notebook_rel)
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", norm) or "_"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _dir(workspace: Path | str, notebook_rel: str) -> Path:
    return Path(workspace) / _STATE_DIR / _UNDO_DIR / _key(notebook_rel)


def _snapshots(d: Path) -> list[Path]:
    """Existing snapshot files for a notebook, oldest → newest (numeric stems)."""
    if not d.is_dir():
        return []
    snaps = [p for p in d.glob("*.py") if p.stem.isdigit()]
    return sorted(snaps, key=lambda p: int(p.stem))


def snapshot(workspace: Path | str, notebook_rel: str, data: bytes) -> str:
    """Push ``data`` (the notebook's current bytes) onto the undo stack; returns the
    snapshot token. Prunes the oldest beyond :data:`_MAX_SNAPSHOTS`."""
    d = _dir(workspace, notebook_rel)
    d.mkdir(parents=True, exist_ok=True)
    existing = _snapshots(d)
    token = f"{(int(existing[-1].stem) + 1 if existing else 1):012d}"
    safe_write_bytes(d / f"{token}.py", data)
    for stale in _snapshots(d)[:-_MAX_SNAPSHOTS]:
        with contextlib.suppress(OSError):
            stale.unlink()
    return token


def discard(workspace: Path | str, notebook_rel: str, token: str) -> None:
    """Remove a specific snapshot (used to undo a snapshot whose Apply then failed)."""
    with contextlib.suppress(OSError):
        (_dir(workspace, notebook_rel) / f"{token}.py").unlink()


def peek_latest(workspace: Path | str, notebook_rel: str) -> tuple[str, bytes] | None:
    """The most recent snapshot as ``(token, bytes)`` WITHOUT removing it, or ``None``.

    Lets a caller restore-then-:func:`discard`, so a failed restore write leaves the
    snapshot in place to retry (never consumed before it is safely applied).
    """
    snaps = _snapshots(_dir(workspace, notebook_rel))
    if not snaps:
        return None
    latest = snaps[-1]
    return latest.stem, latest.read_bytes()


def pop(workspace: Path | str, notebook_rel: str) -> bytes | None:
    """Pop and return the most recent snapshot's bytes (read-then-remove), or
    ``None`` if there is nothing to undo."""
    snaps = _snapshots(_dir(workspace, notebook_rel))
    if not snaps:
        return None
    latest = snaps[-1]
    data = latest.read_bytes()
    with contextlib.suppress(OSError):
        latest.unlink()
    return data


def depth(workspace: Path | str, notebook_rel: str) -> int:
    """How many undo steps are currently available for ``notebook_rel``."""
    return len(_snapshots(_dir(workspace, notebook_rel)))
