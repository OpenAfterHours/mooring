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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from mooring import gitsha, manifest as manifest_mod, trash
from mooring.config import Config
from mooring.github import (
    GitHubClientProtocol,
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


def within_folders(path: str, folders: tuple[str, ...]) -> bool:
    """Whether a workspace-relative POSIX ``path`` (a file or a directory) falls under
    one of the synced ``folders``. True when ``path`` equals a folder or is nested
    inside it — i.e. exactly the paths :func:`synced_paths` would walk via ``rglob``.
    Used to decide whether a notebook's target folder is already covered by the sync
    scope (so a new sub-folder only needs registering when it is not)."""
    p = path.replace("\\", "/").strip("/")
    for folder in folders:
        f = folder.replace("\\", "/").strip("/")
        if f and (p == f or p.startswith(f + "/")):
            return True
    return False


@dataclass
class FolderCandidate:
    """A top-level repo folder that holds syncable files but is OUTSIDE the current
    sync scope — what :func:`adopt`-style flows register so it rides sync. ``files`` is
    every syncable file under it; ``py_files`` the Python ones (the notebook signal,
    counted cheaply from the tree without fetching blob contents)."""

    folder: str
    files: int
    py_files: int


def _candidate_folder(rel: str, folders: tuple[str, ...]) -> str:
    """The folder to offer for adoption for an out-of-scope file ``rel``: its top-level
    segment, descended just deep enough that no already-synced folder is nested inside
    it. So with ``notebooks/team-a`` synced and ``notebooks/team-b/x.py`` out of scope,
    the candidate is ``notebooks/team-b`` (adopting bare ``notebooks`` would overlap the
    synced ``team-a`` and the file/py counts would undercount what adoption registers).
    ``rel`` is a file path with at least one ``/`` (discovery skips loose root files)."""
    parts = rel.split("/")[:-1]  # the parent directories
    for depth in range(1, len(parts) + 1):
        cand = "/".join(parts[:depth])
        if any(f != cand and f.startswith(cand + "/") for f in folders):
            continue  # a synced folder nests inside cand → descend toward the file
        return cand
    return "/".join(parts)


def discover_unsynced_folders(
    client: GitHubClientProtocol, cfg: Config, head: str | None = None
) -> list[FolderCandidate]:
    """Top-level folders on ``cfg.branch`` that contain syncable files yet fall outside
    the current scope (``cfg.folders``) — the candidates a user can adopt so notebooks
    (and their helper modules) authored in a differently-organised repo finally sync.

    Reads the FULL remote tree once (``get_full_tree`` — the same tree ``get_tree``
    already fetches, so no extra round-trip beyond this one request) and groups blobs by
    their first path segment, applying the SAME :func:`is_synced_path` / exclude filter
    both sync sides use. That keeps the load-bearing local/remote symmetry: a folder
    surfaced here syncs identically on both sides once registered. Paths already
    :func:`within_folders` the scope are skipped (they already sync); loose root-level
    files (no ``/``) are not folders and are skipped too. ``head`` lets a caller that
    already knows ``cfg.branch``'s head (e.g. status) avoid the extra ref lookup.
    """
    if head is None:
        head = client.get_branch_head(cfg.branch)
    counts: dict[str, list[int]] = {}
    for entry in client.get_full_tree(head):
        rel = entry.path
        if "/" not in rel:
            continue  # a loose root-level file is not a folder candidate
        if not is_synced_path(rel, cfg.exclude):
            continue  # dotfiles / machine dirs / [sync] exclude — invisible to sync
        if within_folders(rel, cfg.folders):
            continue  # already in scope; it already syncs
        top = _candidate_folder(rel, cfg.folders)
        tally = counts.setdefault(top, [0, 0])
        tally[0] += 1
        if rel.endswith(".py"):
            tally[1] += 1
    return [
        FolderCandidate(folder=top, files=tally[0], py_files=tally[1])
        for top, tally in sorted(counts.items())
    ]


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
    # The hub's local (no-repo) mode: a file present on disk that isn't tracked
    # against any remote. `classify` never returns it and it is in no PUSH/PULL
    # set, so it is inert in the three-way sync machinery — only `local_report`
    # emits it, for display when there's no repo to diff against.
    LOCAL = "local"


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
        text = f"{synced} in sync, {to_push} to push, {to_pull} to pull, {conflicts} conflicted"
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
    reverted: int = 0
    review_branch: str = ""
    compare_url: str = ""
    skipped_conflicts: list[str] = field(default_factory=list)
    blocked_conflicts: list[str] = field(default_factory=list)
    # (rel_path, trash token) for every local pre-image this operation banked
    # before overwriting/removing the file — the adapters' Undo affordance.
    trashed: list[tuple[str, str]] = field(default_factory=list)
    # (rel_path, value-free description) for every candidate the push guard
    # withheld (see mooring.pushguard) — never pushed silently, never blocked
    # silently. The adapters turn these into the warn-and-confirm flow.
    withheld: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.pulled:
            parts.append(f"pulled {self.pulled} file(s)")
        if self.pushed:
            parts.append(f"pushed {self.pushed} file(s)")
        if self.proposed:
            parts.append(f"proposed {self.proposed} file(s) for review")
        if self.reverted:
            parts.append(f"reverted {self.reverted} file(s)")
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


def synced_paths(
    workspace: Path, folders: tuple[str, ...], exclude: Iterable[str] = ()
) -> Iterator[str]:
    """Yield the workspace-relative POSIX path of every file that participates in
    sync: the files under ``folders`` plus the synced root files (``SYNCED_ROOT_FILES``),
    filtered by :func:`is_synced_path`. The shared enumeration behind
    :func:`scan_local` (which hashes each) and :func:`local_report` (which doesn't)."""
    for folder in folders:
        root = workspace / folder
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(workspace).as_posix()
            if is_synced_path(rel, exclude):
                yield rel
    for name in SYNCED_ROOT_FILES:
        path = workspace / name
        if path.is_file() and is_synced_path(name, exclude):
            yield name


def scan_local(
    workspace: Path, folders: tuple[str, ...], exclude: Iterable[str] = ()
) -> dict[str, str]:
    return {
        rel: gitsha.local_blob_sha(workspace / rel, rel)
        for rel in synced_paths(workspace, folders, exclude)
    }


def local_report(
    workspace: Path, folders: tuple[str, ...], exclude: Iterable[str] = ()
) -> StatusReport:
    """List the workspace's files for the hub's LOCAL mode (no configured repo, no
    login). Every on-disk file under the synced folders/root is reported with state
    ``LOCAL`` — present locally, tracked against no remote. There is no manifest, no
    network call, and nothing to diff: sync (pull/push/propose) stays unavailable
    until a repo is connected. Visibility mirrors :func:`scan_local`, so a workspace
    shows the same files here as it would once a repo is attached.

    The blob sha is deliberately NOT computed (``local_sha`` stays ``None``): a LOCAL
    row is never diffed against a remote, so its presence is carried by the ``LOCAL``
    state itself. This keeps the listing cheap even for a workspace with large data
    files, which the hub re-lists on every refresh (each New/Open).
    """
    report = StatusReport(head_commit="")
    for rel in sorted(synced_paths(workspace, folders, exclude)):
        report.files.append(FileStatus(path=rel, state=FileState.LOCAL))
    return report


def _scope_matches(cfg: Config, mft: manifest_mod.Manifest) -> bool:
    """Whether the manifest's recorded sync scope equals the current one.

    The head-unchanged fast path below trusts ``mft.files`` as the remote tree,
    but ``files`` only covers the folders/exclude in force when it was written.
    A pre-scope manifest (``scope_folders is None``) is treated as a mismatch, so
    widening ``[sync] folders`` (e.g. adding the context folder) forces a real
    tree fetch instead of silently reusing a narrower snapshot — which is what made
    an already-pushed folder un-pullable once the head had caught up.
    """
    if mft.scope_folders is None and mft.scope_exclude is None:
        return False
    return tuple(mft.scope_folders or ()) == tuple(cfg.folders) and tuple(
        mft.scope_exclude or ()
    ) == tuple(cfg.exclude)


def _remote_entries(
    client: GitHubClientProtocol, cfg: Config, head: str, mft: manifest_mod.Manifest
) -> dict[str, str]:
    # If the branch head hasn't moved since our last sync AND the sync scope is
    # unchanged, the remote tree is exactly what the manifest recorded — no tree
    # fetch needed. A changed scope falls through so newly-tracked folders are seen.
    if head and head == mft.head_commit and _scope_matches(cfg, mft):
        return {p: s for p, s in mft.files.items() if is_synced_path(p, cfg.exclude)}
    return {
        e.path: e.sha
        for e in client.get_tree(head, cfg.folders, SYNCED_ROOT_FILES)
        if is_synced_path(e.path, cfg.exclude)
    }


def _review_tree(client: GitHubClientProtocol, cfg: Config, branch: str) -> dict[str, str]:
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
    client: GitHubClientProtocol,
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


def status(client: GitHubClientProtocol, cfg: Config) -> StatusReport:
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


def _prepare(client: GitHubClientProtocol, cfg: Config, *, make_workspace: bool = False) -> _Prepared:
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
            result.lines.append(f"conflict {f.path} (blocked — pull first, or resolve in the hub)")
    if wanted:
        for f in report.by_state(FileState.IN_REVIEW):
            if f.path in wanted:
                result.lines.append(f"in review {f.path} {in_review_note}")
    return candidates


def _read_checked(workspace: Path, f: FileStatus, cfg: Config, result: SyncResult) -> bytes | None:
    """Read a candidate's bytes for upload, enforcing the size limits shared by push
    and propose. Returns the bytes, or None when the file exceeds cfg.max_file_mb (a
    'refused' line is recorded and the caller skips it); a file over cfg.warn_file_mb
    is read but flagged."""
    data = gitsha.read_for_push(workspace / f.path, f.path)
    size_mb = len(data) / (1024 * 1024)
    if size_mb > cfg.max_file_mb:
        result.lines.append(f"refused  {f.path} ({size_mb:.0f} MB > {cfg.max_file_mb} MB limit)")
        return None
    if size_mb > cfg.warn_file_mb:
        result.lines.append(f"warning  {f.path} is {size_mb:.0f} MB")
    return data


def _bank_pre_image(
    workspace: Path,
    rel_path: str,
    action: str,
    after_sha: str | None,
    cap_mb: int,
    result: SyncResult,
    replacement: bytes | None = None,
) -> None:
    """Deposit the file's current bytes into the local trash before a destructive
    write, recording ``(rel_path, token)`` on the result so the adapters can offer
    Undo. Best-effort by design: a trash failure must never block the sync op, and
    a byte-identical ``replacement`` (nothing is being lost) skips the deposit."""
    target = workspace / rel_path
    try:
        if not target.is_file():
            return
        data = target.read_bytes()
        if replacement is not None and data == replacement:
            return
        token = trash.deposit(
            workspace, rel_path, data, action, after_sha=after_sha, max_file_mb=cap_mb
        )
    except OSError:
        return
    if token:
        result.trashed.append((rel_path, token))


def _apply_remote_or_keep_both(
    client: GitHubClientProtocol,
    workspace: Path,
    mft: manifest_mod.Manifest,
    rel_path: str,
    remote_sha: str | None,
    strategy: ConflictStrategy,
    result: SyncResult,
    *,
    origin: str = "resolve",
    trash_cap_mb: int = trash.DEFAULT_MAX_FILE_MB,
) -> bool:
    """Apply the two conflict strategies pull and resolve share: THEIRS (take the
    remote, or delete locally when the remote is gone) and KEEP_BOTH while the
    remote still exists (save it as a .remote-<sha> copy, keep local pushable).

    THEIRS destroys the user's local edits, so their pre-image is banked in the
    local trash first (``origin`` labels the entry pull- vs resolve-initiated).

    Returns True when it handled the strategy; False leaves the caller to handle the
    cases that legitimately differ between pull and resolve — SKIP, KEEP_BOTH with
    the remote already deleted, and resolve's PUSH_COPY."""
    if strategy is ConflictStrategy.THEIRS:
        if remote_sha is None:
            _bank_pre_image(workspace, rel_path, f"{origin}-theirs", None, trash_cap_mb, result)
            (workspace / rel_path).unlink(missing_ok=True)
            mft.files.pop(rel_path, None)
        else:
            data = client.get_blob(remote_sha)
            _bank_pre_image(
                workspace, rel_path, f"{origin}-theirs", remote_sha, trash_cap_mb, result,
                replacement=data,
            )
            _write_blob(workspace, rel_path, data)
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
    client: GitHubClientProtocol,
    cfg: Config,
    strategy: ConflictStrategy = ConflictStrategy.SKIP,
) -> SyncResult:
    prep = _prepare(client, cfg, make_workspace=True)
    workspace, mft, report = prep.workspace, prep.mft, prep.report
    result = SyncResult()

    for f in report.files:
        if f.state in (FileState.NEW_REMOTE, FileState.REMOTE_CHANGED):
            assert f.remote_sha is not None  # these states always carry a remote sha
            data = client.get_blob(f.remote_sha)
            # A REMOTE_CHANGED overwrite destroys only manifest-base-equal bytes
            # (recoverable from GitHub via get_blob), but banking them makes the
            # recovery one click — and it works offline. NEW_REMOTE has no local
            # file, so _bank_pre_image no-ops for it.
            _bank_pre_image(
                workspace, f.path, "pull-overwrite", f.remote_sha,
                cfg.trash_max_file_mb, result, replacement=data,
            )
            _write_blob(workspace, f.path, data)
            mft.files[f.path] = f.remote_sha
            result.pulled += 1
            result.lines.append(f"pulled   {f.path}")
        elif f.state is FileState.DELETED_REMOTE:
            _bank_pre_image(
                workspace, f.path, "pull-remove", None, cfg.trash_max_file_mb, result
            )
            (workspace / f.path).unlink(missing_ok=True)
            mft.files.pop(f.path, None)
            result.pulled += 1
            result.lines.append(f"removed  {f.path} (deleted remotely)")
        elif f.state is FileState.SYNCED:
            if f.local_sha and f.base_sha != f.local_sha:
                mft.files[f.path] = f.local_sha  # same change on both sides
        elif f.state is FileState.CONFLICT:
            if not _apply_remote_or_keep_both(
                client, workspace, mft, f.path, f.remote_sha, strategy, result,
                origin="pull", trash_cap_mb=cfg.trash_max_file_mb,
            ):
                result.skipped_conflicts.append(f.path)
                result.lines.append(f"conflict {f.path} (skipped — resolve in the hub)")

    # Drop manifest entries for files deleted on both sides.
    for path in list(mft.files):
        if path not in prep.local and path not in prep.remote:
            del mft.files[path]

    mft.branch = cfg.branch
    if result.skipped_conflicts:
        # A skipped conflict keeps its OLD base sha in mft.files while the remote has
        # moved on, so the manifest is NOT a faithful snapshot of the remote tree.
        # Recording the new head here would let the _remote_entries fast path serve
        # that stale sha as the remote view on the next cycle, masking the still-
        # unresolved CONFLICT as a plain MODIFIED — which wedges pull (a no-op on
        # MODIFIED) and push (a 409 "remote changed") with no way out, and hides the
        # per-file resolution UI (gated on FileState.CONFLICT). Leave head_commit
        # empty so the next pull/status/push refetches the live tree and re-detects
        # the conflict, keeping it resolvable.
        mft.head_commit = ""
    else:
        mft.head_commit = prep.head
        # A completed pull has reconciled the manifest with the full remote tree under
        # the current scope, so record that scope: the next same-head pull/status can
        # trust the fast path again, and a later scope widening will be detected.
        mft.scope_folders = tuple(cfg.folders)
        mft.scope_exclude = tuple(cfg.exclude)
    manifest_mod.save(workspace, mft)
    return result


