"""The pull digest: what changed on the team branch while you were away — PURE READS.

Answers the question the pull log can't: *who* changed *what* since **your**
last sync, before (or right after) you pull it. The horizon is personal — the
branch head recorded in the manifest at the last clean sync
(``Manifest.head_commit``) — so the digest is "everything that changed since
you last looked", an answer no shared commit list can give.

Strictly read-only and deterministic. Attribution degrades in honest steps:
one ``compare(anchor, head)`` call (the whole window in one request) when the
anchor is valid; one ``list_commits_for_path`` per pending file (capped) when
the anchor is blank — blanked *by design* after a conflict-skipping pull and a
stale push — or lost (force-push/GC → ``NotFound``), or when the compare
window overflowed the API caps; and finally a bare state listing
(``attributed=False``) when even the fallback fails. ``pending_digest`` never
raises for an attribution failure and never writes the manifest or the
workspace. The module lives in the ``sync-domain-is-core`` and
``frozen-core-is-lean`` contracts (``.importlinter``), so it structurally
cannot import the AI, the editor (or ``celldiff``, whose ``marimo_rt`` import
would drag marimo onto the frozen core), or the adapters — "pull can never
block on AI" is a contract, not a habit.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from dataclasses import dataclass, field

from mooring import manifest as manifest_mod
from mooring import sync
from mooring.config import Config
from mooring.github import GitHubClientProtocol, GitHubError, NotFound

# The compare API caps its answer (~250 commits listed, 300 files); a window
# past the caps is truncated, and attributing from a confidently wrong subset
# would be worse than falling back to exact per-file lookups.
COMPARE_MAX_FILES = 300

# The fallback path costs one commits-list request per pending file; cap it so
# a huge backlog can't turn one digest into hundreds of round trips. Files
# beyond the cap still appear — as plain state entries without attribution.
FALLBACK_MAX_LOOKUPS = 20

# summarize_diff's difflib pass is quadratic-ish CPU work; above this cap it
# reports sizes only. Deliberately the same value as celldiff.MAX_TEXT_BYTES
# (which this module must NOT import — see the module docstring) so a >4 MB
# blob gets the same honest "too big" answer on every diff surface.
MAX_TEXT_BYTES = 4 * 1024 * 1024


@dataclass
class CommitGroup:
    """One human-shaped "push": consecutive window commits by the same author
    with the same message, collapsed. mooring's Contents-API writes are one
    commit per file, so a teammate's eight-file push arrives as eight commits
    with near-identical messages — the grouping is load-bearing, not cosmetic."""

    author: str
    message: str  # first line
    date: str  # ISO date of the newest commit in the group
    count: int = 1  # commits collapsed into this group


@dataclass
class DigestEntry:
    """One pending file: its current sync state plus best-effort attribution
    (who touched it in the window, when, and why). Empty attribution fields
    mean "unknown", never "nobody"."""

    path: str
    state: str  # FileState.value: remote changed / new remote / deleted remotely / conflict
    authors: list[str] = field(default_factory=list)  # newest first, deduped
    date: str = ""  # ISO date of the newest attributed commit
    messages: list[str] = field(default_factory=list)  # first lines, newest first, deduped
    commits: int = 0  # attributed commit count (0 = unknown)
    remote_sha: str | None = None  # current remote blob (None = deleted remotely)
    base_sha: str | None = None  # last-synced blob (None = new remote)


@dataclass
class Digest:
    entries: list[DigestEntry] = field(default_factory=list)
    groups: list[CommitGroup] = field(default_factory=list)  # newest first
    anchor: str = ""  # the manifest horizon ("" = none recorded)
    head: str = ""  # the branch head the digest was computed against
    source: str = "states"  # "compare" | "commits" | "states"
    attributed: bool = True  # False: attribution failed; entries carry states only
    truncated: bool = False  # the compare window overflowed the API caps


def pending_digest(
    client: GitHubClientProtocol, cfg: Config, report: sync.StatusReport
) -> Digest:
    """The digest of every synced file a pull would touch — ``PULL_STATES``
    plus conflicts, which appear marked but are never resolved here — with
    who/when/why attributed from the branch history between the manifest
    horizon and ``report.head_commit``. Read-only: writes nothing, and never
    raises past the digest boundary for an attribution failure (a caller that
    cannot even produce the ``report`` has no digest to build)."""
    candidates = {
        f.path: f for f in report.by_state(*sync.PULL_STATES, sync.FileState.CONFLICT)
    }
    anchor = manifest_mod.load(cfg.workspace()).head_commit
    digest = Digest(anchor=anchor, head=report.head_commit)
    if not candidates:
        return digest  # nothing pending (incl. anchor == head: no window at all)
    if anchor and anchor != report.head_commit:
        try:
            data = client.compare(anchor, report.head_commit)
        except NotFound:
            data = None  # force-pushed/GC'd anchor — the window is gone
        except (GitHubError, OSError):
            data = None  # transient read failure — try the per-file fallback
        if data is not None and _from_compare(digest, data, candidates, cfg):
            return digest
    return _from_commits(digest, client, cfg, candidates)


def _shape_commit(c: dict) -> dict:
    """One commits-API entry shaped for humans (the sync.history shaping):
    sha, first message line, author login-or-name, ISO date."""
    commit = c.get("commit") or {}
    author = commit.get("author") or {}
    message = str(commit.get("message") or "")
    return {
        "sha": c.get("sha", ""),
        "message": message.splitlines()[0] if message else "",
        "author": (c.get("author") or {}).get("login") or author.get("name") or "",
        "date": author.get("date", ""),
    }


def group_commits(commits: list[dict]) -> list[CommitGroup]:
    """Collapse CONSECUTIVE same-author, same-message commits into one group
    (oldest-first in — the compare API's order — newest-first out)."""
    groups: list[CommitGroup] = []
    for c in commits:
        if groups and groups[-1].author == c["author"] and groups[-1].message == c["message"]:
            groups[-1].count += 1
            groups[-1].date = c["date"] or groups[-1].date
        else:
            groups.append(CommitGroup(author=c["author"], message=c["message"], date=c["date"]))
    groups.reverse()
    return groups


def _entry(status: sync.FileStatus) -> DigestEntry:
    return DigestEntry(
        path=status.path,
        state=status.state.value,
        remote_sha=status.remote_sha,
        base_sha=status.base_sha,
    )


def _in_scope(path: str, cfg: Config) -> bool:
    """The same visibility both sync sides use (:func:`sync.in_sync_scope`): the
    exclude filter plus the folder scope, or a loose top-level file. A compare answer
    covers the whole repo; anything sync would not see must not shape the digest."""
    return sync.in_sync_scope(path, cfg.folders, cfg.exclude)


def _from_compare(digest: Digest, data: dict, candidates: dict, cfg: Config) -> bool:
    """Fill the digest from one ``compare(anchor, head)`` answer. Returns False
    when the window overflowed the API caps — the caller then falls back to
    exact per-file lookups (``truncated`` is kept on the digest) instead of
    attributing from a confidently wrong subset."""
    commits = [_shape_commit(c) for c in (data.get("commits") or [])]  # oldest first
    files = data.get("files") or []
    total = int(data.get("total_commits") or len(commits))
    if total > len(commits) or len(files) >= COMPARE_MAX_FILES:
        digest.truncated = True
        return False
    changed = {
        str(f.get("filename") or "")
        for f in files
        if _in_scope(str(f.get("filename") or ""), cfg)
    }
    for path, st in sorted(candidates.items()):
        entry = _entry(st)
        # A candidate outside the window (its remote change predates the
        # anchor — possible after a partial, conflict-skipping pull) is still
        # listed, but must not inherit commits that never touched it.
        if path in changed:
            _attribute(entry, commits)
        digest.entries.append(entry)
    digest.groups = group_commits(commits)
    digest.source = "compare"
    return True


def _mentions_path(message: str, path: str) -> bool:
    """Whether ``message`` names ``path`` as a WHOLE path token — a raw substring
    check would hand data/x.csv the commits of data/x.csv.bak ("Update
    data/x.csv.bak via mooring" contains "data/x.csv"). A mention only counts
    when the next character cannot extend the path (end of message, whitespace,
    or non-path punctuation)."""
    start = 0
    while (found := message.find(path, start)) != -1:
        end = found + len(path)
        if end == len(message) or not (message[end].isalnum() or message[end] in "._/-"):
            return True
        start = found + 1
    return False


def _attribute(entry: DigestEntry, commits: list[dict]) -> None:
    """Best-effort per-file attribution from the window's commits. The compare
    API does not say which commits touched which files, but mooring's machine
    messages embed the path ("Update {path} via mooring"), so message matching
    attributes those exactly; failing that, a single-author window still names
    the author (everything in it is theirs) without claiming which messages
    were about this file."""
    mine = [c for c in commits if _mentions_path(c["message"], entry.path)]
    if mine:
        mine.reverse()  # newest first
        entry.authors = _dedupe(c["author"] for c in mine if c["author"])
        entry.messages = _dedupe(c["message"] for c in mine if c["message"])
        entry.date = mine[0]["date"]
        entry.commits = len(mine)
        return
    authors = {c["author"] for c in commits if c["author"]}
    if len(authors) == 1:
        entry.authors = [next(iter(authors))]
        entry.date = commits[-1]["date"] if commits else ""


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _from_commits(
    digest: Digest, client: GitHubClientProtocol, cfg: Config, candidates: dict
) -> Digest:
    """The per-file fallback: one commits-list page per pending file (capped at
    FALLBACK_MAX_LOOKUPS), each file attributed from its own newest commit —
    exact where the compare window was unavailable or untrustworthy. When every
    lookup fails, the digest degrades to the bare state listing
    (``attributed=False``) rather than raising past the digest boundary."""
    successes = failures = 0
    for i, (path, st) in enumerate(sorted(candidates.items())):
        entry = _entry(st)
        digest.entries.append(entry)
        if i >= FALLBACK_MAX_LOOKUPS:
            continue  # visible but unattributed; the cap bounds the round trips
        try:
            commits = client.list_commits_for_path(path, cfg.branch, page=1, per_page=5)
        except (GitHubError, OSError):
            failures += 1
            continue
        successes += 1
        if commits:
            newest = _shape_commit(commits[0])
            # Only the newest commit is safely "since your sync" — without an
            # anchor, older page entries may predate the horizon.
            entry.authors = [newest["author"]] if newest["author"] else []
            entry.messages = [newest["message"]] if newest["message"] else []
            entry.date = newest["date"]
            entry.commits = 1
    digest.source = "commits"
    if failures and not successes:
        digest.attributed = False
        digest.source = "states"
    return digest


def summarize_diff(base: bytes | None, head: bytes | None, path: str) -> dict:
    """Pure line-count summary of ``base`` → ``head`` for one file — the digest
    detail's fallback shape. Kind ``"lines"`` carries added/removed line
    counts; undecodable content degrades to kind ``"binary"`` with sizes only.
    The hub's detail endpoint prefers the cell differ (``mooring.celldiff``)
    for marimo notebooks — which this module must NOT import (celldiff →
    marimo_rt → marimo would put marimo on the frozen core's import graph; see
    ``.importlinter``) — and falls back here for everything else.

    Raises ``ValueError`` when both sides are None (nothing to summarize).
    """
    if base is None and head is None:
        raise ValueError("nothing to summarize: no base and no remote content")
    if max(len(base or b""), len(head or b"")) > MAX_TEXT_BYTES:
        # A 40 MB CSV (max_file_mb allows 45) would pin a worker on difflib for
        # minutes; over the cap the summary degrades to sizes, like celldiff.
        return {
            "kind": "binary",
            "added": 0,
            "removed": 0,
            "base_size": len(base or b""),
            "head_size": len(head or b""),
        }
    if path.endswith(".py"):  # repo bytes are LF for .py (gitsha normalizes on push)
        base = base.replace(b"\r\n", b"\n") if base is not None else None
        head = head.replace(b"\r\n", b"\n") if head is not None else None
    try:
        base_text = base.decode("utf-8") if base is not None else ""
        head_text = head.decode("utf-8") if head is not None else ""
    except UnicodeDecodeError:
        return {
            "kind": "binary",
            "added": 0,
            "removed": 0,
            "base_size": len(base or b""),
            "head_size": len(head or b""),
        }
    added = removed = 0
    for line in difflib.unified_diff(base_text.splitlines(), head_text.splitlines(), lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"kind": "lines", "added": added, "removed": removed}
