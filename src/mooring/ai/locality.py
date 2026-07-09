"""Pick the slice of the data dictionary relevant to what the analyst is doing.

"Locality" scopes context to the analyst's working area so a large dictionary
never floods the prompt. Every signal it uses is already value-free and already
crosses the wire today: the selected dataset's column NAMES (from
:mod:`mooring.schema`) and the identifiers in the notebook ``.py`` SOURCE (which
is already sent). Lexing those locally to choose which value-free dictionary
tables to seed reveals nothing new — the only thing "selection" exposes is which
table NAMES matched, and names are inside the dictionary allowlist anyway.

Seeding is therefore a token optimisation, not a privacy control: the rest of
the dictionary stays reachable via the pull tools, and everything reachable has
already passed the five-slot allowlist in :mod:`mooring.ai.datadictionary`.
"""

from __future__ import annotations

import ast
import re

from mooring.ai.datadictionary import DictionaryIndex, Table, render_tables

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

SEED_MAX_TABLES = 8
SEED_MAX_COLUMNS_TOTAL = 200
HELPER_SEED_MAX_MODULES = 8


def _identifiers(notebook_source: str) -> set[str]:
    """Lowercased identifier-like tokens from the notebook source (NAME tokens and
    the contents of string literals, which catch ``read_parquet("…/orders.csv")``
    and ``FROM fact_loans`` inside ``mo.sql`` strings)."""
    return {m.group(0).lower() for m in _IDENT.finditer(notebook_source or "")}


def _folder_domain(notebook_rel: str) -> str:
    parts = (notebook_rel or "").replace("\\", "/").split("/")
    return parts[-2].lower() if len(parts) >= 2 else ""


def _fk_targets(table: Table) -> set[str]:
    """Table names a table points at via its relationship slots ('FK -> dim_x.id')."""
    targets: set[str] = set()
    for col in table.columns:
        for m in re.finditer(r"->\s*([A-Za-z_][\w.]*)", col.relationship or ""):
            targets.add(m.group(1).split(".")[0].lower())
    return targets


def working_set(
    index: DictionaryIndex,
    *,
    dataset_columns: set[str] | None = None,
    dataset_stem: str = "",
    notebook_source: str = "",
    notebook_rel: str = "",
    max_tables: int = SEED_MAX_TABLES,
    max_columns_total: int = SEED_MAX_COLUMNS_TOTAL,
    expand_fk: bool = True,
) -> tuple[list[Table], dict[str, str], int]:
    """Return ``(tables, reasons, n_more)`` — the seeded tables, why each was
    chosen (keyed by qualified name), and how many relevant tables didn't fit."""
    if index.is_empty():
        return [], {}, 0
    idents = _identifiers(notebook_source)
    if dataset_stem:
        idents.add(dataset_stem.lower())
    cols = {c.lower() for c in (dataset_columns or set())}
    folder = _folder_domain(notebook_rel)

    scored: list[tuple[int, Table, str]] = []
    for table in index.tables:
        score = 0
        reasons: list[str] = []
        if dataset_stem and table.name.lower() == dataset_stem.lower():
            score += 5
            reasons.append("matches your dataset")
        if table.name.lower() in idents or table.qualified.lower() in idents:
            score += 4
            reasons.append("referenced in your notebook")
        overlap = cols & table.column_name_set()
        if overlap:
            score += min(len(overlap), 3)
            shown = ", ".join(sorted(overlap)[:3])
            reasons.append(f"shares columns with your dataset ({shown})")
        elif table.column_name_set() & idents:
            score += 1
            reasons.append("its columns appear in your notebook")
        if folder and folder == table.domain.lower():
            score += 1
            reasons.append(f"in the {table.domain} domain")
        if score > 0:
            scored.append((score, table, "; ".join(reasons)))

    scored.sort(key=lambda s: (-s[0], s[1].qualified))

    selected: list[Table] = []
    reasons: dict[str, str] = {}
    used_cols = 0
    overflow = 0
    for score, table, reason in scored:
        if len(selected) >= max_tables or used_cols + len(table.columns) > max_columns_total:
            overflow += 1
            continue
        selected.append(table)
        reasons[table.qualified] = reason
        used_cols += len(table.columns)

    if expand_fk:
        chosen = {t.qualified.lower() for t in selected}
        for table in list(selected):
            for target in _fk_targets(table):
                hit = index.get(target)
                if (
                    hit is not None
                    and hit.qualified.lower() not in chosen
                    and len(selected) < max_tables
                    and used_cols + len(hit.columns) <= max_columns_total
                ):
                    selected.append(hit)
                    chosen.add(hit.qualified.lower())
                    reasons[hit.qualified] = f"referenced by {table.name}"
                    used_cols += len(hit.columns)

    return selected, reasons, overflow


def seed_text(tables: list[Table], reasons: dict[str, str], n_more: int) -> str:
    """The 'RELEVANT DATA DICTIONARY' block for the system context."""
    if not tables:
        return ""
    why = "; ".join(f"{t.name} ({reasons.get(t.qualified, 'relevant')})" for t in tables)
    head = (
        "Auto-selected for your current work (more tables exist - use "
        "mooring_search_dictionary / mooring_describe_table to fetch them).\n"
        f"Loaded: {why}."
    )
    body = render_tables(tables)
    if n_more:
        body += f"\n\n(+{n_more} more relevant tables not shown - ask and I'll look them up.)"
    return f"{head}\n\n{body}"


