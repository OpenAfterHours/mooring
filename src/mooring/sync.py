"""Three-way sync between the local workspace and the team GitHub repo.

For every path we compare three blob SHAs: base (manifest — what we last
synced), local (computed from the workspace file), and remote (from the
GitHub tree). The comparison yields a FileState; pull and push act on it.
Conflicts are never auto-resolved: pull skips them unless given a strategy,
push blocks them.

propose() uploads push candidates to an auto-created review branch instead
of cfg.branch, so the user can open a pull request on GitHub. Files sent
that way show as IN_REVIEW until the merge is observed on cfg.branch (or the
review branch disappears); reconciling that state means status() may save
the manifest.
"""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from mooring import gitsha, manifest as manifest_mod
from mooring.config import Config
from mooring.github import (
    GitHubClient,
    NotFound,
    RefAlreadyExists,
    RemoteConflict,
    compare_url,
)

# Root-level files that participate in sync even though they live outside the
# configured folders: the repo's notebook-dependency project (see
# mooring.pyproject_env). They ride pull/push/propose like any tracked file.
PROJECT_FILES = ("pyproject.toml", "uv.lock")

# All root-level files that ride sync: the dependency project plus the synced
# per-workspace settings file (mooring.workspace_config), which carries team
# settings such as the per-notebook AI opt-out. Threaded through scan_local and
# the get_tree calls so the two sync sides agree on it like any tracked file.
SYNCED_ROOT_FILES = (*PROJECT_FILES, "mooring.toml")

# Local files matching this marker are mooring-created scratch copies of
# remote versions ("keep both" resolution) and are never synced.
REMOTE_COPY_MARKER = ".remote-"

# Dotfiles are normally excluded, but Power BI project (PBIP) artifacts carry
# a required ".platform" metadata file inside each artifact folder.
KEEP_DOT_NAMES = {".platform"}

# Machine-generated directories that never belong in the shared repo, skipped
# both as a directory anywhere on the path and as a leaf name. __pycache__ is
# CPython bytecode; __marimo__ is marimo's per-session state/layout/cache.
MACHINE_DIRS = frozenset({"__pycache__", "__marimo__"})


def _excluded_by_patterns(rel_path: str, patterns: Iterable[str]) -> bool:
    """Whether a user-configured [sync] exclude pattern hides this path.

    Patterns are case-sensitive globs (paths are POSIX and case-sensitive on
    GitHub). A bare pattern matches any single path segment anywhere in the tree
    (e.g. "*.tmp", "secrets.json", "drafts" — note "drafts" also hides a
    top-level synced folder of that name). A pattern containing "/" matches the
    whole relative path; "*" there spans "/" (so "reports/drafts/*" also hides
    "reports/drafts/sub/deep.py"). A trailing "/" is accepted and means the same
    as the bare form, matching the gitignore "directory" idiom.
    """
    segments = rel_path.split("/")
    for pat in patterns:
        pat = pat.rstrip("/")  # "scratch/" is the directory idiom for "scratch"
        if not pat:
            continue  # empty / all-slashes pattern matches nothing
        if "/" in pat:
            if fnmatch.fnmatchcase(rel_path, pat):
                return True
        elif any(fnmatch.fnmatchcase(seg, pat) for seg in segments):
            return True
    return False


def is_synced_path(rel_path: str, exclude: Iterable[str] = ()) -> bool:
    """Whether a workspace-relative POSIX path participates in sync.

    Applied to both the local scan and the remote tree so the two sides agree:
    a path invisible on one side must be invisible on both, otherwise pull
    records it in the manifest and the next push deletes it remotely. For that
    reason `exclude` must be the same on every call within a run.
    """
    *dirs, name = rel_path.split("/")
    if any(d.startswith(".") or d in MACHINE_DIRS for d in dirs):
        return False  # .mooring/, PBIP .pbi/ machine-local state, __marimo__, etc.
    if name.startswith(".") and name not in KEEP_DOT_NAMES:
        return False
    if name in MACHINE_DIRS:
        return False
    if REMOTE_COPY_MARKER in name:
        return False
    return not _excluded_by_patterns(rel_path, exclude)