# States a single-file revert can act on: the file changed locally since the last
# sync. NEW_LOCAL has no checkpoint to return to (delete it instead); IN_REVIEW and
# the remote-only states are deliberately left for the proposal flow and pull.
_REVERTABLE = {FileState.MODIFIED, FileState.DELETED_LOCAL}

_REVERT_NOTES = {
    FileState.SYNCED: "already at the last synced version",
    FileState.NEW_LOCAL: "never synced — use delete to discard it",
    FileState.CONFLICT: "in conflict — pull first, or revert with --conflicts to discard your edit",
    FileState.IN_REVIEW: "in an open proposal — left as is",
    FileState.NEW_REMOTE: "nothing of yours to revert (a teammate added it — pull)",
    FileState.REMOTE_CHANGED: "nothing of yours to revert (a teammate changed it — pull)",
    FileState.DELETED_REMOTE: "nothing of yours to revert (a teammate deleted it — pull)",
}


def revert(
    client: GitHubClientProtocol,
    cfg: Config,
    rel_path: str,
    *,
    include_conflict: bool = False,
    snapshot_fn=None,
) -> SyncResult:
    """Restore one tracked file to its last-synced checkpoint (the manifest base).

    The inverse of :func:`pull` for a single path: where pull reconciles the working
    file toward the REMOTE blob, revert reconciles it toward the BASE blob recorded in
    the manifest — i.e. it discards local edits and goes back to the last pull/push.
    The bytes are recovered git-free via ``client.get_blob(base_sha)``; a restored file
    re-hashes to its base sha (gitsha LF-normalizes ``.py``; get_blob returns the bytes
    that were pushed), so it classifies SYNCED with no manifest rewrite.

    Acts only on MODIFIED (overwrite local with base) and DELETED_LOCAL (recreate from
    base). With ``include_conflict`` a CONFLICT file is reset to base too, which drops
    only the user's side and turns it into a clean pull. Every other state has nothing
    of the user's to revert and is reported, not touched.

    ``snapshot_fn(rel_path, current_bytes)`` (optional) is called with the file's
    current bytes BEFORE it is overwritten so the revert is itself undoable; it is
    passed in (rather than imported) to keep this module free of a notebook_undo
    dependency, mirroring how the rest of sync.py stays at the GitHub/manifest layer.
    """
    rel_path = rel_path.replace("\\", "/")
    prep = _prepare(client, cfg)
    workspace = prep.workspace
    result = SyncResult()

    match = next((f for f in prep.report.files if f.path == rel_path), None)
    if match is None:
        result.lines.append(f"{rel_path}: not a tracked file")
        return result

    is_conflict = match.state is FileState.CONFLICT
    if match.state not in _REVERTABLE and not (is_conflict and include_conflict):
        result.lines.append(f"{rel_path}: {_REVERT_NOTES.get(match.state, 'nothing to revert')}")
        return result

    # base_sha == the last-synced blob. For MODIFIED/DELETED_LOCAL it equals the
    # current remote sha (reachable from HEAD); for a conflict it is historical and
    # could in rare cases be GC'd, so tolerate a missing blob per file.
    if match.base_sha is None:
        result.lines.append(f"{rel_path}: no checkpoint to restore")
        return result
    try:
        data = client.get_blob(match.base_sha)
    except NotFound:
        result.lines.append(f"could not revert {rel_path} (checkpoint version unavailable)")
        return result

    target = workspace / rel_path
    if snapshot_fn is not None and target.is_file():
        snapshot_fn(rel_path, target.read_bytes())
    # Non-.py files have no notebook-undo stack (snapshot_fn only banks .py), so
    # their pre-image goes to the local trash instead — the two stores stay
    # separate: a .py Revert keeps its existing snapshot + /api/undo path.
    if not rel_path.endswith(".py"):
        _bank_pre_image(
            workspace, rel_path, "revert", match.base_sha,
            cfg.trash_max_file_mb, result, replacement=data,
        )
    _write_blob(workspace, rel_path, data)
    result.reverted += 1
    if match.state is FileState.DELETED_LOCAL:
        result.lines.append(f"restored {rel_path} (was deleted locally)")
    elif is_conflict:
        result.lines.append(f"reverted {rel_path} (discarded your edit; pull to take the remote)")
    else:
        result.lines.append(f"reverted {rel_path}")
    return result


