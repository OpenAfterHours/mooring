"""Format parsers + the shape detector.

Each parser is value-blind by construction: it can only emit
:class:`~mooring.ai.datadictionary.model.Column`/``Table`` objects (the fixed
allowlist), so adding a format here can never add a slot. Register a new format
by adding its module and an entry in :data:`PARSERS`; :func:`detect` sniffs the
shape and :data:`PARSERS` maps it to the parser (``generic`` is the fallback).
"""

from __future__ import annotations

from mooring.ai.datadictionary.parsers.dbt import parse_dbt
from mooring.ai.datadictionary.parsers.frictionless import parse_frictionless
from mooring.ai.datadictionary.parsers.generic import parse_generic
from mooring.ai.datadictionary.parsers.great_expectations import parse_great_expectations

PARSERS = {
    "dbt": parse_dbt,
    "frictionless": parse_frictionless,
    "great_expectations": parse_great_expectations,
    "generic": parse_generic,
}


def detect(data: dict) -> str:
    if any(isinstance(data.get(k), list) for k in ("models", "sources", "seeds", "snapshots")):
        return "dbt"
    if isinstance(data.get("expectations"), list) and (
        "expectation_suite_name" in data
        or any(isinstance(e, dict) and "expectation_type" in e for e in data["expectations"])
    ):
        return "great_expectations"
    if isinstance(data.get("resources"), list) or isinstance(data.get("fields"), list):
        return "frictionless"
    return "generic"
