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
from dataclasses import dataclass

from mooring.ai.pii import SUPPRESS_MARKER, Finding

# Default model: a SAFETENSORS build (no pickle) loaded as the bf16 *variant*, which
# is CPU-friendly and — crucially for a security review — means no ``pytorch_model.bin``
# is ever downloaded or unpickled. Pinned to a specific commit for reproducibility.
# Override via ``[ai.pii] name_model`` / ``name_model_revision`` / ``name_model_variant``.
DEFAULT_MODEL = "gliner-community/gliner_small-v2.5"
DEFAULT_REVISION = "f227d3cd637bd4e6757ae143935316d062393341"
DEFAULT_VARIANT = "bf16"
DEFAULT_LABELS: tuple[str, ...] = ("person", "name")
DEFAULT_THRESHOLD = 0.7


@dataclass(frozen=True)
class ModelRef:
    """A pinned GLiNER model: id + optional commit ``revision`` + safetensors
    ``variant`` (e.g. ``bf16``/``fp16``; empty = the repo's default weights file).

    Bundled so the whole ``(id, revision, variant)`` identity threads through the
    PII guard as one value rather than three parallel parameters."""

    id: str = ""
    revision: str = ""
    variant: str = ""


def _resolve(model: "ModelRef | str | None") -> ModelRef:
    """Coerce ``model`` to a ModelRef. ``None`` -> the pinned safetensors default;
    a bare string -> that id at its latest commit and default weights file."""
    if model is None:
        return ModelRef(DEFAULT_MODEL, DEFAULT_REVISION, DEFAULT_VARIANT)
    if isinstance(model, str):
        return ModelRef(model.strip() or DEFAULT_MODEL, "", "")
    return model


def _allow_patterns(variant: str) -> list[str] | None:
    """``snapshot_download`` allow-list that fetches only the safetensors ``variant``
    (plus configs/tokenizer) — never ``pytorch_model.bin``. ``None`` (no variant)
    downloads everything, including the repo's default weights file."""
    if not variant:
        return None
    return [
        f"model.{variant}.safetensors",
        f"model.{variant}.safetensors.index.json",
        f"model-*-of-*.{variant}.safetensors",
        "*.json",
        "*.txt",
        "*.model",
        "tokenizer*",
        "*.spm",
    ]

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

_models: dict[tuple[str, str, str], object] = {}
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


def load_model(model: "ModelRef | str | None" = None):
    """Load (and cache) the GLiNER model, downloading it on first use.

    Raises :class:`NerUnavailable` if the extra is missing or the model can't load.
    Thread-safe: concurrent callers share one cached instance per (id, revision,
    variant). With a variant set, only safetensors are loaded — never a pickle.
    """
    ref = _resolve(model)
    key = (ref.id, ref.revision, ref.variant)
    cached = _models.get(key)
    if cached is not None:
        return cached
    with _load_lock:
        cached = _models.get(key)  # re-check under the lock
        if cached is not None:
            return cached
        try:
            from gliner import GLiNER
        except Exception as exc:  # noqa: BLE001
            raise NerUnavailable(
                "name detection needs the 'pii' extra: pip install mooring[pii]"
            ) from exc
        kwargs: dict = {}
        if ref.revision:
            kwargs["revision"] = ref.revision
        if ref.variant:
            kwargs["variant"] = ref.variant
        try:
            obj = GLiNER.from_pretrained(ref.id, **kwargs)
        except Exception as exc:  # noqa: BLE001 - network / disk / bad model id
            raise NerUnavailable(f"could not load NER model {ref.id!r}: {exc}") from exc
        _models[key] = obj
        return obj


def is_cached(model: "ModelRef | str | None" = None) -> bool:
    """Whether the model is already in the local HF cache (no network, no download).

    False when the extra isn't installed or the cache is absent/incomplete — so a
    True result means ``load_model`` will be fast and offline."""
    ref = _resolve(model)
    if (ref.id, ref.revision, ref.variant) in _models:
        return True
    try:
        from huggingface_hub import snapshot_download

        kwargs: dict = {"local_files_only": True}
        if ref.revision:
            kwargs["revision"] = ref.revision
        allow = _allow_patterns(ref.variant)
        if allow:
            kwargs["allow_patterns"] = allow
        snapshot_download(ref.id, **kwargs)
        return True
    except Exception:  # noqa: BLE001 - not cached, or hub not installed
        return False


def download_model(model: "ModelRef | str | None" = None, on_progress=None) -> None:
    """Fetch the model into the local cache, reporting byte progress to ``on_progress``.

    ``on_progress(done_bytes, total_bytes)`` is called as the download proceeds
    (aggregated across the model's files). With a variant set, fetches ONLY the
    safetensors variant + configs (no ``pytorch_model.bin``). Resumes a partial
    download. Raises :class:`NerUnavailable` if the extra is missing or it fails.
    """
    ref = _resolve(model)
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:  # noqa: BLE001
        raise NerUnavailable(
            "name detection needs the 'pii' extra: pip install mooring[pii]"
        ) from exc

    kwargs: dict = {}
    if ref.revision:
        kwargs["revision"] = ref.revision
    allow = _allow_patterns(ref.variant)
    if allow:
        kwargs["allow_patterns"] = allow
    tqdm_class = _progress_tqdm(on_progress) if on_progress is not None else None
    try:
        if tqdm_class is not None:
            try:
                snapshot_download(ref.id, tqdm_class=tqdm_class, **kwargs)
                return
            except TypeError:  # older hub without tqdm_class — fall back, no % then
                pass
        snapshot_download(ref.id, **kwargs)
    except Exception as exc:  # noqa: BLE001 - network / disk / bad model id
        raise NerUnavailable(f"could not download NER model {ref.id!r}: {exc}") from exc


def _progress_tqdm(on_progress):
    """A tqdm subclass that reports aggregate byte progress to ``on_progress``.

    huggingface_hub spins up one bar per file; we sum the byte-unit bars so the
    callback sees overall ``(done, total)`` rather than per-file jumps."""
    from tqdm.auto import tqdm as _BaseTqdm

    bars: dict[int, tuple[int, int]] = {}
    lock = threading.Lock()

    class _ProgressTqdm(_BaseTqdm):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._report()

        def update(self, n=1):
            result = super().update(n)
            self._report()
            return result

        def _report(self):
            if self.unit != "B" or not self.total:
                return
            with lock:
                bars[id(self)] = (self.n, self.total)
                done = sum(v[0] for v in bars.values())
                total = sum(v[1] for v in bars.values())
            try:
                on_progress(done, total)
            except Exception:  # noqa: BLE001 - never let reporting break the download
                pass

    return _ProgressTqdm


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
    model: "ModelRef | str | None" = None,
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
