"""Allowlist extractor for a Power BI semantic model saved as a PBIP (TMDL text).

A PBIP's ``<name>.SemanticModel/definition/`` folder holds the model as TMDL
text files. This module extracts a value-conscious SKELETON of it for the AI
copilot and the hub: table names, column names + dataTypes, relationships, and
measure / calculated-column DAX (authored code, the same class as notebook
source). Everything else is dropped by ALLOWLIST — captured constructs are the
exception, not the rule:

- **partition/source M expressions** are skipped WITHOUT capturing their bodies
  (they routinely embed server names, file paths, and credentials);
- **``definition/roles/``** (RLS filter expressions) and **``definition/cultures/``**
  (translations) are NEVER opened — only their file counts are reported;
- **annotations, extendedProperties**, and every construct the parser does not
  recognise are dropped (their kind/key is counted for the ``mooring ai model
  check`` drift report — an identifier token, never a value);
- a parse failure yields an EMPTY model plus a value-free note — never a raise.

Layering: this is an L2 domain module (registered in the ``sync-domain-is-core``
and ``frozen-core-is-lean`` contracts in ``.importlinter``). It must NOT import
``mooring.ai`` — the outbound scrub (``ai/egress.scrub_text``) belongs to the
CALLERS (the model tools, the chat context assembly, the CLI check), because a
rendered string here has not left the machine yet. It also deliberately does not
live in :mod:`mooring.pbip` (the grouping/launch unit), which needs the sync
types this extractor never touches.

The legacy ``model.bim`` (TMSL JSON) form is not read yet — see the roadmap page
``docs/developers/roadmap/pbi-semantic-model.md`` (phase 4).
"""

from __future__ import annotations

import os
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from mooring.pbip import ARTIFACT_DIR_SUFFIXES

MODEL_DIR_SUFFIX = ".SemanticModel"
assert MODEL_DIR_SUFFIX in ARTIFACT_DIR_SUFFIXES  # stays in lock-step with pbip's grouping

# The only sub-constructs a column/measure block may feed into the result.
_COLUMN_PROPS = ("dataType",)
_MEASURE_PROPS = ("formatString", "displayFolder")
_RELATIONSHIP_PROPS = ("fromColumn", "toColumn", "fromCardinality", "toCardinality")


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str = ""
    dax: str = ""  # calculated-column expression, when authored ("" = a source column)


@dataclass(frozen=True)
class Measure:
    name: str
    dax: str = ""
    format_string: str = ""
    folder: str = ""  # displayFolder


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...] = ()
    measures: tuple[Measure, ...] = ()


@dataclass(frozen=True)
class Relationship:
    from_ref: str  # "Sales.DateKey"
    to_ref: str  # "Date.DateKey"
    cardinality: str = "many-to-one"  # TMDL's default when neither side is stated


@dataclass(frozen=True)
class Excluded:
    """Value-free record of what the allowlist dropped (the drift report)."""

    partitions: int = 0  # partition blocks skipped without capture
    roles_files: int = 0  # files under definition/roles/ — never opened
    culture_files: int = 0  # files under definition/cultures/ (translations) — never opened
    dropped: tuple[tuple[str, int], ...] = ()  # (construct/property keyword, count), sorted


@dataclass(frozen=True)
class SemanticModel:
    name: str
    key: str = ""  # the PBIP artifact key, e.g. "reports/Sales"
    tables: tuple[Table, ...] = ()
    relationships: tuple[Relationship, ...] = ()
    files_read: tuple[str, ...] = ()  # model-dir-relative paths actually opened
    notes: tuple[str, ...] = ()  # value-free "could not read X" notes
    excluded: Excluded = field(default_factory=Excluded)

    @property
    def n_measures(self) -> int:
        return sum(len(t.measures) for t in self.tables)

    def get_table(self, name: str) -> Table | None:
        """Look up a table by NAME in the parsed in-memory model, case-insensitive.

        Never a filesystem path — a path-like argument simply finds nothing.
        """
        key = _strip_quotes(name).lower()
        if not key:
            return None
        for table in self.tables:
            if table.name.lower() == key:
                return table
        return None

    def find_measure(self, name: str) -> tuple[Table, Measure] | None:
        """Look up a measure by NAME across the model's tables (case-insensitive)."""
        key = _strip_quotes(name).lower()
        if not key:
            return None
        for table in self.tables:
            for measure in table.measures:
                if measure.name.lower() == key:
                    return table, measure
        return None


