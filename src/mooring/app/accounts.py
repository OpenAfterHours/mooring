"""Managing the GitHub identities mooring can act as.

An *account* is one identity on one instance — ``a.harrison@ghe.service.group``.
Each registered repo is bound to one (``[repos.<alias>].account``), so switching
repos switches identity and a push can never go out under the wrong token.

Everything here is needed by BOTH adapters — ``mooring account add`` and the hub's
accounts panel run the same device flow, the same upsert, the same removal — so it
lives above them rather than twice inside them (see the module docstring in
``notebooks.py``). Nothing here exits the process; refusals are exceptions the
adapter renders.

The split with ``auth.py`` is deliberate: that module is the OAuth transport and
the credential store and knows nothing about config, while the sequence that turns
a finished flow into a registered account lives here, where importing ``github``
and ``config_store`` is normal.
"""

from __future__ import annotations

from dataclasses import replace

import requests

from mooring import auth, config, config_store, githost
from mooring.github import AccountClient, AuthFailed, GitHubError

# A token is parked here between "GitHub issued it" and "we know whose it is".
# "~" cannot appear in a GitHub login, so this can never collide with a real
# account's slot — and it is not the empty login either, so it never touches the
# pre-accounts host-keyed slot that an unbound repo may still be using.
_PENDING = "~pending~"


class AccountError(Exception):
    """A refusal the adapter should render (bad alias, unknown account, …)."""


def _pending_login(alias: str) -> str:
    return f"{_PENDING}{alias}"


def resolve_login(token: str, host: str, session: requests.Session | None = None) -> str:
    """Ask GitHub who this token belongs to. Raises on failure."""
    return str(AccountClient(token, host, session=session).get_user().get("login", ""))


def start_login(
    alias: str,
    host: str,
    client_id: str,
    session: requests.Session | None = None,
) -> auth.DeviceCode:
    """Begin a device flow for ``alias``, recording the account up front.

    The record is written before the flow completes so an interrupted login has
    somewhere to land — with no ``login`` key, which ``config.Config.token_slot``
    reads as "not signed in" rather than falling back to the host-keyed token.
    """
    config_store.validate_alias(alias)
    host = githost.normalize_host(host)
    if not client_id:
        raise AccountError(
            f"No OAuth client id for {host}. Register an OAuth app on that instance "
            "(with Device Flow enabled) and pass its client id."
        )
    config_store.add_account(alias, host, client_id=client_id)
    return auth.start_device_flow(client_id, session=session, host=host, account=alias)


def finish_login(
    device: auth.DeviceCode,
    token: str,
    session: requests.Session | None = None,
) -> config.Account:
    """Store a freshly-issued token against the identity it actually belongs to.

    Save order is load-bearing. The token goes to a per-alias PENDING slot first,
    then ``GET /user`` names the owner, then it is re-keyed to ``login@host``. Doing
    the lookup first would mean a failure there (SAML session, org restriction, a
    dropped connection) discards a device flow the user has already authorised; and
    parking it in the host-keyed slot instead would overwrite a pre-accounts token
    that an unbound repo is still using. On failure the token stays parked and
    :func:`resume_login` finishes the job without a new code.

    Host, client id and account all come from the DeviceCode rather than live
    config, because the user may have switched repos — and therefore accounts —
    while the flow was in the air.
    """
    auth.save_token(token, host=device.host, login=_pending_login(device.account))
    return _adopt(device.account, device.host, device.client_id, token, session=session)


def resume_login(alias: str, session: requests.Session | None = None) -> config.Account:
    """Finish a login whose token arrived but whose identity lookup failed."""
    app_cfg = config.load_app_config()
    try:
        account = app_cfg.account(alias)
    except KeyError:
        raise AccountError(f"Unknown account {alias!r}.") from None
    token = auth.get_token(env={}, host=account.host, login=_pending_login(alias))
    if not token:
        raise AccountError(f"No pending sign-in for {alias!r} — start the login again.")
    return _adopt(alias, account.host, account.client_id, token, session=session)


