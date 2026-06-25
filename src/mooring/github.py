"""Thin GitHub REST API client — everything mooring needs, no git required.

Read path uses the Git Data API (refs/commits/trees/blobs) so a pull costs
3 requests + one per changed blob. Write path uses the Contents API, whose
`sha` parameter gives per-file optimistic concurrency: GitHub rejects the
write if the remote blob changed since we last synced.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from mooring import githost

# Contents API caps: writes fail somewhere below 50 MB, blob reads at 100 MB.
MAX_WRITE_BYTES = 45 * 1024 * 1024


class GitHubError(Exception):
    pass


class AuthFailed(GitHubError):
    """Token missing/expired/revoked (HTTP 401)."""


class NotFound(GitHubError):
    pass


class RemoteConflict(GitHubError):
    """The remote file changed since our recorded base SHA (HTTP 409/422)."""


class RateLimited(GitHubError):
    pass


class RefAlreadyExists(GitHubError):
    """Branch creation hit an existing ref (HTTP 422)."""


def compare_url(
    owner: str, repo: str, base: str, branch: str, host: str = githost.DEFAULT_HOST
) -> str:
    """GitHub's compare page for opening a pull request from `branch` into `base`."""
    return f"{githost.web_root(host)}/{owner}/{repo}/compare/{base}...{branch}?expand=1"


@dataclass
class TreeEntry:
    path: str
    sha: str
    size: int


class GitHubClientProtocol(Protocol):
    """The GitHub client surface the sync engine depends on.

    The sync core types against this structural protocol rather than the
    concrete ``GitHubClient`` so the in-memory test fake can stand in without
    nominal subclassing. ``GitHubClient`` satisfies it structurally.
    """

    def get_user(self) -> dict: ...

    def get_branch_head(self, branch: str) -> str: ...

    def get_tree(
        self,
        commit_sha: str,
        folders: tuple[str, ...],
        extra_paths: tuple[str, ...] = (),
    ) -> list[TreeEntry]: ...

    def get_blob(self, sha: str) -> bytes: ...

    def create_ref(self, branch: str, sha: str) -> dict: ...

    def put_file(
        self,
        path: str,
        content: bytes,
        message: str,
        branch: str,
        base_sha: str | None = None,
    ) -> dict: ...

    def delete_file(self, path: str, message: str, branch: str, base_sha: str) -> dict: ...


class GitHubClient:
    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        host: str = githost.DEFAULT_HOST,
        session: requests.Session | None = None,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.api_root = githost.api_root(host)
        if session is None:
            session = requests.Session()
            # Auto-retry is safe for reads only; a retried PUT could double-commit.
            retry = Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=(500, 502, 503, 504),
                allowed_methods=("GET", "HEAD"),
            )
            session.mount("https://", HTTPAdapter(max_retries=retry))
        self._session = session
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    # -- plumbing ----------------------------------------------------------

    def _repo_url(self, tail: str) -> str:
        return f"{self.api_root}/repos/{self.owner}/{self.repo}/{tail}"

    def _check(self, resp: requests.Response) -> dict:
        if resp.status_code == 401:
            raise AuthFailed("GitHub rejected the token. Log in again.")
        if resp.status_code == 404:
            raise NotFound(f"Not found: {resp.url}")
        if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            raise RateLimited(
                "GitHub API rate limit reached; try again later "
                f"(resets at unix {resp.headers.get('x-ratelimit-reset')})."
            )
        if resp.status_code == 409:
            raise RemoteConflict("Remote changed since your last pull.")
        if resp.status_code == 422:
            message = ""
            try:
                message = resp.json().get("message", "")
            except ValueError:
                pass
            if "does not match" in message or "sha" in message.lower():
                raise RemoteConflict("Remote changed since your last pull.")
            raise GitHubError(f"GitHub rejected the request: {message or resp.text}")
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub API error {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # -- reads ---------------------------------------------------------------

    def get_user(self) -> dict:
        return self._check(self._session.get(f"{self.api_root}/user", timeout=30))

    def get_branch_head(self, branch: str) -> str:
        data = self._check(self._session.get(self._repo_url(f"git/ref/heads/{branch}"), timeout=30))
        return data["object"]["sha"]

    def get_tree(
        self,
        commit_sha: str,
        folders: tuple[str, ...],
        extra_paths: tuple[str, ...] = (),
    ) -> list[TreeEntry]:
        commit = self._check(
            self._session.get(self._repo_url(f"git/commits/{commit_sha}"), timeout=30)
        )
        data = self._check(
            self._session.get(
                self._repo_url(f"git/trees/{commit['tree']['sha']}"),
                params={"recursive": "1"},
                timeout=60,
            )
        )
        if data.get("truncated"):
            raise GitHubError(
                "The repository tree is too large for the GitHub trees API; "
                "mooring cannot sync this repo."
            )
        prefixes = tuple(f"{f.rstrip('/')}/" for f in folders)
        extra = frozenset(extra_paths)
        entries = []
        for item in data.get("tree", []):
            if item["type"] != "blob":
                continue
            if not (item["path"].startswith(prefixes) or item["path"] in extra):
                continue
            if len(item["sha"]) != 40:
                raise GitHubError("SHA-256 object-format repos are not supported.")
            entries.append(TreeEntry(item["path"], item["sha"], item.get("size", 0)))
        return entries

    def get_blob(self, sha: str) -> bytes:
        data = self._check(self._session.get(self._repo_url(f"git/blobs/{sha}"), timeout=120))
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"])
        return data.get("content", "").encode()

    # -- writes (Contents API, one commit per file) ---------------------------

    def create_ref(self, branch: str, sha: str) -> dict:
        resp = self._session.post(
            self._repo_url("git/refs"),
            json={"ref": f"refs/heads/{branch}", "sha": sha},
            timeout=30,
        )
        # _check's 422 heuristics target sha mismatches; "Reference already
        # exists" needs its own type so callers can retry with a new name.
        if resp.status_code == 422:
            try:
                message = resp.json().get("message", "")
            except ValueError:
                message = ""
            if "already exists" in message.lower():
                raise RefAlreadyExists(f"Branch {branch} already exists.")
        return self._check(resp)

    def put_file(
        self,
        path: str,
        content: bytes,
        message: str,
        branch: str,
        base_sha: str | None = None,
    ) -> dict:
        if len(content) > MAX_WRITE_BYTES:
            raise GitHubError(
                f"{path} is {len(content) // (1024 * 1024)} MB; the GitHub contents "
                "API caps writes below 50 MB."
            )
        body: dict = {
            "message": message,
            "content": base64.b64encode(content).decode(),
            "branch": branch,
        }
        if base_sha:
            body["sha"] = base_sha
        return self._check(
            self._session.put(self._repo_url(f"contents/{path}"), json=body, timeout=120)
        )

    def delete_file(self, path: str, message: str, branch: str, base_sha: str) -> dict:
        body = {"message": message, "sha": base_sha, "branch": branch}
        return self._check(
            self._session.delete(self._repo_url(f"contents/{path}"), json=body, timeout=60)
        )
