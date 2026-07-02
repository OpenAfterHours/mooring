"""The push guard: scan bytes leaving for the team repo, before they leave.

All of mooring's privacy machinery watches the AI channel; this module points
the SAME high-precision detectors (:mod:`mooring.ai.secrets`,
:mod:`mooring.ai.pii` — stdlib-only, no copilot extra needed) at the more
damaging channel: the push itself. Because analysts have no git, mooring is the
only write path into the shared repo, so a gate at the push seam covers every
push the team makes.

This module is the ORCHESTRATOR — candidate policy (text extensions, size
cap), the ``mooring: push-ok`` line pragma, the conservative raw-data
heuristic, and the per-file confirm token — while the detectors stay where
they live. It is deliberately a *second consumer* of the scanners, not a
change to the AI channel: ``ai/egress.py`` and its pinned tests are untouched.

Enforcement rides :func:`mooring.sync.push`'s injected ``guard_fn`` (the
``snapshot_fn`` idiom), so the L2 sync core never imports the scanners; the
adapters build the guard with :func:`make_guard` and surface withheld files
with a warn-and-confirm flow. Like the detectors themselves this is
**defence in depth, never a guarantee** — a clean scan does not mean a file
is value-free (see docs/admins/ai-privacy.md).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from mooring.ai import pii, secrets

# A reviewed false positive can be retired by putting this marker on the line —
# the push-scope sibling of ai/pii.py's "mooring: pii-ok" (which keeps working
# here too, but only for PII findings; this one silences the push guard for
# every detector on that line, without changing what the AI channel scans).
PUSH_OK_MARKER = "mooring: push-ok"

# Only text-like files are scanned; anything else passes through untouched
# (a regex scan of binary bytes is noise). ".platform" is PBIP's required
# dot-named metadata file (see sync.KEEP_DOT_NAMES).
TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".csv", ".tsv",
     ".sql", ".ini", ".cfg", ".tmdl", ".bim", ".pbip", ".pbir"}
)
TEXT_NAMES = frozenset({".platform"})

# Detector scan cap: beyond this the regex pass is skipped (the raw-data
# heuristic below still runs — a huge file is exactly what it exists to flag).
_MAX_SCAN_BYTES = 4 * 1024 * 1024

# The raw-data heuristic: a tabular file with at least this many
# delimiter-consistent rows looks like a data export, not an analysis asset.
_ROW_THRESHOLD = 1000
_TABULAR_SUFFIXES = frozenset({".csv", ".tsv"})


@dataclass(frozen=True)
class Finding:
    line: int  # 1-based line the finding sits on (1 for whole-file findings)
    kind: str  # value-free human label — never the matched substring


def _is_text(rel_path: str) -> bool:
    p = Path(rel_path)
    return p.suffix.lower() in TEXT_SUFFIXES or p.name in TEXT_NAMES


def _looks_like_data_export(text: str, suffix: str) -> int:
    """The row count when ``text`` looks like a bulk tabular export, else 0.

    Deliberately conservative (a false positive here is corrosive): fires only
    for .csv/.tsv, only past a row threshold, and only when the first rows are
    delimiter-consistent (same field count, at least three fields) — a prose
    .csv or a small lookup table never trips it.
    """
    if suffix not in _TABULAR_SUFFIXES:
        return 0
    sep = "\t" if suffix == ".tsv" else ","
    lines = text.splitlines()
    rows = len(lines)
    if rows < _ROW_THRESHOLD:
        return 0
    sample = [ln for ln in lines[:50] if ln.strip()]
    if len(sample) < 10:
        return 0
    fields = sample[0].count(sep) + 1
    if fields < 3:
        return 0
    if any(ln.count(sep) + 1 != fields for ln in sample):
        return 0
    return rows


def scan_text(rel_path: str, data: bytes) -> list[Finding]:
    """Value-free findings for one outgoing file (empty when clean or binary).

    Runs the secret + structured-PII detectors over text-like files, drops any
    finding whose line carries the ``mooring: push-ok`` pragma, and adds the
    raw-data heuristic for tabular files. Read-only: never modifies ``data``.
    """
    if not _is_text(rel_path):
        return []
    text = data.decode("utf-8", "replace")
    findings: list[Finding] = []
    if len(data) <= _MAX_SCAN_BYTES:
        merged = [(f.line, f.kind) for f in secrets.scan(text)]
        merged += [(f.line, f.kind) for f in pii.scan(text)]
        lines = text.splitlines()
        for line, kind in sorted(set(merged)):
            if 1 <= line <= len(lines) and PUSH_OK_MARKER in lines[line - 1]:
                continue  # a reviewed false positive, retired in the diff
            findings.append(Finding(line=line, kind=kind))
    rows = _looks_like_data_export(text, Path(rel_path).suffix.lower())
    if rows:
        findings.append(Finding(line=1, kind=f"bulk data export (~{rows} rows)"))
    return findings


def file_token(rel_path: str, data: bytes, findings: list[Finding]) -> str:
    """A stateless per-file confirm token binding the exact findings set to the
    exact bytes: a confirmed token stops matching the moment the file changes or
    a new finding appears, so an old confirm can never cover new exposure."""
    h = hashlib.sha256()
    h.update(rel_path.encode("utf-8"))
    h.update(hashlib.sha256(data).digest())
    for f in sorted(findings, key=lambda x: (x.line, x.kind)):
        h.update(f"{f.line}:{f.kind}".encode())
    return h.hexdigest()[:16]


def describe(findings: list[Finding]) -> list[str]:
    """Human, value-free one-liners ("line 12: GitHub token") for result lines."""
    return [f"line {f.line}: {f.kind}" for f in findings]


def make_guard(allowed_tokens: frozenset[str] | set[str] = frozenset()):
    """Build a ``guard_fn`` for :func:`mooring.sync.push` / ``propose``.

    The returned ``guard_fn(rel_path, data)`` scans the exact upload bytes and
    returns value-free description strings — sync withholds the file when the
    list is non-empty. A file whose :func:`file_token` is in ``allowed_tokens``
    was explicitly acknowledged (warn mode's "Push anyway") and passes.

    Also returns ``collected``: ``rel_path -> {"findings", "token"}`` for every
    withheld file, from which the adapters build the confirm payload.
    """
    collected: dict[str, dict] = {}

    def guard_fn(rel_path: str, data: bytes) -> list[str]:
        findings = scan_text(rel_path, data)
        if not findings:
            return []
        token = file_token(rel_path, data, findings)
        if token in allowed_tokens:
            return []
        collected[rel_path] = {"findings": findings, "token": token}
        return describe(findings)

    return guard_fn, collected
