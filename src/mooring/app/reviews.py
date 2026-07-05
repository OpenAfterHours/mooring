"""The reviewer inbox: approve a teammate's proposal from inside the hub (four-eyes).

mooring's **Propose** flow writes an author's changes to a personal review branch
(``mooring/<login>/<timestamp>``) and opens a PR-gated review. This is the REVIEWER
side: list those open proposals, show a value-free cell-aware diff of what each one
changes (reusing :mod:`mooring.celldiff` — the same engine as the "Review changes"
panel), and submit an **Approve** / **Request-changes** via the GitHub PR-review API —
so a teammate who never learns git can give four-eyes without leaving the hub.

All PR operations fall under the ``repo`` scope the login already holds, so no new
consent is needed. Slice 1 reviews PRs that already EXIST (the author still creates the
PR from the Propose link). Diffs and reviews touch only authored code + PR metadata —
never a data value — so this opens no value-blindness surface.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from mooring import celldiff
from mooring.github import NotFound

# sync.propose names review branches mooring/<login>/<timestamp>; the inbox lists only
# these, never arbitrary PRs that happen to be open on the repo.
REVIEW_BRANCH_PREFIX = "mooring/"

_EVENTS = ("APPROVE", "REQUEST_CHANGES", "COMMENT")


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
    """Open PRs from mooring review branches, EXCLUDING your own — GitHub blocks
    approving your own PR, and four-eyes wants a second person anyway."""
    out: list[Review] = []
    for pr in client.list_open_pulls():
        if pr.get("draft"):
            continue
        ref = (pr.get("head") or {}).get("ref", "")
        if not ref.startswith(REVIEW_BRANCH_PREFIX):
            continue
        author = (pr.get("user") or {}).get("login", "")
        if me and author and author.lower() == me.lower():
            continue
        out.append(
            Review(
                number=int(pr.get("number", 0)),
                title=str(pr.get("title", "")),
                author=author,
                head_ref=ref,
                base_ref=(pr.get("base") or {}).get("ref", ""),
                url=str(pr.get("html_url", "")),
                updated=str(pr.get("updated_at", "")),
            )
        )
    return out


def review_detail(client, number: int) -> dict:
    """PR ``number``'s changed files, each with a cell-aware diff (marimo notebook) or a
    whole-file line diff (anything else). Diffs the PR's exact base/head snapshot."""
    pr = client.get_pull(number)
    base_sha = (pr.get("base") or {}).get("sha", "")
    head_sha = (pr.get("head") or {}).get("sha", "")
    files: list[dict] = []
    for f in client.list_pull_files(number):
        path = str(f.get("filename", ""))
        status = str(f.get("status", ""))
        entry: dict = {"path": path, "status": status}
        diff = None
        if path.endswith(".py"):
            base_bytes = _content_at(client, path, base_sha)
            head_bytes = _content_at(client, path, head_sha)
            try:
                diff = dataclasses.asdict(celldiff.diff(base_bytes, head_bytes, path))
            except ValueError:
                diff = None  # both sides missing — fall back to the API patch below
        if diff is None:
            # Non-notebook, or a notebook celldiff couldn't compare: GitHub's own patch.
            diff = {"kind": "lines", "cells": [], "line_diff": str(f.get("patch", "")), "note": ""}
        entry["diff"] = diff
        files.append(entry)
    return {
        "number": number,
        "title": str(pr.get("title", "")),
        "author": (pr.get("user") or {}).get("login", ""),
        "url": str(pr.get("html_url", "")),
        "files": files,
    }


def submit(client, number: int, event: str, body: str) -> dict:
    """Submit the review. GitHub requires a note for REQUEST_CHANGES / COMMENT; an
    Approve may be noteless. Raises ``ValueError`` for a bad event / a missing note."""
    event = (event or "").upper()
    if event not in _EVENTS:
        raise ValueError("event must be APPROVE, REQUEST_CHANGES, or COMMENT")
    if event in ("REQUEST_CHANGES", "COMMENT") and not (body or "").strip():
        raise ValueError("Add a note describing the change you want.")
    return client.submit_review(number, event, body or "")


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
