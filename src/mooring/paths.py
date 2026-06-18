"""Filesystem locations for config, logs, and the notebook workspace."""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path

import platformdirs

APP_NAME = "mooring"

# On Windows os.replace raises PermissionError if another process holds the TARGET
# open without share-delete — antivirus, the Search indexer, or a cloud-sync agent
# routinely do this for a few ms. A short bounded retry turns those transient
# sharing-violations into success instead of a spurious "could not apply".
_REPLACE_ATTEMPTS = 5


def _replace_with_retry(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def safe_write_bytes(path: Path | str, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` (temp sibling + ``os.replace``).

    The single audited replacement for the tmp-then-``os.replace`` idiom dotted
    around the codebase (manifest/config_store/editor). Crash-safe and, crucially,
    safe against a concurrent reader: the marimo editor's ``--watch`` may read a
    notebook ``.py`` mid-write, and a partial ``write_text`` could corrupt the open
    tab or trip marimo's parser — ``os.replace`` swaps the file in one step instead.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def safe_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically write ``text`` as plain ``encoding`` (no BOM, ``\\n`` preserved).

    Used for the notebook ``.py`` write on Apply: the marimo parser rejects a BOM,
    so this encodes without one and writes the bytes verbatim (no platform newline
    translation) via :func:`safe_write_bytes`.
    """
    safe_write_bytes(path, text.encode(encoding))


def user_config_dir() -> Path:
    # roaming=True so the config follows the user profile on managed Windows networks
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True))


def user_config_file() -> Path:
    return user_config_dir() / "config.toml"


def user_log_dir() -> Path:
    return Path(platformdirs.user_log_dir(APP_NAME, appauthor=False))


def default_workspace(owner: str, repo: str) -> Path:
    # Keyed by owner AND repo so same-named repos under different owners
    # don't share a workspace. Lives under ~/PythonProjects in the user's home
    # directory rather than Documents, which Windows redirects into OneDrive
    # where cloud sync corrupts mooring's state (see synced_folder_provider).
    return Path.home() / "PythonProjects" / APP_NAME / owner / repo


def legacy_workspaces(owner: str, repo: str) -> tuple[Path, ...]:
    """Past default workspace locations (under Documents), newest first, kept so
    we can hint existing users to migrate to the current default_workspace():
    the owner-keyed Documents default, then the pre-multi-repo repo-only key."""
    docs = Path(platformdirs.user_documents_dir()) / APP_NAME
    return (docs / owner / repo, docs / repo)


def synced_folder_provider(workspace: Path) -> str:
    """Name of the cloud-sync service the workspace sits inside, or "" — these
    revert/merge files (including .mooring/manifest.json) behind mooring's back,
    which corrupts sync state. The default workspace lives under ~/PythonProjects
    to steer clear of these, but a user-set 'workspace' can still land in one.
    Matched conservatively per path component to avoid false positives
    (e.g. "sandbox", "toolbox")."""
    for part in (p.lower() for p in workspace.parts):
        if part.startswith("onedrive"):  # "OneDrive", "OneDrive - Contoso"
            return "OneDrive"
        if part == "dropbox":
            return "Dropbox"
        if part in ("google drive", "googledrive", "my drive"):
            return "Google Drive"
        if part in ("box", "box sync"):
            return "Box"
        if "icloud" in part:  # "iCloudDrive", "com~apple~CloudDocs"
            return "iCloud"
    return ""


def synced_folder_hint(workspace: Path) -> str:
    provider = synced_folder_provider(workspace)
    if not provider:
        return ""
    return (
        f"This workspace is inside {provider}. Cloud sync can revert or merge "
        "mooring's files behind its back and corrupt sync state — move it to a "
        "local folder (set MOORING_WORKSPACE, or the repo's 'workspace' path)."
    )
