"""GitHub OAuth Device Flow and token storage.

Device flow needs only a public client_id (no secret): the app shows a short
code, the user enters it at {host}/login/device, and we poll for the
resulting token. Works against github.com and GitHub Enterprise alike — the
flow's endpoints live on the instance's web root. Tokens are stored in the
OS credential store via keyring (Windows Credential Manager / macOS
Keychain), with a plaintext-file fallback, keyed by host so a token never
gets sent to a different GitHub instance; MOORING_TOKEN overrides everything
for CI and tests.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass

import requests

from mooring import githost, paths

SCOPE = "repo"
KEYRING_SERVICE = "mooring-github"
KEYRING_USER = "github-token"
TOKEN_FILE_NAME = "token"


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
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int
    host: str = githost.DEFAULT_HOST


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
    )


def poll_once(
    client_id: str,
    device: DeviceCode,
    interval: int | None = None,
    session: requests.Session | None = None,
) -> PollResult:
    """Single token-poll attempt. Raises AuthError on terminal failures."""
    http = session or requests
    current = interval if interval is not None else device.interval
    resp = http.post(
        token_url(device.host),
        data={
            "client_id": client_id,
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


# The default host keeps the pre-0.2 key/filename so existing logins survive
# the upgrade; other hosts get their own slot so a token is never sent to a
# different GitHub instance after the host setting changes.


def _keyring_user(host: str) -> str:
    if host == githost.DEFAULT_HOST:
        return KEYRING_USER
    return f"{KEYRING_USER}@{host}"


def _token_file(host: str) -> "os.PathLike[str]":
    if host == githost.DEFAULT_HOST:
        return paths.user_config_dir() / TOKEN_FILE_NAME
    return paths.user_config_dir() / f"{TOKEN_FILE_NAME}-{host.replace(':', '_')}"


def _keyring():
    try:
        import keyring
        import keyring.errors  # noqa: F401

        if keyring.get_keyring() is None:
            return None
        return keyring
    except Exception:  # pragma: no cover - environment-dependent
        return None


def save_token(token: str, host: str = githost.DEFAULT_HOST) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(KEYRING_SERVICE, _keyring_user(host), token)
            return
        except Exception:  # pragma: no cover - backend-dependent
            pass
    path = _token_file(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, "utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - chmod is best-effort on Windows
        pass
    print(
        "Warning: no OS credential store available; "
        f"token saved as plain text at {path}."
    )


def get_token(
    env: Mapping[str, str] | None = None, host: str = githost.DEFAULT_HOST
) -> str | None:
    env = os.environ if env is None else env
    if env.get("MOORING_TOKEN"):
        return env["MOORING_TOKEN"]
    kr = _keyring()
    if kr is not None:
        try:
            token = kr.get_password(KEYRING_SERVICE, _keyring_user(host))
            if token:
                return token
        except Exception:  # pragma: no cover - backend-dependent
            pass
    path = _token_file(host)
    if os.path.isfile(path):
        text = open(path, encoding="utf-8").read().strip()
        return text or None
    return None


def delete_token(host: str = githost.DEFAULT_HOST) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, _keyring_user(host))
        except Exception:  # pragma: no cover - includes PasswordDeleteError
            pass
    path = _token_file(host)
    if os.path.isfile(path):
        os.remove(path)
