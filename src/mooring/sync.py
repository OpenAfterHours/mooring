"""Three-way sync between the local workspace and the team GitHub repo.

For every path we compare three blob SHAs: base (manifest — what we last
synced), local (computed from the workspace file), and remote (from the
GitHub tree). The comparison yields a FileState; pull and push act on it.
Conflicts are never auto-resolved: pull skips them unless given a strategy,
push blocks them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from mooring import gitsha, manifest as manifest_mod
from mooring.config import Config
from mooring.github import GitHubClient, RemoteConflict

# Local files matching this marker are mooring-created scratch copies of
# remote versions ("keep both" resolution) and are never synced.
REMOTE_COPY_MARKER = ".remote-"


class FileState(Enum):
    SYNCED = "synced"
    MODIFIED = "modified"  # push candidate
    NEW_LOCAL = "new local"  # push candidate
    DELETED_LOCAL = "deleted locally"  # push will delete remotely
    REMOTE_CHANGED = "remote changed"  # pull will update
    NEW_REMOTE = "new remote"  # pull will add
    DELETED_REMOTE = "deleted remotely"  # pull will remove locally
    CONFLICT = "conflict"


PUSH_STATES = {FileState.MODIFIED, FileState.NEW_LOCAL, FileState.DELETED_LOCAL}
PULL_STATES = {FileState.REMOTE_CHANGED, FileState.NEW_REMOTE, FileState.DELETED_REMOTE}


class ConflictStrategy(Enum):
    SKIP = "skip"
    THEIRS = "theirs"  # overwrite local with remote
    KEEP_BOTH = "keep-both"  # keep local, save remote as a .remote-<sha> copy
    PUSH_COPY = "push-copy"  # publish local under a new name, restore remote


@dataclass
class FileStatus:
    path: str
    state: FileState
    base_sha: str | None = None
    local_sha: str | None = None
    remote_sha: str | None = None


@dataclass
class StatusReport:
    head_commit: str
    files: list[FileStatus] = field(default_factory=list)

    def by_state(self, *states: FileState) -> list[FileStatus]:
        return [f for f in self.files if f.state in states]

    def summary(self) -> str:
        synced = len(self.by_state(FileState.SYNCED))
        to_push = len(self.by_state(*PUSH_STATES))
        to_pull = len(self.by_state(*PULL_STATES))
        conflicts = len(self.by_state(FileState.CONFLICT))
        return (
            f"{synced} in sync, {to_push} to push, {to_pull} to pull, {conflicts} conflicted"
        )


@dataclass
class SyncResult:
    lines: list[str] = field(default_factory=list)
    pulled: int = 0
    pushed: int = 0
    skipped_conflicts: list[str] = field(default_factory=list)
    blocked_conflicts: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.pulled:
            parts.append(f"pulled {self.pulled} file(s)")
        if self.pushed:
            parts.append(f"pushed {self.pushed} file(s)")
        conflicted = self.skipped_conflicts or self.blocked_conflicts
        if conflicted:
            parts.append(f"{len(conflicted)} conflict(s) need attention")
        return "; ".join(parts) if parts else "already up to date"


def classify(base: str | None, local: str | None, remote: str | None) -> FileState | None:
    """The three-way decision matrix. Returns None for stale manifest entries
    (deleted everywhere) which callers should drop."""
    if local == remote:
        if local is None:
            return None
        return FileState.SYNCED
    if base is None:
        if local is not None and remote is not None:
            return FileState.CONFLICT  # created independently on both sides
        return FileState.NEW_LOCAL if local is not None else FileState.NEW_REMOTE
    if local == base:
        return FileState.DELETED_REMOTE if remote is None else FileState.REMOTE_CHANGED
    if local is None:  # deleted locally
        return FileState.DELETED_LOCAL if remote == base else FileState.CONFLICT
    # local changed
    if remote == base:
        return FileState.MODIFIED
    return FileState.CONFLICT  # remote changed or deleted underneath us


def scan_local(workspace: Path, folders: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for folder in folders:
        root = workspace / folder
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in
                   path.relative_to(workspace).parts):
                continue
            if REMOTE_COPY_MARKER in path.name:
                continue
            rel = path.relative_to(workspace).as_posix()
            out[rel] = gitsha.local_blob_sha(path, rel)
    return out


def _remote_entries(
    client: GitHubClient, cfg: Config, head: str, mft: manifest_mod.Manifest
) -> dict[str, str]:
    # If the branch head hasn't moved since our last sync, the remote tree is
    # exactly what the manifest recorded — no tree fetch needed.
    if head and head == mft.head_commit:
        return dict(mft.files)
    return {e.path: e.sha for e in client.get_tree(head, cfg.folders)}


def compute_status(
    mft: manifest_mod.Manifest,
    local: dict[str, str],
    remote: dict[str, str],
    head: str,
) -> StatusReport:
    report = StatusReport(head_commit=head)
    for path in sorted(set(mft.files) | set(local) | set(remote)):
        state = classify(mft.files.get(path), local.get(path), remote.get(path))
        if state is None:
            continue
        report.files.append(
            FileStatus(
                path=path,
                state=state,
                base_sha=mft.files.get(path),
                local_sha=local.get(path),
                remote_sha=remote.get(path),
            )
        )
    return report


def status(client: GitHubClient, cfg: Config) -> StatusReport:
    workspace = cfg.workspace()
    mft = manifest_mod.load(workspace)
    head = client.get_branch_head(cfg.branch)
    local = scan_local(workspace, cfg.folders)
    remote = _remote_entries(client, cfg, head, mft)
    return compute_status(mft, local, remote, head)


def _write_blob(workspace: Path, rel_path: str, data: bytes) -> None:
    target = workspace / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _remote_copy_name(rel_path: str, remote_sha: str) -> str:
    p = Path(rel_path)
    return str(p.with_name(f"{p.stem}{REMOTE_COPY_MARKER}{remote_sha[:7]}{p.suffix}")).replace(
        "\\", "/"
    )


def pull(
    client: GitHubClient,
    cfg: Config,
    strategy: ConflictStrategy = ConflictStrategy.SKIP,
) -> SyncResult:
    workspace = cfg.workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    mft = manifest_mod.load(workspace)
    head = client.get_branch_head(cfg.branch)
    local = scan_local(workspace, cfg.folders)
    remote = _remote_entries(client, cfg, head, mft)
    report = compute_status(mft, local, remote, head)
    result = SyncResult()

    for f in report.files:
        if f.state in (FileState.NEW_REMOTE, FileState.REMOTE_CHANGED):
            _write_blob(workspace, f.path, client.get_blob(f.remote_sha))
            mft.files[f.path] = f.remote_sha
            result.pulled += 1
            result.lines.append(f"pulled   {f.path}")
        elif f.state is FileState.DELETED_REMOTE:
            (workspace / f.path).unlink(missing_ok=True)
            mft.files.pop(f.path, None)
            result.pulled += 1
            result.lines.append(f"removed  {f.path} (deleted remotely)")
        elif f.state is FileState.SYNCED:
            if f.local_sha and f.base_sha != f.local_sha:
                mft.files[f.path] = f.local_sha  # same change on both sides
        elif f.state is FileState.CONFLICT:
            if strategy is ConflictStrategy.THEIRS:
                if f.remote_sha is None:
                    (workspace / f.path).unlink(missing_ok=True)
                    mft.files.pop(f.path, None)
                else:
                    _write_blob(workspace, f.path, client.get_blob(f.remote_sha))
                    mft.files[f.path] = f.remote_sha
                result.pulled += 1
                result.lines.append(f"pulled   {f.path} (overwrote local edits)")
            elif strategy is ConflictStrategy.KEEP_BOTH and f.remote_sha is not None:
                copy_path = _remote_copy_name(f.path, f.remote_sha)
                _write_blob(workspace, copy_path, client.get_blob(f.remote_sha))
                mft.files[f.path] = f.remote_sha  # local file is now "modified", pushable
                result.lines.append(f"kept     {f.path}; remote saved as {copy_path}")
            else:
                result.skipped_conflicts.append(f.path)
                result.lines.append(f"conflict {f.path} (skipped — resolve in the hub)")

    # Drop manifest entries for files deleted on both sides.
    for path in list(mft.files):
        if path not in local and path not in remote:
            del mft.files[path]

    mft.branch = cfg.branch
    mft.head_commit = head
    manifest_mod.save(workspace, mft)
    return result


def push(
    client: GitHubClient,
    cfg: Config,
    paths: list[str] | None = None,
    message: str | None = None,
    throttle: float = 0.8,
    sleep=time.sleep,
) -> SyncResult:
    workspace = cfg.workspace()
    mft = manifest_mod.load(workspace)
    head = client.get_branch_head(cfg.branch)
    local = scan_local(workspace, cfg.folders)
    remote = _remote_entries(client, cfg, head, mft)
    report = compute_status(mft, local, remote, head)
    result = SyncResult()

    wanted = {p.replace("\\", "/") for p in paths} if paths else None
    candidates = [
        f
        for f in report.by_state(*PUSH_STATES)
        if wanted is None or f.path in wanted
    ]
    for f in report.by_state(FileState.CONFLICT):
        if wanted is None or f.path in wanted:
            result.blocked_conflicts.append(f.path)
            result.lines.append(
                f"conflict {f.path} (blocked — pull first, or resolve in the hub)"
            )

    last_commit = ""
    for index, f in enumerate(candidates):
        if index > 0 and throttle:
            sleep(throttle)  # contents-API writes trip secondary rate limits if rapid
        if f.state is FileState.DELETED_LOCAL:
            response = client.delete_file(
                f.path, message or f"Delete {f.path} via mooring", cfg.branch, f.base_sha
            )
            mft.files.pop(f.path, None)
            result.lines.append(f"deleted  {f.path}")
        else:
            data = gitsha.read_for_push(workspace / f.path, f.path)
            size_mb = len(data) / (1024 * 1024)
            if size_mb > cfg.max_file_mb:
                result.lines.append(
                    f"refused  {f.path} ({size_mb:.0f} MB > {cfg.max_file_mb} MB limit)"
                )
                continue
            if size_mb > cfg.warn_file_mb:
                result.lines.append(f"warning  {f.path} is {size_mb:.0f} MB")
            try:
                response = client.put_file(
                    f.path,
                    data,
                    message or f"Update {f.path} via mooring",
                    cfg.branch,
                    base_sha=f.base_sha,
                )
            except RemoteConflict:
                result.blocked_conflicts.append(f.path)
                result.lines.append(f"conflict {f.path} (remote changed — pull first)")
                continue
            mft.files[f.path] = response["content"]["sha"]
            result.lines.append(f"pushed   {f.path}")
        commit = response.get("commit", {}).get("sha", "")
        if commit:
            last_commit = commit
        result.pushed += 1

    if last_commit:
        mft.head_commit = last_commit
    mft.branch = cfg.branch
    manifest_mod.save(workspace, mft)
    return result


def resolve(
    client: GitHubClient,
    cfg: Config,
    rel_path: str,
    strategy: ConflictStrategy,
    username: str = "",
) -> SyncResult:
    """Resolve a single conflicted file (hub per-file actions)."""
    workspace = cfg.workspace()
    mft = manifest_mod.load(workspace)
    head = client.get_branch_head(cfg.branch)
    remote = _remote_entries(client, cfg, head, mft)
    remote_sha = remote.get(rel_path)
    result = SyncResult()

    if strategy is ConflictStrategy.THEIRS:
        if remote_sha is None:
            (workspace / rel_path).unlink(missing_ok=True)
            mft.files.pop(rel_path, None)
        else:
            _write_blob(workspace, rel_path, client.get_blob(remote_sha))
            mft.files[rel_path] = remote_sha
        result.pulled += 1
        result.lines.append(f"pulled   {rel_path} (overwrote local edits)")
    elif strategy is ConflictStrategy.KEEP_BOTH:
        if remote_sha is not None:
            copy_path = _remote_copy_name(rel_path, remote_sha)
            _write_blob(workspace, copy_path, client.get_blob(remote_sha))
            mft.files[rel_path] = remote_sha
            result.lines.append(f"kept     {rel_path}; remote saved as {copy_path}")
        else:
            mft.files.pop(rel_path, None)  # remote deleted; local survives as new
            result.lines.append(f"kept     {rel_path} (remote deleted it)")
    elif strategy is ConflictStrategy.PUSH_COPY:
        p = Path(rel_path)
        suffix = username or "copy"
        copy_path = str(p.with_name(f"{p.stem}-{suffix}{p.suffix}")).replace("\\", "/")
        data = gitsha.read_for_push(workspace / rel_path, rel_path)
        _write_blob(workspace, copy_path, data)
        response = client.put_file(
            copy_path, data, f"Add {copy_path} via mooring (conflict copy)", cfg.branch
        )
        mft.files[copy_path] = response["content"]["sha"]
        if remote_sha is None:
            (workspace / rel_path).unlink(missing_ok=True)
            mft.files.pop(rel_path, None)
        else:
            _write_blob(workspace, rel_path, client.get_blob(remote_sha))
            mft.files[rel_path] = remote_sha
        result.pushed += 1
        result.lines.append(f"pushed   {copy_path}; restored {rel_path} from remote")
    else:
        result.skipped_conflicts.append(rel_path)

    mft.branch = cfg.branch
    manifest_mod.save(workspace, mft)
    return result
