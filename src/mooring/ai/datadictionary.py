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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DESC_CAP = 500  # max chars kept from any single description (the one free-text slot)
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024  # reject a dictionary file larger than this

_DICT_DIR = "dictionaries"  # <context_dir>/dictionaries/*.yaml (per-domain)
_SINGLE_FILE = "datadictionary.yaml"  # <context_dir>/datadictionary.yaml (single-file form)

# Source-key families the generic parser reads into each fixed slot. Mapping
# files override these; anything not named here is dropped.
_NAME_KEYS = ("name", "title", "column", "field", "fieldPath")
_TYPE_KEYS = ("type", "data_type", "dataType", "col_type", "column_type", "sqlType", "dbType")
_DESC_KEYS = ("description", "comment", "doc", "note")
_NULLABLE_KEYS = ("nullable", "is_nullable")
_REL_KEYS = ("relationship", "relationships", "foreign_key", "references", "ref", "fk")


@dataclass(frozen=True)
class Column:
    """A column, reduced to the five fields that may reach the model."""

    name: str
    type: str = ""
    nullable: bool | None = None
    relationship: str = ""
    description: str = ""


@dataclass(frozen=True)
class Table:
    name: str
    domain: str = ""
    description: str = ""
    columns: tuple[Column, ...] = ()

    @property
    def qualified(self) -> str:
        return f"{self.domain}.{self.name}" if self.domain else self.name

    def column_name_set(self) -> set[str]:
        return {c.name.lower() for c in self.columns}


@dataclass
class ParseReport:
    """What a single dictionary file yielded — surfaced so parsing is never silently wrong."""

    path: str  # workspace-relative
    domain: str
    shape: str  # dbt | frictionless | great_expectations | generic | unknown | error
    n_tables: int = 0
    n_columns: int = 0
    dropped_keys: tuple[str, ...] = ()  # source keys ignored (so a team sees what was withheld)
    error: str = ""


@dataclass
class DictionaryIndex:
    tables: tuple[Table, ...] = ()
    reports: tuple[ParseReport, ...] = ()

    def is_empty(self) -> bool:
        return not self.tables

    def get(self, name: str) -> Table | None:
        """Look up a table by qualified (`domain.table`) or bare name, case-insensitive.

        Lookup is over the parsed in-memory objects only — ``name`` is never a
        filesystem path, so a path-like argument simply finds nothing.
        """
        key = (name or "").strip().lower()
        if not key:
            return None
        for table in self.tables:
            if key in (table.qualified.lower(), table.name.lower()):
                return table
        return None

    def list_tables(self) -> list[Table]:
        return list(self.tables)

    def search(self, query: str, limit: int = 8) -> list[Table]:
        """Substring match over table + column names and descriptions (value-minimised)."""
        q = (query or "").strip().lower()
        if not q:
            return []
        scored: list[tuple[int, Table]] = []
        for table in self.tables:
            score = 0
            if q in table.name.lower() or q in table.qualified.lower():
                score += 3
            if q in (table.description or "").lower():
                score += 1
            for col in table.columns:
                if q in col.name.lower():
                    score += 2
                elif q in (col.description or "").lower():
                    score += 1
            if score:
                scored.append((score, table))
        scored.sort(key=lambda s: (-s[0], s[1].qualified))
        return [t for _, t in scored[:limit]]


# -- public entry point -----------------------------------------------------


