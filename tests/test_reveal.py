"""The file-manager reveal launcher (used to open a non-marimo .py in the user's own
editor). Windows-only, mirroring pbip.launch; no real Explorer is spawned in tests."""

from pathlib import Path

import pytest

from mooring import reveal


def test_reveal_off_windows_raises(monkeypatch):
    monkeypatch.setattr(reveal.os, "name", "posix")
    with pytest.raises(reveal.RevealError):
        reveal.reveal(Path("notebooks/helpers.py"))


def test_reveal_selects_the_file_in_explorer(monkeypatch, tmp_path):
    monkeypatch.setattr(reveal.os, "name", "nt")
    target = tmp_path / "notebooks" / "helpers.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", "utf-8")
    calls = []
    monkeypatch.setattr(reveal.subprocess, "Popen", lambda args: calls.append(args))
    reveal.reveal(target)
    assert calls == [["explorer", f"/select,{target.resolve()}"]]


def test_reveal_wraps_os_error(monkeypatch, tmp_path):
    monkeypatch.setattr(reveal.os, "name", "nt")

    def boom(_args):
        raise OSError("explorer missing")

    monkeypatch.setattr(reveal.subprocess, "Popen", boom)
    with pytest.raises(reveal.RevealError):
        reveal.reveal(tmp_path / "x.py")
