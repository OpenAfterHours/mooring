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
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

from mooring import ai_config, githost, paths
from mooring.ai_config import AiConfig

# Appearance: the hub, the AI chat, and the marimo notebooks all follow one
# theme set on the hub. "system" (the default) follows the OS; "light"/"dark"
# pin it. Stored once in [ui] theme; the hub writes it into each workspace's
# .marimo.toml display.theme so notebooks open in the same theme.
VALID_THEMES = ("light", "dark", "system")
DEFAULT_THEME = "system"


def normalize_theme(value: object) -> str:
    """Coerce a config/env/request value to a valid theme, else the default.

    Tolerant by design: an unset, empty, or unknown value falls back to
    :data:`DEFAULT_THEME` rather than raising, so a stray config entry can
    never wedge the hub on an invalid appearance.
    """
    text = str(value or "").strip().lower()
    return text if text in VALID_THEMES else DEFAULT_THEME


@dataclass(frozen=True)
class Config:
    client_id: str = ""
    owner: str = ""
    repo: str = ""
    branch: str = "main"
    host: str = githost.DEFAULT_HOST
    folders: tuple[str, ...] = ("notebooks", "data", "reports")
    exclude: tuple[str, ...] = ()
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
    exclude: tuple[str, ...] = ()
    warn_file_mb: int = 10
    max_file_mb: int = 45
    log_endpoint: str = ""
    log_level: str = "info"
    # Appearance shared by the hub, the chat, and the notebooks (see normalize_theme).
    ui_theme: str = DEFAULT_THEME
    # The copilot's settings, nested (see mooring.ai_config). The whole PiiConfig
    # travels to the chat session as one object, so a guard field can't be dropped
    # in transit. Flat ai_*/ai_pii_* read-only properties below forward here so
    # existing readers are unchanged.
    ai: AiConfig = field(default_factory=AiConfig)

    @property
    def aliases(self) -> list[str]:
        return [spec.alias for spec in self.repos]

    @property
    def sync_folders(self) -> tuple[str, ...]:
        """The folders that ride sync. The team-context folder (mooring.ai.context)
        is folded in here when ``[ai] context`` is on, so ``instructions.md`` and the
        data dictionary push AND pull like any other folder — without each teammate
        having to add it to ``[sync] folders`` by hand (forgetting it on the pull
        side is exactly what made pull skip the folder push had already uploaded).

        Opt-in: with the feature off the result is exactly ``[sync] folders``, so
        behaviour is byte-identical to before. This drives the whole sync surface —
        scan_local, the remote tree fetch, pull/push/propose, and the hub's local
        listing — through the single Config the layers below consume.
        """
        ctx = self.ai.context_dir.strip("/")
        if self.ai.context and ctx and ctx not in self.folders:
            return (*self.folders, ctx)
        return self.folders

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
                    folders=self.sync_folders,
                    exclude=self.exclude,
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
            folders=self.sync_folders,
            exclude=self.exclude,
            warn_file_mb=self.warn_file_mb,
            max_file_mb=self.max_file_mb,
            workspace_path=s.workspace_path,
        )

    # -- flat AI/PII accessors -----------------------------------------------
    # Forward to the nested `ai` config so every existing reader (server, cli,
    # base, tests) is unchanged; `self.ai` (mooring.ai_config) is the canonical store.
    @property
    def ai_enabled(self) -> bool:
        return self.ai.enabled

    @property
    def ai_provider(self) -> str:
        return self.ai.provider

    @property
    def ai_model(self) -> str:
        return self.ai.model

    @property
    def ai_reasoning_effort(self) -> str:
        return self.ai.reasoning_effort

    @property
    def ai_chat_idle_timeout(self) -> int:
        return self.ai.chat_idle_timeout

    @property
    def ai_context(self) -> bool:
        return self.ai.context

    @property
    def ai_context_dir(self) -> str:
        return self.ai.context_dir

    @property
    def ai_context_max_kb(self) -> int:
        return self.ai.context_max_kb

    @property
    def ai_live_schema(self) -> bool:
        return self.ai.live_schema

    @property
    def ai_pii(self) -> bool:
        return self.ai.pii.enabled

    @property
    def ai_pii_block_prompt(self) -> bool:
        return self.ai.pii.block_prompt

    @property
    def ai_pii_scan_source(self) -> bool:
        return self.ai.pii.scan_source

    @property
    def ai_pii_names(self) -> bool:
        return self.ai.pii.names

    @property
    def ai_pii_name_backend(self) -> str:
        return self.ai.pii.name_backend

    @property
    def ai_pii_name_model(self) -> str:
        return self.ai.pii.name_model

    @property
    def ai_pii_name_revision(self) -> str:
        return self.ai.pii.name_revision

    @property
    def ai_pii_name_variant(self) -> str:
        return self.ai.pii.name_variant

    @property
    def ai_pii_name_labels(self) -> tuple[str, ...]:
        return self.ai.pii.name_labels

    @property
    def ai_pii_name_threshold(self) -> float:
        return self.ai.pii.name_threshold

    @property
    def ai_batch_enabled(self) -> bool:
        return self.ai.batch.enabled

    @property
    def ai_batch_max_jobs(self) -> int:
        return self.ai.batch.max_jobs

    @property
    def ai_batch_max_concurrency(self) -> int:
        return self.ai.batch.max_concurrency

    @property
    def ai_batch_job_timeout(self) -> int:
        return self.ai.batch.job_timeout

    @property
    def ai_batch_follow_up_turns(self) -> int:
        return self.ai.batch.follow_up_turns

    @property
    def ai_batch_pii_policy(self) -> str:
        return self.ai.batch.pii_policy


