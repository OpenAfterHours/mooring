"""spaCy backend for offline NER name/organisation detection.

The PyPI-only / air-gapped alternative to the GLiNER backend
(:mod:`mooring.ai.ner`): GLiNER's model only comes from Hugging Face, so where
that is blocked this backend uses spaCy, whose MIT model mooring vendors to PyPI
as ``mooring-spacy-en-md``. Selected via ``[ai.pii] name_backend = "spacy"``.

Same value-free contract as GLiNER: it reads ONLY each entity's
``(label, start-offset)`` and DROPS the matched text, so a finding is
``(line, kind)`` — never a name. The shared chunking + line-mapping +
:class:`~mooring.ai.pii.Finding` construction lives in :mod:`mooring.ai.ner`;
this module only resolves/loads the spaCy model and predicts the entities in a
single chunk.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

from mooring.ai.ner import NAME, ORG, NerUnavailable

# spaCy entity types -> mooring's value-free kinds.
_SPACY_KIND = {"PERSON": NAME, "ORG": ORG}

# mooring's configurable label vocab -> the spaCy entity type to keep. (spaCy is
# not zero-shot: it emits its fixed types and we FILTER, rather than querying.)
_LABEL_TO_SPACY = {
    "person": "PERSON", "people": "PERSON", "name": "PERSON", "person name": "PERSON",
    "full name": "PERSON", "first name": "PERSON", "last name": "PERSON",
    "given name": "PERSON", "surname": "PERSON",
    "organization": "ORG", "organisation": "ORG", "company": "ORG", "business": "ORG",
    "employer": "ORG", "org": "ORG", "organization name": "ORG", "organisation name": "ORG",
    "company name": "ORG", "business name": "ORG",
}

_models: dict[str, object] = {}
_load_lock = threading.Lock()


def available() -> bool:
    """True if the spaCy library imports (the ``pii-spacy`` extra is installed)."""
    try:
        import spacy  # noqa: F401
    except Exception:  # noqa: BLE001  # any import failure means "not available"
        return False
    return True


def _resolve(model: "str | None") -> str:
    """The spaCy model id or directory path.

    Empty/None -> the vendored companion model dir (``mooring-spacy-en-md``); a
    non-empty string is used as-is: a package name (``en_core_web_md``) or a
    filesystem path to a model directory (the sideloaded / frozen-build case)."""
    name = model.strip() if isinstance(model, str) else ""
    if name:
        return name
    from mooring_spacy_en_md import model_path  # raises if absent / not vendored

    return str(model_path())


def _is_model_dir(target: str) -> bool:
    return (Path(target) / "meta.json").is_file()


def is_ready(model: "str | None" = None) -> bool:
    """Whether spaCy AND the model are present and loadable now (no download).

    For spaCy a model is either pip-installed/vendored or it isn't — there is no
    cache to populate, so this is the analogue of GLiNER's ``is_cached``."""
    if not available():
        return False
    try:
        target = _resolve(model)
    except Exception:  # noqa: BLE001  # companion not installed / not vendored
        return False
    if target in _models:
        return True
    import spacy

    return bool(spacy.util.is_package(target)) or _is_model_dir(target)


def load(model: "str | None" = None):
    """Load (and cache) the spaCy ``nlp`` object. Raises :class:`NerUnavailable`
    when spaCy or the model is missing. Only the entity recogniser is kept active
    (tok2vec + ner) for speed; thread-safe per resolved target."""
    try:
        import spacy
    except Exception as exc:  # noqa: BLE001
        raise NerUnavailable(
            "spaCy name detection needs the 'pii-spacy' extra: pip install mooring[pii-spacy]"
        ) from exc
    try:
        target = _resolve(model)
    except Exception as exc:  # noqa: BLE001  # companion missing / model not vendored
        raise NerUnavailable(f"spaCy model unavailable: {exc}") from exc
    cached = _models.get(target)
    if cached is not None:
        return cached
    with _load_lock:
        cached = _models.get(target)
        if cached is not None:
            return cached
        try:
            nlp = spacy.load(target)
        except Exception as exc:  # noqa: BLE001  # not installed / bad path / version skew
            raise NerUnavailable(f"could not load spaCy model {target!r}: {exc}") from exc
        # Names + orgs only need the entity recogniser; disable the rest for speed.
        for pipe in list(getattr(nlp, "pipe_names", [])):
            if pipe not in ("tok2vec", "transformer", "ner"):
                with contextlib.suppress(Exception):
                    nlp.disable_pipe(pipe)
        _models[target] = nlp
        return nlp


def predict(nlp, chunk: str, labels) -> list[tuple[str, int]]:
    """``(value-free kind, start_char)`` for entities of the wanted types in ``chunk``.

    ``labels`` is mooring's configured vocab; map it to spaCy entity types and keep
    only those (defaulting to person + organisation). The matched text is never read.
    """
    want = {_LABEL_TO_SPACY[lab.lower()] for lab in (labels or ()) if lab.lower() in _LABEL_TO_SPACY}
    if not want:
        want = {"PERSON", "ORG"}
    out: list[tuple[str, int]] = []
    for ent in nlp(chunk).ents:
        if ent.label_ in want:
            out.append((_SPACY_KIND.get(ent.label_, ent.label_.lower()), ent.start_char))
    return out
