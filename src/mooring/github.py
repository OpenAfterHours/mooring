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
from urllib.parse import quote

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


class Unreachable(GitHubError):
    """GitHub could not be reached at the transport level — no HTTP response at
    all (offline, DNS failure, connection refused, timeout). Deliberately
    conservative: anything that DID produce an HTTP response classifies in
    ``_check`` instead (a 401 must stay :class:`AuthFailed` — a fixable auth
    problem is never hidden behind an "offline" banner)."""


class TlsFailure(Unreachable):
    """The TLS handshake to GitHub failed — the corporate proxy-interception
    signature (the proxy's root CA is missing from the trust store)."""


def compare_url(
    owner: str, repo: str, base: str, branch: str, host: str = githost.DEFAULT_HOST
) -> str:
    """GitHub's compare page for opening a pull request from `branch` into `base`."""
    return f"{githost.web_root(host)}/{owner}/{repo}/compare/{base}...{branch}?expand=1"


def blob_url(
    owner: str, repo: str, branch: str, path: str, host: str = githost.DEFAULT_HOST
) -> str:
    """GitHub's web view of `path` at `branch` HEAD — the file's ``blob/`` page.

    `path` is a repo-relative POSIX path; its segments are percent-encoded while the
    ``/`` separators are preserved. The page shows the file as it exists on the REMOTE
    branch, which can differ from the local working copy — so callers gate on the file
    actually existing remotely (a non-null remote blob sha)."""
    return f"{githost.web_root(host)}/{owner}/{repo}/blob/{branch}/{quote(path, safe='/')}"


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

    def get_full_tree(self, commit_sha: str) -> list[TreeEntry]: ...

    def get_blob(self, sha: str) -> bytes: ...

    def list_commits_for_path(
        self, path: str, branch: str, page: int = 1, per_page: int = 30
    ) -> list[dict]: ...

    def compare(self, base: str, head: str) -> dict: ...

    def get_file_at(self, path: str, ref: str) -> tuple[str, bytes]: ...

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

    def list_open_pulls(self) -> list[dict]: ...

    def get_pull(self, number: int) -> dict: ...

    def list_pull_files(self, number: int) -> list[dict]: ...

    def submit_review(self, number: int, event: str, body: str = "") -> dict: ...

    def find_open_pull(self, head_ref: str, base: str | None = None) -> dict | None: ...

    def create_pull(self, title: str, head: str, base: str, body: str = "") -> dict: ...


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

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        """Every session call goes through here, so a transport failure (no HTTP
        response at all) classifies into a typed error exactly once. HTTP-status
        classification stays entirely in :meth:`_check`. SSLError is caught first:
        under requests it subclasses ConnectionError, and the TLS diagnosis is the
        more specific (and more actionable) one."""
        try:
            return self._session.request(method, url, **kwargs)
        except requests.exceptions.SSLError as exc:
            raise TlsFailure(
                "Could not make a secure (TLS) connection to GitHub — a corporate "
                "proxy may be intercepting traffic. Try `mooring doctor`."
            ) from exc
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            raise Unreachable(
                "GitHub is unreachable — check your network connection and try again."
            ) from exc
        except requests.RequestException as exc:
            raise GitHubError(f"GitHub request failed: {exc}") from exc

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
        return self._check(self._send("GET", f"{self.api_root}/user", timeout=30))

    def get_branch_head(self, branch: str) -> str:
        data = self._check(self._send("GET", self._repo_url(f"git/ref/heads/{branch}"), timeout=30))
        return data["object"]["sha"]

    def get_tree(
        self,
        commit_sha: str,
        folders: tuple[str, ...],
        extra_paths: tuple[str, ...] = (),
    ) -> list[TreeEntry]:
        prefixes = tuple(f"{f.rstrip('/')}/" for f in folders)
        extra = frozenset(extra_paths)
        entries = []
        for e in self.get_full_tree(commit_sha):
            if not (e.path.startswith(prefixes) or e.path in extra):
                continue
            # SHA-256 object-format repos can't be synced (their blob shas are 64 hex).
            # Checked HERE, on the in-scope entries only, so a repo whose synced folders
            # happen to be empty reports an empty tree rather than erroring — the same
            # behaviour as before get_tree delegated to get_full_tree.
            if len(e.sha) != 40:
                raise GitHubError("SHA-256 object-format repos are not supported.")
            entries.append(e)
        return entries

    def get_full_tree(self, commit_sha: str) -> list[TreeEntry]:
        """Every blob in the commit's tree (recursive), UNFILTERED — the discovery
        read behind :func:`mooring.sync.discover_unsynced_folders`. ``get_tree`` is the
        same fetch narrowed to the synced folders (and is where the SHA-256 guard runs,
        on the in-scope entries), so the two can never disagree about the tree."""
        commit = self._check(
            self._send("GET", self._repo_url(f"git/commits/{commit_sha}"), timeout=30)
        )
        data = self._check(
            self._send(
                "GET",
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
        return [
            TreeEntry(item["path"], item["sha"], item.get("size", 0))
            for item in data.get("tree", [])
            if item["type"] == "blob"
        ]

    def get_blob(self, sha: str) -> bytes:
        data = self._check(self._send("GET", self._repo_url(f"git/blobs/{sha}"), timeout=120))
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"])
        return data.get("content", "").encode()

    def list_commits_for_path(
        self, path: str, branch: str, page: int = 1, per_page: int = 30
    ) -> list[dict]:
        """One page of the commits that touched ``path`` on ``branch``, newest
        first (the file's version history). The commits-list API paginates and
        does not follow renames — history for a renamed file starts at the
        rename. Returns the raw commit dicts; callers shape them."""
        data = self._check(
            self._send(
                "GET",
                self._repo_url("commits"),
                params={"path": path, "sha": branch, "per_page": per_page, "page": page},
                timeout=30,
            )
        )
        return data if isinstance(data, list) else []

    def compare(self, base: str, head: str) -> dict:
        """The commits and changed files on ``base...head`` — the whole horizon
        window in one request (the pull digest's primary read). Returns the raw
        dict (``commits`` oldest-first, ``files``, ``total_commits``); callers
        shape it. The API caps its answer (~250 commits listed, 300 files), so
        callers must treat ``total_commits > len(commits)`` or a full ``files``
        page as a truncated window and degrade. A GC'd or force-pushed ``base``
        404s, which :meth:`_check` maps to :class:`NotFound` ("anchor lost")."""
        return self._check(
            self._send("GET", self._repo_url(f"compare/{base}...{head}"), timeout=60)
        )

    def get_file_at(self, path: str, ref: str) -> tuple[str, bytes]:
        """``(blob_sha, bytes)`` of ``path`` as it existed at ``ref``.

        One request in the common case — the contents API inlines base64 content
        up to ~1 MB — falling back to :meth:`get_blob` for larger files. Cheaper
        than walking a full historic tree. Raises :class:`NotFound` when the
        path did not exist at that ref."""
        data = self._check(
            self._send(
                "GET",
                self._repo_url(f"contents/{quote(path, safe='/')}"),
                params={"ref": ref},
                timeout=60,
            )
        )
        if isinstance(data, list):  # a directory, not a file
            raise NotFound(f"{path} is a directory at {ref}")
        sha = data.get("sha", "")
        if data.get("encoding") == "base64" and data.get("content"):
            return sha, base64.b64decode(data["content"])
        return sha, self.get_blob(sha)

    # -- writes (Contents API, one commit per file) ---------------------------

    def create_ref(self, branch: str, sha: str) -> dict:
        resp = self._send(
            "POST",
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
            self._send("PUT", self._repo_url(f"contents/{path}"), json=body, timeout=120)
        )

    def delete_file(self, path: str, message: str, branch: str, base_sha: str) -> dict:
        body = {"message": message, "sha": base_sha, "branch": branch}
        return self._check(
            self._send("DELETE", self._repo_url(f"contents/{path}"), json=body, timeout=60)
        )

    # -- pull requests (the reviewer inbox + propose auto-open) --------------

    def _get_all(self, tail: str, cap_pages: int = 30) -> list[dict]:
        """GET a list endpoint, following ``?page=N`` until a short (final) page or the
        cap — so a review is never computed from a silently truncated first page. ``tail``
        already carries ``per_page=100`` + any other query. All PR operations fall under
        the ``repo`` scope this client already holds."""
        out: list[dict] = []
        page = 1
        sep = "&" if "?" in tail else "?"
        while page <= cap_pages:
            data = self._check(
                self._send("GET", self._repo_url(f"{tail}{sep}page={page}"), timeout=30)
            )
            if not isinstance(data, list) or not data:
                break
            out.extend(data)
            if len(data) < 100:  # a short page is the last page
                break
            page += 1
        return out

    def list_open_pulls(self) -> list[dict]:
        """Open pull requests, most-recently-updated first (all pages). Returns the raw
        PR dicts."""
        return self._get_all("pulls?state=open&per_page=100&sort=updated&direction=desc")

    def get_pull(self, number: int) -> dict:
        """One pull request — carries ``base``/``head`` with their immutable ``sha``,
        so a review diffs the exact snapshot the PR proposes."""
        return self._check(self._send("GET", self._repo_url(f"pulls/{number}"), timeout=30))

    def list_pull_files(self, number: int) -> list[dict]:
        """Every file changed in PR ``number`` (all pages) — each ``{filename, status,
        previous_filename?, patch?, …}`` (``patch`` is GitHub's unified diff, absent for
        binary/oversized files)."""
        return self._get_all(f"pulls/{number}/files?per_page=100")

    def submit_review(self, number: int, event: str, body: str = "") -> dict:
        """Submit a review on PR ``number``: ``event`` is ``APPROVE`` /
        ``REQUEST_CHANGES`` / ``COMMENT`` (GitHub requires a ``body`` for the latter
        two). GitHub rejects approving your OWN PR with a 422 (surfaced as GitHubError)."""
        payload: dict = {"event": event}
        if body.strip():
            payload["body"] = body
        return self._check(
            self._send("POST", self._repo_url(f"pulls/{number}/reviews"), json=payload, timeout=30)
        )

    def find_open_pull(self, head_ref: str, base: str | None = None) -> dict | None:
        """The open PR whose head is ``head_ref`` — and, when given, whose base is
        ``base`` — on THIS repo, or ``None``. Filtering by BASE too matters: a review
        branch can have an unrelated PR open into a different base (e.g. ``develop``), and
        returning that would point the author at the wrong PR."""
        query = f"pulls?state=open&head={quote(f'{self.owner}:{head_ref}', safe='')}&per_page=1"
        if base:
            query += f"&base={quote(base, safe='')}"
        data = self._check(self._send("GET", self._repo_url(query), timeout=30))
        if not (isinstance(data, list) and data):
            return None
        pr = data[0]
        if base and (pr.get("base") or {}).get("ref") != base:
            return None  # never claim a PR into a different base than the caller asked for
        return pr

    def create_pull(self, title: str, head: str, base: str, body: str = "") -> dict:
        """Open a pull request from ``head`` into ``base`` and return it. If one is
        already open for exactly this ``head``->``base`` (a race with a concurrent
        propose), return it instead of failing. Only an "already exists" 422 triggers that
        fallback — any OTHER 422 (e.g. "No commits between…") is a real error and raised."""
        payload = {"title": title, "head": head, "base": base, "body": body}
        resp = self._send("POST", self._repo_url("pulls"), json=payload, timeout=30)
        if resp.status_code == 422:
            message = ""
            try:
                message = str(resp.json().get("message", ""))
            except ValueError:
                pass
            if "already exist" in message.lower():
                existing = self.find_open_pull(head, base=base)
                if existing is not None:
                    return existing
        return self._check(resp)  # a new PR, or raise (incl. a non-already-exists 422)
