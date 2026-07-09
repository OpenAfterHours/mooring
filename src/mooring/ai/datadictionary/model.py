"""The value-minimised data model — the allowlist every parser must produce.

The dataclasses here ARE the privacy allowlist: only the five :class:`Column`
fields and a table's name/description may ever cross the wire. A parser (in
:mod:`mooring.ai.datadictionary.parsers`) chooses which source keys feed these
slots and drops everything else; it can never add a slot. The renderers serialise
these objects to the compact text the tools/seeding send — names + dtypes only,
never a value.
"""

from __future__ import annotations

from dataclasses import dataclass

DESC_CAP = 500  # max chars kept from any single description (the one free-text slot)


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
    # The context FOLDER this table was loaded from (a value-free path). NON-KEY:
    # ``qualified`` and :meth:`DictionaryIndex.get` ignore it — it exists only to
    # attribute a cross-folder collision in a :class:`ParseReport` (see
    # :func:`merge_indexes`) when several context folders are read at once.
    source: str = ""

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


# -- cross-folder merge (several context folders read at once) ---------------


def merge_indexes(indexes_in_order: "list[DictionaryIndex]") -> "DictionaryIndex":
    """Merge the per-folder indexes into one, FIRST-FOLDER-WINS on a name clash.

    ``indexes_in_order`` is the list of :class:`DictionaryIndex` from each context
    folder in precedence (stable sorted-folder) order. Tables are concatenated in
    that order; on a duplicate ``qualified`` name (the domain is the FILE STEM, so
    two ``credit.yaml`` in different folders both become domain ``credit``) the
    earlier folder's table is kept and a :class:`ParseReport` is EMITTED for each
    shadowed copy — a collision is surfaced, never silently resolved by concat
    order (the "parsing is never silently wrong" contract). Every folder's own
    reports are preserved. A single index passes through unchanged (byte-identical
    single-folder behaviour), so ``get``/FK/annotation first-match stays
    deterministic. ``Table.source`` disambiguates otherwise-identical findings.
    """
    if len(indexes_in_order) == 1:
        return indexes_in_order[0]
    merged: list[Table] = []
    reports: list[ParseReport] = []
    collisions: list[ParseReport] = []
    winners: dict[str, Table] = {}
    for index in indexes_in_order:
        reports.extend(index.reports)
        for table in index.tables:
            key = table.qualified.lower()
            winner = winners.get(key)
            if winner is None:
                winners[key] = table
                merged.append(table)
            else:
                collisions.append(
                    ParseReport(
                        path=table.source or table.domain,
                        domain=table.domain,
                        shape="error",
                        error=(
                            f"duplicate table '{table.qualified}' - already defined by "
                            f"context folder '{winner.source or '?'}'; this copy from "
                            f"'{table.source or '?'}' is ignored"
                        ),
                    )
                )
    return DictionaryIndex(tables=tuple(merged), reports=tuple(reports + collisions))


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
