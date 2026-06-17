"""Per-repo notebook dependencies via a root ``pyproject.toml`` + ``uv.lock``.

A repo's notebooks all share one environment declared in ``pyproject.toml`` at
the workspace root (synced to GitHub like any other file — see sync.PROJECT_FILES).
uv users run notebooks against it (``uv run --frozen`` — see editor.py); for a
frozen ``.pyz`` with no uv, the bundle the admin built is used and `missing_deps`
warns about anything it can't provide.

This module owns: scaffolding the file (minimal — just marimo, so teams add their
own packages rather than inheriting a stack), checking which declared packages the
current interpreter can satisfy, and the thin ``uv`` wrappers behind
``mooring deps`` / ``mooring init`` / ``mooring build-requirements``.
"""

from __future__ import annotations

import importlib.metadata
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

# The seed only carries marimo (the editor needs it); everything else is the
# team's choice. Floor matches mooring's own marimo floor (mooring.marimo_rt
# asserts the same minimum at runtime; kept in sync by test_marimo_floor).
MARIMO_REQUIREMENT = "marimo>=0.23.9"

PYPROJECT_NAME = "pyproject.toml"
LOCK_NAME = "uv.lock"

# Leading distribution name of a PEP 508 requirement, e.g. "polars",
# "requests>=2", "foo[extra]>=1; python_version>'3.10'", "pkg @ https://…".
_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


class UvNotAvailable(RuntimeError):
    """uv is needed for the requested operation but isn't on PATH."""


def uv_available() -> bool:
    return shutil.which("uv") is not None


def require_uv() -> None:
    if not uv_available():
        raise UvNotAvailable(
            "This needs uv, which isn't installed. Install it from "
            "https://docs.astral.sh/uv/, or run mooring from PyPI (`uvx mooring`)."
        )


def pyproject_path(workspace: Path) -> Path:
    return workspace / PYPROJECT_NAME


def lock_path(workspace: Path) -> Path:
    return workspace / LOCK_NAME


def has_pyproject(workspace: Path) -> bool:
    return pyproject_path(workspace).is_file()


def _project_name(raw: str) -> str:
    """A PEP 508-safe project name derived from a repo/dir name."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip().lower()).strip("-._") or "mooring"
    return name if name.endswith("notebooks") else f"{name}-notebooks"


def _seed_text(name: str) -> str:
    # package = false marks this a virtual project (deps only, no build backend),
    # so uv lock/run/export work without the workspace being an installable package.
    return (
        "[project]\n"
        f'name = "{name}"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = [\n"
        f'    "{MARIMO_REQUIREMENT}",\n'
        "]\n"
        "\n"
        "[tool.uv]\n"
        "package = false\n"
    )


def scaffold(workspace: Path, name: str | None = None, lock: bool = True) -> bool:
    """Write a minimal ``pyproject.toml`` if absent. Returns True if created.

    When uv is present and ``lock`` is set, also generate ``uv.lock`` so the env
    is reproducible from the first commit. Never overwrites an existing file.
    """
    target = pyproject_path(workspace)
    if target.exists():
        return False
    workspace.mkdir(parents=True, exist_ok=True)
    project = _project_name(name or workspace.name)
    target.write_text(_seed_text(project), encoding="utf-8", newline="\n")
    if lock and uv_available():
        try:
            run_lock(workspace)
        except subprocess.CalledProcessError:
            pass  # the pyproject still stands; the lock can be made later
    return True


def declared_deps(workspace: Path) -> list[str]:
    """The raw requirement strings from ``[project].dependencies`` (empty if no
    pyproject or it's unparseable)."""
    target = pyproject_path(workspace)
    if not target.is_file():
        return []
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    deps = data.get("project", {}).get("dependencies", [])
    return [d for d in deps if isinstance(d, str)]


def _bare_name(requirement: str) -> str | None:
    m = _NAME_RE.match(requirement.strip())
    return m.group(1) if m else None


def declares(workspace: Path, dist_name: str) -> bool:
    """Whether the repo pyproject lists a requirement for ``dist_name``."""
    target = dist_name.lower()
    return any((_bare_name(r) or "").lower() == target for r in declared_deps(workspace))


def _is_installed(dist_name: str) -> bool:
    try:
        importlib.metadata.distribution(dist_name)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def dep_status(workspace: Path) -> list[tuple[str, bool]]:
    """(requirement, available-in-current-env) for each declared dependency."""
    out: list[tuple[str, bool]] = []
    for req in declared_deps(workspace):
        name = _bare_name(req)
        out.append((req, bool(name) and _is_installed(name)))
    return out


def missing_deps(workspace: Path) -> list[str]:
    """Bare names of declared packages the current interpreter can't import.

    Best-effort (no marker/extra evaluation): used to warn frozen-.pyz users that
    a notebook's repo declares packages the bundle doesn't carry.
    """
    missing = []
    for req in declared_deps(workspace):
        name = _bare_name(req)
        if name and not _is_installed(name):
            missing.append(name)
    return missing


# -- uv wrappers ------------------------------------------------------------


def _run_uv(workspace: Path, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    require_uv()
    cmd = ["uv", *args, "--project", str(workspace)]
    return subprocess.run(
        cmd,
        cwd=str(workspace),
        check=True,
        text=True,
        capture_output=capture,
    )


def run_lock(workspace: Path) -> None:
    _run_uv(workspace, "lock")


def add(workspace: Path, packages: list[str]) -> None:
    _run_uv(workspace, "add", *packages)


def remove(workspace: Path, packages: list[str]) -> None:
    _run_uv(workspace, "remove", *packages)


def export_requirements(workspace: Path) -> str:
    """The repo's declared top-level packages, one per line, for feeding a frozen
    build (``uv add -r``). marimo is omitted — mooring's own bundle always carries
    it. Reads the pyproject directly, so it works without uv."""
    lines = [
        req for req in declared_deps(workspace) if (_bare_name(req) or "").lower() != "marimo"
    ]
    return "".join(f"{line}\n" for line in lines)
