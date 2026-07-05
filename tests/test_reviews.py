"""The reviewer-inbox service: list mooring proposals, cell-aware diff, submit a review.

A fake GitHub client stands in for the REST calls (the sync tests' structural-protocol
idiom); these pin the branch/own-PR/fork filtering (enforced at EVERY endpoint), the
merge-base + rename handling in the diff, and the event/note validation.
"""

from __future__ import annotations

import pytest

from mooring.app import reviews
from mooring.github import NotFound


def _nb(value: int) -> str:
    return (
        "import marimo\n\n"
        '__generated_with = "0.23.9"\n'
        "app = marimo.App()\n\n\n"
        "@app.cell\n"
        "def _():\n"
        f"    x = {value}\n"
        "    return (x,)\n\n\n"
        'if __name__ == "__main__":\n'
        "    app.run()\n"
    )


NB_BASE = _nb(1)
NB_HEAD = _nb(2)
REPO = "acme/nbs"


class FakeClient:
    def __init__(self, pulls=(), files=None, blobs=None, merge_base="MB"):
        self._pulls = list(pulls)
        self._files = files or {}  # {number: [file dicts]}
        self._blobs = blobs or {}  # {ref: {path: bytes}}
        self._merge_base = merge_base
        self.submitted: list[tuple] = []

    def list_open_pulls(self):
        return self._pulls

    def get_pull(self, number):
        for p in self._pulls:
            if p["number"] == number:
                return p
        raise NotFound(str(number))

    def list_pull_files(self, number):
        return self._files.get(number, [])

    def compare(self, base, head):
        return {"merge_base_commit": {"sha": self._merge_base}}

    def get_file_at(self, path, ref):
        table = self._blobs.get(ref, {})
        if path not in table:
            raise NotFound(f"{path}@{ref}")
        return ("sha", table[path])

    def submit_review(self, number, event, body=""):
        self.submitted.append((number, event, body))
        return {"id": 1}


def _pr(number, *, author="alice", head="mooring/alice/20260101-0900", base="main",
        base_sha="B", head_sha="H", title="t", draft=False, fork=False):
    head_repo = "carol/nbs-fork" if fork else REPO
    return {
        "number": number,
        "title": title,
        "user": {"login": author},
        "head": {"ref": head, "sha": head_sha, "repo": {"full_name": head_repo}},
        "base": {"ref": base, "sha": base_sha, "repo": {"full_name": REPO}},
        "html_url": f"https://github.com/o/r/pull/{number}",
        "updated_at": "2026-07-05T10:00:00Z",
        "draft": draft,
    }


# -- listing --------------------------------------------------------------------


def test_list_reviews_filters_branch_own_fork_and_draft():
    pulls = [
        _pr(1, author="alice", head="mooring/alice/x"),  # a teammate's proposal — kept
        _pr(2, author="me", head="mooring/me/y"),  # own — self-approval blocked
        _pr(3, author="bob", head="feature/other"),  # not a mooring review branch
        _pr(4, author="carol", head="mooring/carol/z", draft=True),  # a draft PR
        _pr(5, author="dan", head="mooring/dan/w", fork=True),  # a fork PR
    ]
    got = reviews.list_reviews(FakeClient(pulls=pulls), me="me")
    assert [r.number for r in got] == [1]


def test_list_reviews_case_insensitive_own_login():
    pulls = [_pr(1, author="Me", head="mooring/Me/x")]
    assert reviews.list_reviews(FakeClient(pulls=pulls), me="me") == []


# -- the diff -------------------------------------------------------------------


