import base64

import pytest
import responses

from mooring.github import (
    AuthFailed,
    GitHubClient,
    GitHubError,
    RefAlreadyExists,
    RemoteConflict,
    blob_url,
    compare_url,
)

API_ROOT = "https://api.github.com"
REPO = f"{API_ROOT}/repos/acme/nbs"


def client() -> GitHubClient:
    return GitHubClient("tok", "acme", "nbs")


@responses.activate
def test_get_branch_head():
    responses.add(
        responses.GET,
        f"{REPO}/git/ref/heads/main",
        json={"object": {"sha": "c0ffee"}},
    )
    assert client().get_branch_head("main") == "c0ffee"
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok"


@responses.activate
def test_get_tree_filters_to_configured_folders():
    responses.add(responses.GET, f"{REPO}/git/commits/c0ffee", json={"tree": {"sha": "tree1"}})
    responses.add(
        responses.GET,
        f"{REPO}/git/trees/tree1",
        json={
            "truncated": False,
            "tree": [
                {"path": "notebooks/a.py", "type": "blob", "sha": "a" * 40, "size": 10},
                {"path": "data/x.csv", "type": "blob", "sha": "b" * 40, "size": 20},
                {"path": "README.md", "type": "blob", "sha": "c" * 40, "size": 5},
                {"path": "notebooks", "type": "tree", "sha": "d" * 40},
            ],
        },
    )
    entries = client().get_tree("c0ffee", ("notebooks", "data"))
    assert [e.path for e in entries] == ["notebooks/a.py", "data/x.csv"]


@responses.activate
def test_get_full_tree_returns_all_blobs_unfiltered():
    responses.add(responses.GET, f"{REPO}/git/commits/c0ffee", json={"tree": {"sha": "tree1"}})
    responses.add(
        responses.GET,
        f"{REPO}/git/trees/tree1",
        json={
            "truncated": False,
            "tree": [
                {"path": "notebooks/a.py", "type": "blob", "sha": "a" * 40, "size": 10},
                {"path": "analysis/q1.py", "type": "blob", "sha": "b" * 40, "size": 20},
                {"path": "README.md", "type": "blob", "sha": "c" * 40, "size": 5},
                {"path": "analysis", "type": "tree", "sha": "d" * 40},  # dropped (not a blob)
            ],
        },
    )
    entries = client().get_full_tree("c0ffee")
    assert [e.path for e in entries] == ["notebooks/a.py", "analysis/q1.py", "README.md"]


@responses.activate
def test_sha256_guard_only_fires_for_in_scope_blobs():
    # A SHA-256 repo whose synced folders are empty must report an empty tree, not
    # error — the guard runs on the FILTERED (in-scope) entries, where a real sync
    # would actually need them. get_full_tree (discovery) tolerates the long shas.
    responses.add(responses.GET, f"{REPO}/git/commits/c0ffee", json={"tree": {"sha": "tree1"}})
    responses.add(
        responses.GET,
        f"{REPO}/git/trees/tree1",
        json={
            "truncated": False,
            "tree": [{"path": "analysis/q1.py", "type": "blob", "sha": "a" * 64, "size": 10}],
        },
    )
    assert client().get_tree("c0ffee", ("notebooks",)) == []  # nothing in scope → no error
    assert [e.path for e in client().get_full_tree("c0ffee")] == ["analysis/q1.py"]
    with pytest.raises(GitHubError, match="SHA-256"):
        client().get_tree("c0ffee", ("analysis",))  # in-scope sha-256 blob → error


@responses.activate
def test_get_tree_truncated_is_an_error():
    responses.add(responses.GET, f"{REPO}/git/commits/c0ffee", json={"tree": {"sha": "tree1"}})
    responses.add(responses.GET, f"{REPO}/git/trees/tree1", json={"truncated": True, "tree": []})
    with pytest.raises(GitHubError, match="too large"):
        client().get_tree("c0ffee", ("notebooks",))


@responses.activate
def test_get_blob_decodes_base64():
    responses.add(
        responses.GET,
        f"{REPO}/git/blobs/{'a' * 40}",
        json={"encoding": "base64", "content": base64.b64encode(b"hi there").decode()},
    )
    assert client().get_blob("a" * 40) == b"hi there"