# -- code library (reusable helper modules) locality ---------------------------
# The same token-optimisation idea as the dictionary above, applied to the value-free
# code skeleton: seed the skeletons of modules the notebook already imports / references,
# and leave the rest to the pull tools. Every signal is value-free (module + symbol NAMES,
# the notebook's own import statements), so seeding reveals nothing new.


def _notebook_imports(notebook_source: str) -> set[str]:
    """Lower-cased module names the notebook imports (ast-parsed) — the highest-precision
    reuse signal. Empty on a syntax error."""
    names: set[str] = set()
    try:
        tree = ast.parse(notebook_source or "")
    except (SyntaxError, ValueError):
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                names.add(n.name.lower())
                names.add(n.name.split(".")[0].lower())
                if n.asname:
                    names.add(n.asname.lower())
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.lower())
            names.add(node.module.split(".")[0].lower())
    return names


def helper_working_set(
    code_index,
    *,
    notebook_source: str = "",
    notebook_rel: str = "",
    max_modules: int = HELPER_SEED_MAX_MODULES,
    expand_imports: bool = True,
) -> tuple[list, dict[str, str], int]:
    """Return ``(modules, reasons, n_more)`` — the helper modules to seed, why each was
    chosen (keyed by import_path/path), and how many relevant modules didn't fit."""
    if code_index.is_empty():
        return [], {}, 0
    imported = _notebook_imports(notebook_source)
    idents = _identifiers(notebook_source)
    folder = _folder_domain(notebook_rel)

    scored: list[tuple[int, object, str]] = []
    for module in code_index.modules:
        score = 0
        reasons: list[str] = []
        ip = module.import_path.lower()
        stem = ip.split(".")[-1] if ip else ""
        aliases = {a.lower() for _, a in module.import_aliases}
        if ip and (ip in imported or stem in imported or (aliases & imported)):
            score += 5
            reasons.append("imported by your notebook")
        symbols = {f.name.lower() for f in module.functions} | {c.name.lower() for c in module.classes}
        if (stem and stem in idents) or (symbols & idents):
            score += 4
            reasons.append("referenced in your notebook")
        top = ip.split(".")[0] if ip else ""
        if folder and top and folder == top:
            score += 1
            reasons.append("in your working area")
        if score > 0:
            scored.append((score, module, "; ".join(reasons)))

    scored.sort(key=lambda s: (-s[0], (s[1].import_path or s[1].path)))
    selected: list = []
    reasons_out: dict[str, str] = {}
    overflow = 0
    for _score, module, reason in scored:
        if len(selected) >= max_modules:
            overflow += 1
            continue
        selected.append(module)
        reasons_out[module.import_path or module.path] = reason

    if expand_imports:
        by_path = {m.import_path: m for m in code_index.modules if m.import_path}
        chosen = {m.import_path or m.path for m in selected}
        for module in list(selected):
            for imp in module.imports:
                parts = imp.lstrip(".").split(".")
                for i in range(len(parts), 0, -1):
                    hit = by_path.get(".".join(parts[:i]))
                    if hit and hit.import_path not in chosen and len(selected) < max_modules:
                        selected.append(hit)
                        chosen.add(hit.import_path)
                        reasons_out[hit.import_path] = f"used by {module.import_path or module.path}"
                        break
    return selected, reasons_out, overflow


def helper_seed_text(modules: list, reasons: dict[str, str], n_more: int) -> str:
    """The 'RELEVANT HELPER MODULES' block. Returns '' when nothing matched — so a repo
    with .py present but no relevant module adds NO dangling head (byte-identity)."""
    if not modules:
        return ""
    from mooring.ai import codelib

    def _key(m):
        return m.import_path or m.path

    why = "; ".join(f"{_key(m)} ({reasons.get(_key(m), 'relevant')})" for m in modules)
    head = (
        "Auto-selected reusable helpers for your current work (more exist - use "
        "mooring_search_helpers / mooring_describe_helper to fetch them).\n"
        f"Loaded: {why}."
    )
    body = codelib.render_modules(modules, max_methods=12)
    if n_more:
        body += f"\n\n(+{n_more} more relevant modules not shown - ask and I'll look them up.)"
    return f"{head}\n\n{body}"


def enrich_dataset_schema(schema, index: DictionaryIndex, source_rel: str = "") -> str:
    """Render the dataset schema with dictionary annotations attached by column name.

    The FILE schema stays ground truth: the real dtype is kept verbatim and the
    dictionary only ADDS a description / FK / a 'dict type' second opinion. A
    stale dictionary can annotate but never misrepresent the actual type.
    """
    if schema is None:
        return ""
    ann = _column_annotations(index)
    where = f" loaded from `{source_rel}`" if source_rel else ""
    rows = f" ({schema.n_rows:,} rows)" if schema.n_rows is not None else ""
    lines = [f"Polars DataFrame `df`{where}{rows}.", "Columns (name: dtype):"]
    for name, dtype in schema.columns:
        line = f"- {name}: {dtype}"
        extra = ann.get(name.lower())
        if extra:
            line += f"  - {extra}"
        lines.append(line)
    return "\n".join(lines)


def _column_annotations(index: DictionaryIndex) -> dict[str, str]:
    """Map column-name -> a short annotation drawn from the dictionary (first hit
    that carries a description/relationship/type wins)."""
    out: dict[str, str] = {}
    for table in index.tables:
        for col in table.columns:
            key = col.name.lower()
            if key in out:
                continue
            bits = []
            if col.relationship:
                bits.append(col.relationship)
            if col.nullable is False:
                bits.append("not null")
            if col.type:
                bits.append(f"dict type: {col.type}")
            if col.description:
                bits.append(f'"{col.description}"')
            if bits:
                out[key] = "; ".join(bits)
    return out
