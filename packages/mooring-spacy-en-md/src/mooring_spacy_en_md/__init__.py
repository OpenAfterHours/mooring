"""Vendored spaCy ``en_core_web_md`` model for mooring's offline PII name detection.

GLiNER's weights only come from Hugging Face; for a PyPI-only or air-gapped team
that is unreachable. spaCy's own models aren't on PyPI either (they live on GitHub
releases). So this companion package republishes the **MIT-licensed**
``en_core_web_md`` model directory as ordinary package data on PyPI — the one
channel such teams have. ``pip install mooring[pii-spacy]`` then delivers both the
spaCy library and the model with no external fetch.

The model bytes are not committed to git; a maintainer vendors them once on a
connected machine with ``scripts/vendor_spacy_model.py`` before building and
publishing this wheel. See the model's own license/attribution (its ``meta.json``
``license`` field) shipped inside the vendored directory.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

MODEL_NAME = "en_core_web_md"
_DATA_DIR = "_model"


def is_vendored() -> bool:
    """Whether the model bytes are present in this build (a real, loadable dir)."""
    try:
        return (model_path() / "meta.json").is_file()
    except FileNotFoundError:
        return False


def model_path() -> Path:
    """The on-disk path to the vendored spaCy model directory (pass to ``spacy.load``).

    Raises :class:`FileNotFoundError` with an actionable message when the model was
    not vendored into this build — i.e. someone installed the package from source
    without running the vendor step.
    """
    root = Path(str(resources.files(__name__).joinpath(_DATA_DIR)))
    if not (root / "meta.json").is_file():
        raise FileNotFoundError(
            f"{__name__}: the spaCy model was not vendored into this build. A "
            "maintainer runs scripts/vendor_spacy_model.py on a machine that can "
            "reach the model, then rebuilds and publishes this wheel."
        )
    return root