class FileState(Enum):
    SYNCED = "synced"
    MODIFIED = "modified"  # push candidate
    NEW_LOCAL = "new local"  # push candidate
    DELETED_LOCAL = "deleted locally"  # push will delete remotely
    REMOTE_CHANGED = "remote changed"  # pull will update
    NEW_REMOTE = "new remote"  # pull will add
    DELETED_REMOTE = "deleted remotely"  # pull will remove locally
    CONFLICT = "conflict"
    IN_REVIEW = "in review"  # proposed on a review branch, awaiting merge


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
    review_branch: str = ""

    def by_state(self, *states: FileState) -> list[FileStatus]:
        return [f for f in self.files if f.state in states]

    def summary(self) -> str:
        synced = len(self.by_state(FileState.SYNCED))
        to_push = len(self.by_state(*PUSH_STATES))
        to_pull = len(self.by_state(*PULL_STATES))
        conflicts = len(self.by_state(FileState.CONFLICT))
        text = (
            f"{synced} in sync, {to_push} to push, {to_pull} to pull, {conflicts} conflicted"
        )
        in_review = len(self.by_state(FileState.IN_REVIEW))
        if in_review:
            text += f", {in_review} in review"
        return text


@dataclass
class SyncResult:
    lines: list[str] = field(default_factory=list)
    pulled: int = 0
    pushed: int = 0
    proposed: int = 0
    review_branch: str = ""
    compare_url: str = ""
    skipped_conflicts: list[str] = field(default_factory=list)
    blocked_conflicts: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.pulled:
            parts.append(f"pulled {self.pulled} file(s)")
        if self.pushed:
            parts.append(f"pushed {self.pushed} file(s)")
        if self.proposed:
            parts.append(f"proposed {self.proposed} file(s) for review")
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


def scan_local(
    workspace: Path, folders: tuple[str, ...], exclude: Iterable[str] = ()
) -> dict[str, str]:
    out: dict[str, str] = {}
    for folder in folders:
        root = workspace / folder
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(workspace).as_posix()
            if not is_synced_path(rel, exclude):
                continue
            out[rel] = gitsha.local_blob_sha(path, rel)
    for name in SYNCED_ROOT_FILES:
        path = workspace / name
        if path.is_file() and is_synced_path(name, exclude):
            out[name] = gitsha.local_blob_sha(path, name)
    return out


def _remote_entries(
    client: GitHubClient, cfg: Config, head: str, mft: manifest_mod.Manifest
) -> dict[str, str]:
    # If the branch head hasn't moved since our last sync, the remote tree is
    # exactly what the manifest recorded — no tree fetch needed.
    if head and head == mft.head_commit:
        return {p: s for p, s in mft.files.items() if is_synced_path(p, cfg.exclude)}
    return {
        e.path: e.sha
        for e in client.get_tree(head, cfg.folders, SYNCED_ROOT_FILES)
        if is_synced_path(e.path, cfg.exclude)
    }


def _review_tree(client: GitHubClient, cfg: Config, branch: str) -> dict[str, str]:
    """The synced-file blob shas currently on an existing review branch, keyed by
    path — the base shas needed to write further commits onto it."""
    review_head = client.get_branch_head(branch)
    return {
        e.path: e.sha
        for e in client.get_tree(review_head, cfg.folders, SYNCED_ROOT_FILES)
        if is_synced_path(e.path, cfg.exclude)
    }


