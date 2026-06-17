# mooring-spacy-en-md

A thin companion package that **vendors the MIT-licensed spaCy `en_core_web_md`
model** so [mooring](https://pypi.org/project/mooring/)'s offline PII *name/organisation*
detection works in **PyPI-only / air-gapped** environments.

## Why it exists

mooring's default name-detection model (GLiNER) downloads from Hugging Face, and
spaCy's own models are distributed from GitHub releases — neither reachable in a
locked-down corporate network whose only package channel is an internal PyPI
mirror. The `en_core_web_md` pipeline is MIT-licensed, so this package republishes
its model directory as ordinary **PyPI package data**, the one channel such teams
have. No Hugging Face, no GitHub, no model mirror at install time.

You don't install this directly — `pip install "mooring[pii-spacy]"` pulls it in,
then set `[ai.pii] name_backend = "spacy"` in your mooring config.

## For maintainers: vendoring the model before a release

The model bytes are **not committed to git** (they're ~40 MB). On a machine that
can reach the model once:

```bash
pip install "spacy>=3.7,<4" en_core_web_md   # or: python -m spacy download en_core_web_md
python scripts/vendor_spacy_model.py          # exports the model into src/mooring_spacy_en_md/_model/
cd packages/mooring-spacy-en-md && uv build   # the wheel now carries the model + its license
uv publish
```

Keep the `spacy` pin in mooring's `pii-spacy` extra compatible with the model's
spaCy version (the vendor script prints it). The model's own license/attribution
ships inside the vendored `_model/` directory (its `meta.json`).
