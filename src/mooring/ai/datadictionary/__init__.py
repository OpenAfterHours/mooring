"""Parse team data dictionaries into a value-minimised, in-memory index.

The privacy spine of the dictionary feature. Teams describe their tables in
varied YAML shapes (dbt ``schema.yml`` is the primary target; Frictionless,
catalog exports, Great Expectations, and hand-rolled files are also handled), so
parsing is **flexible about where to read**. But what may ever cross the wire is
**fixed and closed**: only the five fields on :class:`Column`
(``name``/``type``/``nullable``/``relationship``/``description``) and a table's
``name``/``description``. The dataclasses ARE the allowlist — a detector chooses
which source keys feed those slots and drops everything else (sample values,
defaults, enums, test literals, ``meta``/``comment`` blobs). Adding a parser can
never add a slot, so a mis-detection degrades to missing fields, not a leak.

This is not a structural value-freeness guarantee the way :mod:`mooring.schema`
is (which never materialises a value): ``description`` is free text a human
wrote and can contain whatever they typed. It is best-effort minimisation —
opt-in, secret-scanned, capped, and shown in the UI — paired with human review.

Selection/search over the index is a token optimisation, NOT a privacy control:
the whole index is reachable, so every reachable field is already minimised here.

Layout: :mod:`.model` is the data model + allowlist + renderers; :mod:`.parsers`
holds one module per source format behind a shape detector; :mod:`.loader` is the
file discovery + orchestration. This facade re-exports the stable public surface.
"""

from __future__ import annotations

from mooring.ai.datadictionary.loader import DEFAULT_MAX_FILE_BYTES, load_index
from mooring.ai.datadictionary.model import (
    DESC_CAP,
    Column,
    DictionaryIndex,
    ParseReport,
    Table,
    merge_indexes,
    render_listing,
    render_table,
    render_tables,
)

__all__ = [
    "Column",
    "Table",
    "ParseReport",
    "DictionaryIndex",
    "load_index",
    "merge_indexes",
    "render_table",
    "render_tables",
    "render_listing",
    "DESC_CAP",
    "DEFAULT_MAX_FILE_BYTES",
]
