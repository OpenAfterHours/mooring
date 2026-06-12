"""Shared fixtures: an in-memory fake of the GitHub client and a tmp workspace."""

import pytest

from mooring import gitsha
from mooring.config import Config
from mooring.github import RemoteConflict, TreeEntry


class FakeClient:
    def __init__(self, files: dict[str, bytes] | None = None):
        self.blobs: dict[str, bytes] = {}
        self.tree: dict[str, str] = {}
        self.commit_count = 0
        self.head = "head-0"
        for path, data in (files or {}).items():
            self.seed(path, data)

    def seed(self, path: str, data: bytes) -> None:
        """Simulate someone else pushing to the repo."""
        sha = gitsha.blob_sha(data)
        self.blobs[sha] = data
        self.tree[path] = sha
        self._advance()

    def remove(self, path: str) -> None:
        del self.tree[path]
        self._advance()

    def _advance(self) -> str:
        self.commit_count += 1
        self.head = f"head-{self.commit_count}"
        return self.head

    # -- GitHubClient interface ------------------------------------------------

    def get_user(self):
        return {"login": "phil"}

    def get_branch_head(self, branch):
        return self.head

    def get_tree(self, commit_sha, folders):
        prefixes = tuple(f"{f}/" for f in folders)
        return [
            TreeEntry(p, s, len(self.blobs[s]))
            for p, s in self.tree.items()
            if p.startswith(prefixes)
        ]

    def get_blob(self, sha):
        return self.blobs[sha]

    def put_file(self, path, content, message, branch, base_sha=None):
        current = self.tree.get(path)
        if base_sha is None and current is not None:
            raise RemoteConflict("file already exists")
        if base_sha is not None and current != base_sha:
            raise RemoteConflict("remote changed")
        sha = gitsha.blob_sha(content)
        self.blobs[sha] = content
        self.tree[path] = sha
        return {"content": {"sha": sha}, "commit": {"sha": self._advance()}}

    def delete_file(self, path, message, branch, base_sha):
        if self.tree.get(path) != base_sha:
            raise RemoteConflict("remote changed")
        del self.tree[path]
        return {"commit": {"sha": self._advance()}}


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
