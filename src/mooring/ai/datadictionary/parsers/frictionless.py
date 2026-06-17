"""Frictionless Table Schema / Data Package parser."""

from __future__ import annotations

from mooring.ai.datadictionary.model import Column, Table
from mooring.ai.datadictionary.parsers._common import (
    _as_list,
    _cap,
    _first,
    _record_dropped,
    _scalar_str,
)


def parse_frictionless(data: dict, domain: str, dropped: set[str], _mapping: dict) -> list[Table]:
    tables: list[Table] = []
    resources = data.get("resources")
    if isinstance(resources, list):
        for res in resources:
            if isinstance(res, dict):
                schema = res.get("schema") if isinstance(res.get("schema"), dict) else {}
                tables.append(_frictionless_table(res, schema, domain, dropped))
    elif isinstance(data.get("fields"), list):  # bare Table Schema
        tables.append(_frictionless_table({"name": domain or "table"}, data, domain, dropped))
    return tables


def _frictionless_table(res: dict, schema: dict, domain: str, dropped: set[str]) -> Table:
    _record_dropped(res, {"name", "title", "description", "schema"}, dropped)
    _record_dropped(schema, {"fields", "primaryKey", "foreignKeys", "description"}, dropped)
    fks = {}
    for fk in _as_list(schema.get("foreignKeys")):
        if not isinstance(fk, dict):
            continue
        ref = fk.get("reference") if isinstance(fk.get("reference"), dict) else {}
        ref_fields = _as_list(ref.get("fields"))
        ref_field = _scalar_str(ref_fields[0]) if ref_fields else ""
        ref_res = _scalar_str(ref.get("resource"))
        target = f"FK -> {ref_res}.{ref_field}" if ref_field else f"FK -> {ref_res}"
        for f in _as_list(fk.get("fields")):  # 'fields' may be a single string or a list
            name = _scalar_str(f)
            if name:
                fks[name] = target
    cols = []
    for fdef in _as_list(schema.get("fields")):
        if not isinstance(fdef, dict):
            continue
        _record_dropped(fdef, {"name", "title", "type", "format", "description"}, dropped)
        name = _first(fdef, ("name", "title"))
        cols.append(
            Column(
                name=name,
                type=_first(fdef, ("type", "format")),
                relationship=fks.get(name, ""),
                description=_cap(fdef.get("description", "")),
            )
        )
    return Table(
        name=_first(res, ("name", "title"), domain or "table"),
        domain=domain,
        description=_cap(res.get("description", "")),
        columns=tuple(cols),
    )
