"""Discover .py files under the offered/synced folders and extract each skeleton.

The I/O + orchestration layer, mirroring :mod:`mooring.ai.datadictionary.loader`:
per-folder discovery, a double path-escape guard, a size cap, BOM-safe decode, and a
never-raise contract (a bad file records an :class:`ExtractReport` error — TYPE + line
only — and is skipped). Vendored/third-party trees are excluded so only the team's own
reusable modules are indexed.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from mooring.ai.codelib import ast_walk, importpath
from mooring.ai.codelib.model import CodeIndex, ExtractReport, Module

DEFAULT_MAX_FILE_BYTES = 512 * 1024  # skip a .py larger than this (a generated/vendored blob)

# Directory names never indexed: virtualenvs, caches, VCS, build output, third-party.
_IGNORE_DIRS = frozenset({
    ".venv", "venv", "env", ".env", "site-packages", "__pycache__", ".mooring", ".git",
    ".hg", ".svn", "build", "dist", "node_modules", ".ipynb_checkpoints", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
})


def load_index(
    workspace: Path,
    folders: Iterable[str],
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    exclude: Iterable[str] = (),
) -> CodeIndex:
    """Parse every reusable ``*.py`` under ``<workspace>/<folder>`` into a
    :class:`CodeIndex`. ``exclude`` holds workspace-relative POSIX paths to skip (e.g.
    the current notebook + dataset). Never raises — a bad file becomes a value-free
    error report and is dropped."""
    ws = Path(workspace)
    ws_resolved = ws.resolve()
    excluded = {str(e).replace("\\", "/").strip("/") for e in exclude if str(e).strip()}

    files: list[Path] = []
    seen: set[Path] = set()
    for folder in folders:
        root = (ws / str(folder)).resolve()
        try:
            root.relative_to(ws_resolved)  # reject a folder that escapes the workspace
        except ValueError:
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if _ignored(path, ws_resolved):
                continue
            rp = path.resolve()
            if rp in seen:  # dedupe symlinked / overlapping folders
                continue
            seen.add(rp)
            files.append(path)

    modules: list[Module] = []
    reports: list[ExtractReport] = []
    for path in files:
        rel = _safe_rel(path, ws_resolved)
        if rel is None:
            reports.append(ExtractReport(path=str(path), error="PathEscape@0"))
            continue
        if rel in excluded:
            continue
        module, report = _extract_file(path, rel, ws, max_file_bytes)
        if module is not None:
            modules.append(module)
        reports.append(report)
    return CodeIndex(modules=tuple(modules), reports=tuple(reports))


def _ignored(path: Path, ws_resolved: Path) -> bool:
    try:
        parts = path.resolve().relative_to(ws_resolved).parts
    except (ValueError, OSError):
        return True
    return any(
        part in _IGNORE_DIRS or part.endswith(".egg-info") or part.startswith(".")
        for part in parts[:-1]  # directories only, not the filename
    )


def _safe_rel(path: Path, ws_resolved: Path) -> str | None:
    try:
        return path.resolve().relative_to(ws_resolved).as_posix()
    except (ValueError, OSError):
        return None


def _extract_file(
    path: Path, rel: str, ws: Path, max_file_bytes: int
) -> tuple[Module | None, ExtractReport]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, ExtractReport(path=rel, error=f"{type(exc).__name__}@0")
    if len(raw) > max_file_bytes:
        return None, ExtractReport(path=rel, error=f"TooLarge@{len(raw) // 1024}")
    source = raw.decode("utf-8-sig", errors="replace")  # -sig strips a UTF-8 BOM (Windows hazard)
    import_path, importable, note = importpath.dotted_path(path, ws)
    module, report = ast_walk.extract_module(
        source, rel, import_path=import_path, importable=importable, import_note=note
    )
    if not (module.functions or module.classes):
        return None, report  # nothing reusable — keep the report, drop the empty module
    return module, report