def history(client: GitHubClientProtocol, cfg: Config, rel_path: str, page: int = 1) -> list[dict]:
    """One page of ``rel_path``'s version history on cfg.branch, newest first,
    shaped for humans: sha/short/message (first line)/author/date. The commits
    API paginates and does not follow renames — history starts at a rename."""
    rel_path = rel_path.replace("\\", "/")
    out = []
    for c in client.list_commits_for_path(rel_path, cfg.branch, page=page):
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        message = str(commit.get("message") or "")
        out.append(
            {
                "sha": c.get("sha", ""),
                "short": str(c.get("sha", ""))[:7],
                "message": message.splitlines()[0] if message else "",
                "author": (c.get("author") or {}).get("login") or author.get("name") or "",
                "date": author.get("date", ""),
            }
        )
    return out


def restore_version(
    client: GitHubClientProtocol,
    cfg: Config,
    rel_path: str,
    at: str,
    *,
    as_copy: bool = False,
    snapshot_fn=None,
) -> SyncResult:
    """Bring back ``rel_path`` as it existed at commit ``at`` — the git-free
    time machine (revert reaches only the last-synced checkpoint; this reaches
    anything ever pushed).

    A pure LOCAL write: no ``put_file``, no manifest mutation — the restored
    file simply reclassifies on the next status (``new local`` for a copy,
    ``modified``/``conflict`` for an overwrite) and rides standard sync, so a
    restore can never silently overwrite the remote. ``as_copy`` writes
    ``{stem}.restored-{sha7}{suffix}`` beside the file (always safe); an
    overwrite banks the current bytes first — ``snapshot_fn`` for the ``.py``
    undo stack (the revert idiom), the local trash for anything else.
    """
    rel_path = rel_path.replace("\\", "/")
    workspace = cfg.workspace()
    result = SyncResult()
    try:
        _, data = client.get_file_at(rel_path, at)
    except NotFound:
        result.lines.append(
            f"{rel_path}: no version at {at[:7]} (the file may not have existed there)"
        )
        return result
    short = at[:7]
    if as_copy:
        p = Path(rel_path)
        copy_path = str(p.with_name(f"{p.stem}.restored-{short}{p.suffix}")).replace("\\", "/")
        _write_blob(workspace, copy_path, data)
        result.reverted += 1
        result.lines.append(f"restored {rel_path} @ {short} as {copy_path} (a new local file)")
        return result
    target = workspace / rel_path
    if snapshot_fn is not None and target.is_file():
        snapshot_fn(rel_path, target.read_bytes())
    if not rel_path.endswith(".py"):
        _bank_pre_image(
            workspace, rel_path, "restore-version", gitsha.blob_sha(data),
            cfg.trash_max_file_mb, result, replacement=data,
        )
    _write_blob(workspace, rel_path, data)
    result.reverted += 1
    result.lines.append(
        f"restored {rel_path} to version {short} (local only — push to share it)"
    )
    return result


