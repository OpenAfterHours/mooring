"""Schema-only dataset inspection — the privacy core of the AI helper.

These functions extract ONLY a dataset's column names, dtype strings, and a row
count. They provably never return a cell value: they read parquet footers and
csv/xlsx headers, and although polars/fastexcel may sample rows *internally* to
infer csv/xlsx dtypes, those values are decoded inside the library and never
reach the caller — we only ever emit ``str(name)``, ``str(dtype)`` and an
``int`` count. The one rule that keeps this airtight: never call a
materialisation method (``.collect()`` on data, ``.head``, ``.row``, ``.item``
on a real column, ``to_polars``/``to_arrow``/``to_pandas``).

Verified against polars 1.41.2, fastexcel 0.20.2 (pyarrow is not bundled, so the
parquet row count comes from polars' ``pl.len()`` lazy aggregate).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Extensions we can extract a schema from (data files analysts load into df).
SUPPORTED_EXTENSIONS = (".parquet", ".pq", ".csv", ".xlsx", ".xlsm")


@dataclass(frozen=True)
class DatasetSchema:
    name: str
    columns: tuple[tuple[str, str], ...]  # (column_name, dtype_string) — never a value
    n_rows: int | None = None


def extract_schema(path: str | Path) -> DatasetSchema:
    """Return only column names, dtype strings, and a row count for a dataset.

    Raises ``ValueError`` for an unsupported extension and lets the underlying
    reader raise (e.g. ``FileNotFoundError``) for an unreadable file.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".parquet", ".pq"):
        return _from_parquet(p)
    if ext == ".csv":
        return _from_csv(p)
    if ext in (".xlsx", ".xlsm"):
        return _from_xlsx(p)
    raise ValueError(f"Unsupported file type {ext!r} for {p.name}")


def _from_parquet(p: Path) -> DatasetSchema:
    import polars as pl

    # Names + dtypes come from the parquet footer/Arrow schema only (no data pages).
    schema = pl.scan_parquet(p).collect_schema()
    columns = tuple((name, str(dtype)) for name, dtype in schema.items())
    # Row count from row-group metadata: the optimised plan projects 0 columns.
    n_rows = _safe_count(lambda: pl.scan_parquet(p).select(pl.len()).collect().item())
    return DatasetSchema(name=p.name, columns=columns, n_rows=n_rows)


def _from_csv(p: Path) -> DatasetSchema:
    import polars as pl

    # Header gives names; polars samples a bounded number of rows to infer dtypes,
    # but only names + dtype strings are returned — no sampled value is exposed.
    schema = pl.scan_csv(p).collect_schema()
    columns = tuple((name, str(dtype)) for name, dtype in schema.items())
    n_rows = _safe_count(lambda: pl.scan_csv(p).select(pl.len()).collect().item())
    return DatasetSchema(name=p.name, columns=columns, n_rows=n_rows)


def _from_xlsx(p: Path) -> DatasetSchema:
    import fastexcel

    excel = fastexcel.read_excel(str(p))
    # n_rows=1 lets fastexcel infer dtypes from a single row without loading the
    # body; available_columns() exposes only .name and .dtype (never values).
    sheet = excel.load_sheet(0, n_rows=1, header_row=0)
    columns = tuple((c.name, str(c.dtype)) for c in sheet.available_columns())
    n_rows = _safe_count(lambda: int(sheet.total_height))
    return DatasetSchema(name=p.name, columns=columns, n_rows=n_rows)


def _safe_count(fn) -> int | None:
    try:
        return int(fn())
    except Exception:  # noqa: BLE001  # a row count is a nicety, never block on it
        return None


def list_datasets(workspace: Path, folders: tuple[str, ...]) -> list[str]:
    """Workspace-relative paths of inspectable data files under ``folders``, plus any
    loose top-level data file (which syncs by default — see sync.in_sync_scope)."""
    found: list[str] = []
    for folder in folders:
        root = workspace / folder
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                found.append(path.relative_to(workspace).as_posix())
    # Loose root-level data files (non-recursive; dot-prefixed names excluded to match
    # is_synced_path, which keeps them out of sync).
    for path in workspace.glob("*"):
        if (
            path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ):
            found.append(path.name)
    return sorted(set(found))


def format_for_ai(schema: DatasetSchema, source: str | None = None) -> str:
    """Render a schema as compact text for the model — names + dtypes only."""
    lines = []
    where = f" loaded from `{source}`" if source else ""
    rows = f" ({schema.n_rows:,} rows)" if schema.n_rows is not None else ""
    lines.append(f"Polars DataFrame `df`{where}{rows}.")
    lines.append("Columns (name: dtype):")
    for name, dtype in schema.columns:
        lines.append(f"- {name}: {dtype}")
    return "\n".join(lines)