def compute_status(
    mft: manifest_mod.Manifest,
    local: dict[str, str],
    remote: dict[str, str],
    head: str,
    review: dict[str, str | None] | None = None,
) -> StatusReport:
    report = StatusReport(head_commit=head)
    review = review or {}
    for path in sorted(set(mft.files) | set(local) | set(remote) | set(review)):
        state = classify(mft.files.get(path), local.get(path), remote.get(path))
        if state is None:
            # A proposed *addition* the user has since deleted locally is absent
            # from cfg.branch (base/local/remote all None) but still lives on the
            # review branch. Keep it as a delete candidate so push/propose can
            # withdraw it from the open PR instead of silently dropping it.
            if review.get(path) is not None:
                state = FileState.DELETED_LOCAL
            else:
                continue
        # A push candidate whose local content matches what was already sent
        # to the review branch is awaiting its PR, not awaiting a push.
        # CONFLICT stays CONFLICT: remote moved underneath the proposal and
        # the user must resolve regardless.
        if state in PUSH_STATES and review and path in review and review[path] == local.get(path):
            state = FileState.IN_REVIEW
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


def _reconcile_review(
    client: GitHubClient,
    mft: manifest_mod.Manifest,
    remote: dict[str, str],
    exclude: Iterable[str] = (),
) -> bool:
    """Clear review records once cfg.branch caught up (merge observed) or the
    review branch is gone (PR closed). Returns whether the manifest changed."""
    if not mft.review_branch:
        return False
    changed = False
    for path, sent in list(mft.review_files.items()):
        # An excluded path is absent from `remote` because the filter hid it,
        # not because the proposal merged. For a proposed deletion (sent is None)
        # that absence would otherwise read as None == None and wrongly clear the
        # record, abandoning a still-open PR — so skip excluded paths entirely.
        if _excluded_by_patterns(path, exclude):
            continue
        if remote.get(path) == sent:  # blob shas are content-addressed: merged
            # The proposal landed on cfg.branch, so it is now the sync base.
            # Advance the base too: otherwise it stays at the pre-proposal blob,
            # and any edits made after the merge classify as a spurious CONFLICT
            # that neither pull (skips) nor push (blocks) can clear.
            if sent is None:
                mft.files.pop(path, None)
            else:
                mft.files[path] = sent
            del mft.review_files[path]
            changed = True
    if not mft.review_files:
        mft.review_branch = ""
        return True
    try:
        client.get_branch_head(mft.review_branch)
    except NotFound:
        mft.review_branch = ""
        mft.review_files = {}
        return True
    return changed


def status(client: GitHubClient, cfg: Config) -> StatusReport:
    prep = _prepare(client, cfg)
    if prep.review_changed:
        manifest_mod.save(prep.workspace, prep.mft)
    report = prep.report
    report.review_branch = prep.mft.review_branch
    return report


def _write_blob(workspace: Path, rel_path: str, data: bytes) -> None:
    target = workspace / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _remote_copy_name(rel_path: str, remote_sha: str) -> str:
    p = Path(rel_path)
    return str(p.with_name(f"{p.stem}{REMOTE_COPY_MARKER}{remote_sha[:7]}{p.suffix}")).replace(
        "\\", "/"
    )


@dataclass
class _Prepared:
    """The shared sync preamble result: the workspace, the loaded manifest,
    cfg.branch's head, the local/remote blob maps, the three-way status report,
    and whether _reconcile_review changed the manifest (so status can persist it)."""

    workspace: Path
    mft: manifest_mod.Manifest
    head: str
    local: dict[str, str]
    remote: dict[str, str]
    report: StatusReport
    review_changed: bool


def _prepare(client: GitHubClient, cfg: Config, *, make_workspace: bool = False) -> _Prepared:
    """The identical opening of status/pull/push/propose: load the manifest, fetch
    cfg.branch's head + remote tree, scan the local tree, reconcile any open review
    state, and compute the three-way status.

    Callers that mutate and persist the manifest themselves (pull/push/propose)
    ignore ``review_changed``; status uses it to avoid rewriting the manifest on a
    no-op. Only pull creates the workspace (``make_workspace``)."""
    workspace = cfg.workspace()
    if make_workspace:
        workspace.mkdir(parents=True, exist_ok=True)
    mft = manifest_mod.load(workspace)
    head = client.get_branch_head(cfg.branch)
    local = scan_local(workspace, cfg.folders, cfg.exclude)
    remote = _remote_entries(client, cfg, head, mft)
    review_changed = _reconcile_review(client, mft, remote, cfg.exclude)
    report = compute_status(mft, local, remote, head, review=mft.review_files)
    return _Prepared(workspace, mft, head, local, remote, report, review_changed)


