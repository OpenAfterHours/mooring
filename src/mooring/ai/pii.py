"""Best-effort structured-PII detection for text leaving mooring for the AI server.

A SIBLING of :mod:`mooring.ai.secrets` (which catches credentials): this module
catches the *structured personal identifiers* an analyst might mistakenly type
into a chat prompt, hard-code into a notebook cell, or carry in a pivoted
dataframe's column name — payment cards, IBANs, NHS numbers, emails, UK NINOs —
*before* that text reaches GitHub Copilot.

It is **defence in depth, never a guarantee** — exactly like the secret scanner.
The real privacy guarantee remains structural: schema-only tools, the deny-all
permission backstop, the empty working dir, and human review. This adds a thin,
deterministic floor on top.

Design rules, all chosen for PRECISION over recall (a false block is corrosive —
it trains analysts to ignore the warning):

* **Checksum-validated** kinds (``payment card`` via Luhn, ``IBAN`` via ISO
  7064 mod-97-10, ``NHS number`` via mod-11) are high-confidence even inside
  code, and are the only kinds aggressive enough to *withhold* a column name or
  a whole instructions file (see :data:`CHECKSUM_KINDS`).
* **Shape-anchored** kinds (``email address``, ``UK National Insurance number``)
  carry no checksum, so they only ever *warn* or drop a single line.
* Findings are **value-free**: a :class:`Finding` is ``(line, kind)`` — never the
  matched substring — so the scanner's own output can be logged and shown safely.
* What it CANNOT catch, by construction: names, addresses, account/customer
  narratives, UK sort codes, US SSNs, phone numbers, dates of birth, IP
  addresses. A clean scan is **not** a value-free guarantee. (Person NAMES can be
  caught by the optional local NER pass in :mod:`mooring.ai.ner` — Phase 2, opt-in
  via the ``mooring[pii]`` extra; combined with this scanner in :func:`scan_prose`
  and :func:`guard_prompt`.)

Pure stdlib (``re`` + integer arithmetic): no third-party dependency, so it ships
in the lean wheel and freezes into the single-minor ``.pyz`` with no size impact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A reviewed false positive can be retired by putting this marker on the line
# (in the spirit of a lint-suppression comment); the scanner then skips it.
SUPPRESS_MARKER = "mooring: pii-ok"

# Scan at most this many characters per line, so a pathological mega-line can
# neither stall the regex engine nor reach the checksum helpers with a huge int.
_MAX_LINE = 4096

# Kind labels (also what a Finding.kind reads as — keep them value-free).
CARD = "payment card"
IBAN = "IBAN"
NHS = "NHS number"
EMAIL = "email address"
NINO = "UK National Insurance number"

# The kinds backed by a checksum: high enough confidence to act on destructively
# (withhold a column name, withhold a whole instructions file). The shape-only
# kinds (EMAIL, NINO) are deliberately excluded — they only warn / drop a line.
CHECKSUM_KINDS = frozenset({CARD, IBAN, NHS})


@dataclass(frozen=True)
class Finding:
    line: int  # 1-based line number within the scanned text
    kind: str  # one of the kind labels above — never the matched value


# -- candidate patterns (a match is only a *candidate* until it validates) -----

# 13-19 digits, optionally single space/hyphen separated, as a standalone token.
_CARD = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")
# Country code + 2 check digits + 11-30 alphanumerics, contiguous (the common
# form in data/CSV/code). Printed IBANs with spaces are intentionally not caught.
_IBAN = re.compile(r"(?<![\w-])[A-Za-z]{2}\d{2}[A-Za-z0-9]{11,30}(?![\w-])")
# 10 digits in the canonical 3-3-4 grouping (or contiguous).
_NHS = re.compile(r"(?<![\w-])\d{3}[ -]?\d{3}[ -]?\d{4}(?![\w-])")
_EMAIL = re.compile(
    r"(?<![\w.%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}(?![\w\-])"
)
# UK NINO: 2 prefix letters (1st not D F I Q U V; 2nd not D F I O Q U V),
# 6 digits, suffix A-D. Shape only — no checksum exists.
_NINO = re.compile(r"(?<![\w])[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\d{6}[A-D](?![\w])", re.IGNORECASE)

# Domains whose "TLD" means this @-token is an asset/path, not an email.
_NON_EMAIL_TLDS = frozenset(
    {"png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico", "mp4", "mov", "css", "js"}
)
# NINO prefixes the scheme never issues.
_NINO_BAD_PREFIX = frozenset({"BG", "GB", "NK", "KN", "NT", "TN", "ZZ"})

_IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BR": 29, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18,
    "DO": 28, "EE": 20, "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22,
    "GI": 23, "GL": 18, "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IS": 26,
    "IT": 27, "KW": 30, "KZ": 20, "LB": 28, "LI": 21, "LT": 20, "LU": 20, "LV": 21,
    "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MT": 31, "MU": 30, "NL": 18, "NO": 15,
    "PL": 28, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24, "SE": 24, "SI": 19,
    "SK": 24, "SM": 27, "TN": 24, "TR": 26, "UA": 29, "VG": 24,
}

# Canonical industry TEST PANs — deliberate non-PII placeholders analysts paste
# precisely to AVOID real data. Flagging them is the most corrosive false positive.
_TEST_PANS = frozenset(
    {"4111111111111111", "4242424242424242", "5555555555554444", "378282246310005",
     "371449635398431", "6011111111111117", "5105105105105100"}
)


# -- validators ----------------------------------------------------------------


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _trivial_run(digits: str) -> bool:
    """All-identical or a strictly +/-1 (mod 10) monotonic run — never a real id."""
    if len(set(digits)) == 1:
        return True
    asc = all((int(digits[i + 1]) - int(digits[i])) % 10 == 1 for i in range(len(digits) - 1))
    desc = all((int(digits[i]) - int(digits[i + 1])) % 10 == 1 for i in range(len(digits) - 1))
    return asc or desc


def _is_card(digits: str) -> bool:
    if not (13 <= len(digits) <= 19):
        return False
    if digits[0] not in "3456":  # real card networks start 3/4/5/6
        return False
    if digits in _TEST_PANS or _trivial_run(digits):
        return False
    return _luhn_ok(digits)


def _iban_ok(s: str) -> bool:
    s = s.upper()
    cc = s[:2]
    if cc not in _IBAN_LENGTHS or len(s) != _IBAN_LENGTHS[cc]:
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


def _nhs_ok(digits: str) -> bool:
    if len(digits) != 10:
        return False
    total = sum(int(digits[i]) * (10 - i) for i in range(9))
    check = 11 - (total % 11)
    if check == 11:
        check = 0
    if check == 10:  # an invalid check digit by construction
        return False
    return check == int(digits[9]) and not _trivial_run(digits)


def _is_email(match: str) -> bool:
    domain = match.rsplit("@", 1)[-1]
    labels = domain.split(".")
    tld = labels[-1].lower()
    if tld in _NON_EMAIL_TLDS:
        return False
    # retina/asset shapes like "arr@2x.png" -> domain label before TLD is "2x"
    if len(labels) >= 2 and re.fullmatch(r"\d+x", labels[-2]):
        return False
    return True


def _is_nino(match: str) -> bool:
    return match[:2].upper() not in _NINO_BAD_PREFIX


# -- scanning ------------------------------------------------------------------


def _scan_line(line: str, lineno: int) -> list[Finding]:
    found: list[Finding] = []
    for m in _CARD.finditer(line):
        if _is_card(re.sub(r"[ -]", "", m.group())):
            found.append(Finding(lineno, CARD))
    for m in _IBAN.finditer(line):
        if _iban_ok(m.group()):
            found.append(Finding(lineno, IBAN))
    for m in _NHS.finditer(line):
        if _nhs_ok(re.sub(r"[ -]", "", m.group())):
            found.append(Finding(lineno, NHS))
    for m in _EMAIL.finditer(line):
        if _is_email(m.group()):
            found.append(Finding(lineno, EMAIL))
    for m in _NINO.finditer(line):
        if _is_nino(m.group()):
            found.append(Finding(lineno, NINO))
    return found


def scan(text: str) -> list[Finding]:
    """Return value-free structured-PII findings in ``text`` (empty if clean).

    Best-effort only — see the module docstring. A line carrying the
    :data:`SUPPRESS_MARKER` comment is skipped (a reviewed false positive).
    """
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if SUPPRESS_MARKER in line:
            continue
        findings.extend(_scan_line(line[:_MAX_LINE], lineno))
    return findings


def has_pii(text: str) -> bool:
    return bool(scan(text))


def scrub_columns(
    columns: tuple[tuple[str, str], ...],
) -> tuple[tuple[tuple[str, str], ...], list[Finding]]:
    """Drop any column whose NAME is a CHECKSUM-VALIDATED PII value, returning
    ``(kept, findings)``.

    A column whose header passes a checksum (card/IBAN/NHS) is almost certainly a
    *value* promoted to a name by a pivot/transpose on a PII key (e.g.
    ``df.pivot(on="customer_pan")``), so it is withheld from the schema before it
    reaches the model. The shape-only kinds (email, NINO) are deliberately NOT
    withheld here — they are too low-confidence to silently drop a real,
    legitimately named column (a ``support@acme.com`` or product-code ``AB123456C``
    header), which would hand the model an incomplete schema. Findings carry the
    column position as ``line`` — value-free.
    """
    kept: list[tuple[str, str]] = []
    findings: list[Finding] = []
    for i, (name, dtype) in enumerate(columns, start=1):
        hits = [h for h in _scan_line(str(name), i) if h.kind in CHECKSUM_KINDS]
        if hits:
            findings.append(Finding(i, hits[0].kind))
        else:
            kept.append((name, dtype))
    return tuple(kept), findings


def scan_prose(
    text: str,
    *,
    names: bool = False,
    labels: tuple[str, ...] | None = None,
    threshold: float = 0.7,
    model: str | None = None,
    backend: str = "gliner",
) -> list[Finding]:
    """Structured-PII findings, plus NER names when ``names`` and the extra is ready.

    The ADVISORY scanner (notebook-source banner, ``mooring ai pii check``): a
    missing/failed NER backend degrades SILENTLY to structured-only. The enforcing
    prompt valve (:func:`guard_prompt`) is strict instead — it reports the failure.
    ``backend`` picks the NER backend (``"gliner"`` or the offline ``"spacy"``).
    """
    findings = scan(text)
    if names:
        try:
            from mooring.ai import ner

            findings = findings + ner.scan_names(
                text, labels=labels, threshold=threshold, model=model, backend=backend
            )
        except Exception:  # noqa: BLE001 - advisory path: never fail the caller
            pass
    return findings


def guard_prompt(
    text: str,
    *,
    enabled: bool,
    block: bool,
    names: bool = False,
    labels: tuple[str, ...] | None = None,
    threshold: float = 0.7,
    model: str | None = None,
    backend: str = "gliner",
) -> tuple[bool, list[Finding], bool]:
    """Evaluate an outbound chat prompt. Returns ``(hold, findings, scan_error)``.

    THE shared prompt valve, called identically by both chat-session classes so
    the policy and its fail mode live in one place. ``hold`` is True only when the
    feature is on, ``block`` is on, a scan succeeded, and there is a hit — the
    caller must then NOT forward the text.

    When ``names`` is set, an optional LOCAL NER pass (:mod:`mooring.ai.ner`) also
    flags person names. Either scanner failing FAILS OPEN for that scanner but sets
    ``scan_error=True`` so the caller can be loud that the guard did not fully run —
    e.g. ``names`` is configured but the ``mooring[pii]`` extra isn't installed.
    """
    if not enabled:
        return False, [], False
    findings: list[Finding] = []
    scan_error = False
    try:
        findings += scan(text)
    except Exception:  # noqa: BLE001 - fail open on the live path, but report it
        scan_error = True
    if names:
        try:
            from mooring.ai import ner

            findings += ner.scan_names(
                text, labels=labels, threshold=threshold, model=model, backend=backend
            )
        except Exception:  # noqa: BLE001 - extra missing / model load / inference error
            scan_error = True
    return (bool(findings) and block), findings, scan_error
