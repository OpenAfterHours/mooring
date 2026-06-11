import base64

import pytest
import responses

from mooring.github import (
    API_ROOT,
    AuthFailed,
    GitHubClient,
    GitHubError,
    RemoteConflict,
)

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
    responses.add(
        responses.GET, f"{REPO}/git/commits/c0ffee", json={"tree": {"sha": "tree1"}}
    )
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
def test_get_tree_truncated_is_an_error():
    responses.add(
        responses.GET, f"{REPO}/git/commits/c0ffee", json={"tree": {"sha": "tree1"}}
    )
    responses.add(
        responses.GET, f"{REPO}/git/trees/tree1", json={"truncated": True, "tree": []}
    )
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