def _gather_candidates(
    report: StatusReport,
    paths: list[str] | None,
    result: SyncResult,
    *,
    in_review_note: str,
) -> list[FileStatus]:
    """The push/propose candidate set, plus the conflict-blocked and in-review
    reporting both share. ``paths`` (if given) restricts to those workspace-relative
    paths; an in-scope conflict is recorded as blocked and left out of the set. Only
    the in-review wording differs between the two callers (``in_review_note``)."""
    wanted = {p.replace("\\", "/") for p in paths} if paths else None
    candidates = [f for f in report.by_state(*PUSH_STATES) if wanted is None or f.path in wanted]
    for f in report.by_state(FileState.CONFLICT):
        if wanted is None or f.path in wanted:
            result.blocked_conflicts.append(f.path)
            result.lines.append(
                f"conflict {f.path} (blocked — pull first, or resolve in the hub)"
            )
    if wanted:
        for f in report.by_state(FileState.IN_REVIEW):
            if f.path in wanted:
                result.lines.append(f"in review {f.path} {in_review_note}")
    return candidates


def _read_checked(
    workspace: Path, f: FileStatus, cfg: Config, result: SyncResult
) -> bytes | None:
    """Read a candidate's bytes for upload, enforcing the size limits shared by push
    and propose. Returns the bytes, or None when the file exceeds cfg.max_file_mb (a
    'refused' line is recorded and the caller skips it); a file over cfg.warn_file_mb
    is read but flagged."""
    data = gitsha.read_for_push(workspace / f.path, f.path)
    size_mb = len(data) / (1024 * 1024)
    if size_mb > cfg.max_file_mb:
        result.lines.append(
            f"refused  {f.path} ({size_mb:.0f} MB > {cfg.max_file_mb} MB limit)"
        )
        return None
    if size_mb > cfg.warn_file_mb:
        result.lines.append(f"warning  {f.path} is {size_mb:.0f} MB")
    return data


def _apply_remote_or_keep_both(
    client: GitHubClient,
    workspace: Path,
    mft: manifest_mod.Manifest,
    rel_path: str,
    remote_sha: str | None,
    strategy: ConflictStrategy,
    result: SyncResult,
) -> bool:
    """Apply the two conflict strategies pull and resolve share: THEIRS (take the
    remote, or delete locally when the remote is gone) and KEEP_BOTH while the
    remote still exists (save it as a .remote-<sha> copy, keep local pushable).

    Returns True when it handled the strategy; False leaves the caller to handle the
    cases that legitimately differ between pull and resolve — SKIP, KEEP_BOTH with
    the remote already deleted, and resolve's PUSH_COPY."""
    if strategy is ConflictStrategy.THEIRS:
        if remote_sha is None:
            (workspace / rel_path).unlink(missing_ok=True)
            mft.files.pop(rel_path, None)
        else:
            _write_blob(workspace, rel_path, client.get_blob(remote_sha))
            mft.files[rel_path] = remote_sha
        result.pulled += 1
        result.lines.append(f"pulled   {rel_path} (overwrote local edits)")
        return True
    if strategy is ConflictStrategy.KEEP_BOTH and remote_sha is not None:
        copy_path = _remote_copy_name(rel_path, remote_sha)
        _write_blob(workspace, copy_path, client.get_blob(remote_sha))
        mft.files[rel_path] = remote_sha  # local file is now "modified", pushable
        result.lines.append(f"kept     {rel_path}; remote saved as {copy_path}")
        return True
    return False