def push(
    client: GitHubClientProtocol,
    cfg: Config,
    paths: list[str] | None = None,
    message: str | None = None,
    throttle: float = 0.8,
    sleep=time.sleep,
    guard_fn=None,
) -> SyncResult:
    """``guard_fn(rel_path, data) -> list[str]`` (optional) sees the exact bytes
    about to upload; a non-empty return (value-free finding descriptions)
    WITHHOLDS the file with a result line — never silently. Passed in rather
    than imported (the ``snapshot_fn`` idiom), so the sync core stays free of
    the scanners; see :mod:`mooring.pushguard`."""
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
    # What this push wrote to cfg.branch — path -> {prev, new} blob shas — so
    # "recall last push" can write the prior state back (see recall()).
    recall_log: dict[str, dict] = {}
    for index, f in enumerate(candidates):
        if index > 0 and throttle:
            sleep(throttle)  # contents-API writes trip secondary rate limits if rapid
        outcome = _push_candidate(
            client, cfg, workspace, mft, review_tree, f, message, result,
            guard_fn=guard_fn, recall_log=recall_log,
        )
        stale_remote = stale_remote or outcome.stale_remote
        if not outcome.counted:
            continue
        if outcome.touched_review:
            touched_review = True
        elif outcome.commit:  # only cfg.branch writes advance the sync base
            last_commit = outcome.commit
        result.pushed += 1

    _finalize_push(
        workspace, mft, cfg, result, last_commit, touched_review, stale_remote,
        recall_log=recall_log,
    )
    return result


