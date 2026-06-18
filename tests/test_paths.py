"""Workspace-location hints (cloud-sync folder detection)."""

import os
from pathlib import Path

import pytest

from mooring import paths


@pytest.mark.parametrize(
    ("workspace", "provider"),
    [
        ("C:/Users/phil/OneDrive/Documents/mooring/acme/nbs", "OneDrive"),
        ("C:/Users/phil/OneDrive - Contoso/Documents/mooring/nbs", "OneDrive"),
        ("C:/Users/phil/Dropbox/mooring/nbs", "Dropbox"),
        ("G:/My Drive/mooring/nbs", "Google Drive"),
        ("C:/Users/phil/Box/mooring/nbs", "Box"),
        ("C:/Users/phil/iCloudDrive/mooring/nbs", "iCloud"),
        # local paths and lookalikes must NOT trip the heuristic
        ("C:/Users/phil/Documents/mooring/nbs", ""),
        ("/home/phil/projects/sandbox/mooring/nbs", ""),  # 'sandbox' != 'box'
        ("C:/dev/toolbox/mooring/nbs", ""),
    ],
)
def test_synced_folder_provider(workspace, provider):
    assert paths.synced_folder_provider(Path(workspace)) == provider


def test_synced_folder_hint_text():
    hint = paths.synced_folder_hint(Path("C:/Users/phil/OneDrive/Documents/mooring/nbs"))
    assert "OneDrive" in hint
    assert "MOORING_WORKSPACE" in hint
    assert paths.synced_folder_hint(Path("C:/dev/mooring/nbs")) == ""


# -- atomic writes ------------------------------------------------------------


def test_safe_write_text_writes_utf8_no_bom_and_overwrites(tmp_path):
    p = tmp_path / "f.py"
    paths.safe_write_text(p, "first = 1\n")
    paths.safe_write_text(p, "second = 2\n")
    assert p.read_text("utf-8") == "second = 2\n"
    assert not p.read_bytes().startswith(b"\xef\xbb\xbf")


def test_safe_write_text_preserves_lf_newlines(tmp_path):
    # No platform newline translation — the marimo codegen emits \n and we keep it.
    p = tmp_path / "f.py"
    paths.safe_write_text(p, "a\nb\nc\n")
    assert p.read_bytes() == b"a\nb\nc\n"


def test_safe_write_bytes_is_exact(tmp_path):
    p = tmp_path / "f.bin"
    paths.safe_write_bytes(p, b"\x00\x01raw\r\nbytes")
    assert p.read_bytes() == b"\x00\x01raw\r\nbytes"


def test_safe_write_leaves_no_temp_files(tmp_path):
    paths.safe_write_text(tmp_path / "f.py", "x = 1\n")
    assert [p.name for p in tmp_path.iterdir()] == ["f.py"]  # tmp sibling was renamed away


def test_safe_write_retries_transient_permission_error(tmp_path, monkeypatch):
    # A few transient Windows sharing-violations (AV/indexer/cloud-sync) must not fail
    # the write — os.replace is retried, then succeeds.
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr(paths.os, "replace", flaky)
    paths.safe_write_bytes(tmp_path / "f.bin", b"data")
    assert (tmp_path / "f.bin").read_bytes() == b"data"
    assert calls["n"] == 3  # failed twice, succeeded on the third


def test_safe_write_reraises_persistent_permission_error(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("x")))
    with pytest.raises(PermissionError):
        paths.safe_write_bytes(tmp_path / "f.bin", b"data")
    assert list(tmp_path.iterdir()) == []  # the temp sibling was cleaned up