def pull(
    client: GitHubClient,
    cfg: Config,
    strategy: ConflictStrategy = ConflictStrategy.SKIP,
) -> SyncResult:
    prep = _prepare(client, cfg, make_workspace=True)
    workspace, mft, report = prep.workspace, prep.mft, prep.report
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
            if not _apply_remote_or_keep_both(
                client, workspace, mft, f.path, f.remote_sha, strategy, result
            ):
                result.skipped_conflicts.append(f.path)
                result.lines.append(f"conflict {f.path} (skipped — resolve in the hub)")

    # Drop manifest entries for files deleted on both sides.
    for path in list(mft.files):
        if path not in prep.local and path not in prep.remote:
            del mft.files[path]

    mft.branch = cfg.branch
    mft.head_commit = prep.head
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
    prep = _prepare(client, cfg)
    workspace, mft, report = prep.workspace, prep.mft, prep.report
    result = SyncResult()

    candidates = _gather_candidates(
        report,
        paths,
        result,
        in_review_note="(no local changes — already in the proposal)",
    )

    # A candidate that belongs to an open proposal keeps going to the review
    # branch, so the (still-unapproved) PR picks up the new edits instead of them
    # landing on cfg.branch behind the reviewer's back. Reaching cfg.branch means
    # merging/closing the PR first — _reconcile_review then clears the state.
    review_tree = (
        _review_tree(client, cfg, mft.review_branch)
        if mft.review_branch and any(f.path in mft.review_files for f in candidates)
        else {}
    )

    last_commit = ""
    touched_review = False
    stale_remote = False
    for index, f in enumerate(candidates):
        if index > 0 and throttle:
            sleep(throttle)  # contents-API writes trip secondary rate limits if rapid
        in_review = bool(mft.review_branch) and f.path in mft.review_files
        target = mft.review_branch if in_review else cfg.branch
        base = review_tree.get(f.path) if in_review else f.base_sha
        dest = " → review branch (PR)" if in_review else ""
        response: dict | None = None
        if f.state is FileState.DELETED_LOCAL:
            if not in_review:
                response = client.delete_file(
                    f.path, message or f"Delete {f.path} via mooring", target, base
                )
                mft.files.pop(f.path, None)
                result.lines.append(f"deleted  {f.path}")
            elif base is not None:
                response = client.delete_file(
                    f.path, message or f"Propose deleting {f.path} via mooring", target, base
                )
                mft.review_files[f.path] = None
                result.lines.append(f"deleted  {f.path}{dest}")
            else:
                mft.review_files[f.path] = None
                result.lines.append(f"deleted  {f.path} (already absent on review branch)")
        else:
            data = _read_checked(workspace, f, cfg, result)
            if data is None:
                continue
            try:
                response = client.put_file(
                    f.path,
                    data,
                    message or f"Update {f.path} via mooring",
                    target,
                    base_sha=base,
                )
            except RemoteConflict:
                result.blocked_conflicts.append(f.path)
                if base is None:
                    # We tried to *create* the file but it already exists on the
                    # target — our cached remote view is stale (manifest out of
                    # sync with cfg.branch). Force the next pull to refetch.
                    stale_remote = True
                    reason = "already on the remote — pull first"
                elif in_review:
                    reason = "review branch changed — refresh and retry"
                else:
                    reason = "remote changed — pull first"
                result.lines.append(f"conflict {f.path} ({reason})")
                continue
            if in_review:
                mft.review_files[f.path] = response["content"]["sha"]
            else:
                mft.files[f.path] = response["content"]["sha"]
                mft.review_files.pop(f.path, None)
            result.lines.append(f"pushed   {f.path}{dest}")
        if in_review:
            touched_review = True
        else:  # only cfg.branch writes advance the sync base
            commit = (response or {}).get("commit", {}).get("sha", "")
            if commit:
                last_commit = commit
        result.pushed += 1

    if not mft.review_files:
        mft.review_branch = ""
    if touched_review and mft.review_branch:
        result.review_branch = mft.review_branch
        result.compare_url = compare_url(
            cfg.owner, cfg.repo, cfg.branch, mft.review_branch, host=cfg.host
        )
    if last_commit:
        mft.head_commit = last_commit
    if stale_remote:
        # Drop the head-commit short-circuit in _remote_entries so the next pull
        # refetches the live tree and rebuilds a consistent manifest.
        mft.head_commit = ""
    mft.branch = cfg.branch
    manifest_mod.save(workspace, mft)
    return result


