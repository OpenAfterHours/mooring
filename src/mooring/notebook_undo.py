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
import secrets
from pathlib import Path

from mooring.paths import safe_write_bytes

# == manifest.MANIFEST_DIR; kept literal so this stays a dependency-free leaf. The
# per-workspace state dir is foundational ('.mooring') and structurally sync-excluded.
_STATE_DIR = ".mooring"
_UNDO_DIR = "undo"
_MAX_SNAPSHOTS = 25  # bounded undo depth per notebook; older snapshots are pruned

# A snapshot's filename: an ORDERING sequence and a UNIQUENESS suffix, e.g.
# ``000000000003-1f9c0ab27de4.py``. Both halves are load-bearing and neither can do
# the other's job:
#
# * the zero-padded sequence orders the stack (a snapshot is always ``max + 1`` of
#   what is there, so newest sorts last) — but it RESTARTS whenever the stack drains,
#   which made bare counters repeat across a notebook's lifetime;
# * the random suffix makes the token globally unique, which is what the two callers
#   that compare tokens actually mean. ``ApplyGuard._extends_turn`` asks "is the
#   snapshot my turn took still the one on top?" and ``ApplyGuard.restore_undo``'s
#   ``expect_token`` asks "is the layer I am undoing still the newest?" — with a
#   repeating counter both could be answered YES by a DIFFERENT, later snapshot that
#   merely reused the number, silently skipping a needed checkpoint or reverting the
#   wrong layer.
#
# Identity lives in the filename rather than in the bytes on purpose: an Undo restores
# exactly the bytes its snapshot holds, so the very next snapshot taken after one often
# has IDENTICAL content — content could not tell those two apart, and a token must.
# Nothing is held in memory, so the answer survives a hub restart mid-turn.
#
# The optional-suffix form also reads stacks written before this scheme (bare digits),
# so an upgrade neither loses nor mis-sorts an analyst's existing undo history.
_SNAPSHOT_RE = re.compile(r"^(\d+)(?:-[0-9a-f]+)?$")
_SUFFIX_BYTES = 6  # 12 hex chars; a repeat needs the SAME sequence AND the same draw


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


def _order(p: Path) -> tuple[int, str]:
    """A snapshot's sort key: its sequence first, then its whole stem as a tiebreak so
    the order is total and stable even if a hand-edited dir ever repeated a sequence."""
    return (int(p.stem.split("-", 1)[0]), p.stem)


def _snapshots(d: Path) -> list[Path]:
    """Existing snapshot files for a notebook, oldest → newest."""
    if not d.is_dir():
        return []
    snaps = [p for p in d.glob("*.py") if _SNAPSHOT_RE.match(p.stem)]
    return sorted(snaps, key=_order)


def snapshot(workspace: Path | str, notebook_rel: str, data: bytes) -> str:
    """Push ``data`` (the notebook's current bytes) onto the undo stack; returns the
    snapshot token — unique for the life of the notebook, never reused once popped (see
    :data:`_SNAPSHOT_RE`). Prunes the oldest beyond :data:`_MAX_SNAPSHOTS`."""
    d = _dir(workspace, notebook_rel)
    d.mkdir(parents=True, exist_ok=True)
    existing = _snapshots(d)
    seq = _order(existing[-1])[0] + 1 if existing else 1
    token = f"{seq:012d}-{secrets.token_hex(_SUFFIX_BYTES)}"
    safe_write_bytes(d / f"{token}.py", data)
    for stale in _snapshots(d)[:-_MAX_SNAPSHOTS]:
        with contextlib.suppress(OSError):
            stale.unlink()
    return token


def discard(workspace: Path | str, notebook_rel: str, token: str) -> None:
    """Remove a specific snapshot (used to undo a snapshot whose Apply then failed).

    A token that is not one this module minted deletes nothing. Every caller today
    passes a token straight back from :func:`snapshot` or :func:`peek_latest`, but the
    token shape now travels to the browser and back (``undo_token``), and "no caller
    would pass ``../../nb``" is a convention where this is a check.
    """
    if not _SNAPSHOT_RE.match(str(token)):
        return
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
