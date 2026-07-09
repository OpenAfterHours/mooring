"""Shared fixtures: an in-memory fake of the GitHub client and a tmp workspace."""

import pytest

from mooring import gitsha
from mooring.config import Config
from mooring.github import NotFound, RefAlreadyExists, RemoteConflict, TreeEntry

DEFAULT_BRANCH = "main"


@pytest.fixture(autouse=True)
def _hermetic_telemetry(monkeypatch):
    """Keep telemetry off and isolated for the whole suite.

    With no MOORING_LOG_ENDPOINT, ``telemetry.configure()`` is a no-op, so the
    suite never POSTs or writes to the real log dir. Tests that want to exercise
    telemetry set MOORING_LOG_ENDPOINT to a tmp path themselves. The teardown
    drops the daemon/state so nothing leaks across tests.
    """
    monkeypatch.delenv("MOORING_LOG_ENDPOINT", raising=False)
    monkeypatch.delenv("MOORING_LOG_LEVEL", raising=False)
    monkeypatch.setenv("MOORING_TRUSTSTORE", "0")  # main() injects into global ssl
    yield
    from mooring import telemetry

    telemetry.flush(0.2)
    telemetry._reset_for_tests()


class FakeClient:
    def __init__(self, files: dict[str, bytes] | None = None):
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {DEFAULT_BRANCH: {}}
        self.commit_count = 0
        self.heads: dict[str, str] = {DEFAULT_BRANCH: "head-0"}
        # Every commit ever made, oldest first: (sha, branch, tree snapshot,
        # message) — what list_commits_for_path/get_file_at answer from.
        self.commit_log: list[dict] = [
            {"sha": "head-0", "branch": DEFAULT_BRANCH, "tree": {}, "message": "init"}
        ]
        # Open pull requests keyed by head branch (the reviewer-inbox / auto-open flow).
        self.pulls: dict[str, dict] = {}
        self._next_pull = 1
        self.create_pull_calls = 0
        for path, data in (files or {}).items():
            self.seed(path, data)

    # The bulk of the suite only cares about the default branch; keep the
    # original single-branch attribute names as views onto it.
    @property
    def tree(self) -> dict[str, str]:
        return self.trees[DEFAULT_BRANCH]

    @property
    def head(self) -> str:
        return self.heads[DEFAULT_BRANCH]

    def seed(self, path: str, data: bytes) -> None:
        """Simulate someone else pushing to the repo."""
        sha = gitsha.blob_sha(data)
        self.blobs[sha] = data
        self.tree[path] = sha
        self._advance(DEFAULT_BRANCH, message=f"Seed {path}")

    def remove(self, path: str) -> None:
        del self.tree[path]
        self._advance(DEFAULT_BRANCH)

    def merge(self, branch: str, into: str = DEFAULT_BRANCH) -> None:
        """Simulate a PR from `branch` getting merged on GitHub."""
        self.trees[into] = dict(self.trees[branch])
        self._advance(into)

    def delete_branch(self, branch: str) -> None:
        """Simulate a PR getting closed and its branch deleted on GitHub."""
        del self.trees[branch]
        del self.heads[branch]

    def _advance(self, branch: str, message: str = "update") -> str:
        self.commit_count += 1
        self.heads[branch] = f"head-{self.commit_count}"
        self.commit_log.append(
            {
                "sha": self.heads[branch],
                "branch": branch,
                "tree": dict(self.trees[branch]),
                "message": message,
            }
        )
        return self.heads[branch]

    # -- GitHubClient interface ------------------------------------------------

    def get_user(self):
        return {"login": "phil"}

    def get_branch_head(self, branch):
        if branch not in self.heads:
            raise NotFound(f"branch {branch}")
        return self.heads[branch]

    def get_tree(self, commit_sha, folders, extra_paths=(), include_root=False):
        prefixes = tuple(f"{f}/" for f in folders)
        extra = frozenset(extra_paths)
        return [
            e
            for e in self.get_full_tree(commit_sha)
            if e.path.startswith(prefixes)
            or e.path in extra
            or (include_root and "/" not in e.path)
        ]

    def get_full_tree(self, commit_sha):
        tree = self.tree
        for branch, head in self.heads.items():
            if head == commit_sha:
                tree = self.trees[branch]
                break
        return [TreeEntry(p, s, len(self.blobs[s])) for p, s in tree.items()]

    def get_blob(self, sha):
        return self.blobs[sha]

    def create_ref(self, branch, sha):
        if branch in self.trees:
            raise RefAlreadyExists(f"branch {branch} already exists")
        source = next(b for b, h in self.heads.items() if h == sha)
        self.trees[branch] = dict(self.trees[source])
        self.heads[branch] = sha
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}

    def find_open_pull(self, head_ref, base=None):
        pr = self.pulls.get(head_ref)
        if pr is None or (base and pr.get("base", {}).get("ref") != base):
            return None
        return pr

    def create_pull(self, title, head, base, body=""):
        self.create_pull_calls += 1
        if head in self.pulls:  # already open -> return it (a repeated propose)
            return self.pulls[head]
        pr = {
            "number": self._next_pull,
            "html_url": f"https://github.com/acme/nbs/pull/{self._next_pull}",
            "title": title,
            "head": {"ref": head},
            "base": {"ref": base},
        }
        self.pulls[head] = pr
        self._next_pull += 1
        return pr

    def put_file(self, path, content, message, branch, base_sha=None):
        if branch not in self.trees:
            raise NotFound(f"branch {branch}")
        tree = self.trees[branch]
        current = tree.get(path)
        if base_sha is None and current is not None:
            raise RemoteConflict("file already exists")
        if base_sha is not None and current != base_sha:
            raise RemoteConflict("remote changed")
        sha = gitsha.blob_sha(content)
        self.blobs[sha] = content
        tree[path] = sha
        return {"content": {"sha": sha}, "commit": {"sha": self._advance(branch, message)}}

    def list_commits_for_path(self, path, branch, page=1, per_page=30):
        """The commits (newest first) whose tree changed `path` on `branch` —
        the FakeClient's answer to the GitHub commits-list API."""
        entries = [c for c in self.commit_log if c["branch"] == branch]
        entries.reverse()  # newest first; parent of entries[i] is entries[i+1]
        touched = []
        for i, c in enumerate(entries):
            parent = entries[i + 1]["tree"] if i + 1 < len(entries) else {}
            if c["tree"].get(path) != parent.get(path):
                touched.append(
                    {
                        "sha": c["sha"],
                        "commit": {
                            "message": c["message"],
                            "author": {"name": "phil", "date": "2026-07-02T09:00:00Z"},
                        },
                        "author": {"login": "phil"},
                    }
                )
        start = (page - 1) * per_page
        return touched[start : start + per_page]

    def compare(self, base, head):
        """The GitHub compare API: the commits between two shas plus the
        aggregate file diff — answered from the recorded commit_log (commits
        oldest-first, like the real API) and its tree snapshots."""
        idx = {c["sha"]: i for i, c in enumerate(self.commit_log)}
        if base not in idx:
            raise NotFound(f"compare base {base}")
        if head not in idx:
            raise NotFound(f"compare head {head}")
        i, j = idx[base], idx[head]
        branch = self.commit_log[j]["branch"]
        window = [
            c for c in self.commit_log[i + 1 : j + 1] if c["branch"] == branch
        ]
        commits = [
            {
                "sha": c["sha"],
                "commit": {
                    "message": c["message"],
                    "author": {"name": "phil", "date": "2026-07-02T09:00:00Z"},
                },
                "author": {"login": "phil"},
            }
            for c in window
        ]
        base_tree = self.commit_log[i]["tree"]
        head_tree = self.commit_log[j]["tree"]
        files = []
        for path in sorted(set(base_tree) | set(head_tree)):
            b, h = base_tree.get(path), head_tree.get(path)
            if b == h:
                continue
            status = "added" if b is None else "removed" if h is None else "modified"
            files.append({"filename": path, "status": status})
        return {"commits": commits, "files": files, "total_commits": len(commits)}

    def get_file_at(self, path, ref):
        for c in self.commit_log:
            if c["sha"] == ref:
                sha = c["tree"].get(path)
                if sha is None:
                    raise NotFound(f"{path} at {ref}")
                return sha, self.blobs[sha]
        raise NotFound(f"ref {ref}")

    def delete_file(self, path, message, branch, base_sha):
        if branch not in self.trees:
            raise NotFound(f"branch {branch}")
        tree = self.trees[branch]
        if tree.get(path) != base_sha:
            raise RemoteConflict("remote changed")
        del tree[path]
        return {"commit": {"sha": self._advance(branch, message)}}


@pytest.fixture
def cfg(tmp_path):
    return Config(
        client_id="cid",
        owner="acme",
        repo="nbs",
        workspace_path=str(tmp_path / "ws"),
    )


def write_local(cfg, rel_path, text):
    target = cfg.workspace() / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8", newline="\n")


def read_local(cfg, rel_path):
    return (cfg.workspace() / rel_path).read_text("utf-8")
