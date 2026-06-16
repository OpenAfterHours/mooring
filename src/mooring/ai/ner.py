"""Optional NER-based name detection for outbound text — Phase 2 of the PII guard.

Phase 1 (:mod:`mooring.ai.pii`) is pure stdlib and catches only STRUCTURED
identifiers (cards, IBANs, NHS numbers, emails, NINOs); by construction it cannot
see a person's NAME, which has no checksum or fixed shape. This module adds that
missing capability with a LOCAL, zero-shot NER model (GLiNER), shipped as the
opt-in ``mooring[pii]`` extra so the lean wheel and the frozen ``.pyz`` stay free
of the heavy ML stack (torch + transformers).

Privacy posture, the same as pii.py:

* **Local only.** The model runs on the analyst's machine; no text leaves for a
  third party to scan it. The single network touch is a one-time model download
  from Hugging Face on first use (pre-fetch it with ``mooring ai pii model``).
* **Value-free findings.** GLiNER returns the matched substring; this module reads
  ONLY the ``(label, start-offset)``, maps the offset to a line number, and DROPS
  the text — a :class:`~mooring.ai.pii.Finding` is ``(line, kind)``, never the name.
  So a finding can be logged, shown, and sent over SSE safely.
* **Best-effort, never a guarantee.** NER misses and false-positives; a clean scan
  is not proof of no names. The structural guarantee remains the schema-only design.

It reuses ``Finding``/``SUPPRESS_MARKER`` from pii.py so NER findings interleave
with the structured scanner's at every egress.
"""

from __future__ import annotations

import threading

from mooring.ai.pii import SUPPRESS_MARKER, Finding

# A PII-trained default; override via ``[ai.pii] name_model``. Both "person" and
# "name" are passed as labels so detection is robust to either model's vocabulary.
DEFAULT_MODEL = "urchade/gliner_multi_pii-v1"
DEFAULT_LABELS: tuple[str, ...] = ("person", "name")
DEFAULT_THRESHOLD = 0.7

# value-free kind labels (mirrors pii.py's CARD/EMAIL/... style).
NAME = "person name"
ORG = "organization"

# Author-configured GLiNER labels folded to a stable, value-free kind. Person- and
# org-ish vocab variants map to NAME / ORG; anything else surfaces under its own
# (lowercased) label — all of these are config strings, never a data value.
_PERSON_LABELS = frozenset(
    {"person", "people", "name", "person name", "full name", "first name",
     "last name", "given name", "surname"}
)
_ORG_LABELS = frozenset(
    {"organization", "organisation", "company", "business", "employer",
     "organization name", "organisation name", "company name", "business name"}
)

# Batch lines up to this many characters per forward pass (bounds inference cost
# so a huge prompt/notebook can't stall the model), and never scan more than the
# total ceiling (a pathological input is truncated, not run unbounded).
_CHUNK_CHARS = 2000
_MAX_TOTAL_CHARS = 200_000

_models: dict[str, object] = {}
_load_lock = threading.Lock()


class NerUnavailable(RuntimeError):
    """The ``mooring[pii]`` extra isn't installed, or the model couldn't load."""


def available() -> bool:
    """True if the GLiNER backend imports (the ``pii`` extra is installed)."""
    try:
        import gliner  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "not available"
        return False
    return True


def load_model(model_id: str | None = None):
    """Load (and cache) the GLiNER model, downloading it on first use.

    Raises :class:`NerUnavailable` if the extra is missing or the model can't load.
    Thread-safe: concurrent callers share one cached instance per model id.
    """
    mid = (model_id or "").strip() or DEFAULT_MODEL
    cached = _models.get(mid)
    if cached is not None:
        return cached
    with _load_lock:
        cached = _models.get(mid)  # re-check under the lock
        if cached is not None:
            return cached
        try:
            from gliner import GLiNER
        except Exception as exc:  # noqa: BLE001
            raise NerUnavailable(
                "name detection needs the 'pii' extra: pip install mooring[pii]"
            ) from exc
        try:
            model = GLiNER.from_pretrained(mid)
        except Exception as exc:  # noqa: BLE001 - network / disk / bad model id
            raise NerUnavailable(f"could not load NER model {mid!r}: {exc}") from exc
        _models[mid] = model
        return model


def _kind_for(label: str) -> str:
    """Map a GLiNER label to a value-free kind. The label is author-configured
    (e.g. "person", "organization"), never a data value, so surfacing it is safe."""
    low = (label or "").strip().lower()
    if low in _PERSON_LABELS:
        return NAME
    if low in _ORG_LABELS:
        return ORG
    return low or NAME


def _chunks(text: str):
    """Yield ``(chunk_text, first_lineno)`` blocks of <= ``_CHUNK_CHARS``.

    A line carrying :data:`SUPPRESS_MARKER` is blanked (kept as an empty line) so it
    yields no entities while line numbers stay aligned to the original text.
    """
    buf: list[str] = []
    buf_chars = 0
    first = 1
    total = 0
    for i, line in enumerate(text.splitlines(), start=1):
        if total >= _MAX_TOTAL_CHARS:
            break
        safe = "" if SUPPRESS_MARKER in line else line
        total += len(safe)
        if buf and buf_chars + len(safe) + 1 > _CHUNK_CHARS:
            yield "\n".join(buf), first
            buf, buf_chars = [], 0
            first = i
        if not buf:
            first = i
        buf.append(safe)
        buf_chars += len(safe) + 1
    if buf:
        yield "\n".join(buf), first


def scan_names(
    text: str,
    *,
    labels: tuple[str, ...] | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    model: str | None = None,
) -> list[Finding]:
    """Value-free person-name (and configured-label) findings in ``text``.

    Raises :class:`NerUnavailable` if the backend/model isn't ready — the caller
    chooses whether that is loud (the prompt valve) or a silent structured-only
    fallback (advisory scans). Returns ``[]`` on clean text. Best-effort: a chunk
    that errors in inference is skipped, never aborting the whole scan.
    """
    model_obj = load_model(model)
    label_list = list(labels) if labels else list(DEFAULT_LABELS)
    seen: set[tuple[int, str]] = set()
    findings: list[Finding] = []
    for chunk, first in _chunks(text):
        if not chunk.strip():
            continue
        try:
            ents = model_obj.predict_entities(chunk, label_list, threshold=threshold)
        except Exception:  # noqa: BLE001 - a bad chunk must not abort the scan
            continue
        for ent in ents:
            start = ent.get("start") if isinstance(ent, dict) else None
            if not isinstance(start, int):
                continue
            line = first + chunk.count("\n", 0, start)
            kind = _kind_for(str(ent.get("label", "")))
            key = (line, kind)
            if key not in seen:
                seen.add(key)
                findings.append(Finding(line, kind))
    return sorted(findings, key=lambda f: (f.line, f.kind))