def load_index(
    workspace: Path,
    context_dir: str = "context",
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> DictionaryIndex:
    """Discover and parse every dictionary file under ``<workspace>/<context_dir>``.

    Reads ``<context_dir>/dictionaries/*.yaml|*.yml`` (per-domain; the stem is the
    domain) and a single ``<context_dir>/datadictionary.yaml`` if present. Each
    file is path-checked, size-capped, parsed with ``yaml.safe_load`` (no
    includes/tags), normalised through the fixed five-slot allowlist, and given a
    :class:`ParseReport`. Never raises for a bad file — it records the error in
    that file's report and carries on.
    """
    root = (workspace / context_dir).resolve()
    try:
        root.relative_to(workspace.resolve())
    except ValueError:
        return DictionaryIndex()
    if not root.is_dir():
        return DictionaryIndex()

    files: list[Path] = []
    single = root / _SINGLE_FILE
    if single.is_file():
        files.append(single)
    dict_dir = root / _DICT_DIR
    if dict_dir.is_dir():
        files += sorted(p for p in dict_dir.rglob("*") if p.suffix.lower() in (".yaml", ".yml"))

    all_tables: list[Table] = []
    reports: list[ParseReport] = []
    workspace_resolved = workspace.resolve()
    for path in files:
        rel = _safe_rel(path, workspace_resolved)
        domain = path.stem
        if rel is None:
            reports.append(ParseReport(str(path), domain, "error", error="path escapes workspace"))
            continue
        tables, report = _parse_file(path, rel, domain, max_file_bytes, workspace_resolved)
        all_tables.extend(tables)
        reports.append(report)
    return DictionaryIndex(tables=tuple(all_tables), reports=tuple(reports))


def _safe_rel(path: Path, workspace_resolved: Path) -> str | None:
    try:
        return path.resolve().relative_to(workspace_resolved).as_posix()
    except ValueError:
        return None


def _parse_file(path: Path, rel: str, domain: str, max_file_bytes: int, workspace_resolved: Path):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [], ParseReport(rel, domain, "error", error=str(exc))
    if len(raw) > max_file_bytes:
        return [], ParseReport(
            rel, domain, "error",
            error=f"file is {len(raw) // 1024} KiB (cap {max_file_bytes // 1024} KiB) - split it",
        )
    try:
        import yaml
    except ImportError:
        return [], ParseReport(rel, domain, "error", error="PyYAML is not installed")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return [], ParseReport(rel, domain, "error", error=f"invalid YAML: {exc}")
    if not isinstance(data, dict):
        return [], ParseReport(rel, domain, "unknown", error="top level is not a mapping")

    mapping = _load_mapping(path, workspace_resolved)
    dropped: set[str] = set()
    shape = mapping.get("format") if mapping else _detect(data)
    parser = _PARSERS.get(shape, _parse_generic)
    # Honour the "never raises for a bad file" contract: any parser error on a
    # valid-but-oddly-typed YAML degrades to an error report, never a crash.
    try:
        tables = parser(data, domain, dropped, mapping)
    except Exception as exc:  # noqa: BLE001 - report and skip the file, don't crash the chat
        return [], ParseReport(rel, domain, "error", error=f"could not parse: {exc}")
    report = ParseReport(
        path=rel,
        domain=domain,
        shape=shape,
        n_tables=len(tables),
        n_columns=sum(len(t.columns) for t in tables),
        dropped_keys=tuple(sorted(dropped)),
    )
    return tables, report


def _load_mapping(dict_path: Path, workspace_resolved: Path) -> dict:
    """Optional navigational override: ``<file>.map.yaml`` beside the dictionary.

    Navigational ONLY — it renames where keys live; it cannot name a new output
    slot (the slots are the :class:`Column` fields, hard-coded below). Path-checked
    like every other read so a symlinked sidecar can't be read from outside.
    """
    map_path = dict_path.with_name(dict_path.name + ".map.yaml")
    if not map_path.is_file():
        map_path = dict_path.parent / "datadictionary.map.yaml"
    if not map_path.is_file():
        return {}
    try:
        map_path.resolve().relative_to(workspace_resolved)
    except ValueError:
        return {}
    try:
        import yaml

        loaded = yaml.safe_load(map_path.read_text("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError, ImportError):
        return {}


# -- detection --------------------------------------------------------------


def _detect(data: dict) -> str:
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


# -- helpers ----------------------------------------------------------------


def _scalar_str(value) -> str:
    """A slot may only ever hold a scalar. A nested list/dict placed under an
    allowed key is NOT stringified — that would leak the nested values; it
    degrades to empty so a mis-shaped field becomes a missing field, not a leak."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (str, int, float)):
        return str(value)
    return ""


def _as_list(value) -> list:
    """Coerce a value that may legally be a scalar, a list, or absent to a list,
    so 'x or []' style concatenation can't blow up on a mapping/scalar."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _first(node: dict, keys, default: str = "") -> str:
    for k in keys:
        if k in node:
            scalar = _scalar_str(node[k])
            if scalar != "":
                return scalar
    return default


def _cap(text) -> str:
    text = " ".join(_scalar_str(text).split())
    return text if len(text) <= DESC_CAP else text[: DESC_CAP - 1].rstrip() + "..."


def _leaf_name(raw: str) -> str:
    """dbt DataHub-style fieldPath '[version=2.0].[type=string].status' -> 'status'."""
    name = str(raw).strip().strip('"').strip("`")
    if "]." in name:
        name = name.rsplit(".", 1)[-1]
    return name


def _record_dropped(node: dict, used: set[str], dropped: set[str]) -> None:
    for key in node:
        if key not in used:
            dropped.add(str(key))


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


# -- dbt --------------------------------------------------------------------


def _parse_dbt(data: dict, domain: str, dropped: set[str], _mapping: dict) -> list[Table]:
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
    if target.startswith("ref(") or target.startswith("source("):
        # ref('dim_x') / source('s','t') -> last quoted token
        inner = target[target.find("(") + 1 : target.rfind(")")]
        parts = [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
        target = parts[-1] if parts else target
    fld = _scalar_str(field).strip().strip("'\"")
    if not target:
        return ""
    return f"FK -> {target}.{fld}" if fld else f"FK -> {target}"


# -- frictionless -----------------------------------------------------------


def _parse_frictionless(data: dict, domain: str, dropped: set[str], _mapping: dict) -> list[Table]:
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


# -- great expectations -----------------------------------------------------


def _parse_great_expectations(data: dict, domain: str, dropped: set[str], _m: dict) -> list[Table]:
    """GE has no column list and is almost all literals: derive names + type only,
    dropping the entire kwargs payload (value_set/min_value/regex/...)."""
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


# -- generic / mapped -------------------------------------------------------


def _parse_generic(data: dict, domain: str, dropped: set[str], mapping: dict) -> list[Table]:
    """Hand-rolled shapes: ``tables`` as a list-of-{name} or a map keyed by name,
    columns likewise, with the candidate key families (or mapping overrides).
    Also a one-level group nesting (schemas/databases > tables)."""
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


def _render_relationship(value: str) -> str:
    value = str(value or "").strip()
    return f"FK -> {value}" if value and "->" not in value else value


_PARSERS = {
    "dbt": _parse_dbt,
    "frictionless": _parse_frictionless,
    "great_expectations": _parse_great_expectations,
    "generic": _parse_generic,
}


# -- rendering (what the tools / seeding serialise) -------------------------


def render_table(table: Table, *, max_cols: int = 40) -> str:
    """One table as compact text — the five slots only, never a value."""
    head = f"Table `{table.qualified}`"
    if table.description:
        head += f" - {table.description}"
    lines = [head, "Columns (name: type):"]
    for col in table.columns[:max_cols]:
        bits = [f"- {col.name}: {col.type or '?'}"]
        if col.nullable is False:
            bits.append("not null")
        if col.relationship:
            bits.append(col.relationship)
        if col.description:
            bits.append(f'"{col.description}"')
        lines.append(bits[0] if len(bits) == 1 else bits[0] + "  " + "; ".join(bits[1:]))
    if len(table.columns) > max_cols:
        lines.append(f"... (+{len(table.columns) - max_cols} more columns - narrow your search)")
    return "\n".join(lines)


def render_tables(tables, *, max_cols: int = 40) -> str:
    return "\n\n".join(render_table(t, max_cols=max_cols) for t in tables)


def render_listing(index: DictionaryIndex) -> str:
    """A grouped-by-domain table listing for ``mooring_list_tables``."""
    by_domain: dict[str, list[Table]] = {}
    for t in index.tables:
        by_domain.setdefault(t.domain or "(default)", []).append(t)
    out: list[str] = []
    for domain in sorted(by_domain):
        out.append(f"[{domain}]")
        for t in sorted(by_domain[domain], key=lambda x: x.name):
            desc = f" - {t.description}" if t.description else ""
            out.append(f"  {t.name} ({len(t.columns)} cols){desc}")
    return "\n".join(out)
