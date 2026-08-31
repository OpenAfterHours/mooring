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

# How an account proves who it is. Persisted per account as [accounts.<alias>] auth.
#   "device" — OAuth device flow against an OAuth app's client_id (the default, and
#              the only method before 0.4.33; an absent key means this)
#   "token"  — a token the user pasted, stored in the OS credential store
#   "git"    — no stored credential at all: borrowed from git's credential helper on
#              every read, so the helper keeps ownership and can refresh it. The
#              answer for orgs that restrict OAuth apps AND cap PAT lifetimes.
AUTH_DEVICE = "device"
AUTH_TOKEN = "token"
AUTH_GIT = "git"
AUTH_METHODS = (AUTH_DEVICE, AUTH_TOKEN, AUTH_GIT)


def normalize_auth(value: object) -> str:
    """Coerce a stored ``auth`` value to a known method, else the default.

    Tolerant like :func:`normalize_theme`, and fail-closed with it: an unknown
    method degrades to ``"device"``, which looks for a STORED token and finds none
    for a borrowed account. That reads as "not signed in" — the safe reading of a
    corrupt or future config value, never "use whatever credential is lying about".
    """
    text = str(value or "").strip().lower()
    return text if text in AUTH_METHODS else AUTH_DEVICE


def normalize_theme(value: object) -> str:
    """Coerce a config/env/request value to a valid theme, else the default.

    Tolerant by design: an unset, empty, or unknown value falls back to
    :data:`DEFAULT_THEME` rather than raising, so a stray config entry can
    never wedge the hub on an invalid appearance.
    """
    text = str(value or "").strip().lower()
    return text if text in VALID_THEMES else DEFAULT_THEME


@dataclass(frozen=True)
class Account:
    """One GitHub identity on one instance — the thing a token belongs to.

    Keyed by (host, login) rather than host alone so two users can coexist on the
    same instance. ``client_id`` rides here because each instance needs its own
    OAuth app: a github.com client id does not work against Enterprise. It is empty
    for the non-device methods, which have no OAuth app at all.

    ``login`` is discovered from ``GET /user`` at sign-in, never typed. A blank
    ``login`` means sign-in never finished, and is treated as NOT LOGGED IN — see
    :meth:`is_signed_in` and ``Config.token_login``. That holds for every method:
    a borrowed credential still has to name its owner before the account counts.
    """

    alias: str
    host: str = githost.DEFAULT_HOST
    login: str = ""
    client_id: str = ""
    # Where this account's credential comes from; see AUTH_METHODS. Defaults to the
    # device flow so every pre-0.4.33 config keeps its exact meaning.
    auth: str = AUTH_DEVICE

    @property
    def is_signed_in(self) -> bool:
        return bool(self.login)

    @property
    def label(self) -> str:
        return f"{self.login}@{self.host}" if self.login else self.host


@dataclass(frozen=True)
class Config:
    client_id: str = ""
    owner: str = ""
    repo: str = ""
    branch: str = "main"
    host: str = githost.DEFAULT_HOST
    # The repo's account: which alias it is bound to, that account's GitHub login,
    # and why resolution failed (empty when fine). `account_error` is the third
    # state between "configured" and "not configured" — a repo whose account was
    # deleted or misspelt is NOT unconfigured, it is broken in a way the user can
    # fix, and the adapters need to say so rather than silently falling back to
    # local mode. See AppConfig.config_for, which never raises.
    account: str = ""
    account_login: str = ""
    account_error: str = ""
    # The bound account's credential source (see AUTH_METHODS). Pass it to
    # auth.token_for alongside token_slot; on its own it decides nothing.
    auth_method: str = AUTH_DEVICE
    folders: tuple[str, ...] = ("notebooks", "data", "reports")
    exclude: tuple[str, ...] = ()
    warn_file_mb: int = 10
    max_file_mb: int = 45
    warn_shadowed_notebooks: bool = True
    # Whether Propose OPENS the pull request for you (else it just links to the compare
    # page — the pre-Slice-2 behaviour). Per-machine ([review] open_pr).
    open_pr: bool = True
    workspace_path: str = ""
    # Local trash retention (the pre-image safety net; see mooring.trash).
    trash_keep_days: int = 14
    trash_keep_per_file: int = 10
    trash_max_file_mb: int = 45
    trash_max_total_mb: int = 200

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def is_configured(self) -> bool:
        """Whether there is a team repo to talk to at all (vs. local-only mode).

        A BOUND account is an identity route whatever method it uses, so a client id
        is only required for an unbound pre-accounts repo, which has nothing else to
        sign in with. Requiring one unconditionally would drop every token/borrowed
        account into local mode — they have no OAuth app — and it also made a repo
        whose account binding broke *look unconfigured* rather than broken, which is
        the failure the comment in hub/routes/setup.py's api_state warns about. Being
        "configured" says nothing about being signed in: that is token_slot's job.
        """
        return bool(self.owner and self.repo and (self.client_id or self.account))

    @property
    def token_slot(self) -> tuple[str, str] | None:
        """Where this repo's token lives — ``(host, login)`` — or ``None`` if no
        token can exist for it. Pass to :func:`mooring.auth.token_for`.

        This is the ONE place the account/legacy token decision is made, and it is
        deliberately fail-closed. A repo bound to an account that never finished
        signing in (blank login) resolves to ``None``, NOT to the host-keyed slot:
        that slot may still hold a previous user's pre-accounts token, and handing
        it back would silently push under their name. An UNBOUND repo does read the
        host-keyed slot — that token is genuinely its own, from before accounts
        existed — which is what keeps upgrades seamless.
        """
        if self.account_error:
            return None
        if self.account and not self.account_login:
            return None
        return (self.host, self.account_login)

    def workspace(self) -> Path:
        if self.workspace_path:
            return Path(self.workspace_path).expanduser()
        return paths.default_workspace(self.owner or "_", self.repo or "workspace", self.host)


