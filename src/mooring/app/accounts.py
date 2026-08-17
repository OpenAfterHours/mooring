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

from mooring import auth, config, config_store, credhelper, githost
from mooring.github import AccountClient, AuthFailed, GitHubClient, GitHubError

# A token is parked here between "GitHub issued it" and "we know whose it is".
# "~" cannot appear in a GitHub login, so this can never collide with a real
# account's slot — and it is not the empty login either, so it never touches the
# pre-accounts host-keyed slot that an unbound repo may still be using.
_PENDING = "~pending~"


class AccountError(Exception):
    """A refusal the adapter should render (bad alias, unknown account, …)."""


def _pending_login(alias: str) -> str:
    return f"{_PENDING}{alias}"


def fresh_alias(app_cfg: config.AppConfig, host: str) -> str:
    """An unused account alias derived from the host (github, ghe, ghe-2, …).

    Needed by BOTH adapters — the CLI's `login` on a pre-accounts repo and the hub's
    equivalent both have to invent an alias for an account the user never named — so
    it lives here rather than once in each.
    """
    import re

    base = re.sub(r"[^A-Za-z0-9_-]", "-", host.split(":")[0].split(".")[0]) or "github"
    taken = {a.alias for a in app_cfg.accounts}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


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


def sign_in_with_git(
    alias: str,
    host: str,
    session: requests.Session | None = None,
) -> config.Account:
    """Register an account that BORROWS the credential git already holds for ``host``.

    The escape hatch for an org that restricts OAuth apps (so the device flow cannot
    run) and caps personal access token lifetimes (so a pasted token is useless): the
    credential behind the user's daily `git clone` is neither, and git's helper keeps
    it alive without us.

    Unlike :func:`finish_login` this stores NO token. The account record only says
    *where the credential comes from*; every read goes back to the helper, which is
    what makes it refresh for free — and leaves one fewer copy of a credential on the
    machine than either other method. It follows that there is nothing to park and no
    :func:`resume_login` equivalent: if the identity lookup fails, re-running this
    costs the user nothing.

    Like ``_adopt``, an identity that already has a record collapses onto it rather
    than gaining a second one, so re-running this against an account that used the
    device flow SWITCHES it to borrowed credentials — which is exactly what someone
    running it is asking for.
    """
    config_store.validate_alias(alias)
    host = githost.normalize_host(host)
    if not credhelper.available():
        raise AccountError(
            "git isn't on PATH, so there's no stored credential to borrow. Install git, "
            "or sign in another way."
        )
    cred = credhelper.borrow(host)
    if cred is None:
        raise AccountError(
            f"No stored git credential for {host}. Clone a repository from {host} over "
            "HTTPS first — an SSH clone (git@…) stores no credential that can reach the "
            "GitHub API — or sign in another way."
        )
    try:
        login = resolve_login(cred.password, host, session=session)
    except (GitHubError, requests.RequestException) as exc:
        raise AccountError(
            f"Found a git credential for {host}, but couldn't read the account name "
            f"with it: {exc} It may not carry API access — check that you can reach "
            f"{host} in a browser, then try again."
        ) from exc
    if not login:
        raise AccountError(f"Signed in to {host}, but GitHub returned no account name.")

    # One record per identity (see _adopt), and the alias the caller asked for only
    # wins when this identity is new.
    existing = _find(host, login)
    target = existing.alias if existing is not None else alias
    config_store.add_account(target, host, login=login, auth=config.AUTH_GIT)
    # Drop any cache entry from before the account existed so the first real read
    # re-asks the helper rather than trusting a probe from a second ago.
    auth.forget_borrowed(host)
    return config.Account(alias=target, host=host, login=login, auth=config.AUTH_GIT)


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

    # auth is passed explicitly so a device flow re-run on an account that had been
    # switched to borrowed credentials moves it back, instead of storing a token the
    # "git" method would then ignore.
    config_store.add_account(
        alias, host, login=login, client_id=client_id, auth=config.AUTH_DEVICE
    )
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
    # A borrowed account has no stored token to delete, but it may have a cached one
    # in this process. Dropping it is what makes "remove" take effect immediately.
    auth.forget_borrowed(account.host)
    # Only drop the identity's token if no OTHER alias still points at it — two
    # aliases can legitimately have collapsed onto one (host, login).
    if account.login and _find(account.host, account.login) is None:
        auth.delete_token(host=account.host, login=account.login)
    return orphaned


def account_token(account: config.Account) -> str | None:
    """The credential for an account, from whichever source its method names.

    The account-scoped analogue of ``auth.token_for(cfg.token_slot, ...)``: same
    fail-closed rule (an account that never finished signing in has no credential,
    whatever its method) with the account rather than a repo as the starting point.
    """
    if not account.is_signed_in:
        return None
    return auth.token_for((account.host, account.login), method=account.auth)


def _signed_in(account: config.Account) -> bool:
    """Whether this account can produce a credential, cheaply.

    A borrowed account is judged on its RECORD, not by asking git: ``status`` runs on
    every hub state poll, and a subprocess per account per poll would be a real cost
    for no gain — a credential that has since gone will surface as the 401 that
    ``reject_borrowed`` is there to handle.
    """
    if not account.login:
        return False
    if account.auth == config.AUTH_GIT:
        return True
    return bool(auth.get_token(host=account.host, login=account.login))


def status(app_cfg: config.AppConfig) -> tuple[dict, ...]:
    """One value-free row per account, for `account list` and the hub panel.

    No token ever leaves here — only whether one exists, and which SOURCE it comes
    from, so an adapter can say "borrowed from git" and offer the right repair when a
    credential stops working.
    """
    return tuple(
        {
            "alias": a.alias,
            "host": a.host,
            "login": a.login,
            "label": a.label,
            "auth": a.auth,
            "signed_in": _signed_in(a),
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
    token = account_token(account)
    if not token:
        raise AuthFailed(f"Account {alias!r} is not signed in.")
    return AccountClient(token, account.host)


def repo_client_for_account(
    app_cfg: config.AppConfig, alias: str, owner: str, repo: str
) -> GitHubClient:
    """A repo-scoped client authenticated as ``alias``, for a repo not yet registered.

    Needed for the seed-the-folders step right after ``create_repo``: the repo has
    no ``[repos]`` entry yet, so ``notebooks.client_for`` has nothing to resolve.
    """
    try:
        account = app_cfg.account(alias)
    except KeyError:
        raise AccountError(f"Unknown account {alias!r}.") from None
    token = account_token(account)
    if not token:
        raise AuthFailed(f"Account {alias!r} is not signed in.")
    return GitHubClient(token, owner, repo, host=account.host)


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
