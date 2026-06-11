"""Sync engine tests against an in-memory fake of the GitHub client."""

import pytest

from mooring import gitsha, manifest, sync
from mooring.config import Config
from mooring.github import RemoteConflict, TreeEntry
from mooring.sync import ConflictStrategy, FileState, classify


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


# -- the decision matrix -----------------------------------------------------


@pytest.mark.parametrize(
    ("base", "local", "remote", "expected"),
    [
        (None, None, None, None),
        ("b", None, None, None),  # stale manifest entry
        (None, "x", None, FileState.NEW_LOCAL),
        (None, None, "x", FileState.NEW_REMOTE),
        (None, "x", "x", FileState.SYNCED),
        (None, "x", "y", FileState.CONFLICT),  # created independently on both sides
        ("b", "b", "b", FileState.SYNCED),
        ("b", "b", "r", FileState.REMOTE_CHANGED),
        ("b", "b", None, FileState.DELETED_REMOTE),
        ("b", "l", "b", FileState.MODIFIED),
        ("b", None, "b", FileState.DELETED_LOCAL),
        ("b", "x", "x", FileState.SYNCED),  # same change on both sides
        ("b", "l", "r", FileState.CONFLICT),
        ("b", None, "r", FileState.CONFLICT),  # deleted here, changed there
        ("b", "l", None, FileState.CONFLICT),  # changed here, deleted there
    ],
)
def test_classify_matrix(base, local, remote, expected):
    assert classify(base, local, remote) is expected


# -- pull ---------------------------------------------------------------------


def test_pull_into_empty_workspace(cfg):
    client = FakeClient(
        {"notebooks/a.py": b"print(1)\n", "data/x.csv": b"a,b\n1,2\n", "README.md": b"no"}
    )
    result = sync.pull(client, cfg)
    assert result.pulled == 2  # README.md is outside the synced folders
    assert read_local(cfg, "notebooks/a.py") == "print(1)\n"
    mft = manifest.load(cfg.workspace())
    assert set(mft.files) == {"notebooks/a.py", "data/x.csv"}
    assert mft.head_commit == client.head


def test_pull_is_idempotent(cfg):
    client = FakeClient({"notebooks/a.py": b"print(1)\n"})
    sync.pull(client, cfg)
    result = sync.pull(client, cfg)
    assert result.pulled == 0
    assert result.summary() == "already up to date"


