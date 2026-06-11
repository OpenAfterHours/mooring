"""Layered configuration: packaged defaults <- user config file <- environment."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from mooring import paths


@dataclass(frozen=True)
class Config:
    client_id: str = ""
    owner: str = ""
    repo: str = ""
    branch: str = "main"
    folders: tuple[str, ...] = ("notebooks", "data")
    warn_file_mb: int = 10
    max_file_mb: int = 45
    workspace_path: str = ""

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.owner and self.repo)

    def workspace(self) -> Path:
        if self.workspace_path:
            return Path(self.workspace_path).expanduser()
        return paths.default_workspace(self.repo or "workspace")


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(
    user_config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    env = os.environ if env is None else env
    default_text = resources.files("mooring").joinpath("config_default.toml").read_text("utf-8")
    data = tomllib.loads(default_text)
    path = user_config_path if user_config_path is not None else paths.user_config_file()
    if path.is_file():
        data = _merge(data, tomllib.loads(path.read_text("utf-8")))
    gh = data.get("github", {})
    sync = data.get("sync", {})
    ws = data.get("workspace", {})
    return Config(
        client_id=env.get("MOORING_CLIENT_ID", gh.get("client_id", "")),
        owner=env.get("MOORING_OWNER", gh.get("owner", "")),
        repo=env.get("MOORING_REPO", gh.get("repo", "")),
        branch=env.get("MOORING_BRANCH", gh.get("branch", "main")),
        folders=tuple(sync.get("folders", ("notebooks", "data"))),
        warn_file_mb=int(sync.get("warn_file_mb", 10)),
        max_file_mb=int(sync.get("max_file_mb", 45)),
        workspace_path=env.get("MOORING_WORKSPACE", ws.get("path", "")),
    )
