"""Host → URL rules for github.com and GitHub Enterprise.

Stdlib-only on purpose: config.py normalizes the host at load time and must
not drag requests into startup. The three host classes (mirroring gh CLI):

- github.com          → API https://api.github.com
- *.ghe.com           → API https://api.{host}    (GHE Cloud data residency)
- anything else       → API https://{host}/api/v3 (GHE Server)

Device-flow endpoints live on the web root for all classes (see auth.py).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

DEFAULT_HOST = "github.com"

# hostname labels, optionally followed by :port
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d+)?$")


def normalize_host(value: str) -> str:
    """A bare host or full URL → lowercase host[:port]; empty → DEFAULT_HOST."""
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
    return text


def web_root(host: str) -> str:
    return f"https://{host}"


def api_root(host: str) -> str:
    if host == DEFAULT_HOST:
        return "https://api.github.com"
    if host.endswith(".ghe.com"):
        return f"https://api.{host}"
    return f"{web_root(host)}/api/v3"