def test_pull_applies_remote_update_and_delete(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n", "notebooks/b.py": b"keep\n"})
    sync.pull(client, cfg)
    client.seed("notebooks/a.py", b"v2\n")
    client.remove("notebooks/b.py")
    result = sync.pull(client, cfg)
    assert result.pulled == 2
    assert read_local(cfg, "notebooks/a.py") == "v2\n"
    assert not (cfg.workspace() / "notebooks/b.py").exists()


def test_pull_never_overwrites_local_edits_by_default(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")
    result = sync.pull(client, cfg)
    assert result.skipped_conflicts == ["notebooks/a.py"]
    assert read_local(cfg, "notebooks/a.py") == "mine\n"


def test_pull_theirs_overwrites(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")
    sync.pull(client, cfg, strategy=ConflictStrategy.THEIRS)
    assert read_local(cfg, "notebooks/a.py") == "theirs\n"
    # resolved: file is in sync again
    assert sync.status(client, cfg).by_state(FileState.CONFLICT) == []


def test_pull_keep_both_saves_remote_copy_and_keeps_mine_pushable(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")
    sync.pull(client, cfg, strategy=ConflictStrategy.KEEP_BOTH)
    assert read_local(cfg, "notebooks/a.py") == "mine\n"
    copies = list((cfg.workspace() / "notebooks").glob("a.remote-*.py"))
    assert len(copies) == 1
    assert copies[0].read_text("utf-8") == "theirs\n"
    # my version is now MODIFIED against the new remote base — pushable
    report = sync.status(client, cfg)
    assert [f.state for f in report.files if f.path == "notebooks/a.py"] == [FileState.MODIFIED]


# -- push -----------------------------------------------------------------------


def test_push_new_and_modified_files(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "v2\n")
    write_local(cfg, "notebooks/new.py", "fresh\n")
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 2
    assert client.blobs[client.tree["notebooks/a.py"]] == b"v2\n"
    assert client.blobs[client.tree["notebooks/new.py"]] == b"fresh\n"
    # baselines updated: everything in sync now
    assert sync.status(client, cfg).summary().startswith("2 in sync, 0 to push")


def test_push_local_delete(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n", "notebooks/b.py": b"v1\n"})
    sync.pull(client, cfg)
    (cfg.workspace() / "notebooks/b.py").unlink()
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 1
    assert "notebooks/b.py" not in client.tree
    assert "notebooks/b.py" not in manifest.load(cfg.workspace()).files


def test_push_blocks_conflicts(cfg):
    client = FakeClient({"notebooks/a.py": b"v1\n"})
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 0
    assert result.blocked_conflicts == ["notebooks/a.py"]
    assert client.blobs[client.tree["notebooks/a.py"]] == b"theirs\n"  # untouched


def test_push_refuses_oversized_files(cfg):
    client = FakeClient()
    big = b"x" * (2 * 1024 * 1024)
    write_local(cfg, "data/big.bin", "")
    (cfg.workspace() / "data/big.bin").write_bytes(big)
    small_cfg = Config(
        client_id="cid", owner="acme", repo="nbs",
        workspace_path=cfg.workspace_path, max_file_mb=1,
    )
    result = sync.push(client, small_cfg, sleep=lambda s: None)
    assert result.pushed == 0
    assert "data/big.bin" not in client.tree
    assert any("refused" in line for line in result.lines)


def test_push_specific_paths_only(cfg):
    client = FakeClient()
    write_local(cfg, "notebooks/a.py", "a\n")
    write_local(cfg, "notebooks/b.py", "b\n")
    result = sync.push(client, cfg, paths=["notebooks/a.py"], sleep=lambda s: None)
    assert result.pushed == 1
    assert "notebooks/a.py" in client.tree
    assert "notebooks/b.py" not in client.tree


# -- resolve --------------------------------------------------------------------


def _make_conflict(cfg, client):
    client.seed("notebooks/a.py", b"v1\n")
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/a.py", "mine\n")
    client.seed("notebooks/a.py", b"theirs\n")


def test_resolve_push_copy(cfg):
    client = FakeClient()
    _make_conflict(cfg, client)
    result = sync.resolve(
        client, cfg, "notebooks/a.py", ConflictStrategy.PUSH_COPY, username="phil"
    )
    assert result.pushed == 1
    # my version published under a new name, original restored to remote
    assert client.blobs[client.tree["notebooks/a-phil.py"]] == b"mine\n"
    assert read_local(cfg, "notebooks/a.py") == "theirs\n"
    assert read_local(cfg, "notebooks/a-phil.py") == "mine\n"
    assert sync.status(client, cfg).by_state(FileState.CONFLICT) == []


def test_resolve_theirs(cfg):
    client = FakeClient()
    _make_conflict(cfg, client)
    sync.resolve(client, cfg, "notebooks/a.py", ConflictStrategy.THEIRS)
    assert read_local(cfg, "notebooks/a.py") == "theirs\n"
    assert sync.status(client, cfg).by_state(FileState.CONFLICT) == []


# -- hygiene ----------------------------------------------------------------------


def test_scan_skips_scratch_and_hidden_files(cfg):
    write_local(cfg, "notebooks/a.py", "a\n")
    write_local(cfg, "notebooks/a.remote-1234567.py", "scratch\n")
    write_local(cfg, "notebooks/__pycache__/a.cpython-312.pyc", "junk")
    write_local(cfg, "notebooks/.hidden", "junk")
    found = sync.scan_local(cfg.workspace(), cfg.folders)
    assert set(found) == {"notebooks/a.py"}


def test_crlf_local_file_counts_as_synced(cfg):
    client = FakeClient({"notebooks/a.py": b"line1\nline2\n"})
    sync.pull(client, cfg)
    # simulate a Windows editor rewriting the file with CRLF
    (cfg.workspace() / "notebooks/a.py").write_bytes(b"line1\r\nline2\r\n")
    report = sync.status(client, cfg)
    assert [f.state for f in report.files] == [FileState.SYNCED]