@dataclass
class _PushOutcome:
    """Per-file effects of pushing one candidate, folded into push()'s accumulators."""

    counted: bool = False  # False == the original `continue` (skipped / conflicted)
    commit: str = ""  # cfg.branch commit sha (blank for review-branch writes)
    touched_review: bool = False
    stale_remote: bool = False


def _withhold(f, findings: list[str], result: SyncResult) -> _PushOutcome:
    """Record a guard-withheld candidate — visible, never silent (the adapters
    turn result.withheld into the warn-and-confirm flow)."""
    result.withheld.extend((f.path, desc) for desc in findings)
    noun = "finding" if len(findings) == 1 else "findings"
    result.lines.append(f"withheld {f.path} ({len(findings)} {noun} — fix or acknowledge)")
    return _PushOutcome()


def _push_candidate(
    client, cfg, workspace, mft, review_tree, f, message, result,
    *, guard_fn=None, recall_log=None,
) -> _PushOutcome:
    """Push or delete ONE candidate to its target branch (cfg.branch, or the open
    proposal's review branch). Mutates ``mft`` + ``result``; returns the per-file effects
    push() folds into its accumulators."""
    in_review = bool(mft.review_branch) and f.path in mft.review_files
    target = mft.review_branch if in_review else cfg.branch
    base = review_tree.get(f.path) if in_review else f.base_sha
    dest = " → review branch (PR)" if in_review else ""
    if f.state is FileState.DELETED_LOCAL:
        response = _push_delete(client, mft, f, target, base, in_review, dest, message, result)
        if response is not None and not in_review and recall_log is not None:
            recall_log[f.path] = {"prev": f.base_sha, "new": None}
    else:
        data = _read_checked(workspace, f, cfg, result)
        if data is None:
            return _PushOutcome()
        if guard_fn is not None:
            findings = guard_fn(f.path, data)
            if findings:
                return _withhold(f, findings, result)
        try:
            response = client.put_file(
                f.path, data, message or f"Update {f.path} via mooring", target, base_sha=base
            )
        except RemoteConflict:
            return _push_conflict(f, base, in_review, result)
        if in_review:
            mft.review_files[f.path] = response["content"]["sha"]
        else:
            mft.files[f.path] = response["content"]["sha"]
            mft.review_files.pop(f.path, None)
            if recall_log is not None:
                recall_log[f.path] = {"prev": f.base_sha, "new": response["content"]["sha"]}
        result.lines.append(f"pushed   {f.path}{dest}")
    commit = "" if in_review else (response or {}).get("commit", {}).get("sha", "")
    return _PushOutcome(counted=True, commit=commit, touched_review=in_review)


