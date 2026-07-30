"""Discover the workspace's marimo notebooks and extract each catalog entry.

The I/O + orchestration layer, mirroring :mod:`mooring.ai.codelib.loader`: per-folder
discovery plus the loose-root sweep (root ``.py`` files sync by default), a double
path-escape guard, a size cap, BOM-safe decode, and a never-raise contract (a bad file
records an :class:`ExtractReport` error — TYPE + line only — and is skipped). Only
marimo notebooks are catalogued; a plain helper module belongs to the code library.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from mooring.ai.notebookindex import ast_walk
from mooring.ai.notebookindex.model import Catalog, ExtractReport, Notebook

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024  # skip a .py larger than this (a generated blob)

# Directory names never scanned: virtualenvs, caches, VCS, build output, third-party.
# Duplicated from the code library's loader rather than imported: each feature package
# owns what it will read, so tightening one can never silently loosen the other.
_IGNORE_DIRS = frozenset({
    ".venv", "venv", "env", ".env", "site-packages", "__pycache__", ".mooring", ".git",
    ".hg", ".svn", "build", "dist", "node_modules", ".ipynb_checkpoints", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
})


def load_catalog(
    workspace: Path,
    folders: Iterable[str],
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    exclude: Iterable[str] = (),
) -> Catalog:
    """Parse every marimo notebook under ``<workspace>/<folder>`` (and the loose repo
    root) into a :class:`Catalog`.

    ``exclude`` holds workspace-relative POSIX paths to skip — the caller passes the
    team's per-notebook AI opt-out here, so a notebook the team fenced off never enters
    the catalog the copilot can search. Never raises: a bad file becomes a value-free
    error report and is dropped.
    """
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

    # Loose top-level notebooks sync by default (sync.in_sync_scope), so one dropped at
    # the repo root is a first-class team notebook. Sweep the root NON-recursively (folder
    # trees are handled above); skip dot-prefixed names to match is_synced_path.
    for path in sorted(ws.glob("*.py")):
        if path.name.startswith(".") or _ignored(path, ws_resolved):
            continue
        rp = path.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        files.append(path)

    notebooks: list[Notebook] = []
    reports: list[ExtractReport] = []
    for path in files:
        rel = _safe_rel(path, ws_resolved)
        if rel is None:
            reports.append(ExtractReport(path=str(path), error="PathEscape@0"))
            continue
        if rel in excluded:
            continue
        notebook, report = _extract_file(path, rel, max_file_bytes)
        if notebook is not None:
            notebooks.append(notebook)
        reports.append(report)
    return Catalog(
        notebooks=tuple(_with_helpers(nb, ws_resolved) for nb in notebooks),
        reports=tuple(reports),
    )


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
    path: Path, rel: str, max_file_bytes: int
) -> tuple[Notebook | None, ExtractReport]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, ExtractReport(path=rel, error=f"{type(exc).__name__}@0")
    if len(raw) > max_file_bytes:
        return None, ExtractReport(path=rel, error=f"TooLarge@{len(raw) // 1024}")
    source = raw.decode("utf-8-sig", errors="replace")  # -sig strips a UTF-8 BOM (Windows hazard)
    notebook, report = ast_walk.extract_notebook(source, rel)
    if report.error:
        return None, report  # unparseable (mid-edit, or hand-mangled) — keep the report only
    if not report.is_notebook:
        return None, report  # a plain helper module — the code library's business, not ours
    return notebook, report


def _with_helpers(nb: Notebook, ws_resolved: Path) -> Notebook:
    """Mark which of a notebook's imports are the TEAM's own modules — a dotted name that
    resolves to a ``.py`` (or a package ``__init__.py``) inside the workspace.

    Existence is checked with :meth:`Path.is_file`; the module is never imported, so this
    stays inside the never-execute contract. It answers "which of our helpers does this
    notebook build on?", the reverse of the code library's "who could reuse this?".
    """
    from dataclasses import replace

    helpers = []
    for name in nb.imports:
        head = name.lstrip(".")
        if not head:
            continue
        parts = head.split(".")
        if not all(p.isidentifier() for p in parts):
            continue  # never hand a non-identifier segment to the filesystem
        # `from pkg.mod import thing` records "pkg.mod.thing"; try the longest prefix that
        # is a real file, so both the module and the symbol form resolve to the module.
        for cut in (len(parts), len(parts) - 1):
            if cut < 1:
                continue
            stem = ws_resolved.joinpath(*parts[:cut])
            if stem.with_suffix(".py").is_file() or (stem / "__init__.py").is_file():
                helpers.append(".".join(parts[:cut]))
                break
    return replace(nb, helpers=tuple(dict.fromkeys(helpers)))
