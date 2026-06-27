"""Detect notebook filenames that shadow an importable module (a sys.path[0] trap).

When marimo runs a notebook, the notebook's own directory goes on ``sys.path[0]``.
So a notebook named ``polars.py`` makes ``import polars`` resolve to that file
instead of the real Polars package — and because marimo's own internals import
the data-science stack (polars/pandas/...) to render tables, this breaks EVERY
notebook in the folder, even ones that never import polars. The symptom is an
inscrutable kernel traceback (``AttributeError: module 'polars' has no attribute
'DataFrame'``). This module spots the collision so the adapters can warn with a
plain rename instruction instead.

L0 leaf (registered in .importlinter ``foundation-is-pure``): this imports only
the stdlib and NOTHING from mooring, so keep it that way — the ``extra``/``ignore``
policy sets are passed in as parameters rather than read here, so the detector
stays pure and load-time-safe. Filesystem reads (ast-parsing sibling notebooks)
are intentional.

Three empirically-verified rules drive the boundary (see the design notes):

- **Exact-case match.** Python's final import match is case-sensitive even on a
  case-insensitive filesystem, so ``polars.py`` shadows but ``Polars.py`` does
  not — we never casefold.
- **Built-in/frozen modules can't be shadowed.** ``os``/``sys``/``io``/``math``
  precede the path finder, so an ``os.py`` never wins; :func:`_shadowable` drops
  them (and drops names that aren't importable at all, like ``test`` on a CPython
  build that omits the test package — which is why a notebook named ``test.py``
  is never flagged).
- **Stdlib names only when actually in play.** A stdlib-named notebook is flagged
  only when that module is genuinely loaded in the running process or imported by
  a sibling notebook — so ``json.py`` warns but a dormant ``code.py`` stays quiet.

The danger set is unconditional: third-party stems we know the notebook stack
imports (their package lives in the notebook venv, which the detecting process
can't see, so we never gate them on :func:`_shadowable`).
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path

# Third-party import names common in analyst notebooks that marimo itself or the
# notebook commonly imports — the headline footguns, flagged unconditionally (the
# real package lives in the notebook venv, which this process can't introspect, so
# we match on the name alone). Import names, not distribution names, so the famous
# divergences are covered here (PIL, cv2, yaml, bs4, sklearn).
DANGER_MODULES: frozenset[str] = frozenset(
    {
        "polars",
        "pandas",
        "numpy",
        "scipy",
        "matplotlib",
        "seaborn",
        "plotly",
        "altair",
        "bokeh",
        "sklearn",
        "statsmodels",
        "pyarrow",
        "narwhals",
        "duckdb",
        "ibis",
        "dask",
        "openpyxl",
        "xlsxwriter",
        "xlrd",
        "fastexcel",
        "requests",
        "httpx",
        "sqlalchemy",
        "psycopg2",
        "pymysql",
        "snowflake",
        "boto3",
        "pydantic",
        "marimo",
        "anywidget",
        "networkx",
        "sympy",
        "geopandas",
        "shapely",
        "tqdm",
        "joblib",
        "numba",
        "torch",
        "tensorflow",
        "transformers",
        "datasets",
        "cv2",
        "PIL",
        "yaml",
        "bs4",
        "lxml",
        "dateutil",
        "pytz",
    }
)

_STDLIB_NAMES = frozenset(sys.stdlib_module_names)
_SHADOWABLE_CACHE: dict[str, bool] = {}


def _shadowable(name: str) -> bool:
    """Whether a same-directory ``<name>.py`` could actually win the import — i.e.
    ``name`` resolves to a module loaded from a real file. Built-in and frozen
    modules (``sys``/``os``/``io``/``math``) precede the path finder and can never
    be shadowed; a name that isn't importable at all (``test`` on a slim CPython)
    resolves to nothing. Cached; never raises (an L0 leaf must not blow up)."""
    cached = _SHADOWABLE_CACHE.get(name)
    if cached is not None:
        return cached
    result = False
    try:
        spec = importlib.util.find_spec(name)
    except Exception:  # noqa: BLE001  # find_spec can raise on a half-broken parent
        spec = None
    if spec is not None and spec.origin and spec.origin not in ("built-in", "frozen"):
        result = True
    _SHADOWABLE_CACHE[name] = result
    return result


def _loaded_stdlib() -> set[str]:
    """Stdlib modules currently imported in this process — the ones a notebook run
    is certain to re-import (and thus collide with). Read live (cheap) so it
    reflects whatever the adapter has loaded by the time it scans."""
    return set(sys.modules) & _STDLIB_NAMES


def _top_level_imports(source: str) -> set[str]:
    """Top-level import roots in ``source`` (first dotted segment of ``import x.y``
    and ``from x.y import z``; relative imports skipped). Uses ``ast.walk`` so it
    recurses into function bodies — marimo wraps every notebook import inside an
    ``@app.cell def _()`` body, so a module-level-only scan would miss them all."""
    roots: set[str] = set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return roots
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _norm(rel: str) -> str:
    return str(rel).replace("\\", "/")


def _dir_imports(workspace: Path, rels: Sequence[str]) -> set[str]:
    """Union of the top-level imports declared by the ``.py`` files in one folder.
    Best-effort: an unreadable or in-progress notebook is skipped, never raised."""
    roots: set[str] = set()
    for rel in rels:
        try:
            text = (workspace / rel).read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        roots |= _top_level_imports(text)
    return roots


def scan(
    rel_paths: Sequence[str],
    *,
    workspace: Path,
    extra: frozenset[str] = frozenset(),
    ignore: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Map each ``.py`` in ``rel_paths`` whose stem shadows an importable module to
    that module name. ``extra`` is the repo's own importable package names (also
    flagged unconditionally); ``ignore`` is the set of acknowledged notebook paths
    to skip. Pure given the inputs + the workspace files; never raises."""
    by_dir: dict[str, list[tuple[str, str]]] = {}
    for rel in rel_paths:
        r = _norm(rel)
        if not r.endswith(".py"):
            continue
        parent, _, base = r.rpartition("/")
        by_dir.setdefault(parent, []).append((r, base))

    loaded = _loaded_stdlib()
    findings: dict[str, str] = {}
    for items in by_dir.values():
        sibling: set[str] | None = None  # parsed lazily, only if a stem needs it
        for r, base in items:
            stem = base[:-3]  # drop ".py"
            if not stem.isidentifier() or stem.startswith("__") or r in ignore:
                continue
            if stem in DANGER_MODULES or stem in extra:
                findings[r] = stem
                continue
            if not _shadowable(stem):
                continue
            if stem in loaded:
                findings[r] = stem
                continue
            if sibling is None:
                sibling = _dir_imports(workspace, [it[0] for it in items])
            if stem in sibling:
                findings[r] = stem
    return findings


