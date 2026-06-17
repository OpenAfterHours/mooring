"""Vendor the MIT spaCy ``en_core_web_md`` model into the companion package.

Run ONCE on a machine that can obtain the model (spaCy ships its models from
GitHub releases, not PyPI). It exports the installed model into the companion's
package-data dir so the next ``uv build`` of packages/mooring-spacy-en-md carries
the weights — letting a PyPI-only team install everything with
``pip install mooring[pii-spacy]``.

    pip install "spacy>=3.7,<4" en_core_web_md   # or: python -m spacy download en_core_web_md
    python scripts/vendor_spacy_model.py
    cd packages/mooring-spacy-en-md && uv build && uv publish

The export uses spaCy's own ``nlp.to_disk`` (so the result is exactly what
``spacy.load`` expects) and includes the model's ``meta.json`` (its license +
sources) for attribution.
"""

from __future__ import annotations

import shutil
from pathlib import Path

MODEL = "en_core_web_md"
_DEST = (
    Path(__file__).resolve().parent.parent
    / "packages"
    / "mooring-spacy-en-md"
    / "src"
    / "mooring_spacy_en_md"
    / "_model"
)


def main() -> int:
    try:
        import spacy
    except ImportError:
        print('spaCy is not installed. Run:  pip install "spacy>=3.7,<4" ' + MODEL)
        return 1
    try:
        nlp = spacy.load(MODEL)
    except OSError:
        print(f"{MODEL} is not installed. Run:  pip install {MODEL}")
        print(f"   (or, where GitHub is reachable:  python -m spacy download {MODEL})")
        return 1

    if _DEST.exists():
        shutil.rmtree(_DEST)
    _DEST.parent.mkdir(parents=True, exist_ok=True)
    nlp.to_disk(_DEST)

    meta = nlp.meta
    print(
        f"Vendored {MODEL} v{meta.get('version', '?')} "
        f"(spaCy {meta.get('spacy_version', '?')}, license {meta.get('license', '?')})"
    )
    print(f"  -> {_DEST}")
    print("Next: cd packages/mooring-spacy-en-md && uv build && uv publish")
    print("Check mooring's [pii-spacy] spaCy pin matches the model's spacy_version above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
