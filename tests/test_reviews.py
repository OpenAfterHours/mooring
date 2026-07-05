"""The reviewer-inbox service: list mooring proposals, cell-aware diff, submit a review.

A fake GitHub client stands in for the REST calls (the sync tests' structural-protocol
idiom); these pin the branch/own-PR filtering, the cell-aware diff reuse, and the
event/note validation.
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


class FakeClient:
    def __init__(self, pulls=(), files=None, blobs=None):
        self._pulls = list(pulls)
        self._files = files or {}  # {number: [file dicts]}
        self._blobs = blobs or {}  # {ref: {path: bytes}}
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

    def get_file_at(self, path, ref):
        table = self._blobs.get(ref, {})
        if path not in table:
            raise NotFound(f"{path}@{ref}")
        return ("sha", table[path])

    def submit_review(self, number, event, body=""):
        self.submitted.append((number, event, body))
        return {"id": 1}


def _pr(number, *, author="alice", head="mooring/alice/20260101-0900", base="main",
        base_sha="B", head_sha="H", title="t", draft=False):
    return {
        "number": number,
        "title": title,
        "user": {"login": author},
        "head": {"ref": head, "sha": head_sha},
        "base": {"ref": base, "sha": base_sha},
        "html_url": f"https://github.com/o/r/pull/{number}",
        "updated_at": "2026-07-05T10:00:00Z",
        "draft": draft,
    }


def test_list_reviews_filters_to_mooring_branches_and_excludes_own():
    pulls = [
        _pr(1, author="alice", head="mooring/alice/x"),  # a teammate's proposal — kept
        _pr(2, author="me", head="mooring/me/y"),  # my own — GitHub blocks self-approval
        _pr(3, author="bob", head="feature/other"),  # not a mooring review branch
        _pr(4, author="carol", head="mooring/carol/z", draft=True),  # a draft PR
    ]
    got = reviews.list_reviews(FakeClient(pulls=pulls), me="me")
    assert [r.number for r in got] == [1]
    assert got[0].author == "alice" and got[0].head_ref == "mooring/alice/x"


def test_list_reviews_case_insensitive_own_login():
    pulls = [_pr(1, author="Me", head="mooring/Me/x")]
    assert reviews.list_reviews(FakeClient(pulls=pulls), me="me") == []


def test_review_detail_cell_aware_for_notebooks_line_diff_for_rest():
    pr = _pr(1, base_sha="B", head_sha="H")
    files = {
        1: [
            {"filename": "notebooks/recon.py", "status": "modified", "patch": "@@ irrelevant @@"},
            {"filename": "data/notes.txt", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"},
        ]
    }
    blobs = {
        "B": {"notebooks/recon.py": NB_BASE.encode(), "data/notes.txt": b"a\n"},
        "H": {"notebooks/recon.py": NB_HEAD.encode(), "data/notes.txt": b"b\n"},
    }
    detail = reviews.review_detail(FakeClient(pulls=[pr], files=files, blobs=blobs), 1)

    py = next(f for f in detail["files"] if f["path"].endswith(".py"))
    assert py["diff"]["kind"] == "cells"  # the value-free cell-aware view
    assert any(c["status"] == "changed" for c in py["diff"]["cells"])

    txt = next(f for f in detail["files"] if f["path"].endswith(".txt"))
    assert txt["diff"]["kind"] == "lines"
    assert txt["diff"]["line_diff"] == "@@ -1 +1 @@\n-a\n+b"  # GitHub's own patch


def test_review_detail_added_notebook_has_no_base_side():
    # A file added by the PR isn't present at the base ref -> base None -> all cells "added".
    pr = _pr(1, base_sha="B", head_sha="H")
    files = {1: [{"filename": "notebooks/new.py", "status": "added", "patch": ""}]}
    blobs = {"B": {}, "H": {"notebooks/new.py": NB_HEAD.encode()}}
    detail = reviews.review_detail(FakeClient(pulls=[pr], files=files, blobs=blobs), 1)
    diff = detail["files"][0]["diff"]
    assert diff["kind"] == "cells"
    assert all(c["status"] == "added" for c in diff["cells"])


def test_submit_validates_event_and_requires_a_note():
    client = FakeClient()
    with pytest.raises(ValueError):
        reviews.submit(client, 1, "REQUEST_CHANGES", "")  # note required
    with pytest.raises(ValueError):
        reviews.submit(client, 1, "BOGUS", "x")  # unknown event
    reviews.submit(client, 1, "APPROVE", "")  # an approve may be noteless
    reviews.submit(client, 1, "request_changes", "please rename x")  # case-insensitive
    assert client.submitted == [(1, "APPROVE", ""), (1, "REQUEST_CHANGES", "please rename x")]


def test_content_at_missing_ref_or_path_is_none():
    client = FakeClient(blobs={"B": {"a.py": b"x"}})
    assert reviews._content_at(client, "a.py", "") is None  # no ref
    assert reviews._content_at(client, "missing.py", "B") is None  # not at ref
    assert reviews._content_at(client, "a.py", "B") == b"x"
