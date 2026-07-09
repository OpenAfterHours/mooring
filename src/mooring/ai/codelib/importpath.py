"""Resolve a .py file's importable dotted path — METADATA-ONLY, never imports it.

The only helper import root is the ABSOLUTE workspace root, which
``editor.ensure_runtime_config`` writes into ``<workspace>/.marimo.toml`` as the head
of ``runtime.pythonpath`` — so the dotted path is computed RELATIVE TO THE WORKSPACE
ROOT (``<ws>/pkg/utils/helpers.py`` -> ``pkg.utils.helpers``), never a subfolder. This
resolver reads only the path; it must NEVER import/introspect the target (that would
execute code and break the ast-never-executes property).
"""

from __future__ import annotations

import keyword
from pathlib import Path


def dotted_path(file: Path, ws_root: Path) -> tuple[str, bool, str]:
    """``(import_path, importable, note)`` for ``file`` under ``ws_root``.

    ``note`` is a value-free reason when the module is not importable (outside the
    root, or a path segment that is not a valid identifier — the ``polars.py``-style
    footgun a caller can flag against installed distributions separately).
    """
    try:
        rel = file.resolve().relative_to(ws_root.resolve())
    except (ValueError, OSError):
        return "", False, "outside the workspace import root"
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return "", False, "the workspace root itself is not an importable module"
    import_path = ".".join(parts)
    bad = [p for p in parts if not p.isidentifier() or keyword.iskeyword(p)]
    if bad:
        return import_path, False, f"path segment {bad[0]!r} is not a valid module name"
    return import_path, True, ""
