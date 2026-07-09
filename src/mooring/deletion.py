"""Delete notebooks (and Power BI projects) from the local workspace.

Deletion is local-only: removing the file makes the three-way sync classify it
as "deleted locally" (a push candidate), and the next push or propose removes it
from the team repo — the same explicit-sync path every other change takes.
Nothing here touches the manifest or the remote.

A ``.pbip`` pointer deletes the whole artifact: the pointer plus every file under
its sibling ``.SemanticModel/`` and ``.Report/`` folders (see :mod:`mooring.pbip`).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable
from pathlib import Path

from mooring import pbip, trash
from mooring.sync import is_synced_path, within_folders


def _resolve_within(workspace: Path, rel_path: str) -> Path:
    """Resolve a workspace-relative path, rejecting anything that escapes it
    (including via a symlink/junction component, since ``resolve()`` follows them)."""
    target = (workspace / rel_path).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ValueError("Path escapes the workspace.") from exc
    return target


def _is_within(ws_real: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(ws_real)
        return True
    except (ValueError, OSError):
        return False


def _in_synced_folder(rel_path: str, folders: Iterable[str] | None) -> bool:
    """Whether ``rel_path`` is in sync scope: under one of the configured sync roots,
    or a loose top-level file (which sync by default). ``None`` means 'unrestricted'
    (direct callers/tests); production passes cfg.folders so the delete set matches the
    files the hub actually lists (scan_local scope)."""
    if folders is None:
        return True
    # Mirror sync.in_sync_scope exactly (within a synced folder — supporting multi-segment
    # folders like "notebooks/team-a" — OR a loose top-level file), so the delete set
    # matches what the hub lists and sync tracks. A plain top-segment membership test would
    # refuse a nested notebook under a multi-segment synced folder that the hub does list.
    return within_folders(rel_path, tuple(folders)) or "/" not in rel_path


def _expand(workspace: Path, rel_path: str) -> list[str]:
    """The workspace-relative files that make up ``rel_path``: just the file
    itself, or — for a ``.pbip`` pointer — the pointer plus every file under its
    artifact folders, so the project folder disappears cleanly.

    ``rglob`` descends into symlinks/junctions, so each member's real path is
    re-checked against the workspace: a reparse point planted inside an artifact
    folder must never make a delete reach outside the workspace.
    """
    if not rel_path.endswith(pbip.POINTER_SUFFIX):
        return [rel_path]
    key = rel_path[: -len(pbip.POINTER_SUFFIX)]
    ws_real = workspace.resolve()
    members = [rel_path]
    for suffix in pbip.ARTIFACT_DIR_SUFFIXES:
        folder = workspace / f"{key}{suffix}"
        if folder.is_dir():
            members += [
                p.relative_to(workspace).as_posix()
                for p in sorted(folder.rglob("*"))
                if p.is_file() and _is_within(ws_real, p)
            ]
    return members


def _prune_empty_dirs(workspace: Path, start: Path) -> None:
    """Remove now-empty directories a delete left behind, walking up to — but
    never removing — the workspace root."""
    ws = workspace.resolve()
    current = start.resolve()
    while current != ws and ws in current.parents:
        try:
            current.rmdir()  # raises if the directory still has contents
        except OSError:
            return
        current = current.parent


def target_paths(
    workspace: Path,
    rel_path: str,
    exclude: Iterable[str] = (),
    folders: Iterable[str] | None = None,
) -> list[str]:
    """The workspace-relative files that deleting ``rel_path`` would remove.

    Does not touch disk. Raises ``ValueError`` for a path that escapes the
    workspace or is not a deletable notebook — a dotfile, the ``.mooring``
    manifest, or anything outside the configured sync folders — so callers can
    preview and confirm before deleting.
    """
    rel_path = rel_path.replace("\\", "/").rstrip("/")
    if not rel_path:
        raise ValueError("No path to delete.")
    _resolve_within(workspace, rel_path)
    if not is_synced_path(rel_path, exclude) or not _in_synced_folder(rel_path, folders):
        raise ValueError(f"Refusing to delete {rel_path!r}: not a notebook in this workspace.")
    return _expand(workspace, rel_path)


def delete(
    workspace: Path,
    rel_path: str,
    exclude: Iterable[str] = (),
    folders: Iterable[str] | None = None,
    *,
    trash_cap_mb: int = trash.DEFAULT_MAX_FILE_MB,
    on_trash: Callable[[str, str], None] | None = None,
) -> list[str]:
    """Delete one notebook — a single file, or a whole PBIP artifact for a
    ``.pbip`` pointer — from the workspace.

    Every removed file's bytes are first banked in the local trash
    (:mod:`mooring.trash`) so a misclicked delete is recoverable;
    ``on_trash(rel, token)`` is called per deposit so callers can offer Undo.
    The deposit is best-effort — a trash failure never blocks the delete.

    Returns the workspace-relative POSIX paths actually removed. Raises
    ``ValueError`` on a traversal/non-notebook path and ``FileNotFoundError``
    when nothing existed to delete.
    """
    removed: list[str] = []
    for rel in target_paths(workspace, rel_path, exclude, folders):
        target = workspace / rel
        if target.is_file():
            token = None
            with contextlib.suppress(OSError):
                token = trash.deposit(
                    workspace, rel, target.read_bytes(), "delete", max_file_mb=trash_cap_mb
                )
            target.unlink()
            removed.append(rel)
            if token and on_trash is not None:
                on_trash(rel, token)
            _prune_empty_dirs(workspace, target.parent)
    if not removed:
        raise FileNotFoundError(rel_path)
    return removed
