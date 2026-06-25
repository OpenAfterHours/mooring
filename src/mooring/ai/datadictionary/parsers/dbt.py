"""dbt ``schema.yml`` parser (the primary target)."""

from __future__ import annotations

from mooring.ai.datadictionary.model import Column, Table
from mooring.ai.datadictionary.parsers._common import (
    _as_list,
    _cap,
    _record_dropped,
    _scalar_str,
)


def parse_dbt(data: dict, domain: str, dropped: set[str], _mapping: dict) -> list[Table]:
    tables: list[Table] = []
    for key in ("models", "seeds", "snapshots", "analyses"):
        for node in data.get(key, []) or []:
            if isinstance(node, dict):
                tables.append(_dbt_table(node, domain, dropped))
    for src in data.get("sources", []) or []:
        if not isinstance(src, dict):
            continue
        src_domain = domain or str(src.get("name", ""))
        for node in src.get("tables", []) or []:
            if isinstance(node, dict):
                tables.append(_dbt_table(node, src_domain, dropped))
    return tables


def _dbt_table(node: dict, domain: str, dropped: set[str]) -> Table:
    used = {"name", "description", "columns"}
    _record_dropped(node, used, dropped)
    cols = tuple(
        _dbt_column(c, dropped) for c in _as_list(node.get("columns")) if isinstance(c, dict)
    )
    return Table(
        name=_scalar_str(node.get("name")),
        domain=domain,
        description=_cap(node.get("description", "")),
        columns=cols,
    )


def _dbt_column(node: dict, dropped: set[str]) -> Column:
    used = {"name", "data_type", "description", "constraints", "tests", "data_tests"}
    _record_dropped(node, used, dropped)
    nullable: bool | None = None
    rels: list[str] = []
    for constraint in _as_list(node.get("constraints")):
        if not isinstance(constraint, dict):
            continue
        ctype = str(constraint.get("type", "")).lower()
        if ctype == "not_null":
            nullable = False
        elif ctype == "foreign_key":
            rels.append(_dbt_ref(constraint.get("to"), constraint.get("field")))
    for test in _as_list(node.get("tests")) + _as_list(node.get("data_tests")):
        if isinstance(test, str) and test == "not_null":
            nullable = False
        elif isinstance(test, dict) and "relationships" in test:
            spec = test["relationships"] if isinstance(test["relationships"], dict) else {}
            args = spec.get("arguments", spec) if isinstance(spec.get("arguments"), dict) else spec
            rels.append(_dbt_ref(args.get("to"), args.get("field")))
    return Column(
        name=_scalar_str(node.get("name")),
        type=_scalar_str(node.get("data_type")),
        nullable=nullable,
        relationship="; ".join(r for r in rels if r),
        description=_cap(node.get("description", "")),
    )


def _dbt_ref(to, field) -> str:
    """Render a dbt FK target as 'table.field' — names only, never a literal."""
    target = _scalar_str(to).strip()
    if target.startswith(("ref(", "source(")):
        # ref('dim_x') / source('s','t') -> last quoted token
        inner = target[target.find("(") + 1 : target.rfind(")")]
        parts = [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
        target = parts[-1] if parts else target
    fld = _scalar_str(field).strip().strip("'\"")
    if not target:
        return ""
    return f"FK -> {target}.{fld}" if fld else f"FK -> {target}"
