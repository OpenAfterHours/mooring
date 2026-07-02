"""The local activity ledger: "what just happened?" for people without a reflog.

An append-only JSONL journal, ``<workspace>/.mooring/activity.jsonl``, written
by the adapters at the same seams where they already log telemetry events —
pull/push/propose/adopt, delete, revert/undo, AI apply/rollback, trash
restores. The hub renders it as human sentences ("Yesterday 16:42 — you pushed
sales_review.py"); the CLI prints it with ``mooring activity``.

This is NOT telemetry. The opt-in central log (:mod:`mooring.telemetry`) ships
event records — op names and counts, never file paths — to an admin-configured
sink. The ledger holds filenames and one-line summaries and stays on the
machine, full stop: it lives in the ``.mooring`` state dir, which sync excludes
structurally, so it can never ride a push.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from mooring.paths import safe_write_bytes

# == manifest.MANIFEST_DIR; kept literal so this stays a dependency-free leaf.
_STATE_DIR = ".mooring"
_LEDGER_NAME = "activity.jsonl"

# Rotation bound: when an append finds the file larger than this, the newest
# _ROTATE_KEEP lines are kept and the rest dropped. Entries are ~100-300 bytes,
# so this holds months of normal use while bounding the worst case.
_ROTATE_BYTES = 1024 * 1024
_ROTATE_KEEP = 2000


def _ledger(workspace: Path | str) -> Path:
    return Path(workspace) / _STATE_DIR / _LEDGER_NAME


def record(workspace: Path | str, op: str, **fields) -> None:
    """Append one entry, best-effort: a full disk or a locked file must never
    break the sync/delete/apply operation being recorded."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "op": op}
    for key, value in fields.items():
        if value or value == 0:
            entry[key] = value
    path = _ledger(workspace)
    with contextlib.suppress(OSError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if path.stat().st_size > _ROTATE_BYTES:
                lines = path.read_bytes().splitlines()[-_ROTATE_KEEP:]
                safe_write_bytes(path, b"\n".join(lines) + b"\n")
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def read(workspace: Path | str, limit: int = 200, path: str | None = None) -> list[dict]:
    """The newest entries, newest first; optionally only those touching ``path``
    (matched against an entry's ``path`` field or ``paths`` list). Corrupt lines
    are skipped — an interrupted append must not hide the rest of the ledger."""
    ledger = _ledger(workspace)
    try:
        lines = ledger.read_text("utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or "op" not in entry:
            continue
        if path is not None and entry.get("path") != path and path not in entry.get("paths", []):
            continue
        out.append(entry)
    return out
