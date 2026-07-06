"""The reviewer inbox: approve a teammate's proposal from inside the hub (four-eyes).

mooring's **Propose** flow writes an author's changes to a personal review branch
(``mooring/<login>/<timestamp>``) and opens a PR-gated review. This is the REVIEWER
side: list those open proposals, show a value-free cell-aware diff of what each one
changes (reusing :mod:`mooring.celldiff` — the same engine as the "Review changes"
panel), and submit an **Approve** / **Request-changes** via the GitHub PR-review API —
so a teammate who never learns git can give four-eyes without leaving the hub.

The "only a mooring proposal, never your own, never a fork" rule is enforced by
:func:`_verify_reviewable` at EVERY endpoint that reads or acts on a PR — not just at
the list — so a hand-crafted POST of an arbitrary PR number can't slip a review past the
filter. All PR operations fall under the ``repo`` scope the login already holds, so no
new consent is needed. Slice 1 reviews PRs that already EXIST (the author still creates
the PR from the Propose link). Diffs and reviews touch only authored code + PR metadata —
never a data value — so this opens no value-blindness surface.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from mooring import celldiff
from mooring.github import GitHubError, NotFound

# sync.propose names review branches mooring/<login>/<timestamp>; the inbox lists only
# these, never arbitrary PRs that happen to be open on the repo.
REVIEW_BRANCH_PREFIX = "mooring/"

# Slice 1 offers exactly Approve and Request-changes (the two UI buttons); a plain
# COMMENT review is out of scope and rejected rather than mislabelled.
_EVENTS = ("APPROVE", "REQUEST_CHANGES")


class NotReviewable(ValueError):
    """The PR isn't something this reviewer may act on (not a mooring proposal, a fork,
    or their own). ``str(exc)`` is the user-facing reason; the adapters map it to a 4xx."""


@dataclass
class Review:
    number: int
    title: str
    author: str
    head_ref: str
    base_ref: str
    url: str
    updated: str


def list_reviews(client, me: str) -> list[Review]:
    """Open PRs that are mooring proposals a DIFFERENT teammate can review — a
    ``mooring/*`` head branch, same-repo (not a fork), and not your own. When ``me`` is
    unknown (the login lookup failed), the own-PR filter can't run, so the caller should
    treat that as "can't safely list" (see the route)."""
    out: list[Review] = []
    for pr in client.list_open_pulls():
        if pr.get("draft") or not _is_mooring_proposal(pr):
            continue
        author = (pr.get("user") or {}).get("login", "")
        if me and author and author.lower() == me.lower():
            continue
        out.append(
            Review(
                number=int(pr.get("number", 0)),
                title=str(pr.get("title", "")),
                author=author,
                head_ref=(pr.get("head") or {}).get("ref", ""),
                base_ref=(pr.get("base") or {}).get("ref", ""),
                url=str(pr.get("html_url", "")),
                updated=str(pr.get("updated_at", "")),
            )
        )
    return out


def review_detail(client, number: int, me: str = "") -> dict:
    """PR ``number``'s changed files, each with a cell-aware diff (marimo notebook) or a
    whole-file line diff (anything else). Refuses a PR the reviewer may not act on."""
    pr = _verify_reviewable(client, number, me)
    base_sha = (pr.get("base") or {}).get("sha", "")
    head_sha = (pr.get("head") or {}).get("sha", "")
    # Diff against the MERGE-BASE (the fork point), not the base branch's current tip —
    # so a later unrelated merge into the base branch doesn't fold into (or cancel) what
    # this proposal actually changed. This matches GitHub's own three-dot file list.
    merge_base = _merge_base(client, base_sha, head_sha) or base_sha
    files: list[dict] = []
    for f in client.list_pull_files(number):
        path = str(f.get("filename", ""))
        status = str(f.get("status", ""))
        # A renamed file existed under a DIFFERENT name at the base — fetch the base blob
        # there, or it 404s and the whole notebook renders as brand-new.
        base_path = str(f.get("previous_filename") or path)
        diff = None
        if path.endswith(".py"):
            base_bytes = _content_at(client, base_path, merge_base)
            head_bytes = _content_at(client, path, head_sha)
            try:
                diff = dataclasses.asdict(celldiff.diff(base_bytes, head_bytes, path))
            except ValueError:
                diff = None  # both sides missing — fall back to the API patch below
        if diff is None:
            # Non-notebook, or a notebook celldiff couldn't compare: GitHub's own patch.
            diff = {"kind": "lines", "cells": [], "line_diff": str(f.get("patch", "")), "note": ""}
        files.append({"path": path, "status": status, "diff": diff})
    return {
        "number": number,
        "title": str(pr.get("title", "")),
        "author": (pr.get("user") or {}).get("login", ""),
        "url": str(pr.get("html_url", "")),
        "files": files,
    }


def submit(client, number: int, event: str, body: str, me: str = "") -> dict:
    """Approve / Request-changes. GitHub requires a note for REQUEST_CHANGES; an Approve
    may be noteless. Re-verifies the PR is a reviewable mooring proposal (not your own)
    before posting — the list filter alone can't guard a direct POST. Raises
    :class:`NotReviewable` / ``ValueError`` on a bad request."""
    event = (event or "").upper()
    if event not in _EVENTS:
        raise ValueError("event must be APPROVE or REQUEST_CHANGES")
    if event == "REQUEST_CHANGES" and not (body or "").strip():
        raise ValueError("Add a note describing the change you want.")
    _verify_reviewable(client, number, me)
    return client.submit_review(number, event, body or "")


# -- guards ---------------------------------------------------------------------


def _is_mooring_proposal(pr: dict) -> bool:
    """A same-repo PR off a ``mooring/*`` review branch (not a fork). A fork PR's head
    commit isn't in the base repo, so its blobs can't be fetched — and it was never a
    mooring proposal, which only ever pushes a branch to the repo itself."""
    ref = (pr.get("head") or {}).get("ref", "")
    if not ref.startswith(REVIEW_BRANCH_PREFIX):
        return False
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    if head_repo and base_repo and head_repo != base_repo:
        return False  # a fork — review it on GitHub
    return True


def _verify_reviewable(client, number: int, me: str) -> dict:
    """Fetch PR ``number`` and confirm the reviewer may act on it — a mooring proposal,
    not a fork, and (when ``me`` is known) not their own. THE choke point both the detail
    and the submit endpoints go through, so the inbox's guarantee holds even against a
    hand-crafted PR number. Raises :class:`NotReviewable`."""
    pr = client.get_pull(number)
    if not _is_mooring_proposal(pr):
        raise NotReviewable("That pull request isn't a mooring proposal you can review here.")
    author = (pr.get("user") or {}).get("login", "")
    if me and author and author.lower() == me.lower():
        raise NotReviewable("You can't review your own proposal.")
    return pr


def _merge_base(client, base: str, head: str) -> str:
    """The merge-base (fork-point) sha of ``base``..``head`` via the compare API, or ``""``
    when it can't be resolved (the caller falls back to ``base``)."""
    if not (base and head):
        return ""
    try:
        cmp = client.compare(base, head)
    except (GitHubError, OSError):
        return ""
    if isinstance(cmp, dict):
        return ((cmp.get("merge_base_commit") or {}).get("sha")) or ""
    return ""


def _content_at(client, path: str, ref: str) -> bytes | None:
    """The bytes of ``path`` at ``ref``, or ``None`` when it did not exist there (a file
    added or removed by the PR). celldiff handles a None side (added / removed)."""
    if not ref:
        return None
    try:
        _, data = client.get_file_at(path, ref)
        return data
    except NotFound:
        return None
