"""Local pre-image trash: the last resort for mooring-initiated destruction.

Every code path that overwrites or removes LOCAL bytes on the user's behalf —
conflict "Use remote" (resolve/pull THEIRS), a pull's overwrite/remove arms,
delete — first deposits the file's current bytes here, so a misclick is
recoverable with one click instead of gone. Deposits live under
``<workspace>/.mooring/trash/`` — inside the per-workspace ``.mooring`` state
dir, which sync, the listing, and deletion already ignore structurally (a
``.``-prefixed dir), so a trashed pre-image can never reach the team repo.

Each deposit is one flat blob file plus a small JSON meta file, both named by
the deposit token. Names reuse the injective ``slug-hash8`` recipe from
:mod:`mooring.notebook_undo` (readable slugs alone collide across separators:
``a/b.py`` and ``a_b.py`` slug identically), with a monotonic-ish
timestamp+random tail so repeated deposits of one file never clash. The true
rel-path travels in the meta file, never parsed back out of the name.

Restore is token-exact and refuses when a LATER write is on top (the file's
current blob sha no longer matches the sha the destructive action left
behind), mirroring how the notebook-undo stack refuses a superseded token —
the two stores stay separate and neither can restore the other's layer.

Like ``.mooring/undo/``, the trash inherits the workspace's fate under a
cloud-sync provider: it is a convenience, not durable history.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from mooring.gitsha import blob_sha, is_normalized_path, normalize
from mooring.paths import safe_write_bytes

# == manifest.MANIFEST_DIR; kept literal so this stays a dependency-free leaf.
_STATE_DIR = ".mooring"
_TRASH_DIR = "trash"

# Retention defaults (overridable via the [trash] config keys).
DEFAULT_KEEP_DAYS = 14
DEFAULT_KEEP_PER_FILE = 10
DEFAULT_MAX_FILE_MB = 45  # matches [sync] max_file_mb — nothing larger rides sync
DEFAULT_MAX_TOTAL_MB = 200


class RestoreSuperseded(Exception):
    """A later write is on top of the deposit — restoring would clobber it."""


def _dir(workspace: Path | str) -> Path:
    return Path(workspace) / _STATE_DIR / _TRASH_DIR


def _token(rel_path: str) -> str:
    """An injective, time-sortable deposit token (also the blob/meta file stem).
    The slug is readability only (the hash carries injectivity), so it is capped —
    on default Windows a deep workspace plus a long rel-path could otherwise push
    the deposit filename past MAX_PATH and silently cost the file its safety net."""
    norm = str(rel_path).replace("\\", "/").strip("/")
    slug = (re.sub(r"[^A-Za-z0-9._-]", "_", norm) or "_")[:40]
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}-{int(time.time() * 1000):013d}-{secrets.token_hex(2)}"


def _local_sha(data: bytes, rel_path: str) -> str:
    """The blob sha ``data`` would hash to as a workspace file at ``rel_path``
    (``.py`` is LF-normalized, matching gitsha.local_blob_sha / manifest shas)."""
    return blob_sha(normalize(data) if is_normalized_path(rel_path) else data)


def deposit(
    workspace: Path | str,
    rel_path: str,
    data: bytes,
    action: str,
    *,
    after_sha: str | None = None,
    max_file_mb: int = DEFAULT_MAX_FILE_MB,
) -> str | None:
    """Save ``data`` (the file's pre-image) before a destructive action.

    ``action`` names what destroyed it (``pull-theirs``, ``resolve-theirs``,
    ``delete``, ``pull-overwrite``, ``restore``, …). ``after_sha`` is the blob
    sha the destructive action wrote afterwards (``None`` for a removal) —
    :func:`restore` uses it to detect supersession. Returns the token, or
    ``None`` when the file exceeds the per-file cap (deliberately not an
    error: the destructive action proceeds either way; the trash is a net,
    not a gate).
    """
    if len(data) > max_file_mb * 1024 * 1024:
        return None
    rel = str(rel_path).replace("\\", "/").strip("/")
    token = _token(rel)
    d = _dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    safe_write_bytes(d / f"{token}.bin", data)
    meta = {
        "token": token,
        "path": rel,
        "action": action,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "size": len(data),
        "sha": _local_sha(data, rel),
        "after_sha": after_sha,
    }
    safe_write_bytes(d / f"{token}.json", json.dumps(meta).encode("utf-8"))
    return token


def entries(workspace: Path | str) -> list[dict]:
    """All deposits, newest first. Unreadable meta files are skipped, not fatal."""
    d = _dir(workspace)
    if not d.is_dir():
        return []
    out = []
    for meta_file in d.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("token") and meta.get("path"):
            out.append(meta)
    out.sort(key=lambda m: str(m.get("ts", "")), reverse=True)
    return out


def _load(workspace: Path | str, token: str) -> tuple[dict, bytes]:
    d = _dir(workspace)
    # Tokens are generated (never user paths), but be strict anyway: a token is a
    # single flat file stem, so a separator means it is not one of ours.
    if not token or any(sep in token for sep in ("/", "\\", "..")):
        raise KeyError(token)
    try:
        meta = json.loads((d / f"{token}.json").read_text("utf-8"))
        data = (d / f"{token}.bin").read_bytes()
    except (OSError, ValueError) as exc:
        raise KeyError(token) from exc
    return meta, data


def restore(workspace: Path | str, token: str) -> str:
    """Put a deposit's bytes back at its original path.

    Refuses (:class:`RestoreSuperseded`) when the file has moved on since the
    destructive action: a removal's path now exists again, or an overwrite's
    current blob sha no longer matches the sha the action wrote. The current
    bytes (when any) are deposited first, so a restore is itself undoable.
    Never touches the manifest — the three-way engine simply reclassifies the
    file on the next status, so a restore can never silently diverge from the
    remote. Raises ``KeyError`` for an unknown token.
    """
    workspace = Path(workspace)
    meta, data = _load(workspace, token)
    rel = str(meta["path"])
    target = workspace / rel
    after_sha = meta.get("after_sha")
    if after_sha is None:
        # The action removed the file; if something exists there now, a later
        # write is on top — restoring would clobber it.
        if target.exists():
            raise RestoreSuperseded(rel)
    else:
        if not target.is_file():
            raise RestoreSuperseded(rel)
        current = target.read_bytes()
        # after_sha may be a manifest-convention sha (LF-normalized for .py) or a
        # raw remote blob sha (a CRLF .py committed outside mooring hashes raw) —
        # accept either, or every restore of such a file would refuse forever.
        if after_sha not in (_local_sha(current, rel), blob_sha(current)):
            raise RestoreSuperseded(rel)
        # Undo is itself undoable: bank what the destructive action wrote.
        deposit(workspace, rel, current, "restore", after_sha=str(meta.get("sha") or ""))
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_write_bytes(target, data)
    return rel


def prune(
    workspace: Path | str,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    keep_per_file: int = DEFAULT_KEEP_PER_FILE,
    max_total_mb: int = DEFAULT_MAX_TOTAL_MB,
) -> int:
    """Best-effort retention: drop deposits older than ``keep_days``, keep at
    most ``keep_per_file`` per rel-path (newest win), then evict oldest-first
    until the store fits ``max_total_mb``. Returns how many were dropped."""
    all_entries = entries(workspace)  # newest first
    cutoff = time.time() - keep_days * 86400
    per_file: dict[str, int] = {}
    keep: list[dict] = []
    drop: list[dict] = []
    for meta in all_entries:
        try:
            ts = datetime.fromisoformat(str(meta.get("ts", ""))).timestamp()
        except ValueError:
            drop.append(meta)
            continue
        count = per_file.get(meta["path"], 0)
        if ts < cutoff or count >= keep_per_file:
            drop.append(meta)
            continue
        per_file[meta["path"]] = count + 1
        keep.append(meta)
    total = sum(int(m.get("size", 0)) for m in keep)
    cap = max_total_mb * 1024 * 1024
    while keep and total > cap:
        oldest = keep.pop()  # keep[] is newest-first
        total -= int(oldest.get("size", 0))
        drop.append(oldest)
    d = _dir(workspace)
    for meta in drop:
        for suffix in (".bin", ".json"):
            with contextlib.suppress(OSError):
                (d / f"{meta['token']}{suffix}").unlink()
    return len(drop)
