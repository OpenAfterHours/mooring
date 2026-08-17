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
    """Ensure data has a [repos] section reflecting the effective repo set.

    Note the two checks are INDEPENDENT: accounts must materialize even for a file
    that already has [repos], which is every multi-repo user — i.e. exactly the
    people this feature is for.
    """
    if not isinstance(data.get("repos"), dict):
        specs, active = config.repo_specs_from_data(config.merged_data())
        repos: dict = {"active": active} if active else {}
        for s in specs:
            repos[s.alias] = {"owner": s.owner, "repo": s.repo, "branch": s.branch}
            if s.workspace_path:
                repos[s.alias]["workspace"] = s.workspace_path
            if s.account:
                repos[s.alias]["account"] = s.account
        data["repos"] = repos
    if not isinstance(data.get("accounts"), dict):
        data["accounts"] = _legacy_account(data)
    return data


def _legacy_account(data: dict) -> dict:
    """Seed [accounts] from a pre-accounts [github] host/client_id, if there is one.

    The login is left blank on purpose — we do not know who the stored token belongs
    to until someone signs in, and config.Config.token_slot treats a blank login as
    "not signed in" rather than resolving onto the host-keyed token. So this records
    the CONNECTION details (which instance, which OAuth app) without ever claiming
    an identity. Repos stay unbound, which is what keeps their existing token working.
    """
    gh = config.merged_data().get("github", {})  # already has the user file overlaid
    client_id = str(gh.get("client_id", "") or "")
    try:
        host = githost.normalize_host(str(gh.get("host", "") or ""))
    except ValueError:
        host = githost.DEFAULT_HOST
    if not client_id and host == githost.DEFAULT_HOST:
        return {}
    return {"legacy": {"host": host, "client_id": client_id}}


def add_account(
    alias: str, host: str, login: str = "", client_id: str = "", auth: str = ""
) -> None:
    """Create or update an account record, merging into any existing table.

    Merging matters: the device flow writes the login *after* the record exists, and
    an overwrite would drop the client_id the flow was started with.

    ``auth`` is written only when given, so an existing record keeps its method —
    except that switching an account to a NEW method must actually take effect, which
    is why the caller passes it explicitly on every sign-in rather than relying on the
    default. The device flow's own value is left implicit (an absent key reads as
    ``"device"``), so a config written by an older mooring is untouched.
    """
    validate_alias(alias)
    data = _materialized(read_user_data())
    entry = data["accounts"].get(alias)
    if not isinstance(entry, dict):
        entry = {}
    entry["host"] = githost.normalize_host(host)
    if login:
        entry["login"] = login
    if client_id:
        entry["client_id"] = client_id
    if auth and auth != config.AUTH_DEVICE:
        entry["auth"] = auth
    elif auth == config.AUTH_DEVICE:
        entry.pop("auth", None)
    data["accounts"][alias] = entry
    if not data["accounts"].get("active"):
        data["accounts"]["active"] = alias
    write_user_data(data)


def clear_account_login(alias: str) -> None:
    """Drop an account's ``login``, marking it NOT signed in while keeping the record.

    The borrowed-credential ("git") counterpart to :func:`auth.delete_token`. There is
    no stored token to delete for that method and deleting git's own credential would
    be an act of vandalism on the user's git setup, so signing out has to be recorded
    on mooring's side: a blank login is what ``Config.token_slot`` already reads as
    "cannot produce a credential". The host, client id and any repo bindings survive,
    so signing back in restores the account exactly.
    """
    data = _materialized(read_user_data())
    entry = data["accounts"].get(alias)
    if not isinstance(entry, dict):
        raise KeyError(alias)
    entry.pop("login", None)
    data["accounts"][alias] = entry
    write_user_data(data)


def remove_account(alias: str) -> tuple[str, ...]:
    """Forget an account. Returns the repo aliases that were bound to it.

    Those repos KEEP the now-dangling binding rather than being unbound, and keep
    their files and sync history. Unbinding would look tidier but is the unsafe
    option: an unbound repo falls back to the global [github] host and the
    pre-accounts token slot, so an Enterprise repo would quietly start pointing at
    github.com with a different workspace. A dangling binding instead resolves to
    a reported account_error and NO token at all (config.Config.token_slot),
    which is the honest state — and re-adding the account restores it as it was.
    """
    data = _materialized(read_user_data())
    if alias not in data["accounts"] or alias in RESERVED_ALIASES:
        raise KeyError(alias)
    del data["accounts"][alias]
    remaining = sorted(k for k in data["accounts"] if k not in RESERVED_ALIASES)
    if data["accounts"].get("active") == alias:
        if remaining:
            data["accounts"]["active"] = remaining[0]
        else:
            data["accounts"].pop("active", None)
    orphaned = tuple(
        sorted(
            name
            for name, tbl in data.get("repos", {}).items()
            if name not in RESERVED_ALIASES and isinstance(tbl, dict) and tbl.get("account") == alias
        )
    )
    write_user_data(data)
    return orphaned