@dataclass(frozen=True)
class RepoSpec:
    alias: str
    owner: str
    repo: str
    branch: str = "main"
    workspace_path: str = ""
    # The account alias this repo is bound to. Empty = unbound, i.e. a pre-accounts
    # config that still uses the global [github] host/client_id. Binding is what
    # makes identity follow the repo, so switching repos can never push under the
    # wrong token.
    account: str = ""
    # This machine's per-user AI context SUBSCRIPTION for the repo: which of the team's
    # offered context folders (synced mooring.toml [ai] context_folders) this user's copilot
    # actually reads. ``None`` = no choice recorded → read the WHOLE offer (the opt-out
    # default); ``()`` = subscribed to nothing; a non-empty tuple = read that subset
    # (intersected with the offer, which stays authoritative). See mooring.app.context_folders.
    context_folders: tuple[str, ...] | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class AppConfig:
    client_id: str = ""
    repos: tuple[RepoSpec, ...] = ()
    active_alias: str = ""
    host: str = githost.DEFAULT_HOST
    # The known GitHub identities. This tuple IS the account index: keyring has no
    # portable way to enumerate credentials for a service, so the config file is
    # the only list of which accounts exist. `active_account` is just the default
    # offered when registering a new repo — it is NOT a global identity switch,
    # because identity follows the repo (RepoSpec.account).
    accounts: tuple[Account, ...] = ()
    active_account: str = ""
    # Accounts dropped while parsing, as (alias, reason) — a bad host or a
    # malformed table takes out that one account and is reported, never raised.
    # Mirrors policy.Policy.ignored; see config_for's totality guarantee.
    ignored_accounts: tuple[tuple[str, str], ...] = ()
    folders: tuple[str, ...] = ("notebooks", "data", "reports")
    exclude: tuple[str, ...] = ()
    warn_file_mb: int = 10
    max_file_mb: int = 45
    warn_shadowed_notebooks: bool = True
    open_pr: bool = True  # Propose opens the PR for you (see Config.open_pr).
    # Local trash retention (the pre-image safety net; see mooring.trash).
    trash_keep_days: int = 14
    trash_keep_per_file: int = 10
    trash_max_file_mb: int = 45
    trash_max_total_mb: int = 200
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

    def account(self, alias: str) -> Account:
        for a in self.accounts:
            if a.alias == alias:
                return a
        raise KeyError(alias)

    def _identity(self, spec: RepoSpec) -> tuple[str, str, str, str, str]:
        """Resolve a repo's (host, client_id, login, auth_method, error). NEVER raises.

        Totality matters here more than anywhere else in this file: Hub.app_cfg is
        a property that calls config_for on EVERY read, so an exception would 500
        every hub route — including the ones that would let the user fix it — and
        would stop policy.tighten_app_config from ever running, silently dropping
        the admin policy. A dangling binding therefore degrades to a reported
        error, exactly the way policy.parse records rather than raises.
        """
        if not spec.account:
            # Unbound: the pre-accounts world, where host/client_id are global and
            # the only method that ever existed was the device flow.
            return self.host, self.client_id, "", AUTH_DEVICE, ""
        for a in self.accounts:
            if a.alias == spec.account:
                if not a.is_signed_in:
                    return (
                        a.host,
                        a.client_id,
                        "",
                        a.auth,
                        f"Account {spec.account!r} is not signed in — "
                        f"run `mooring account add {spec.account}`.",
                    )
                return a.host, a.client_id, a.login, a.auth, ""
        dropped = dict(self.ignored_accounts).get(spec.account)
        reason = (
            f"Account {spec.account!r} was dropped from the config: {dropped}"
            if dropped
            else f"Account {spec.account!r} is not configured — "
            f"run `mooring account add {spec.account}`."
        )
        return githost.DEFAULT_HOST, "", "", AUTH_DEVICE, reason

    def config_for(self, alias: str | None = None) -> Config:
        """The single-repo Config for an alias (None = the active repo).

        An app with no repos yields an unconfigured Config so callers can
        keep using cfg.is_configured. A repo bound to a missing or unusable
        account yields a Config carrying ``account_error`` rather than raising
        (see _identity) — the repo is broken, not unconfigured, and the adapters
        need to say which.
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
                    warn_shadowed_notebooks=self.warn_shadowed_notebooks,
                    open_pr=self.open_pr,
                    trash_keep_days=self.trash_keep_days,
                    trash_keep_per_file=self.trash_keep_per_file,
                    trash_max_file_mb=self.trash_max_file_mb,
                    trash_max_total_mb=self.trash_max_total_mb,
                )
            alias = self.active_alias
        s = self.spec(alias)
        host, client_id, login, auth_method, account_error = self._identity(s)
        return Config(
            client_id=client_id,
            owner=s.owner,
            repo=s.repo,
            branch=s.branch,
            host=host,
            account=s.account,
            account_login=login,
            account_error=account_error,
            auth_method=auth_method,
            folders=self.sync_folders,
            exclude=self.exclude,
            warn_file_mb=self.warn_file_mb,
            max_file_mb=self.max_file_mb,
            warn_shadowed_notebooks=self.warn_shadowed_notebooks,
            open_pr=self.open_pr,
            workspace_path=s.workspace_path,
            trash_keep_days=self.trash_keep_days,
            trash_keep_per_file=self.trash_keep_per_file,
            trash_max_file_mb=self.trash_max_file_mb,
            trash_max_total_mb=self.trash_max_total_mb,
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
    def ai_openai_base_url(self) -> str:
        return self.ai.openai_base_url

    @property
    def ai_openai_api_version(self) -> str:
        return self.ai.openai_api_version

    @property
    def ai_routing_enabled(self) -> bool:
        return self.ai.routing.enabled

    @property
    def ai_trusted_base_url(self) -> str:
        return self.ai.routing.trusted_base_url

    @property
    def ai_trusted_api_version(self) -> str:
        return self.ai.routing.trusted_api_version

    @property
    def ai_trusted_classifier_model(self) -> str:
        return self.ai.routing.classifier_model

    @property
    def ai_trusted_coding_model(self) -> str:
        return self.ai.routing.coding_model

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
    def ai_semantic_model(self) -> bool:
        return self.ai.semantic_model

    @property
    def ai_code_index(self) -> bool:
        return self.ai.code_index

    @property
    def ai_notebook_catalog(self) -> bool:
        return self.ai.notebook_catalog

    @property
    def ai_traceback_guard(self) -> bool:
        return self.ai.traceback_guard

    @property
    def ai_apply_guard(self) -> bool:
        return self.ai.apply_guard

    @property
    def ai_apply_runs(self) -> bool:
        return self.ai.apply_runs

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

    @property
    def ai_investigate_enabled(self) -> bool:
        return self.ai.investigate.enabled

    @property
    def ai_investigate_max_branches(self) -> int:
        return self.ai.investigate.max_branches

    @property
    def ai_investigate_max_concurrency(self) -> int:
        return self.ai.investigate.max_concurrency

    @property
    def ai_investigate_branch_timeout(self) -> int:
        return self.ai.investigate.branch_timeout

    @property
    def ai_investigate_pii_policy(self) -> str:
        return self.ai.investigate.pii_policy


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


def _folder_list(raw: object, key: str) -> tuple[str, ...]:
    """Like :func:`_str_list` but for ``[sync] folders``: also canonicalise each entry to
    a clean workspace-relative POSIX sub-path so the two scan sides agree on it. Backslashes
    become ``/``; ``""``, ``.``, ``./`` and any ``..`` segment are dropped; duplicates are
    removed order-preserving.

    This is load-bearing, not cosmetic: the LOCAL scan resolves a folder through the
    filesystem (``workspace / folder`` + ``rglob``), while the REMOTE scan does a literal
    ``path.startswith(folder + "/")`` on GitHub's forward-slash tree. A ``.`` (== the
    workspace root), a ``"notebooks\\team"`` backslash, or a ``"./reports"`` prefix resolves
    differently on the two sides, so the local side over-includes and pull then classifies
    identical files DELETED_REMOTE and removes them from disk. Dropping ``.`` loses no
    coverage — loose root files sync on their own rule (see :func:`mooring.sync.in_sync_scope`)."""
    out: list[str] = []
    for entry in _str_list(raw, key):
        segs = [s for s in entry.replace("\\", "/").split("/") if s not in ("", ".")]
        if not segs or ".." in segs:
            continue  # resolves to the workspace root or escapes it — never a sync folder
        norm = "/".join(segs)
        if norm not in out:
            out.append(norm)
    return tuple(out)


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


def _opt_subscription(raw: object) -> tuple[str, ...] | None:
    """Parse a repo's ``ai_context_folders`` subscription. ``None`` when the key is
    absent (no choice → read the whole offer); a normalized tuple otherwise (an empty
    list stays ``()`` = subscribed to nothing). Tolerant of a bare string or garbage."""
    if raw is None:
        return None
    if isinstance(raw, str):
        norm = raw.replace("\\", "/").strip().strip("/")
        return (norm,) if norm else ()
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for p in raw:
            norm = str(p).replace("\\", "/").strip().strip("/")
            if norm and norm not in out:
                out.append(norm)
        return tuple(out)
    return None  # a malformed value (e.g. a table) → treated as "no choice"


def account_specs_from_data(
    data: dict,
) -> tuple[tuple[Account, ...], str, tuple[tuple[str, str], ...]]:
    """Extract (accounts, active_account, ignored) from raw config data.

    Mirrors repo_specs_from_data's shape — ``sorted()`` for a stable order and
    ``isinstance(tbl, dict)`` so the scalar ``active`` key is skipped rather than
    becoming a phantom account. Tolerant like policy.parse: a table with an
    unparseable host is DROPPED with a recorded reason instead of raising, because
    this is parsed on every config load and an exception here would wedge every
    command, including the one that would fix it.
    """
    accounts_data = data.get("accounts")
    if not isinstance(accounts_data, dict):
        return (), "", ()
    specs: list[Account] = []
    ignored: list[tuple[str, str]] = []
    for alias, tbl in sorted(accounts_data.items()):
        if not isinstance(tbl, dict):
            continue
        try:
            host = githost.normalize_host(str(tbl.get("host", "")))
        except ValueError as exc:
            ignored.append((str(alias), str(exc)))
            continue
        specs.append(
            Account(
                alias=str(alias),
                host=host,
                login=str(tbl.get("login", "")),
                client_id=str(tbl.get("client_id", "")),
                auth=normalize_auth(tbl.get("auth")),
            )
        )
    active = str(accounts_data.get("active", ""))
    if active not in {a.alias for a in specs}:
        active = specs[0].alias if specs else ""
    return tuple(specs), active, tuple(ignored)


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
                context_folders=_opt_subscription(tbl.get("ai_context_folders")),
                account=str(tbl.get("account", "")),
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
    trash = data.get("trash", {})
    review = data.get("review", {})

    accounts, active_account, ignored_accounts = account_specs_from_data(data)

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

    # MOORING_GITHUB_HOST / MOORING_CLIENT_ID follow the same rule as MOORING_OWNER:
    # they override the ACTIVE repo. When that repo is bound to an account they
    # retarget that one account (leaving every other account alone); otherwise they
    # fall through to the global [github] fields below, which is the pre-accounts
    # behaviour the CI recipe in CLAUDE.md relies on.
    env_host = env.get("MOORING_GITHUB_HOST")
    env_client_id = env.get("MOORING_CLIENT_ID")
    bound = next((s.account for s in specs if s.alias == active and s.account), "")
    if bound and (env_host or env_client_id):
        accounts = tuple(
            replace(
                a,
                host=githost.normalize_host(env_host) if env_host else a.host,
                client_id=env_client_id if env_client_id is not None else a.client_id,
            )
            if a.alias == bound
            else a
            for a in accounts
        )

    return AppConfig(
        client_id=env.get("MOORING_CLIENT_ID", gh.get("client_id", "")),
        repos=specs,
        active_alias=active,
        accounts=accounts,
        active_account=active_account,
        ignored_accounts=ignored_accounts,
        host=githost.normalize_host(env_host or str(gh.get("host", ""))),
        folders=_folder_list(sync.get("folders", ("notebooks", "data", "reports")), "folders"),
        exclude=_str_list(sync.get("exclude", ()), "exclude"),
        warn_file_mb=int(sync.get("warn_file_mb", 10)),
        max_file_mb=int(sync.get("max_file_mb", 45)),
        warn_shadowed_notebooks=_as_bool(sync.get("warn_shadowed_notebooks"), True),
        open_pr=_as_bool(review.get("open_pr"), True),
        trash_keep_days=int(trash.get("keep_days", 14)),
        trash_keep_per_file=int(trash.get("keep_per_file", 10)),
        trash_max_file_mb=int(trash.get("max_file_mb", 45)),
        trash_max_total_mb=int(trash.get("max_total_mb", 200)),
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
