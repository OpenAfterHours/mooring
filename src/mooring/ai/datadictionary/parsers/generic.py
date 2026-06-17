"""Hand-rolled / mapped dictionary parser, and the catch-all fallback.

Reads ``tables`` as a list-of-{name} or a map keyed by name, columns likewise,
with the candidate key families below (or a ``.map.yaml`` override). Also handles
a one-level group nesting (schemas/databases > tables).
"""

from __future__ import annotations

from mooring.ai.datadictionary.model import Column, Table
from mooring.ai.datadictionary.parsers._common import (
    _cap,
    _first,
    _record_dropped,
    _scalar_str,
)

# Source-key families the generic parser reads into each fixed slot. Mapping
# files override these; anything not named here is dropped.
_NAME_KEYS = ("name", "title", "column", "field", "fieldPath")
_TYPE_KEYS = ("type", "data_type", "dataType", "col_type", "column_type", "sqlType", "dbType")
_DESC_KEYS = ("description", "comment", "doc", "note")
_NULLABLE_KEYS = ("nullable", "is_nullable")
_REL_KEYS = ("relationship", "relationships", "foreign_key", "references", "ref", "fk")


def _nullable_of(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "not_null", "notnull"):
            return False if low in ("not_null", "notnull") else True
        if low in ("false", "no", "nullable"):
            return True if low == "nullable" else False
    return None


def _render_relationship(value: str) -> str:
    value = str(value or "").strip()
    return f"FK -> {value}" if value and "->" not in value else value


def parse_generic(data: dict, domain: str, dropped: set[str], mapping: dict) -> list[Table]:
    mapping = mapping or {}
    tables_key = mapping.get("tables_key", "tables")
    group_key = mapping.get("group_key")  # e.g. "schemas" / "databases"

    name_keys = (mapping["name_key"],) if mapping.get("name_key") else _NAME_KEYS
    col_name_keys = (mapping["column_name_key"],) if mapping.get("column_name_key") else _NAME_KEYS
    columns_key = mapping.get("columns_key", "columns")
    type_keys = (mapping["type_key"],) if mapping.get("type_key") else _TYPE_KEYS
    desc_keys = (mapping["desc_key"],) if mapping.get("desc_key") else _DESC_KEYS
    cdesc_keys = (mapping["column_desc_key"],) if mapping.get("column_desc_key") else _DESC_KEYS
    rel_keys = (mapping["relationship_key"],) if mapping.get("relationship_key") else _REL_KEYS
    null_keys = (mapping["nullable_key"],) if mapping.get("nullable_key") else _NULLABLE_KEYS

    def column(name: str, node) -> Column:
        if not isinstance(node, dict):
            # A scalar leaf: in a map (`id: bigint`) it's the type; in a list
            # (`- id`) it's the column name.
            return Column(name=name, type=_scalar_str(node)) if name else Column(name=_scalar_str(node))
        used = set(col_name_keys) | set(type_keys) | set(cdesc_keys) | set(rel_keys) | set(null_keys)
        _record_dropped(node, used, dropped)
        nullable = None
        for k in null_keys:
            if k in node:
                nullable = _nullable_of(node[k])
                break
        return Column(
            name=name or _first(node, col_name_keys),
            type=_first(node, type_keys),
            nullable=nullable,
            relationship=_render_relationship(_first(node, rel_keys)),
            description=_cap(_first(node, cdesc_keys)),
        )

    def columns_of(tnode: dict) -> tuple[Column, ...]:
        raw = tnode.get(columns_key)
        if isinstance(raw, dict):
            cols = [column(str(cn), cv) for cn, cv in raw.items()]
        elif isinstance(raw, list):
            cols = [column("", c) for c in raw]
        else:
            cols = []
        return tuple(c for c in cols if c.name)  # drop unusable (nameless) entries

    def table(name: str, tnode, dom: str) -> Table:
        tnode = tnode if isinstance(tnode, dict) else {}
        used = {tables_key, columns_key, *name_keys, *desc_keys}
        _record_dropped(tnode, used, dropped)
        return Table(
            name=name or _first(tnode, name_keys),
            domain=dom,
            description=_cap(_first(tnode, desc_keys)),
            columns=columns_of(tnode),
        )

    def tables_from(container: dict, dom: str) -> list[Table]:
        raw = container.get(tables_key)
        if isinstance(raw, dict):
            return [table(str(tn), tv, dom) for tn, tv in raw.items()]
        if isinstance(raw, list):
            return [table("", t, dom) for t in raw if isinstance(t, dict)]
        return []

    if group_key and isinstance(data.get(group_key), (list, dict)):
        groups = data[group_key]
        items = groups.values() if isinstance(groups, dict) else groups
        out: list[Table] = []
        for g in items:
            if isinstance(g, dict):
                out += tables_from(g, domain or _first(g, ("name", "schema", "schema_name")))
        return out
    return tables_from(data, domain)
