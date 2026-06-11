"""GitHub OAuth Device Flow and token storage.

Device flow needs only a public client_id (no secret): the app shows a short
code, the user enters it at https://github.com/login/device, and we poll for
the resulting token. Tokens are stored in the OS credential store via keyring
(Windows Credential Manager / macOS Keychain), with a plaintext-file fallback,
and MOORING_TOKEN overrides everything for CI and tests.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass

import requests

from mooring import paths

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
SCOPE = "repo"
KEYRING_SERVICE = "mooring-github"
KEYRING_USER = "github-token"
TOKEN_FILE_NAME = "token"


class AuthError(Exception):
    pass


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


@dataclass
class PollResult:
    """One poll attempt: exactly one of token/pending is set; pending carries
    the interval to wait before the next attempt."""

    token: str | None = None
    interval: int = 5

    @property
    def pending(self) -> bool:
        return self.token is None


def start_device_flow(client_id: str, session: requests.Session | None = None) -> DeviceCode:
    http = session or requests
    resp = http.post(
        DEVICE_CODE_URL,
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
        TOKEN_URL,
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
        raise AuthError("Login was cancelled on github.com.")
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


def _token_file() -> "os.PathLike[str]":
    return paths.user_config_dir() / TOKEN_FILE_NAME


def _keyring():
    try:
        import keyring
        import keyring.errors  # noqa: F401

        if keyring.get_keyring() is None:
            return None
        return keyring
    except Exception:  # pragma: no cover - environment-dependent
        return None


def save_token(token: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(KEYRING_SERVICE, KEYRING_USER, token)
            return
        except Exception:  # pragma: no cover - backend-dependent
            pass
    path = _token_file()
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


def get_token(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    if env.get("MOORING_TOKEN"):
        return env["MOORING_TOKEN"]
    kr = _keyring()
    if kr is not None:
        try:
            token = kr.get_password(KEYRING_SERVICE, KEYRING_USER)
            if token:
                return token
        except Exception:  # pragma: no cover - backend-dependent
            pass
    path = _token_file()
    if os.path.isfile(path):
        text = open(path, encoding="utf-8").read().strip()
        return text or None
    return None


def delete_token() -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:  # pragma: no cover - includes PasswordDeleteError
            pass
    path = _token_file()
    if os.path.isfile(path):
        os.remove(path)