def _push_delete(client, mft, f, target, base, in_review, dest, message, result) -> dict | None:
    """Delete one candidate on its target branch, mirroring the deletion into the
    manifest (or the review-file map for a proposal). Returns the API response, if any."""
    if not in_review:
        response = client.delete_file(
            f.path, message or f"Delete {f.path} via mooring", target, base
        )
        mft.files.pop(f.path, None)
        result.lines.append(f"deleted  {f.path}")
        return response
    if base is not None:
        response = client.delete_file(
            f.path, message or f"Propose deleting {f.path} via mooring", target, base
        )
        mft.review_files[f.path] = None
        result.lines.append(f"deleted  {f.path}{dest}")
        return response
    mft.review_files[f.path] = None
    result.lines.append(f"deleted  {f.path} (already absent on review branch)")
    return None


def _push_conflict(f, base, in_review, result) -> _PushOutcome:
    """Record a per-file optimistic-concurrency rejection; never silent. A rejection
    from cfg.branch — a ``base is None`` create-collision, or any base-mismatch that is
    not a review-branch write — proves our cached remote view of cfg.branch is stale, so
    flag a forced refetch on the next pull. Only a review-branch base-mismatch is left
    alone ("refresh and retry"): it reflects the review branch moving, not cfg.branch,
    whose head cache is still valid."""
    result.blocked_conflicts.append(f.path)
    stale_remote = base is None or not in_review
    if base is None:
        reason = "already on the remote — pull first"
    elif in_review:
        reason = "review branch changed — refresh and retry"
    else:
        reason = "remote changed — pull first"
    result.lines.append(f"conflict {f.path} ({reason})")
    return _PushOutcome(stale_remote=stale_remote)