def _str_list(raw: object, key: str) -> tuple[str, ...]:
    """Coerce a ``[sync]`` array value to a tuple of strings.

    A bare string is accepted as the single-element form (``exclude = "*.tmp"``):
    iterating a ``str`` would otherwise explode it into characters, and a lone
    ``"*"`` would then silently match every path. Any other non-array type (e.g.
    an accidental ``[sync.exclude]`` table, which TOML parses as a dict) is a
    config error rather than silent garbage.
    """
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"[sync] {key} must be an array of strings, got {type(raw).__name__}")
    if not all(isinstance(p, str) for p in raw):
        raise ValueError(f"[sync] {key} entries must all be strings")
    return tuple(s for s in raw if isinstance(s, str))


def _as_bool(value: object, default: bool) -> bool:
    """Coerce a TOML bool or a string env override to bool; None keeps default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


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
                alias=str(alias),
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
    ui = data.get("ui", {})
    ai = data.get("ai", {})

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
        # No repos resolved from config. Env vars may still define a one-off
        # repo, but the legacy v0.1 [github] owner/repo apply *only* when there
        # is no [repos] section at all: a present (even empty) [repos] section
        # is the whole truth, so it must not resurrect the legacy [github] repo.
        # (That resurrection is what made a cleared registry — 'repo remove
        # --all' writes [repos]={} — still surface the old repo in the hub.)
        legacy_gh = {} if "repos" in data else gh
        legacy_ws = {} if "repos" in data else ws
        owner = env.get("MOORING_OWNER", str(legacy_gh.get("owner", "")))
        repo = env.get("MOORING_REPO", str(legacy_gh.get("repo", "")))
        if owner or repo:
            spec = RepoSpec(
                alias=repo or owner,
                owner=owner,
                repo=repo,
                branch=env.get("MOORING_BRANCH", str(legacy_gh.get("branch", "main") or "main")),
                workspace_path=env.get("MOORING_WORKSPACE", str(legacy_ws.get("path", ""))),
            )
            specs, active = (spec,), spec.alias

    return AppConfig(
        client_id=env.get("MOORING_CLIENT_ID", gh.get("client_id", "")),
        repos=specs,
        active_alias=active,
        host=githost.normalize_host(env.get("MOORING_GITHUB_HOST") or str(gh.get("host", ""))),
        folders=_str_list(sync.get("folders", ("notebooks", "data", "reports")), "folders"),
        exclude=_str_list(sync.get("exclude", ()), "exclude"),
        warn_file_mb=int(sync.get("warn_file_mb", 10)),
        max_file_mb=int(sync.get("max_file_mb", 45)),
        log_endpoint=env.get("MOORING_LOG_ENDPOINT", str(log.get("endpoint", ""))),
        log_level=env.get("MOORING_LOG_LEVEL", str(log.get("level", "info"))),
        ui_theme=normalize_theme(env.get("MOORING_UI_THEME", ui.get("theme", DEFAULT_THEME))),
        ai=ai_config.load_ai_config(ai, env),
    )


def load_config(
    user_config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    return load_app_config(user_config_path, env).config_for(None)