@responses.activate
def test_put_file_sends_base_sha_and_returns_new_sha():
    responses.add(
        responses.PUT,
        f"{REPO}/contents/notebooks/a.py",
        json={"content": {"sha": "newsha"}, "commit": {"sha": "commit1"}},
    )
    result = client().put_file("notebooks/a.py", b"data", "msg", "main", base_sha="oldsha")
    assert result["content"]["sha"] == "newsha"
    body = responses.calls[0].request.body
    assert isinstance(body, bytes)
    assert b'"sha": "oldsha"' in body
    assert base64.b64encode(b"data") in body


@responses.activate
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (409, {"message": "merge conflict"}),
        (422, {"message": "notebooks/a.py does not match expected sha"}),
    ],
)
def test_put_file_conflicts(status, body):
    responses.add(responses.PUT, f"{REPO}/contents/notebooks/a.py", json=body, status=status)
    with pytest.raises(RemoteConflict):
        client().put_file("notebooks/a.py", b"data", "msg", "main", base_sha="oldsha")


@responses.activate
def test_401_raises_auth_failed():
    responses.add(responses.GET, f"{API_ROOT}/user", json={}, status=401)
    with pytest.raises(AuthFailed):
        client().get_user()


@responses.activate
def test_create_ref_posts_branch_ref():
    responses.add(
        responses.POST,
        f"{REPO}/git/refs",
        json={"ref": "refs/heads/mooring/phil/20260612-0900", "object": {"sha": "c0ffee"}},
        status=201,
    )
    client().create_ref("mooring/phil/20260612-0900", "c0ffee")
    body = responses.calls[0].request.body
    assert isinstance(body, bytes)
    assert b'"ref": "refs/heads/mooring/phil/20260612-0900"' in body
    assert b'"sha": "c0ffee"' in body


@responses.activate
def test_create_ref_existing_branch():
    responses.add(
        responses.POST,
        f"{REPO}/git/refs",
        json={"message": "Reference already exists"},
        status=422,
    )
    with pytest.raises(RefAlreadyExists):
        client().create_ref("mooring/phil/20260612-0900", "c0ffee")


@responses.activate
def test_create_ref_other_422_is_generic_error():
    responses.add(
        responses.POST,
        f"{REPO}/git/refs",
        json={"message": "Object does not exist"},
        status=422,
    )
    with pytest.raises(GitHubError):
        client().create_ref("mooring/phil/20260612-0900", "c0ffee")


def test_compare_url():
    assert compare_url("acme", "nbs", "main", "mooring/phil/20260612-0900") == (
        "https://github.com/acme/nbs/compare/main...mooring/phil/20260612-0900?expand=1"
    )


def test_compare_url_on_enterprise_host():
    assert compare_url("acme", "nbs", "main", "fix", host="ghe.service.group") == (
        "https://ghe.service.group/acme/nbs/compare/main...fix?expand=1"
    )


def test_blob_url():
    assert blob_url("acme", "nbs", "main", "notebooks/sales.py") == (
        "https://github.com/acme/nbs/blob/main/notebooks/sales.py"
    )


def test_blob_url_on_enterprise_host():
    assert blob_url("acme", "nbs", "main", "reports/q3.py", host="ghe.service.group") == (
        "https://ghe.service.group/acme/nbs/blob/main/reports/q3.py"
    )


def test_blob_url_percent_encodes_segments_but_keeps_slashes():
    # A space in a path segment is encoded; the folder separators survive.
    assert blob_url("acme", "nbs", "main", "my data/sales report.py") == (
        "https://github.com/acme/nbs/blob/main/my%20data/sales%20report.py"
    )


@responses.activate
def test_enterprise_host_routes_to_api_v3():
    ghes = GitHubClient("tok", "acme", "nbs", host="ghe.service.group")
    responses.add(
        responses.GET,
        "https://ghe.service.group/api/v3/repos/acme/nbs/git/ref/heads/main",
        json={"object": {"sha": "c0ffee"}},
    )
    responses.add(responses.GET, "https://ghe.service.group/api/v3/user", json={"login": "phil"})
    assert ghes.get_branch_head("main") == "c0ffee"
    assert ghes.get_user()["login"] == "phil"


# -- version history reads (commits list + contents-at-ref) -------------------


@responses.activate
def test_list_commits_for_path_request_shape_and_parsing():
    responses.add(
        responses.GET,
        f"{REPO}/commits",
        json=[
            {
                "sha": "abc1234def",
                "commit": {"message": "Update x", "author": {"name": "Maria", "date": "2026-06-30T09:00:00Z"}},
                "author": {"login": "maria"},
            }
        ],
    )
    commits = client().list_commits_for_path("notebooks/a.py", "main", page=2, per_page=30)
    assert commits[0]["sha"] == "abc1234def"
    params = responses.calls[0].request.params
    assert params["path"] == "notebooks/a.py"
    assert params["sha"] == "main"
    assert params["page"] == "2"
    assert params["per_page"] == "30"