def _finalize_push(
    workspace, mft, cfg, result, last_commit, touched_review, stale_remote,
    *, recall_log=None,
) -> None:
    """Persist the manifest after a push: clear an emptied review branch, surface the PR
    compare URL, advance the sync base, and force a refetch when the remote went stale.
    A push that wrote to cfg.branch also replaces the recallable ``last_push`` record
    wholesale — only the LAST push is recallable; that is the promise in the name."""
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
    if recall_log:
        mft.last_push = dict(recall_log)
        mft.last_push_branch = cfg.branch
    mft.branch = cfg.branch
    manifest_mod.save(workspace, mft)


def recall(client: GitHubClientProtocol, cfg: Config) -> SyncResult:
    """Write the pre-push state of the LAST push back to cfg.branch — "get it off
    the branch, now" after a bad push.

    Per file: a changed file gets its previous blob re-pushed with the pushed
    sha as the optimistic base (``put_file(..., base_sha=<new>)``), so if a
    teammate pushed on top since, GitHub rejects it and the conflict is loud —
    exactly like any stale write. A file the push CREATED is deleted; a file
    the push DELETED is re-created. Local files are untouched (they simply
    reclassify on the next status), and the record is consumed — only the last
    push is recallable, once.

    Honest by design: **git history still retains the recalled commit** — a
    leaked secret must still be rotated; recall only stops the bleeding on the
    branch head.
    """
    workspace = cfg.workspace()
    mft = manifest_mod.load(workspace)
    result = SyncResult()
    if not mft.last_push:
        result.lines.append("nothing to recall (no recorded push)")
        return result
    if mft.last_push_branch and mft.last_push_branch != cfg.branch:
        result.lines.append(
            f"nothing to recall on {cfg.branch} (the last push went to "
            f"{mft.last_push_branch})"
        )
        return result

    for path, rec in sorted(mft.last_push.items()):
        prev, new = rec.get("prev"), rec.get("new")
        try:
            if prev is None:
                # The push created it — recall removes it from the branch head.
                client.delete_file(path, f"Recall {path} via mooring", cfg.branch, new)
                mft.files.pop(path, None)
                result.lines.append(f"recalled {path} (removed from {cfg.branch})")
            else:
                data = client.get_blob(prev)
                response = client.put_file(
                    path, data, f"Recall {path} via mooring", cfg.branch, base_sha=new
                )
                mft.files[path] = response["content"]["sha"]
                result.lines.append(f"recalled {path} (previous version restored)")
        except RemoteConflict:
            result.blocked_conflicts.append(path)
            result.lines.append(
                f"conflict {path} (cannot recall — a teammate pushed on top; pull first)"
            )
            continue
        except NotFound:
            result.lines.append(f"could not recall {path} (previous version unavailable)")
            continue
        result.pushed += 1

    if result.pushed:
        result.lines.append(
            "note: the recalled commit remains in the repo's history — if a secret "
            "leaked, rotate it."
        )
    # Consumed either way: a recall is a one-shot on the recorded push. Force the
    # next status/pull to refetch the live tree rather than trust the cache.
    mft.last_push = {}
    mft.last_push_branch = ""
    mft.head_commit = ""
    manifest_mod.save(workspace, mft)
    return result


