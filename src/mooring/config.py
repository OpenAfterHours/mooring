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
    ai_enabled: bool = True
    ai_provider: str = "copilot"
    ai_model: str = ""
    ai_reasoning_effort: str = ""
    ai_chat_idle_timeout: int = 900
    # Team context (instructions + data dictionary). Opt-in: when off, the copilot
    # behaves exactly as before (dataset schema + notebook source only).
    ai_context: bool = False
    ai_context_dir: str = "context"
    ai_context_max_kb: int = 256
    # Read the schema of dataframes live in the running kernel (covers data loaded
    # from outside the workspace). Value-free; on by default, kill-switch to off.
    ai_live_schema: bool = True
    # Best-effort structured-PII pre-flight scan on text leaving for the AI server
    # (chat prompt, notebook source, schema column names, team context). Opt-in:
    # when off, the copilot behaves exactly as before. block_prompt = warn-and-hold
    # on the chat prompt (the analyst confirms "send anyway"); scan_source = the
    # one-time notebook-source banner. See docs/admins/ai-privacy.md.
    ai_pii: bool = False
    ai_pii_block_prompt: bool = True
    ai_pii_scan_source: bool = True
    # Phase 2: optional LOCAL NER name detection (needs the `mooring[pii]` extra).
    # Only acts when ai_pii is also on. See mooring.ai.ner / docs/admins/ai-privacy.md.
    # The default model is a SAFETENSORS build loaded as its bf16 variant (no pickle),
    # pinned to a commit for reproducibility.
    ai_pii_names: bool = False
    ai_pii_name_model: str = "gliner-community/gliner_small-v2.5"
    ai_pii_name_revision: str = "f227d3cd637bd4e6757ae143935316d062393341"
    ai_pii_name_variant: str = "bf16"
    ai_pii_name_labels: tuple[str, ...] = ("person", "name")
    ai_pii_name_threshold: float = 0.7

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
            folders=self.folders,
            exclude=self.exclude,
            warn_file_mb=self.warn_file_mb,
            max_file_mb=self.max_file_mb,
            workspace_path=s.workspace_path,
        )


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
    return tuple(raw)


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
    ai = data.get("ai", {})
    ai_pii = ai.get("pii", {}) if isinstance(ai.get("pii"), dict) else {}

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
        ai_enabled=_as_bool(env.get("MOORING_AI_ENABLED"), _as_bool(ai.get("enabled"), True)),
        ai_provider=env.get("MOORING_AI_PROVIDER", str(ai.get("provider", "copilot"))),
        ai_model=env.get("MOORING_AI_MODEL", str(ai.get("model", ""))),
        ai_reasoning_effort=env.get(
            "MOORING_AI_REASONING_EFFORT", str(ai.get("reasoning_effort", ""))
        ),
        ai_chat_idle_timeout=int(
            env.get("MOORING_AI_CHAT_IDLE_SEC", ai.get("chat_idle_timeout_sec", 900))
        ),
        ai_context=_as_bool(env.get("MOORING_AI_CONTEXT"), _as_bool(ai.get("context"), False)),
        ai_context_dir=env.get("MOORING_AI_CONTEXT_DIR", str(ai.get("context_dir", "context"))),
        ai_context_max_kb=int(
            env.get("MOORING_AI_CONTEXT_MAX_KB", ai.get("context_max_kb", 256))
        ),
        ai_live_schema=_as_bool(
            env.get("MOORING_AI_LIVE_SCHEMA"), _as_bool(ai.get("live_schema"), True)
        ),
        ai_pii=_as_bool(env.get("MOORING_AI_PII"), _as_bool(ai_pii.get("enabled"), False)),
        ai_pii_block_prompt=_as_bool(
            env.get("MOORING_AI_PII_BLOCK_PROMPT"), _as_bool(ai_pii.get("block_prompt"), True)
        ),
        ai_pii_scan_source=_as_bool(
            env.get("MOORING_AI_PII_SCAN_SOURCE"), _as_bool(ai_pii.get("scan_notebook_source"), True)
        ),
        ai_pii_names=_as_bool(
            env.get("MOORING_AI_PII_NAMES"), _as_bool(ai_pii.get("detect_names"), False)
        ),
        ai_pii_name_model=env.get(
            "MOORING_AI_PII_NAME_MODEL",
            str(ai_pii.get("name_model", "gliner-community/gliner_small-v2.5")),
        ),
        ai_pii_name_revision=env.get(
            "MOORING_AI_PII_NAME_REVISION",
            str(ai_pii.get("name_model_revision", "f227d3cd637bd4e6757ae143935316d062393341")),
        ),
        ai_pii_name_variant=env.get(
            "MOORING_AI_PII_NAME_VARIANT", str(ai_pii.get("name_model_variant", "bf16"))
        ),
        ai_pii_name_labels=_str_list(
            ai_pii.get("name_labels", ("person", "name")), "name_labels"
        ),
        ai_pii_name_threshold=float(
            env.get("MOORING_AI_PII_NAME_THRESHOLD", ai_pii.get("name_threshold", 0.7))
        ),
    )


def load_config(
    user_config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    return load_app_config(user_config_path, env).config_for(None)
