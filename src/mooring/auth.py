"""GitHub OAuth Device Flow and token storage.

Device flow needs only a public client_id (no secret): the app shows a short
code, the user enters it at {host}/login/device, and we poll for the
resulting token. Works against github.com and GitHub Enterprise alike — the
flow's endpoints live on the instance's web root. Tokens are stored in the
OS credential store via keyring (Windows Credential Manager / macOS
Keychain), with a plaintext-file fallback, keyed by ACCOUNT — login@host — so
two GitHub users can coexist on one instance and a token never gets sent to a
different GitHub instance; MOORING_TOKEN overrides everything for CI and tests.

This module is deliberately just the OAuth transport and the credential store.
The sequence that turns a finished device flow into a registered account (poll →
GET /user → save → write the config record) lives in ``app/accounts.py``, above
both adapters — keeping it out of here is what stops an L1 identity leaf from
growing a dependency on ``github`` and ``config_store``.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import requests

from mooring import credhelper, githost, paths

SCOPE = "repo"
KEYRING_SERVICE = "mooring-github"
KEYRING_USER = "github-token"
TOKEN_FILE_NAME = "token"

# The one sign-in method whose credential is NOT stored here. The full vocabulary
# (and its parsing/validation) lives in config.AUTH_METHODS, which owns the config
# key; this module only has to recognise the borrowed case.
GIT_METHOD = "git"


def device_code_url(host: str = githost.DEFAULT_HOST) -> str:
    return f"{githost.web_root(host)}/login/device/code"


def token_url(host: str = githost.DEFAULT_HOST) -> str:
    return f"{githost.web_root(host)}/login/oauth/access_token"


class AuthError(Exception):
    pass


def device_flow_hint(host: str, exc: Exception) -> str:
    """A friendly one-line explanation for a failed device-code request.

    Names the host (and HTTP status, if any) so a misrouted login is obvious,
    and only suggests setting a host when the request went to the default
    github.com — a real GHE host that 404s has a different cause (device flow
    disabled, or a client_id from the wrong instance).
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    head = f"Couldn't start GitHub login against {host}"
    head += f" (HTTP {status})." if status else f": {exc}"
    if host == githost.DEFAULT_HOST:
        head += (
            " If this repo is on GitHub Enterprise, set its host: run "
            '`mooring login --host ghe.example.com`, or add `host = "ghe.example.com"` '
            "under [github] in your config."
        )
    return head


@dataclass
class DeviceCode:
    """One in-flight device flow, carrying everything needed to finish it.

    ``host``, ``client_id`` and ``account`` are captured at START and must be used
    for the poll and the save rather than re-read from live config: the user can
    switch repos (and therefore accounts) mid-login, and polling with a different
    account's client_id returns ``unauthorized_client``.
    """

    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int
    host: str = githost.DEFAULT_HOST
    client_id: str = ""
    account: str = ""


@dataclass
class PollResult:
    """One poll attempt: exactly one of token/pending is set; pending carries
    the interval to wait before the next attempt."""

    token: str | None = None
    interval: int = 5

    @property
    def pending(self) -> bool:
        return self.token is None