def propose(
    client: GitHubClientProtocol,
    cfg: Config,
    paths: list[str] | None = None,
    message: str | None = None,
    throttle: float = 0.8,
    sleep=time.sleep,
    now=time.localtime,
    guard_fn=None,
) -> SyncResult:
    """Upload push candidates to an auto-created review branch instead of
    cfg.branch, so the user can open a pull request on GitHub. The sync base
    (manifest files/head_commit/branch) stays pointed at cfg.branch.
    ``guard_fn`` withholds flagged candidates exactly as in :func:`push`."""
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
            if guard_fn is not None:
                findings = guard_fn(f.path, data)
                if findings:
                    _withhold(f, findings, result)
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
                    result.lines.append(f"conflict {f.path} (already on the remote — pull first)")
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
    client: GitHubClientProtocol,
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

    if _apply_remote_or_keep_both(
        client, workspace, mft, rel_path, remote_sha, strategy, result,
        origin="resolve", trash_cap_mb=cfg.trash_max_file_mb,
    ):
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
        # The local bytes survive at copy_path (and were just pushed), so this
        # deposit is redundancy — but if the copy is later deleted, it is the
        # only pre-image left, and banking it costs one small blob.
        _bank_pre_image(
            workspace, rel_path, "resolve-push-copy", remote_sha,
            cfg.trash_max_file_mb, result,
        )
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