@dataclass(frozen=True)
class ModelRef:
    key: str  # artifact key, e.g. "reports/Sales"
    name: str  # "Sales"
    path: Path  # the <name>.SemanticModel directory


# -- discovery ------------------------------------------------------------------


def definition_signature(model_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """A cheap stat-only fingerprint of the model's definition: sorted
    ``(relpath, mtime_ns, size)`` per ``.tmdl`` file. Empty when there is no
    readable definition (callers treat that as "no model"). This is the cache
    key for anything that must not re-parse a large TMDL tree per poll."""
    definition = model_dir / "definition"
    entries: list[tuple[str, int, int]] = []
    try:
        if not definition.is_dir():
            return ()
        for p in sorted(definition.rglob("*.tmdl")):
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((p.relative_to(model_dir).as_posix(), st.st_mtime_ns, st.st_size))
    except OSError:
        pass
    return tuple(entries)


def find_models(workspace: Path, folders: tuple[str, ...]) -> list[ModelRef]:
    """Discover ``<name>.SemanticModel/`` dirs with a readable definition under
    the synced folders. A model dir without any definition ``.tmdl`` (e.g. a
    report-only PBIP) is not a model."""
    refs: list[ModelRef] = []
    seen: set[str] = set()
    for folder in folders:
        root = workspace / folder
        if not root.is_dir():
            continue
        for dirpath, dirnames, _files in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
            for d in list(dirnames):
                if not d.endswith(MODEL_DIR_SUFFIX):
                    continue
                dirnames.remove(d)  # never descend into the model dir itself
                model_dir = Path(dirpath) / d
                if not definition_signature(model_dir):
                    continue
                try:
                    rel = model_dir.relative_to(workspace).as_posix()
                except ValueError:  # pragma: no cover - folders are workspace-relative
                    continue
                key = rel[: -len(MODEL_DIR_SUFFIX)]
                if key in seen:
                    continue
                seen.add(key)
                refs.append(ModelRef(key=key, name=key.rsplit("/", 1)[-1], path=model_dir))
    return sorted(refs, key=lambda r: r.key)


# -- extraction -------------------------------------------------------------------


def extract_model(path: Path, *, key: str = "", name: str = "") -> SemanticModel:
    """Extract the allowlisted skeleton of the model at ``path`` (a
    ``<name>.SemanticModel`` directory). Tolerant by contract: unknown TMDL
    constructs are dropped (and counted), an unreadable file becomes a value-free
    note, and any unexpected failure yields an empty model — never a raise."""
    if not name:
        name = path.name.removesuffix(MODEL_DIR_SUFFIX)
    try:
        return _extract(path, key=key, name=name)
    except Exception:  # noqa: BLE001  # the never-raise contract: fail to an empty model
        return SemanticModel(
            name=name, key=key, notes=("could not parse the model definition",)
        )


def _extract(path: Path, *, key: str, name: str) -> SemanticModel:
    definition = path / "definition"
    files_read: list[str] = []
    notes: list[str] = []
    dropped: Counter[str] = Counter()
    partitions = 0
    tables: list[Table] = []
    relationships: list[Relationship] = []

    # RLS roles and translations: NEVER opened, only counted — an allowlist means
    # the bytes are not read, not read-then-discarded.
    roles_files = _count_files(definition / "roles")
    culture_files = _count_files(definition / "cultures")

    tables_dir = definition / "tables"
    if tables_dir.is_dir():
        for f in sorted(tables_dir.glob("*.tmdl"), key=lambda p: p.name):
            rel = f"definition/tables/{f.name}"
            text = _read(f)
            if text is None:
                notes.append(f"could not read {rel}")
                continue
            files_read.append(rel)
            parsed, n_parts = _parse_tables(text, dropped)
            tables.extend(parsed)
            partitions += n_parts

    rel_file = definition / "relationships.tmdl"
    if rel_file.is_file():
        text = _read(rel_file)
        if text is None:
            notes.append("could not read definition/relationships.tmdl")
        else:
            files_read.append("definition/relationships.tmdl")
            relationships = _parse_relationships(text, dropped)

    if not files_read and not notes:
        notes.append("no readable definition")
    excluded = Excluded(
        partitions=partitions,
        roles_files=roles_files,
        culture_files=culture_files,
        dropped=tuple(sorted(dropped.items())),
    )
    return SemanticModel(
        name=name,
        key=key,
        tables=tuple(tables),
        relationships=tuple(relationships),
        files_read=tuple(files_read),
        notes=tuple(notes),
        excluded=excluded,
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _count_files(folder: Path) -> int:
    try:
        return sum(1 for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else 0
    except OSError:
        return 0


# -- the TMDL line parser ---------------------------------------------------------
# Indentation-scoped and deliberately shallow: TMDL indents with tabs (a run of
# four spaces is tolerated for hand-edited files). A construct owns every deeper-
# indented line that follows it, so "skip this block" is a single indent compare.


def _indent(line: str) -> int:
    n = i = 0
    while i < len(line):
        if line[i] == "\t":
            n, i = n + 1, i + 1
        elif line[i : i + 4] == "    ":
            n, i = n + 1, i + 4
        else:
            break
    return n


def _strip_quotes(name: str) -> str:
    name = (name or "").strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in ("'", '"'):
        return name[1:-1]
    return name


def _split_declaration(stripped: str) -> tuple[str, str, str | None]:
    """``"measure 'Total' = SUM(x)"`` -> ``("measure", "Total", "SUM(x)")``.

    Returns ``(kind, name, expr)`` where ``expr`` is None when the line has no
    ``=`` (a plain declaration) and ``""`` when the expression starts on the
    following lines. Quoted names may contain spaces and ``=``.
    """
    kind, _, rest = stripped.partition(" ")
    rest = rest.strip()
    expr: str | None = None
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        end = rest.find(quote, 1)
        if end > 0:
            name = rest[1 : end]
            tail = rest[end + 1 :].strip()
            if tail.startswith("="):
                expr = tail[1:].strip()
            return kind, name, expr
    name, eq, tail = rest.partition("=")
    if eq:
        expr = tail.strip()
    return kind, name.strip(), expr


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("//")  # blank lines and /// doc comments


def _block_end(lines: list[str], start: int, indent: int) -> int:
    """Index of the first content line after ``start`` at indent <= ``indent``
    (i.e. the end of the block the line at ``start`` opens)."""
    i = start + 1
    while i < len(lines):
        if _is_content(lines[i]) and _indent(lines[i]) <= indent:
            return i
        i += 1
    return i


def _collect_expression(lines: list[str], i: int, decl_indent: int) -> tuple[str, int]:
    """DAX continuation lines: deeper than the construct's property level
    (``decl_indent + 1``), immediately after the declaration."""
    parts: list[str] = []
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            parts.append("")  # interior blank; trailing blanks trimmed below
            i += 1
            continue
        if _indent(line) >= decl_indent + 2:
            parts.append(line)
            i += 1
            continue
        break
    while parts and not parts[-1].strip():
        parts.pop()
    return textwrap.dedent("\n".join(parts)).strip("\n"), i


def _parse_property(stripped: str) -> tuple[str, str] | None:
    key, colon, value = stripped.partition(":")
    key = key.strip()
    if colon and key and " " not in key and key.isidentifier():
        return key, value.strip()
    return None


def _parse_tables(text: str, dropped: Counter) -> tuple[list[Table], int]:
    lines = text.splitlines()
    n = len(lines)
    tables: list[Table] = []
    partitions = 0
    i = 0
    current_cols: list[Column] = []
    current_measures: list[Measure] = []
    current_name: str | None = None

    def _flush() -> None:
        nonlocal current_name, current_cols, current_measures
        if current_name is not None:
            tables.append(
                Table(
                    name=current_name,
                    columns=tuple(current_cols),
                    measures=tuple(current_measures),
                )
            )
        current_name, current_cols, current_measures = None, [], []

    while i < n:
        line = lines[i]
        if not _is_content(line):
            i += 1
            continue
        indent = _indent(line)
        stripped = line.strip()
        kind, obj_name, expr = _split_declaration(stripped)
        if indent == 0:
            if kind == "table" and obj_name:
                _flush()
                current_name = _strip_quotes(obj_name)
                i += 1
                continue
            dropped[kind or "?"] += 1  # unknown top-level construct -> whole block dropped
            i = _block_end(lines, i, indent)
            continue
        if current_name is None or indent != 1:
            dropped[kind or "?"] += 1  # a stray deeper line outside any known construct
            i = _block_end(lines, i, indent)
            continue
        # Direct children of the table.
        if kind == "column" and obj_name:
            column, i = _parse_column(lines, i, indent, dropped)
            current_cols.append(column)
            continue
        if kind == "measure" and obj_name:
            measure, i = _parse_measure(lines, i, indent, dropped)
            current_measures.append(measure)
            continue
        if kind == "partition":
            # The M/source body is NEVER captured: jump straight past the block.
            partitions += 1
            i = _block_end(lines, i, indent)
            continue
        prop = _parse_property(stripped)
        dropped[prop[0] if prop else (kind or "?")] += 1
        i = _block_end(lines, i, indent)
    _flush()
    return tables, partitions


def _parse_member(
    lines: list[str],
    i: int,
    indent: int,
    dropped: Counter,
    keep_props: tuple[str, ...],
) -> tuple[str, str | None, dict[str, str], int]:
    """Parse one column/measure block: ``(name, dax, kept_props, next_i)``."""
    _kind, obj_name, expr = _split_declaration(lines[i].strip())
    i += 1
    block, i = _collect_expression(lines, i, indent)
    dax = "\n".join(p for p in ((expr or "").strip(), block) if p).strip()
    props: dict[str, str] = {}
    n = len(lines)
    while i < n:
        line = lines[i]
        if not _is_content(line):
            i += 1
            continue
        child = _indent(line)
        if child <= indent:
            break
        stripped = line.strip()
        prop = _parse_property(stripped)
        if child == indent + 1 and prop is not None:
            key, value = prop
            if key in keep_props:
                props[key] = value
            else:
                dropped[key] += 1
            i += 1
            # A property's own continuation block (e.g. a multi-line value) is
            # dropped without capture unless the property itself was kept — and
            # the kept props (dataType/formatString/displayFolder) are one-liners.
            i = max(i, _block_end(lines, i - 1, child))
            continue
        kind, _, _ = _split_declaration(stripped)
        dropped[kind or "?"] += 1  # annotation / extendedProperty / unknown sub-construct
        i = _block_end(lines, i, child)
    return _strip_quotes(obj_name), dax or None, props, i


def _parse_column(lines: list[str], i: int, indent: int, dropped: Counter):
    name, dax, props, i = _parse_member(lines, i, indent, dropped, _COLUMN_PROPS)
    return Column(name=name, data_type=props.get("dataType", ""), dax=dax or ""), i


def _parse_measure(lines: list[str], i: int, indent: int, dropped: Counter):
    name, dax, props, i = _parse_member(lines, i, indent, dropped, _MEASURE_PROPS)
    return (
        Measure(
            name=name,
            dax=dax or "",
            format_string=props.get("formatString", ""),
            folder=props.get("displayFolder", ""),
        ),
        i,
    )


def _parse_relationships(text: str, dropped: Counter) -> list[Relationship]:
    lines = text.splitlines()
    n = len(lines)
    out: list[Relationship] = []
    i = 0
    while i < n:
        line = lines[i]
        if not _is_content(line):
            i += 1
            continue
        indent = _indent(line)
        stripped = line.strip()
        kind, _obj, _expr = _split_declaration(stripped)
        if indent != 0 or kind != "relationship":
            dropped[kind or "?"] += 1
            i = _block_end(lines, i, indent)
            continue
        props: dict[str, str] = {}
        i += 1
        while i < n:
            child_line = lines[i]
            if not _is_content(child_line):
                i += 1
                continue
            if _indent(child_line) <= indent:
                break
            prop = _parse_property(child_line.strip())
            if prop is not None and prop[0] in _RELATIONSHIP_PROPS:
                props[prop[0]] = prop[1]
                i += 1
            else:
                key = prop[0] if prop else _split_declaration(child_line.strip())[0] or "?"
                dropped[key] += 1
                i = _block_end(lines, i, _indent(child_line))
        if props.get("fromColumn") and props.get("toColumn"):
            cardinality = (
                f"{props.get('fromCardinality', 'many')}-to-{props.get('toCardinality', 'one')}"
            )
            out.append(
                Relationship(
                    from_ref=props["fromColumn"],
                    to_ref=props["toColumn"],
                    cardinality=cardinality,
                )
            )
    return out


# -- value-conscious renderers ------------------------------------------------------
# These serialise ONLY the allowlisted skeleton. Callers that send a rendered
# string to the model must still route it through ai/egress.scrub_text — authored
# DAX can embed literal values, and the scrub is the callers' floor, not ours.


def render_summary(model: SemanticModel) -> str:
    """Names only — table names, column counts, and measure NAMES (no DAX)."""
    lines = [
        f"Semantic model `{model.name}`"
        + (f" ({model.key})" if model.key else "")
        + f": {len(model.tables)} tables, {model.n_measures} measures"
    ]
    if model.tables:
        lines.append("Tables:")
        for t in model.tables:
            entry = f"- {t.name} ({len(t.columns)} columns"
            if t.measures:
                names = ", ".join(f"'{m.name}'" for m in t.measures)
                entry += f"; measures: {names}"
            lines.append(entry + ")")
    if model.relationships:
        lines.append("Relationships:")
        for r in model.relationships:
            lines.append(f"- {r.from_ref} -> {r.to_ref} ({r.cardinality})")
    for note in model.notes:
        lines.append(f"(note: {note})")
    return "\n".join(lines)


def render_table(model: SemanticModel, table: Table) -> str:
    """One table: columns (+ calculated-column DAX) and its measures with DAX."""
    lines = [f"Table `{table.name}` (semantic model `{model.name}`)"]
    lines.append("Columns (name: dataType):")
    for c in table.columns:
        entry = f"- {c.name}: {c.data_type or '?'}"
        if c.dax:
            entry += f"  = {_inline(c.dax)}  (calculated)"
        lines.append(entry)
    if table.measures:
        lines.append("Measures:")
        for m in table.measures:
            lines.append(f"- '{m.name}' = {_inline(m.dax)}")
    rels = [
        r
        for r in model.relationships
        if r.from_ref.split(".")[0].strip("'\"") == table.name
        or r.to_ref.split(".")[0].strip("'\"") == table.name
    ]
    if rels:
        lines.append("Relationships:")
        for r in rels:
            lines.append(f"- {r.from_ref} -> {r.to_ref} ({r.cardinality})")
    return "\n".join(lines)


def render_measure(model: SemanticModel, table: Table, measure: Measure) -> str:
    """One measure in full: its DAX plus format string and display folder."""
    lines = [f"Measure '{measure.name}' (table `{table.name}`, model `{model.name}`)"]
    if measure.folder:
        lines.append(f"displayFolder: {measure.folder}")
    if measure.format_string:
        lines.append(f"formatString: {measure.format_string}")
    lines.append("DAX:")
    lines.append(measure.dax or "(no expression)")
    return "\n".join(lines)


def render_models_hint(models: list[SemanticModel]) -> str:
    """The names-only system-context hint: which models exist and how to read
    them (the detail stays behind the pull tools, out of the context window)."""
    if not models:
        return ""
    lines = [
        "This workspace has a Power BI semantic model the tools can read "
        "(tables, columns, relationships, measure DAX — never data):"
        if len(models) == 1
        else f"This workspace has {len(models)} Power BI semantic models the tools "
        "can read (tables, columns, relationships, measure DAX — never data):"
    ]
    for m in models:
        lines.append(
            f"- `{m.name}`"
            + (f" ({m.key})" if m.key else "")
            + f" — {len(m.tables)} tables, {m.n_measures} measures"
        )
    return "\n".join(lines)


def _inline(dax: str) -> str:
    """A DAX expression flattened to one rendering-friendly line block."""
    if not dax:
        return "(no expression)"
    lines = [ln.strip() for ln in dax.splitlines() if ln.strip()]
    return "\n      ".join(lines)