@responses.activate
def test_list_commits_for_path_empty_page():
    responses.add(responses.GET, f"{REPO}/commits", json=[])
    assert client().list_commits_for_path("notebooks/a.py", "main", page=9) == []


# -- compare (the pull digest's one-request horizon window) -------------------


@responses.activate
def test_compare_request_shape_and_raw_passthrough():
    responses.add(
        responses.GET,
        f"{REPO}/compare/aaa111...bbb222",
        json={
            "total_commits": 2,
            "commits": [
                {
                    "sha": "c1",
                    "commit": {
                        "message": "Update notebooks/a.py via mooring",
                        "author": {"name": "Maria", "date": "2026-06-30T09:00:00Z"},
                    },
                    "author": {"login": "maria"},
                },
                {
                    "sha": "c2",
                    "commit": {
                        "message": "Update notebooks/b.py via mooring",
                        "author": {"name": "Maria", "date": "2026-06-30T09:00:05Z"},
                    },
                    "author": {"login": "maria"},
                },
            ],
            "files": [
                {"filename": "notebooks/a.py", "status": "modified"},
                {"filename": "notebooks/b.py", "status": "added"},
            ],
        },
    )
    data = client().compare("aaa111", "bbb222")
    # Raw dict passthrough — callers (mooring.whatsnew) shape it themselves.
    assert data["total_commits"] == 2
    assert [c["sha"] for c in data["commits"]] == ["c1", "c2"]
    assert [f["filename"] for f in data["files"]] == ["notebooks/a.py", "notebooks/b.py"]
    assert responses.calls[0].request.url.endswith("/compare/aaa111...bbb222")


@responses.activate
def test_compare_lost_anchor_raises_notfound():
    # A GC'd or force-pushed-away base 404s — the "anchor lost" signal callers
    # treat as "fall back to per-file commit lookups".
    from mooring.github import NotFound

    responses.add(responses.GET, f"{REPO}/compare/gone111...head222", status=404)
    with pytest.raises(NotFound):
        client().compare("gone111", "head222")


@responses.activate
def test_compare_surfaces_auth_and_rate_errors():
    from mooring.github import RateLimited

    responses.add(responses.GET, f"{REPO}/compare/a...b", status=401)
    with pytest.raises(AuthFailed):
        client().compare("a", "b")
    responses.reset()
    responses.add(
        responses.GET,
        f"{REPO}/compare/a...b",
        status=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"},
    )
    with pytest.raises(RateLimited):
        client().compare("a", "b")


@responses.activate
def test_get_file_at_inline_content():
    responses.add(
        responses.GET,
        f"{REPO}/contents/notebooks/a.py",
        json={
            "sha": "blob1",
            "encoding": "base64",
            "content": base64.b64encode(b"x = 1\n").decode(),
        },
    )
    sha, data = client().get_file_at("notebooks/a.py", "abc1234")
    assert (sha, data) == ("blob1", b"x = 1\n")
    assert responses.calls[0].request.params["ref"] == "abc1234"


@responses.activate
def test_get_file_at_large_file_falls_back_to_blob():
    # Past ~1 MB the contents API omits inline content; the blob API serves it.
    responses.add(
        responses.GET,
        f"{REPO}/contents/data/big.csv",
        json={"sha": "blob9", "encoding": "none", "content": ""},
    )
    responses.add(
        responses.GET,
        f"{REPO}/git/blobs/blob9",
        json={"encoding": "base64", "content": base64.b64encode(b"a,b\n").decode()},
    )
    assert client().get_file_at("data/big.csv", "ref1") == ("blob9", b"a,b\n")


@responses.activate
def test_get_file_at_missing_raises_notfound():
    from mooring.github import NotFound

    responses.add(responses.GET, f"{REPO}/contents/notebooks/gone.py", status=404)
    with pytest.raises(NotFound):
        client().get_file_at("notebooks/gone.py", "ref1")


@responses.activate
def test_history_reads_surface_auth_and_rate_errors():
    from mooring.github import RateLimited

    responses.add(responses.GET, f"{REPO}/commits", status=401)
    with pytest.raises(AuthFailed):
        client().list_commits_for_path("a.py", "main")
    responses.reset()
    responses.add(
        responses.GET,
        f"{REPO}/commits",
        status=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"},
    )
    with pytest.raises(RateLimited):
        client().list_commits_for_path("a.py", "main")
