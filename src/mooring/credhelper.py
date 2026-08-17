"""Borrowing the credential git already holds for a host.

Some enterprises restrict which OAuth apps may reach their repositories AND cap
personal access token lifetimes (24 hours is not unusual). That combination blocks
both of mooring's other sign-in methods at once: the device flow needs an approved
OAuth app, and a pasted PAT dies overnight. Yet those same machines clone over HTTPS
every day — so a working credential is already sitting in whatever credential helper
git is configured with. On Windows that is normally Git Credential Manager, whose
GitHub credential is an *OAuth* token (a ``gho_`` prefix) that GCM refreshes on its
own, and which is therefore not governed by the PAT lifetime policy at all.

This module borrows that credential through git's own documented protocol —
``git credential fill`` / ``reject``, see gitcredentials(7) — rather than reading a
platform credential store directly. That choice is what makes it work with GCM,
wincred, libsecret, osxkeychain, or a corporate custom helper alike, with no
knowledge here of where any of them keep their data.

Two rules keep the borrowing safe and durable:

- **Borrow, never copy.** Nothing here writes a credential anywhere. A copy saved
  into mooring's own keyring would go stale with nothing able to refresh it — the
  very problem this module exists to avoid. Callers re-ask instead; ``auth`` keeps
  only a short in-process cache so a hub poll is not a subprocess storm.
- **Never prompt from a background caller.** ``fill`` runs the helper with prompting
  DISABLED by default (``credential.interactive=false`` plus ``GIT_TERMINAL_PROMPT=0``
  and ``GCM_INTERACTIVE=never``), because the hub polls state on a timer and a GCM
  dialog opened behind the browser would hang the request with nothing on screen to
  answer it. Interactive re-auth is opt-in and belongs on an explicit user action.

L0 leaf: stdlib only, so it stays importable at load time and inside the frozen
build. Nothing here logs a secret, embeds one in an exception, or returns one
anywhere but the :class:`Credential` it was asked for.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_TIMEOUT = 20.0

# Discovery probes several hosts back-to-back behind a hub route, so it gets a
# tighter budget than a sign-in the user deliberately asked for, and a ceiling on
# how many hosts it will try at all.
DISCOVER_TIMEOUT = 8.0
MAX_DISCOVER = 8

# hostname labels, optionally followed by :port. Deliberately a copy of githost's
# rule rather than an import: this module is an L0 leaf and stays stdlib-pure, and
# here the pattern only has to REJECT junk before it costs a subprocess —
# canonicalization proper is githost.normalize_host's job, a layer up.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d+)?$")

# Every host mooring talks to is HTTPS (see githost.web_root), and a credential
# helper keys its store on the protocol as well as the host.
_PROTOCOL = "https"

# GitHub stamps a type prefix on its tokens. The prefix is NOT a secret and saying
# which one we borrowed is genuinely useful: gho_/ghu_ are OAuth and GitHub-App
# tokens that a helper can refresh, while ghp_/github_pat_ are personal access
# tokens and so inherit any enterprise PAT-lifetime cap.
_TOKEN_PREFIXES = ("github_pat_", "gho_", "ghu_", "ghp_", "ghs_", "ghr_")

# Prefixes whose credential the helper owns and can renew without us.
_REFRESHABLE = ("gho_", "ghu_")


@dataclass(frozen=True)
class Credential:
    """One borrowed credential. ``password`` is the bearer token GitHub accepts.

    ``expires_at`` is unix seconds when the helper volunteered a
    ``password_expiry_utc`` (git 2.42+); ``None`` means it said nothing, which is
    the common case and does not imply the credential is permanent.
    """

    host: str
    username: str
    password: str
    expires_at: int | None = None

    @property
    def kind(self) -> str:
        """The credential's GitHub type prefix, or ``""`` — value-free."""
        return token_kind(self.password)

    @property
    def refreshable(self) -> bool:
        """Whether the prefix says the helper can renew this without a human.

        Advisory only: an unrecognised prefix (a GHES token, a future scheme) is
        reported False but may still be perfectly durable.
        """
        return self.kind in _REFRESHABLE


