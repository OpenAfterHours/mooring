"""Shared fixtures: an in-memory fake of the GitHub client and a tmp workspace."""

import pytest

from mooring import gitsha
from mooring.config import Config
from mooring.github import NotFound, RefAlreadyExists, RemoteConflict, TreeEntry

DEFAULT_BRANCH = "main"


class FakeClient:
    def __init__(self, files: dict[str, bytes] | None = None):
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {DEFAULT_BRANCH: {}}
        self.commit_count = 0
        self.heads: dict[str, str] = {DEFAULT_BRANCH: "head-0"}
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
        self._advance(DEFAULT_BRANCH)

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

    def _advance(self, branch: str) -> str:
        self.commit_count += 1
        self.heads[branch] = f"head-{self.commit_count}"
        return self.heads[branch]

    # -- GitHubClient interface ------------------------------------------------

    def get_user(self):
        return {"login": "phil"}

    def get_branch_head(self, branch):
        if branch not in self.heads:
            raise NotFound(f"branch {branch}")
        return self.heads[branch]

    def get_tree(self, commit_sha, folders):
        tree = self.tree
        for branch, head in self.heads.items():
            if head == commit_sha:
                tree = self.trees[branch]
                break
        prefixes = tuple(f"{f}/" for f in folders)
        return [
            TreeEntry(p, s, len(self.blobs[s]))
            for p, s in tree.items()
            if p.startswith(prefixes)
        ]

    def get_blob(self, sha):
        return self.blobs[sha]

    def create_ref(self, branch, sha):
        if branch in self.trees:
            raise RefAlreadyExists(f"branch {branch} already exists")
        source = next(b for b, h in self.heads.items() if h == sha)
        self.trees[branch] = dict(self.trees[source])
        self.heads[branch] = sha
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}

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
        return {"content": {"sha": sha}, "commit": {"sha": self._advance(branch)}}

    def delete_file(self, path, message, branch, base_sha):
        if branch not in self.trees:
            raise NotFound(f"branch {branch}")
        tree = self.trees[branch]
        if tree.get(path) != base_sha:
            raise RemoteConflict("remote changed")
        del tree[path]
        return {"commit": {"sha": self._advance(branch)}}


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