def folder_shadows(
    rel_path: str,
    *,
    workspace: Path,
    extra: frozenset[str] = frozenset(),
    ignore: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Shadowing ``.py`` files in ``rel_path``'s folder (read off disk, including
    ``rel_path`` itself). Used at open time: opening an innocent notebook still
    warns when a sibling poisons the folder's ``sys.path[0]``."""
    parent = _norm(rel_path).rpartition("/")[0]
    dir_abs = workspace / parent if parent else workspace
    try:
        names = [e.name for e in dir_abs.iterdir() if e.is_file() and e.name.endswith(".py")]
    except OSError:
        return {}
    rels = [f"{parent}/{n}" if parent else n for n in names]
    return scan(rels, workspace=workspace, extra=extra, ignore=ignore)


def warning_lines(findings: dict[str, str]) -> list[str]:
    """Format ``findings`` the house way (a ``Warning:`` head line + indented
    remediation, like ``cli._missing_deps_lines``). Empty when there's nothing."""
    if not findings:
        return []
    lines = ["Warning: notebook name(s) shadow an importable module:"]
    for rel in sorted(findings):
        name = findings[rel]
        lines.append(f"  {rel} — `import {name}` would load this file instead of the {name} module.")
    lines.append(
        "  Rename the file(s); otherwise every notebook in that folder can fail to import."
    )
    lines.append("  Already aware? Silence one with `mooring shadow ignore <path>`.")
    return lines
