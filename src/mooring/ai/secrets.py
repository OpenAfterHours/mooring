"""Best-effort secret detection for user-authored context files.

This is **defence in depth, never a guarantee**. The primary controls for the
context feature are the fixed five-slot data-dictionary allowlist
(:mod:`mooring.ai.datadictionary`) and human review; this scanner only catches
the obvious, high-confidence leaks (private keys, cloud/API tokens, connection
strings with embedded credentials) before context text is sent to the model or
pushed to the team repo.

It deliberately favours **precision over recall**: a clean scan does NOT mean a
file is value-free. PII in prose — a customer name, an internal account code, a
sample value typed into a column description — is not detectable by regex and is
out of scope by design. Findings never carry the matched value, only a line
number and a human-readable kind, so the scanner's own output is value-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    line: int  # 1-based line number within the scanned text
    kind: str  # human label, e.g. "connection string with credentials" — never the value


# (kind, pattern). High-precision only: each pattern targets a token/credential
# shape that essentially never appears by accident in a schema/instructions file.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("API secret key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "JWT",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    # proto://[user]:pass@host — a DSN/URL carrying real credentials. The user
    # part is optional so password-only DSNs (e.g. redis://:pass@host) still match.
    (
        "connection string with credentials",
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:@/]*:[^\s:@/]+@", re.IGNORECASE),
    ),
)


def scan(text: str) -> list[Finding]:
    """Return high-confidence secret findings in ``text`` (empty list if clean).

    Best-effort only — see the module docstring. The result is value-free: it
    records where and what kind, never the matched substring.
    """
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _PATTERNS:
            if pattern.search(line):
                findings.append(Finding(line=lineno, kind=kind))
    return findings


def has_secrets(text: str) -> bool:
    return bool(scan(text))
