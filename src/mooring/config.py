"""Layered configuration: packaged defaults <- user config file <- environment.

Two levels: AppConfig knows every registered repo and which one is active;
Config is the single-repo view that the sync/client layers consume. The
legacy single-[github] schema (v0.1) is still understood: when no [repos]
section exists, one repo is synthesized from [github] owner/repo/branch.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path

from mooring import githost, paths


@dataclass(frozen=True)
class Config:
    client_id: str = ""
    owner: str = ""
    repo: str = ""
    branch: str = "main"
    host: str = githost.DEFAULT_HOST
    folders: tuple[str, ...] = ("notebooks", "data", "reports")
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
        return paths.default_workspace(self.owner or "_", self.repo or "workspace")


@dataclass(frozen=True)
class RepoSpec:
    alias: str
    owner: str
    repo: str
    branch: str = "main"
    workspace_path: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class AppConfig:
    client_id: str = ""
    repos: tuple[RepoSpec, ...] = ()
    active_alias: str = ""
    host: str = githost.DEFAULT_HOST
    folders: tuple[str, ...] = ("notebooks", "data", "reports")
    warn_file_mb: int = 10
    max_file_mb: int = 45
    log_endpoint: str = ""
    log_level: str = "info"

    @property
    def aliases(self) -> list[str]:
        return [spec.alias for spec in self.repos]

    def spec(self, alias: str) -> RepoSpec:
        for s in self.repos:
            if s.alias == alias:
                return s
        raise KeyError(alias)

    def config_for(self, alias: str | None = None) -> Config:
        """The single-repo Config for an alias (None = the active repo).

        An app with no repos yields an unconfigured Config so callers can
        keep using cfg.is_configured.
        """
        if alias is None:
            if not self.repos:
                return Config(
                    client_id=self.client_id,
                    host=self.host,
                    folders=self.folders,
                    warn_file_mb=self.warn_file_mb,
                    max_file_mb=self.max_file_mb,
                )
            alias = self.active_alias
        s = self.spec(alias)
        return Config(
            client_id=self.client_id,
            owner=s.owner,
            repo=s.repo,
            branch=s.branch,
            host=self.host,
            folders=self.folders,
            warn_file_mb=self.warn_file_mb,
            max_file_mb=self.max_file_mb,
            workspace_path=s.workspace_path,
        )


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def merged_data(user_config_path: Path | None = None) -> dict:
    """Packaged defaults overlaid with the user config file, as raw TOML data."""
    default_text = resources.files("mooring").joinpath("config_default.toml").read_text("utf-8")
    data = tomllib.loads(default_text)
    path = user_config_path if user_config_path is not None else paths.user_config_file()
    if path.is_file():
        data = _merge(data, tomllib.loads(path.read_text("utf-8")))
    return data


def repo_specs_from_data(data: dict) -> tuple[tuple[RepoSpec, ...], str]:
    """Extract (repos, active_alias) from raw config data.

    A [repos] section, when present, is the whole truth and the legacy
    [github] owner/repo keys are ignored — that is what lets the user file
    drop a repo that the packaged default bakes in.
    """
    repos_data = data.get("repos")
    if isinstance(repos_data, dict):
        specs = tuple(
            RepoSpec(
                alias=alias,
                owner=str(tbl.get("owner", "")),
                repo=str(tbl.get("repo", "")),
                branch=str(tbl.get("branch", "main") or "main"),
                workspace_path=str(tbl.get("workspace", "")),
            )
            for alias, tbl in sorted(repos_data.items())
            if isinstance(tbl, dict)
        )
        active = str(repos_data.get("active", ""))
        if active not in {s.alias for s in specs}:
            active = specs[0].alias if specs else ""
        return specs, active
    gh = data.get("github", {})
    ws = data.get("workspace", {})
    if gh.get("owner") and gh.get("repo"):
        spec = RepoSpec(
            alias=str(gh["repo"]),
            owner=str(gh["owner"]),
            repo=str(gh["repo"]),
            branch=str(gh.get("branch", "main") or "main"),
            workspace_path=str(ws.get("path", "")),
        )
        return (spec,), spec.alias
    return (), ""


def load_app_config(
    user_config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    env = os.environ if env is None else env
    data = merged_data(user_config_path)
    gh = data.get("github", {})
    sync = data.get("sync", {})
    ws = data.get("workspace", {})
    log = data.get("logging", {})

    specs, active = repo_specs_from_data(data)
    if env.get("MOORING_ACTIVE_REPO") in {s.alias for s in specs}:
        active = env["MOORING_ACTIVE_REPO"]

    # Env vars override fields of the resolved active repo (v0.1 semantics:
    # field-wise, even on a partially configured app).
    overrides = {
        "owner": env.get("MOORING_OWNER"),
        "repo": env.get("MOORING_REPO"),
        "branch": env.get("MOORING_BRANCH"),
        "workspace_path": env.get("MOORING_WORKSPACE"),
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}
    if specs and overrides:
        specs = tuple(replace(s, **overrides) if s.alias == active else s for s in specs)
    elif not specs:
        owner = env.get("MOORING_OWNER", str(gh.get("owner", "")))
        repo = env.get("MOORING_REPO", str(gh.get("repo", "")))
        if owner or repo:
            spec = RepoSpec(
                alias=repo or owner,
                owner=owner,
                repo=repo,
                branch=env.get("MOORING_BRANCH", str(gh.get("branch", "main") or "main")),
                workspace_path=env.get("MOORING_WORKSPACE", str(ws.get("path", ""))),
            )
            specs, active = (spec,), spec.alias

    return AppConfig(
        client_id=env.get("MOORING_CLIENT_ID", gh.get("client_id", "")),
        repos=specs,
        active_alias=active,
        host=githost.normalize_host(env.get("MOORING_GITHUB_HOST") or str(gh.get("host", ""))),
        folders=tuple(sync.get("folders", ("notebooks", "data", "reports"))),
        warn_file_mb=int(sync.get("warn_file_mb", 10)),
        max_file_mb=int(sync.get("max_file_mb", 45)),
        log_endpoint=env.get("MOORING_LOG_ENDPOINT", str(log.get("endpoint", ""))),
        log_level=env.get("MOORING_LOG_LEVEL", str(log.get("level", "info"))),
    )


def load_config(
    user_config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    return load_app_config(user_config_path, env).config_for(None)
