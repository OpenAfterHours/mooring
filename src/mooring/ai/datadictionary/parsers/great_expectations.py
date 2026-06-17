"""Great Expectations suite parser.

GE has no column list and is almost all literals: derive names + type only,
dropping the entire kwargs payload (value_set/min_value/regex/...).
"""

from __future__ import annotations

from mooring.ai.datadictionary.model import Column, Table
from mooring.ai.datadictionary.parsers._common import _as_list, _first, _scalar_str


def parse_great_expectations(data: dict, domain: str, dropped: set[str], _m: dict) -> list[Table]:
    cols: dict[str, Column] = {}
    for exp in _as_list(data.get("expectations")):
        if not isinstance(exp, dict):
            continue
        kwargs = exp.get("kwargs") if isinstance(exp.get("kwargs"), dict) else {}
        col = _scalar_str(kwargs.get("column"))
        if not col:
            for k in kwargs:
                if k not in ("column",):
                    dropped.add(k)
            continue
        ctype = ""
        if exp.get("expectation_type") in (
            "expect_column_values_to_be_of_type",
            "expect_column_values_to_be_in_type_list",
        ):
            ctype = _first(kwargs, ("type_", "type"))
        for k in kwargs:
            if k not in ("column", "type_", "type"):
                dropped.add(k)
        if col not in cols or ctype:
            cols[col] = Column(name=col, type=ctype or cols.get(col, Column(col)).type)
    name = str(data.get("expectation_suite_name", domain or "table"))
    return [Table(name=name, domain=domain, columns=tuple(cols.values()))]
