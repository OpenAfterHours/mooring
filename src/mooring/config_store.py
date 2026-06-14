"""Mutations of the user config file (repo registry and active-repo pointer).

Reads/writes only the user's config.toml, never the packaged default. The
first write against a file with no [repos] section materializes the currently
effective repo set (including one synthesized from a legacy/baked [github]
section) so the user file becomes authoritative from then on.
"""

from __future__ import annotations

import os
import re
import tomllib

import tomli_w

from mooring import config, githost, paths

# "active" is the pointer key inside [repos], so it can't be an alias.
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RESERVED_ALIASES = {"active"}


def validate_alias(alias: str) -> str:
    if alias in RESERVED_ALIASES or not ALIAS_RE.match(alias):
        raise ValueError(
            f"Invalid repo alias {alias!r}: use letters, digits, '.', '_' or '-' "
            "(and not the reserved word 'active')."
        )
    return alias


def read_user_data() -> dict:
    path = paths.user_config_file()
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text("utf-8"))


def write_user_data(data: dict) -> None:
    path = paths.user_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(data), "utf-8")
    os.replace(tmp, path)


def _materialized(data: dict) -> dict:
    """Ensure data has a [repos] section reflecting the effective repo set."""
    if isinstance(data.get("repos"), dict):
        return data
    specs, active = config.repo_specs_from_data(config.merged_data())
    repos: dict = {"active": active} if active else {}
    for s in specs:
        repos[s.alias] = {"owner": s.owner, "repo": s.repo, "branch": s.branch}
        if s.workspace_path:
            repos[s.alias]["workspace"] = s.workspace_path
    data["repos"] = repos
    return data


def add_repo(
    alias: str,
    owner: str,
    repo: str,
    branch: str = "main",
    workspace: str = "",
    make_active: bool = True,
    client_id: str | None = None,
    host: str | None = None,
) -> None:
    validate_alias(alias)
    data = _materialized(read_user_data())
    data["repos"][alias] = {"owner": owner, "repo": repo, "branch": branch or "main"}
    if workspace:
        data["repos"][alias]["workspace"] = workspace
    if make_active or not data["repos"].get("active"):
        data["repos"]["active"] = alias
    if client_id is not None:
        data.setdefault("github", {})["client_id"] = client_id
    if host is not None:
        data.setdefault("github", {})["host"] = githost.normalize_host(host)
    write_user_data(data)


def set_host(host: str) -> str:
    """Persist the global GitHub host; returns the normalized value.

    Host is a single [github] setting shared by every repo, independent of the
    [repos] registry, so this writes [github].host without materializing repos.
    """
    normalized = githost.normalize_host(host)
    data = read_user_data()
    data.setdefault("github", {})["host"] = normalized
    write_user_data(data)
    return normalized


def remove_repo(alias: str) -> None:
    data = _materialized(read_user_data())
    if alias not in data["repos"] or alias in RESERVED_ALIASES:
        raise KeyError(alias)
    del data["repos"][alias]
    remaining = sorted(k for k in data["repos"] if k not in RESERVED_ALIASES)
    if data["repos"].get("active") == alias:
        if remaining:
            data["repos"]["active"] = remaining[0]
        else:
            data["repos"].pop("active", None)
    write_user_data(data)


def remove_all_repos() -> None:
    """Clear the entire repo registry. Workspaces and the saved token are kept.

    An explicit empty [repos] is authoritative — it also overrides any
    owner/repo baked into the packaged default (repo_specs_from_data treats a
    present [repos] section as the whole truth).
    """
    data = read_user_data()
    data["repos"] = {}
    write_user_data(data)


def set_active(alias: str) -> None:
    data = _materialized(read_user_data())
    if alias not in data["repos"] or alias in RESERVED_ALIASES:
        raise KeyError(alias)
    data["repos"]["active"] = alias
    write_user_data(data)