def test_review_detail_cell_aware_uses_merge_base_and_line_diff_for_rest():
    pr = _pr(1, base_sha="B", head_sha="H")
    files = {
        1: [
            {"filename": "notebooks/recon.py", "status": "modified", "patch": "@@ irrelevant @@"},
            {"filename": "data/notes.txt", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"},
        ]
    }
    # The base blob must be read at the MERGE-BASE ("MB"), NOT the base tip ("B").
    blobs = {
        "MB": {"notebooks/recon.py": NB_BASE.encode()},
        "H": {"notebooks/recon.py": NB_HEAD.encode()},
    }
    detail = reviews.review_detail(FakeClient(pulls=[pr], files=files, blobs=blobs), 1)

    py = next(f for f in detail["files"] if f["path"].endswith(".py"))
    assert py["diff"]["kind"] == "cells"
    assert any(c["status"] == "changed" for c in py["diff"]["cells"])

    txt = next(f for f in detail["files"] if f["path"].endswith(".txt"))
    assert txt["diff"]["kind"] == "lines" and txt["diff"]["line_diff"] == "@@ -1 +1 @@\n-a\n+b"


def test_review_detail_renamed_notebook_diffs_against_the_old_name():
    # A rename fetches the base blob at previous_filename, so the real edit shows as a
    # cell change — not the whole notebook rendered as brand-new.
    pr = _pr(1, base_sha="B", head_sha="H")
    files = {
        1: [{
            "filename": "notebooks/new.py",
            "previous_filename": "notebooks/old.py",
            "status": "renamed",
            "patch": "",
        }]
    }
    blobs = {
        "MB": {"notebooks/old.py": NB_BASE.encode()},  # base under the OLD name
        "H": {"notebooks/new.py": NB_HEAD.encode()},
    }
    diff = reviews.review_detail(FakeClient(pulls=[pr], files=files, blobs=blobs), 1)["files"][0]["diff"]
    assert diff["kind"] == "cells"
    assert any(c["status"] == "changed" for c in diff["cells"])  # the edit is visible
    assert not all(c["status"] == "added" for c in diff["cells"])  # NOT an all-new rewrite


def test_review_detail_added_notebook_has_no_base_side():
    pr = _pr(1, base_sha="B", head_sha="H")
    files = {1: [{"filename": "notebooks/new.py", "status": "added", "patch": ""}]}
    blobs = {"MB": {}, "H": {"notebooks/new.py": NB_HEAD.encode()}}
    diff = reviews.review_detail(FakeClient(pulls=[pr], files=files, blobs=blobs), 1)["files"][0]["diff"]
    assert diff["kind"] == "cells"
    assert all(c["status"] == "added" for c in diff["cells"])


# -- the reviewable guard (enforced at detail AND submit) -----------------------


def test_review_detail_refuses_a_non_mooring_pr():
    client = FakeClient(pulls=[_pr(9, head="feature/x")])
    with pytest.raises(reviews.NotReviewable):
        reviews.review_detail(client, 9)


def test_submit_re_verifies_the_pr_not_just_the_list():
    # THE four-eyes guard: submit must refuse a PR the inbox would never list, even by
    # direct number — a non-mooring branch, a fork, or your own.
    client = FakeClient(pulls=[
        _pr(1, author="alice", head="mooring/alice/x"),  # reviewable
        _pr(2, author="bob", head="feature/y"),  # not a proposal
        _pr(3, author="me", head="mooring/me/z"),  # your own
    ])
    reviews.submit(client, 1, "APPROVE", "", me="me")  # ok
    with pytest.raises(reviews.NotReviewable):
        reviews.submit(client, 2, "APPROVE", "", me="me")  # non-mooring
    with pytest.raises(reviews.NotReviewable):
        reviews.submit(client, 3, "APPROVE", "", me="me")  # own
    assert client.submitted == [(1, "APPROVE", "")]


def test_submit_validates_event_and_requires_a_note():
    client = FakeClient(pulls=[_pr(1, author="alice", head="mooring/alice/x")])
    with pytest.raises(ValueError):
        reviews.submit(client, 1, "REQUEST_CHANGES", "", me="me")  # note required
    with pytest.raises(ValueError):
        reviews.submit(client, 1, "COMMENT", "hi", me="me")  # out of scope in Slice 1
    with pytest.raises(ValueError):
        reviews.submit(client, 1, "BOGUS", "x", me="me")
    reviews.submit(client, 1, "APPROVE", "", me="me")
    reviews.submit(client, 1, "request_changes", "please rename x", me="me")  # case-insensitive
    assert client.submitted == [(1, "APPROVE", ""), (1, "REQUEST_CHANGES", "please rename x")]


def test_content_at_missing_ref_or_path_is_none():
    client = FakeClient(blobs={"B": {"a.py": b"x"}})
    assert reviews._content_at(client, "a.py", "") is None
    assert reviews._content_at(client, "missing.py", "B") is None
    assert reviews._content_at(client, "a.py", "B") == b"x"