def start_device_flow(
    client_id: str,
    session: requests.Session | None = None,
    host: str = githost.DEFAULT_HOST,
    account: str = "",
) -> DeviceCode:
    http = session or requests
    resp = http.post(
        device_code_url(host),
        data={"client_id": client_id, "scope": SCOPE},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "device_code" not in data:
        raise AuthError(f"GitHub rejected the device-flow request: {data}")
    return DeviceCode(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        interval=int(data.get("interval", 5)),
        expires_in=int(data.get("expires_in", 900)),
        host=host,
        client_id=client_id,
        account=account,
    )


def poll_once(
    client_id: str,
    device: DeviceCode,
    interval: int | None = None,
    session: requests.Session | None = None,
) -> PollResult:
    """Single token-poll attempt. Raises AuthError on terminal failures.

    The device's OWN client_id wins over the passed one when it has been captured:
    a caller that re-reads client_id from live config would send the wrong app's id
    after a mid-login repo switch, and GitHub answers `unauthorized_client`.
    """
    http = session or requests
    current = interval if interval is not None else device.interval
    resp = http.post(
        token_url(device.host),
        data={
            "client_id": device.client_id or client_id,
            "device_code": device.device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" in data:
        return PollResult(token=data["access_token"])
    error = data.get("error", "")
    if error == "authorization_pending":
        return PollResult(interval=current)
    if error == "slow_down":
        return PollResult(interval=int(data.get("interval", current + 5)))
    if error == "expired_token":
        raise AuthError("The login code expired. Start the login again.")
    if error == "access_denied":
        raise AuthError("Login was cancelled on GitHub.")
    raise AuthError(f"GitHub login failed: {data.get('error_description', error or data)}")


def poll_for_token(
    client_id: str,
    device: DeviceCode,
    session: requests.Session | None = None,
    sleep=time.sleep,
    clock=time.monotonic,
) -> str:
    """Blocking poll loop used by the CLI; the hub polls via poll_once instead."""
    deadline = clock() + device.expires_in
    interval = device.interval
    while True:
        if clock() >= deadline:
            raise AuthError("The login code expired. Start the login again.")
        result = poll_once(client_id, device, interval=interval, session=session)
        if result.token:
            return result.token
        interval = result.interval
        sleep(interval)


# Storage slots. A token belongs to one ACCOUNT — an identity on an instance —
# so the slot is keyed by login@host, which is what lets two GitHub users coexist
# on the same host. `login=""` means the pre-account scheme: the default host
# keeps the pre-0.2 key/filename so existing logins survive the upgrade, and
# other hosts get their own host-keyed slot so a token is never sent to a
# different GitHub instance after the host setting changes.
#
# SECURITY: an empty login is *not* a wildcard. Callers must only pass `login=""`
# when the config has no [accounts] at all; a configured account whose login is
# still blank (interrupted device flow, GET /user failure) must be treated as
# NOT logged in, never resolved onto whatever legacy token happens to sit in the
# host-keyed slot — that would silently push as the previous user. See
# config.Config.token_login, which is the only place that decision is made.


def _slot(host: str, login: str = "") -> str:
    """The account's storage slot: ``login@host``, or bare ``host`` pre-accounts."""
    return f"{login}@{host}" if login else host


def _keyring_user(host: str, login: str = "") -> str:
    if not login and host == githost.DEFAULT_HOST:
        return KEYRING_USER
    return f"{KEYRING_USER}@{_slot(host, login)}"


def _token_file(host: str, login: str = "") -> Path:
    if not login and host == githost.DEFAULT_HOST:
        return paths.user_config_dir() / TOKEN_FILE_NAME
    slot = _slot(host, login).replace(":", "_")
    return paths.user_config_dir() / f"{TOKEN_FILE_NAME}-{slot}"


def _keyring():
    try:
        import keyring
        import keyring.errors  # noqa: F401

        if keyring.get_keyring() is None:
            return None
        return keyring
    except Exception:  # pragma: no cover - environment-dependent
        return None


def save_token(token: str, host: str = githost.DEFAULT_HOST, login: str = "") -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(KEYRING_SERVICE, _keyring_user(host, login), token)
            return
        except Exception:  # pragma: no cover - backend-dependent
            pass
    path = _token_file(host, login)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, "utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - chmod is best-effort on Windows
        pass
    print(f"Warning: no OS credential store available; token saved as plain text at {path}.")


def get_token(
    env: Mapping[str, str] | None = None,
    host: str = githost.DEFAULT_HOST,
    login: str = "",
) -> str | None:
    env = os.environ if env is None else env
    if env.get("MOORING_TOKEN"):
        return env["MOORING_TOKEN"]
    kr = _keyring()
    if kr is not None:
        try:
            token = kr.get_password(KEYRING_SERVICE, _keyring_user(host, login))
            if token:
                return token
        except Exception:  # pragma: no cover - backend-dependent
            pass
    path = _token_file(host, login)
    if os.path.isfile(path):
        text = open(path, encoding="utf-8").read().strip()
        return text or None
    return None


# -- Borrowed credentials (the "git" method) -----------------------------------
# A borrowed credential is deliberately NEVER saved: git's helper stays its owner,
# which is the whole point — the helper can refresh it and mooring cannot. What we
# keep is a short in-process cache, because token_for runs on every hub state poll
# and every sync step, and each miss is a subprocess spawn.
BORROW_TTL = 60.0
_borrowed: dict[str, tuple[float, credhelper.Credential]] = {}


def borrowed_credential(
    host: str, path: str = "", *, clock=time.monotonic
) -> credhelper.Credential | None:
    """The credential git currently holds for ``host``, cached briefly."""
    now = clock()
    cached = _borrowed.get(host)
    if cached is not None and now < cached[0]:
        return cached[1]
    cred = credhelper.borrow(host, path)
    if cred is None:
        _borrowed.pop(host, None)
        return None
    good_until = now + BORROW_TTL
    if cred.expires_at is not None:
        # Never hold it past the helper's own stated expiry, however short.
        good_until = min(good_until, now + max(0.0, cred.expires_at - time.time()))
    _borrowed[host] = (good_until, cred)
    return cred


def borrowed_token(host: str, path: str = "") -> str | None:
    cred = borrowed_credential(host, path)
    return cred.password if cred is not None else None


def forget_borrowed(host: str = "") -> None:
    """Drop the cache for one host (or all), forcing the next read to re-ask git."""
    if host:
        _borrowed.pop(host, None)
    else:
        _borrowed.clear()


def reject_borrowed(host: str, path: str = "") -> None:
    """Report a borrowed credential as refused, so git's helper mints a new one.

    This is the borrowed-credential answer to a 401, and it replaces
    :func:`delete_token` for that method — there is no stored token to delete, and
    the fix is to make the helper re-authenticate rather than to send the user back
    through a sign-in mooring cannot perform.
    """
    cached = _borrowed.pop(host, None)
    cred = cached[1] if cached is not None else credhelper.fill(host, path)
    credhelper.reject(cred, path)


def token_for(
    slot: tuple[str, str] | None,
    env: Mapping[str, str] | None = None,
    method: str = "device",
) -> str | None:
    """The token for a ``Config.token_slot``, or None when the slot is None.

    Takes the resolved ``(host, login)`` pair rather than a Config so this stays a
    plain identity leaf. Every caller should route through here instead of calling
    ``get_token`` with a hand-assembled login: a ``None`` slot means "this repo's
    account cannot have a token", and passing ``login=""`` in that case would read
    the pre-accounts host-keyed slot and hand back the wrong user's credential.

    ``method`` selects the credential SOURCE (``Config.auth_method``). It defaults
    to the stored-token behaviour, which is fail-closed: a caller that forgets to
    pass it gets None for a borrowed account — "not signed in" — rather than
    somebody else's credential. A ``None`` slot still wins over everything, so an
    account that never finished signing in can't borrow one either.
    """
    if slot is None:
        return None
    host, login = slot
    env_map = os.environ if env is None else env
    # MOORING_TOKEN overrides every source, including a borrowed one — it is the
    # CI/test escape hatch and must behave identically for all methods.
    if env_map.get("MOORING_TOKEN"):
        return env_map["MOORING_TOKEN"]
    if method == GIT_METHOD:
        return borrowed_token(host)
    return get_token(env=env, host=host, login=login)


def delete_token(host: str = githost.DEFAULT_HOST, login: str = "") -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, _keyring_user(host, login))
        except Exception:  # pragma: no cover - includes PasswordDeleteError
            pass
    path = _token_file(host, login)
    if os.path.isfile(path):
        os.remove(path)