def _adopt(
    alias: str,
    host: str,
    client_id: str,
    token: str,
    session: requests.Session | None = None,
) -> config.Account:
    """Name the owner of a parked token and file it under that identity."""
    try:
        login = resolve_login(token, host, session=session)
    except (GitHubError, requests.RequestException) as exc:
        raise AccountError(
            f"Signed in to {host}, but couldn't read the account name: {exc} "
            f"Your sign-in is saved — retry with `mooring account resume {alias}`; "
            "you won't need a new code."
        ) from exc
    if not login:
        raise AccountError(f"Signed in to {host}, but GitHub returned no account name.")

    auth.save_token(token, host=host, login=login)
    auth.delete_token(host=host, login=_pending_login(alias))

    existing = _find(host, login, exclude=alias)
    if existing is not None:
        # This identity already has a record. Collapsing onto it keeps ONE token slot
        # per identity, so removing one alias can't log the other out, and a second
        # sign-in can't silently shadow the first.
        config_store.remove_account(alias)
        return existing

    config_store.add_account(alias, host, login=login, client_id=client_id)
    return config.Account(alias=alias, host=host, login=login, client_id=client_id)


def _find(host: str, login: str, exclude: str = "") -> config.Account | None:
    for a in config.load_app_config().accounts:
        if a.alias != exclude and a.host == host and a.login == login:
            return a
    return None


def forget(alias: str) -> tuple[str, ...]:
    """Remove an account, delete its tokens, and unbind its repos.

    Returns the repo aliases left unbound, so the adapter can say which ones now
    need re-binding. The repos themselves — and their workspaces and sync history —
    are untouched.
    """
    app_cfg = config.load_app_config()
    try:
        account = app_cfg.account(alias)
    except KeyError:
        raise AccountError(f"Unknown account {alias!r}.") from None
    orphaned = config_store.remove_account(alias)
    auth.delete_token(host=account.host, login=_pending_login(alias))
    # Only drop the identity's token if no OTHER alias still points at it — two
    # aliases can legitimately have collapsed onto one (host, login).
    if account.login and _find(account.host, account.login) is None:
        auth.delete_token(host=account.host, login=account.login)
    return orphaned


def status(app_cfg: config.AppConfig) -> tuple[dict, ...]:
    """One value-free row per account, for `account list` and the hub panel.

    No token ever leaves here — only whether one exists.
    """
    return tuple(
        {
            "alias": a.alias,
            "host": a.host,
            "login": a.login,
            "label": a.label,
            "signed_in": bool(a.login and auth.get_token(host=a.host, login=a.login)),
            "active": a.alias == app_cfg.active_account,
            "repos": tuple(s.alias for s in app_cfg.repos if s.account == a.alias),
        }
        for a in app_cfg.accounts
    )


def client_for_account(app_cfg: config.AppConfig, alias: str) -> AccountClient:
    """An account-scoped GitHub client (owner listing, repo listing, repo creation)."""
    try:
        account = app_cfg.account(alias)
    except KeyError:
        raise AccountError(f"Unknown account {alias!r}.") from None
    token = auth.get_token(host=account.host, login=account.login) if account.is_signed_in else None
    if not token:
        raise AuthFailed(f"Account {alias!r} is not signed in.")
    return AccountClient(token, account.host)


def bind(app_cfg: config.AppConfig, repo_alias: str, account_alias: str) -> config.AppConfig:
    """Point a registered repo at an account (in memory; the caller persists)."""
    try:
        app_cfg.account(account_alias)
    except KeyError:
        raise AccountError(f"Unknown account {account_alias!r}.") from None
    try:
        app_cfg.spec(repo_alias)
    except KeyError:
        raise AccountError(f"Unknown repo {repo_alias!r}.") from None
    return replace(
        app_cfg,
        repos=tuple(
            replace(s, account=account_alias) if s.alias == repo_alias else s
            for s in app_cfg.repos
        ),
    )
