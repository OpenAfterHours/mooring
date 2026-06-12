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