def token_kind(secret: str) -> str:
    """A token's GitHub type prefix, or ``""`` if it matches none. Value-free."""
    for prefix in _TOKEN_PREFIXES:
        if secret.startswith(prefix):
            return prefix
    return ""


def available() -> bool:
    """Whether git is on PATH — i.e. whether there can be a credential to borrow."""
    return shutil.which("git") is not None


def _creation_flags() -> int:
    """CREATE_NO_WINDOW on Windows, 0 elsewhere.

    Without it the helper flashes a console window over the hub or the CLI. The
    constant only exists on Windows, and POSIX rejects a NON-zero value, so the
    getattr default is what keeps this portable.
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _child_env(interactive: bool) -> dict[str, str]:
    env = dict(os.environ)
    if not interactive:
        # git's own terminal prompt off, and GCM's GUI off. Belt and braces: a
        # helper that honours neither would hit the timeout in _run instead.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
    return env


def _git_argv(*args: str, interactive: bool) -> list[str]:
    argv = ["git"]
    if not interactive:
        argv += ["-c", "credential.interactive=false"]
    return argv + list(args)


def _run(
    argv: list[str],
    payload: str,
    *,
    interactive: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess | None:
    """Run a helper command, or return None if it cannot be run at all.

    Every failure mode collapses to None on purpose — a missing git, a crashing
    helper, or a prompt we refuse to wait on are all just "no credential
    available", and none of them may raise into a hub route or a sync step.
    """
    try:
        return subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=_child_env(interactive),
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _payload(host: str, path: str = "", username: str = "", password: str = "") -> str:
    """One credential-protocol record. The terminating blank line is required."""
    lines = [f"protocol={_PROTOCOL}", f"host={host}"]
    if path:
        lines.append(f"path={path}")
    if username:
        lines.append(f"username={username}")
    if password:
        lines.append(f"password={password}")
    return "\n".join(lines) + "\n\n"


def _parse(text: str) -> dict[str, str]:
    """Parse a credential-protocol reply. Unknown keys are kept, not rejected.

    Values are taken verbatim after the first ``=`` (a token never contains one,
    but stripping would be a guess). A blank line ends the record.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value
    return fields


def _expiry(fields: dict[str, str]) -> int | None:
    raw = fields.get("password_expiry_utc", "").strip()
    return int(raw) if raw.isdigit() else None


def _secret(fields: dict[str, str]) -> str:
    """The bearer token from a reply, across both credential-protocol shapes.

    git 2.46 added ``authtype``/``credential`` for helpers that hand back a whole
    authorization payload. We accept that ONLY for ``authtype=Bearer``: a Basic
    ``credential`` is base64 ``user:password``, which is not a bearer token and
    must not be sent as one.
    """
    password = fields.get("password", "")
    if password:
        return password
    if fields.get("authtype", "").strip().lower() == "bearer":
        return fields.get("credential", "")
    return ""