def propose(
    client: GitHubClient,
    cfg: Config,
    paths: list[str] | None = None,
    message: str | None = None,
    throttle: float = 0.8,
    sleep=time.sleep,
    now=time.localtime,
) -> SyncResult:
    """Upload push candidates to an auto-created review branch instead of
    cfg.branch, so the user can open a pull request on GitHub. The sync base
    (manifest files/head_commit/branch) stays pointed at cfg.branch."""
    prep = _prepare(client, cfg)
    workspace, mft, report = prep.workspace, prep.mft, prep.report
    result = SyncResult()

    candidates = _gather_candidates(report, paths, result, in_review_note="(already proposed)")

    branch_name = mft.review_branch
    if branch_name:
        review_tree = _review_tree(client, cfg, branch_name)
    else:
        # A fresh branch forks from head, so its tree is exactly `remote`.
        review_tree = dict(prep.remote)

    def ensure_branch() -> str:
        # Created lazily so an all-refused propose leaves no empty branch.
        nonlocal branch_name
        if branch_name:
            return branch_name
        login = client.get_user()["login"]
        name = f"mooring/{login}/{time.strftime('%Y%m%d-%H%M', now())}"
        for suffix in ("", "-2", "-3", "-4"):
            try:
                client.create_ref(name + suffix, prep.head)
                branch_name = name + suffix
                return branch_name
            except RefAlreadyExists:
                continue
        raise RefAlreadyExists(f"No free review branch name near {name}.")

    for index, f in enumerate(candidates):
        if index > 0 and throttle:
            sleep(throttle)  # contents-API writes trip secondary rate limits if rapid
        base = review_tree.get(f.path)
        if f.state is FileState.DELETED_LOCAL:
            if base:
                client.delete_file(
                    f.path,
                    message or f"Propose deleting {f.path} via mooring",
                    ensure_branch(),
                    base,
                )
                result.lines.append(f"proposed {f.path} (delete)")
            else:
                result.lines.append(f"proposed {f.path} (already absent on review branch)")
            mft.review_files[f.path] = None
        else:
            data = _read_checked(workspace, f, cfg, result)
            if data is None:
                continue
            try:
                response = client.put_file(
                    f.path,
                    data,
                    message or f"Propose {f.path} via mooring",
                    ensure_branch(),
                    base_sha=base,
                )
            except RemoteConflict:
                result.blocked_conflicts.append(f.path)
                if base is None:
                    # Creating a file that already exists on cfg.branch (and thus
                    # on the freshly-forked review branch): our cached remote view
                    # is stale. Invalidate it so the next pull refetches and heals.
                    mft.head_commit = ""
                    result.lines.append(
                        f"conflict {f.path} (already on the remote — pull first)"
                    )
                else:
                    result.lines.append(
                        f"conflict {f.path} (review branch changed — refresh and retry)"
                    )
                continue
            mft.review_files[f.path] = response["content"]["sha"]
            result.lines.append(f"proposed {f.path}")
        result.proposed += 1

    if branch_name:
        mft.review_branch = branch_name
        result.review_branch = branch_name
        result.compare_url = compare_url(
            cfg.owner, cfg.repo, cfg.branch, branch_name, host=cfg.host
        )
        result.lines.append(f"open a pull request: {result.compare_url}")
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

    if _apply_remote_or_keep_both(client, workspace, mft, rel_path, remote_sha, strategy, result):
        pass  # THEIRS or KEEP_BOTH-with-remote-present handled by the shared helper
    elif strategy is ConflictStrategy.KEEP_BOTH:  # helper declined: the remote was deleted
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
