"""Host → URL rules for github.com and GitHub Enterprise.

Stdlib-only on purpose: config.py normalizes the host at load time and must
not drag requests into startup. normalize_host() canonicalizes input (the way
gh does) so every host reaching the URL builders lands in one of three classes:

- github.com          → API https://api.github.com
- *.ghe.com           → API https://api.{host}    (GHE Cloud data residency)
- anything else       → API https://{host}/api/v3 (GHE Server)

Canonicalization is what keeps those three exhaustive: any *.github.com
subdomain collapses to github.com and a tenant's *.{tenant}.ghe.com
sub-subdomains collapse to {tenant}.ghe.com, so neither leaks into the
GHE-Server branch and produces a malformed API URL. Device-flow endpoints live
on the web root for all classes (see auth.py).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

DEFAULT_HOST = "github.com"

# hostname labels, optionally followed by :port
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d+)?$")


def normalize_host(value: str) -> str:
    """A bare host or full URL → the canonical lowercase host[:port].

    Empty → DEFAULT_HOST. Canonicalizes the way gh does so one host string
    feeds api_root(), web_root(), and per-host token storage consistently:
    any *.github.com subdomain collapses to github.com, and a data-residency
    tenant's *.{tenant}.ghe.com sub-subdomains collapse to {tenant}.ghe.com.
    A port survives only for GitHub Enterprise Server hosts — github.com and
    *.ghe.com are GitHub-hosted and never carry one.
    """
    text = value.strip()
    if not text:
        return DEFAULT_HOST
    if "//" in text:
        text = urlsplit(text if "://" in text else f"https://{text}").netloc
    text = text.split("@")[-1].split("/")[0].strip().lower()
    if not text:
        return DEFAULT_HOST
    if not _HOST_RE.match(text):
        raise ValueError(f"Not a valid GitHub host: {value.strip()!r}")
    host = text.split(":", 1)[0]
    if host == DEFAULT_HOST or host.endswith("." + DEFAULT_HOST):
        return DEFAULT_HOST
    if host.endswith(".ghe.com"):
        tenant = host[: -len(".ghe.com")].rsplit(".", 1)[-1]
        return f"{tenant}.ghe.com"
    return text


def web_root(host: str) -> str:
    return f"https://{host}"


def api_root(host: str) -> str:
    if host == DEFAULT_HOST:
        return "https://api.github.com"
    if host.endswith(".ghe.com"):
        return f"https://api.{host}"
    return f"{web_root(host)}/api/v3"
