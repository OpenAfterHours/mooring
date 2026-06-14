"""Filesystem locations for config, logs, and the notebook workspace."""

from __future__ import annotations

from pathlib import Path

import platformdirs

APP_NAME = "mooring"


def user_config_dir() -> Path:
    # roaming=True so the config follows the user profile on managed Windows networks
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True))


def user_config_file() -> Path:
    return user_config_dir() / "config.toml"


def user_log_dir() -> Path:
    return Path(platformdirs.user_log_dir(APP_NAME, appauthor=False))


def default_workspace(owner: str, repo: str) -> Path:
    # Keyed by owner AND repo so same-named repos under different owners
    # don't share a workspace.
    return Path(platformdirs.user_documents_dir()) / APP_NAME / owner / repo


def legacy_workspace(repo: str) -> Path:
    """The pre-multi-repo default (keyed by repo name only), kept for hints."""
    return Path(platformdirs.user_documents_dir()) / APP_NAME / repo


def synced_folder_provider(workspace: Path) -> str:
    """Name of the cloud-sync service the workspace sits inside, or "" — these
    revert/merge files (including .mooring/manifest.json) behind mooring's back,
    which corrupts sync state. Windows redirects Documents into OneDrive, so the
    default workspace silently lands there. Matched conservatively per path
    component to avoid false positives (e.g. "sandbox", "toolbox")."""
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