def fill(
    host: str,
    path: str = "",
    *,
    interactive: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> Credential | None:
    """One credential lookup for ``host`` (and optionally a ``owner/repo`` path).

    Returns None whenever nothing usable came back, including the case where git
    exists but no helper is configured — with prompting disabled git simply
    answers with no password rather than asking a human.
    """
    host = (host or "").strip().lower()
    if not host or not available():
        return None
    proc = _run(
        _git_argv("credential", "fill", interactive=interactive),
        _payload(host, path),
        interactive=interactive,
        timeout=timeout,
    )
    if proc is None or proc.returncode != 0:
        return None
    fields = _parse(proc.stdout)
    secret = _secret(fields)
    if not secret:
        return None
    return Credential(
        host=host,
        username=fields.get("username", ""),
        password=secret,
        expires_at=_expiry(fields),
    )


def borrow(
    host: str,
    path: str = "",
    *,
    interactive: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> Credential | None:
    """The best credential available for ``host``: git's helper first, then gh.

    The path-qualified lookup is tried BEFORE the host-only one because a helper
    configured with ``credential.useHttpPath`` stores per-repository entries and
    has nothing filed under the bare host; without the fallback the reverse case
    (the normal one) would break instead.

    ``gh auth token`` is a last resort for machines where the GitHub CLI is signed
    in but git's helper is not — the same class of credential, from the same kind
    of OAuth flow.
    """
    for candidate in ([path, ""] if path else [""]):
        cred = fill(host, candidate, interactive=interactive, timeout=timeout)
        if cred is not None:
            return cred
    token = gh_token(host, timeout=timeout)
    if token:
        return Credential(host=host, username="", password=token)
    return None


def reject(cred: Credential | None, path: str = "") -> None:
    """Tell git's helper its credential was refused, so the next fill re-authenticates.

    This is the verb that makes borrowing self-healing rather than one more thing
    that goes stale: it is exactly what git itself does when a fetch gets a 401,
    and it prompts the helper to mint a replacement through whatever flow the
    enterprise has already approved. Best-effort and silent — a helper that does
    not implement ``reject`` is not an error.
    """
    if cred is None or not cred.password or not available():
        return
    _run(
        _git_argv("credential", "reject", interactive=False),
        _payload(cred.host, path, cred.username, cred.password),
    )


def gh_token(host: str, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """The GitHub CLI's token for ``host``, if gh is installed and signed in."""
    if not host or shutil.which("gh") is None:
        return None
    proc = _run(["gh", "auth", "token", "--hostname", host], "", timeout=timeout)
    if proc is None or proc.returncode != 0:
        return None
    first = proc.stdout.strip().splitlines()
    token = first[0].strip() if first else ""
    return token or None


def _config_hosts() -> list[str]:
    """Hosts named in git's own ``credential.<url>.*`` config entries.

    ``git config --get-regexp`` prints ``credential.https://ghe.example.com.helper
    manager`` — the key is ``credential.<url>.<subkey>``, and since the URL itself is
    full of dots the only reliable split is from the RIGHT: drop the one trailing
    subkey. A bare ``credential.helper`` has no URL left after that and is skipped,
    which is correct — it says a helper exists, not which hosts it holds.

    Value-free: config KEYS only. The values (``manager``, ``true``) are never read,
    and a credential never appears in git config in the first place.
    """
    proc = _run(["git", "config", "--get-regexp", r"^credential\."], "")
    if proc is None:
        return []
    hosts: list[str] = []
    for line in proc.stdout.splitlines():
        key = line.split(None, 1)[0] if line.split() else ""
        if not key.startswith("credential."):
            continue
        url, sep, _subkey = key[len("credential.") :].rpartition(".")
        if not sep or not url:
            continue
        host = _host_of(url)
        if host:
            hosts.append(host)
    return hosts


def _host_of(url: str) -> str:
    """The bare host from a credential-config URL, or ``""``.

    Tolerant by design: this parses whatever a user's git config happens to hold, and
    anything unrecognised must drop out rather than become a bogus host we then go and
    probe. Canonicalization is the caller's job (``githost.normalize_host`` lives a
    layer up), so this only strips the parts a URL can carry.
    """
    text = url.strip()
    if not text:
        return ""
    if "//" in text:
        text = urlsplit(text if "://" in text else f"https://{text}").netloc
    host = text.split("@")[-1].split("/")[0].strip().lower()
    # A host with no dot is a LAN name we cannot reach as a GitHub instance, and
    # bracketed IPv6 or a stray wildcard would only waste a probe.
    return host if _HOST_RE.match(host) else ""


def _gh_hosts() -> list[str]:
    """Hosts the GitHub CLI reports being signed in to.

    ``gh auth status`` prints each host unindented with its accounts beneath, and is
    the only enumeration gh offers — there is no ``--json`` form. Parsed leniently
    from both streams (gh has moved this between stdout and stderr across versions)
    and treated as a SUGGESTION: every host found here is probed before it is offered,
    so a format change costs a missing suggestion, never a wrong one.
    """
    if shutil.which("gh") is None:
        return []
    proc = _run(["gh", "auth", "status"], "")
    if proc is None:
        return []
    hosts: list[str] = []
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        if line[:1].isspace() or not line.strip():
            continue
        candidate = line.strip().lower()
        if "." in candidate and _HOST_RE.match(candidate):
            hosts.append(candidate)
    return hosts


@dataclass(frozen=True)
class Probe:
    """A value-free description of what could be borrowed, for the doctor and the UI.

    Carries the token's TYPE PREFIX, never the token: enough to tell a user their
    credential is a refreshable ``gho_`` rather than a capped ``ghp_``, with nothing
    secret in it.
    """

    host: str
    git_present: bool
    found: bool
    kind: str = ""
    refreshable: bool = False
    expires_in: int | None = None

    @property
    def summary(self) -> str:
        if not self.git_present:
            return "git isn't on PATH, so there's no stored credential to borrow."
        if not self.found:
            return f"No stored git credential for {self.host}."
        # The article travels WITH the phrase — "a gho_" but "an unrecognised-type",
        # which a bare f"a {kind}" gets wrong for the second.
        described = f"a {self.kind}" if self.kind else "an unrecognised-type"
        if self.refreshable:
            return f"Found {described} credential for {self.host}, which git can refresh."
        return f"Found {described} credential for {self.host}."


def probe(host: str, *, timeout: float = DEFAULT_TIMEOUT) -> Probe:
    """Look for a borrowable credential and describe it without exposing it."""
    if not available():
        return Probe(host=host, git_present=False, found=False)
    cred = borrow(host, timeout=timeout)
    if cred is None:
        return Probe(host=host, git_present=True, found=False)
    expires_in = None
    if cred.expires_at is not None:
        expires_in = max(0, int(cred.expires_at - time.time()))
    return Probe(
        host=host,
        git_present=True,
        found=True,
        kind=cred.kind,
        refreshable=cred.refreshable,
        expires_in=expires_in,
    )


def candidate_hosts(extra: Sequence[str] = ()) -> list[str]:
    """Hosts that might have a borrowable credential, best guess first.

    The credential protocol is a LOOKUP — ``fill`` answers for a host you name, and
    gitcredentials(7) has no "list" verb — so a helper cannot be asked what it holds.
    Enumeration has to come from elsewhere, and this gathers the two places a host's
    name shows up in plain sight: git's own ``credential.<url>.*`` config and
    ``gh auth status``.

    That makes the result a guess-list, and deliberately so. It misses a host whose
    credential sits in the store with no config entry naming it, which is exactly why
    ``extra`` comes first: the caller's already-known hosts are facts, not guesses.
    Reading a helper's store directly would enumerate properly and is the one thing
    this module will not do — it would tie mooring to GCM or wincred specifically and
    break every other helper.

    Order is preserved and duplicates dropped, so a host the caller already knows is
    never probed twice.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for host in [*extra, *_config_hosts(), *_gh_hosts()]:
        key = (host or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def discover(
    hosts: Sequence[str],
    *,
    timeout: float = DISCOVER_TIMEOUT,
    limit: int = MAX_DISCOVER,
) -> list[Probe]:
    """Probe ``hosts`` and return a value-free Probe for each one that has a credential.

    Bounded on purpose: each host costs at least one subprocess, this runs behind a hub
    route, and a helper that ignores the no-prompt settings would otherwise hold the
    request open once per host. Hence a shorter per-host ``timeout`` than a sign-in the
    user deliberately asked for, and a hard ``limit`` on how many are tried at all.

    Hosts with nothing to borrow are omitted rather than reported as absent: the caller
    is building an offer, and "no credential here" is not something to show anyone.
    """
    found: list[Probe] = []
    for host in list(hosts)[: max(0, limit)]:
        result = probe(host, timeout=timeout)
        if result.found:
            found.append(result)
    return found
