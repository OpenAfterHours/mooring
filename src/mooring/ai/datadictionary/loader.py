"""Discover dictionary files on disk and turn each into parsed tables + a report.

The I/O + orchestration layer: path-checking, size-capping, ``yaml.safe_load``,
then dispatch to a format parser via :mod:`mooring.ai.datadictionary.parsers`.
Never raises for a bad file — it records the error in that file's report.
"""

from __future__ import annotations

from pathlib import Path

from mooring.ai.datadictionary.model import DictionaryIndex, ParseReport, Table
from mooring.ai.datadictionary.parsers import PARSERS, detect

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024  # reject a dictionary file larger than this

_DICT_DIR = "dictionaries"  # <context_dir>/dictionaries/*.yaml (per-domain)
_SINGLE_FILE = "datadictionary.yaml"  # <context_dir>/datadictionary.yaml (single-file form)


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
    shape = mapping.get("format") if mapping else detect(data)
    parser = PARSERS.get(shape, PARSERS["generic"])
    # Honour the "never raises for a bad file" contract: any parser error on a
    # valid-but-oddly-typed YAML degrades to an error report, never a crash.
    try:
        tables = parser(data, domain, dropped, mapping)
    except Exception as exc:  # noqa: BLE001  # report and skip the file, don't crash the chat
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