def set_active_account(alias: str) -> None:
    data = _materialized(read_user_data())
    if alias not in data["accounts"] or alias in RESERVED_ALIASES:
        raise KeyError(alias)
    data["accounts"]["active"] = alias
    write_user_data(data)


def add_repo(
    alias: str,
    owner: str,
    repo: str,
    branch: str = "main",
    workspace: str = "",
    make_active: bool = True,
    client_id: str | None = None,
    host: str | None = None,
    account: str | None = None,
) -> None:
    validate_alias(alias)
    data = _materialized(read_user_data())
    # MERGE, don't replace: re-adding an existing alias must not silently drop keys
    # it isn't being asked about. Dropping `account` in particular would unbind the
    # repo, and an unbound repo falls back to the pre-accounts host-keyed token —
    # i.e. it would quietly start pushing under whoever logged in before accounts.
    entry = data["repos"].get(alias)
    if not isinstance(entry, dict):
        entry = {}
    entry.update({"owner": owner, "repo": repo, "branch": branch or "main"})
    if workspace:
        entry["workspace"] = workspace
    if account is not None:
        if account:
            entry["account"] = account
        else:
            entry.pop("account", None)
    data["repos"][alias] = entry
    if make_active or not data["repos"].get("active"):
        data["repos"]["active"] = alias
    if client_id is not None:
        data.setdefault("github", {})["client_id"] = client_id
    if host is not None:
        data.setdefault("github", {})["host"] = githost.normalize_host(host)
    write_user_data(data)


def set_repo_context_folders(alias: str, folders: "tuple[str, ...] | list[str] | None") -> None:
    """Set (or clear) this machine's per-user AI context SUBSCRIPTION for ``alias`` in
    the user config.toml ``[repos.<alias>].ai_context_folders``.

    ``folders=None`` DELETES the key — revert to reading the WHOLE team offer (the
    opt-out default). A list is written SORTED + de-duplicated; an empty list stays ``[]``
    = subscribed to nothing. Materializes the ``[repos]`` registry on first write like
    :func:`add_repo`, and preserves every other key in the repo's table. Raises
    ``KeyError`` for an unknown alias."""
    validate_alias(alias)
    data = _materialized(read_user_data())
    if alias not in data["repos"] or alias in RESERVED_ALIASES:
        raise KeyError(alias)
    entry = data["repos"][alias]
    if folders is None:
        entry.pop("ai_context_folders", None)
    else:
        norm = sorted(
            {str(f).replace("\\", "/").strip().strip("/") for f in folders if str(f).strip()}
        )
        entry["ai_context_folders"] = norm
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


# -- generic dotted-key access (the `mooring config` command) -------------------


def _split_key(dotted_key: str) -> list[str]:
    parts = [p.strip() for p in dotted_key.split(".")]
    if not dotted_key or any(not p for p in parts):
        raise ValueError(
            f"Invalid config key {dotted_key!r}: use dotted names like 'ai.pii.enabled'."
        )
    return parts


def set_value(dotted_key: str, value) -> None:
    """Set a dotted key (e.g. ``ai.pii.enabled``) in the user config.toml, creating
    intermediate tables as needed. Every other setting in the file is preserved.

    Deliberately does NOT materialize the repo registry (unlike the repo helpers):
    a generic edit must not inject a ``[repos]`` section and disturb repo resolution.
    """
    keys = _split_key(dotted_key)
    data = read_user_data()
    node = data
    for k in keys[:-1]:
        child = node.get(k)
        if not isinstance(child, dict):
            child = {}
            node[k] = child
        node = child
    node[keys[-1]] = value
    write_user_data(data)


def unset_value(dotted_key: str) -> bool:
    """Remove a dotted key from the user config.toml (reverting it to the packaged
    default). Returns False if the key wasn't present. Prunes tables left empty."""
    keys = _split_key(dotted_key)
    data = read_user_data()
    node = data
    parents = [node]
    for k in keys[:-1]:
        child = node.get(k)
        if not isinstance(child, dict):
            return False
        node = child
        parents.append(node)
    if keys[-1] not in node:
        return False
    del node[keys[-1]]
    for k, parent in zip(reversed(keys[:-1]), reversed(parents[:-1])):
        child = parent.get(k)
        if isinstance(child, dict) and not child:
            del parent[k]
        else:
            break
    write_user_data(data)
    return True


def get_value(dotted_key: str):
    """The effective value (packaged default merged with the user file) for a dotted
    key. Raises KeyError if it is set nowhere. Reflects the config FILES, not
    ephemeral environment-variable overrides applied at load time."""
    node: object = config.merged_data()
    for k in _split_key(dotted_key):
        if not isinstance(node, dict) or k not in node:
            raise KeyError(dotted_key)
        node = node[k]
    return node
