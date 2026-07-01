"""Reveal a file in the OS file manager (Windows Explorer).

This is how a user opens a non-marimo ``.py`` — a plain helper module — from the hub.
mooring never routes a module through the marimo editor: marimo would rewrite it into
notebook form on save (and, under ``--watch`` autorun, execute it). Handing the file to
the file manager instead lets the user edit it in their own editor; the edit then
change-detects and pushes like any other file (sync is type-agnostic).

Selecting the file inside its folder (rather than launching the ``.py`` itself) also
sidesteps the Windows trap where the default shell verb for a ``.py`` *runs* the script
rather than editing it.

Windows-only for now, mirroring :func:`mooring.pbip.launch`; a POSIX file-manager
handoff (``xdg-open`` / macOS ``open -R``) is deferred. A stdlib-pure leaf (L0): it
imports nothing else in mooring.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class RevealError(Exception):
    pass


def reveal(path: Path) -> None:
    """Open Windows Explorer with ``path`` selected inside its containing folder.

    ``path`` must be an existing, already workspace-validated path (the caller confirms
    containment). Raises :class:`RevealError` off Windows or if Explorer can't launch.
    """
    if os.name != "nt":
        raise RevealError("Revealing a file in the file manager needs Windows.")
    target = str(Path(path).resolve())
    try:
        # `explorer /select,<abs path>` opens the folder with the file highlighted.
        # explorer returns a non-zero exit code even on success, so launch it
        # fire-and-forget (Popen, no wait) and never inspect the return code. The
        # argv list (shell=False) means the resolved path can't be interpreted as a
        # command, so a crafted filename can't inject.
        subprocess.Popen(["explorer", f"/select,{target}"])  # noqa: S603,S607
    except OSError as exc:
        raise RevealError(f"Could not reveal {Path(path).name}: {exc}") from exc
