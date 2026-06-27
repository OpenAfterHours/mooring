#!/usr/bin/env python3
"""Cut a mooring release: bump the version, commit, tag, and push.

Runs anywhere with Python 3.12+, git, and uv on PATH.

Bumps the version in pyproject.toml + uv.lock (via ``uv version``) and keeps
src/mooring/__init__.py in sync, runs lint and tests, commits the bump, creates
the vX.Y.Z tag, and pushes branch + tag. The pushed tag triggers
.github/workflows/release.yml, which builds mooring.pyz / mooring.exe, publishes
the GitHub Release, and uploads the sdist/wheel to PyPI.

    python scripts/release.py                  # patch: 0.1.0 -> 0.1.1
    python scripts/release.py minor            # 0.1.0 -> 0.2.0 (also: major)
    python scripts/release.py --version 1.0.0  # set an explicit version
    python scripts/release.py minor --dry-run  # preview without changing anything

Run it from the repo with ``uv run python scripts/release.py`` (or a plain
``python`` that can reach git + uv).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = REPO_ROOT / "src" / "mooring" / "__init__.py"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


# -- console helpers ----------------------------------------------------------
def _color_enabled() -> bool:
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        # Best-effort: turn on VT processing so ANSI codes render in conhost.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            # ENABLE_PROCESSED_OUTPUT | WRAP_AT_EOL | VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(ctypes.c_void_p(handle), 7)
        except Exception:
            return False
    return True


_COLOR = _color_enabled()


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def fail(message: str) -> NoReturn:
    print(_paint(f"ERROR: {message}", "31"), file=sys.stderr)
    raise SystemExit(1)


# -- subprocess helpers -------------------------------------------------------
def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command at the repo root.

    capture=False streams the child's stdout/stderr to our console (for lint,
    tests, git output); capture=True buffers them for inspection.
    """
    return subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=capture)


def last_line(text: str) -> str:
    """The last non-empty, stripped line — mirrors PowerShell's `Select -Last 1`."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cut a mooring release: bump the version, commit, tag, and push.",
    )
    parser.add_argument(
        "bump",
        nargs="?",
        choices=["patch", "minor", "major"],
        default=None,
        help="Which part to bump (default: patch). Ignored when --version is given.",
    )
    parser.add_argument(
        "--version",
        metavar="X.Y.Z",
        help="Set an explicit version instead of bumping.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run preflight + show the target version; write/commit/tag/push nothing.",
    )
    args = parser.parse_args()
    if args.version is not None:
        if args.bump is not None:
            parser.error("pass either a bump (patch/minor/major) or --version, not both")
        if not SEMVER.match(args.version):
            parser.error("--version must be X.Y.Z (e.g. 1.0.0)")
    return args


def main() -> int:
    args = _parse_args()
    explicit = args.version
    bump = args.bump or "patch"
    dry_run = args.dry_run

    # -- preflight ------------------------------------------------------------
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture=True).stdout.strip()
    if branch != "master":
        fail(f"Releases are cut from master (currently on '{branch}').")

    if run(["git", "status", "--porcelain"], capture=True).stdout.strip():
        fail("Working tree is not clean; commit or stash first.")

    if run(["git", "fetch", "origin", "master", "--tags", "--quiet"]).returncode != 0:
        fail("git fetch failed.")

    behind = run(
        ["git", "rev-list", "--count", "HEAD..origin/master"], capture=True
    ).stdout.strip()
    if int(behind or "0") > 0:
        fail(f"master is {behind} commit(s) behind origin/master; pull first.")

    current = last_line(run(["uv", "version", "--short"], capture=True).stdout)

    # -- compute / apply the new version --------------------------------------
    uv_args = ["uv", "version", "--short"]
    uv_args += [explicit] if explicit else ["--bump", bump]
    if dry_run:
        uv_args.append("--dry-run")

    bumped = run(uv_args, capture=True)
    if bumped.returncode != 0:
        sys.stderr.write(bumped.stderr or "")
        fail("uv version failed.")
    new = last_line(bumped.stdout)
    if not re.match(r"^\d+\.\d+\.\d+", new):
        fail(f"Unexpected version output from uv: '{new}'.")
    if new == current:
        fail(f"Version unchanged ({new}) - nothing to release.")
    if run(["git", "tag", "--list", f"v{new}"], capture=True).stdout.strip():
        fail(f"Tag v{new} already exists.")

    if dry_run:
        print(_paint(
            f"Dry run: would release v{new} (currently {current}) - "
            "bump, commit, tag, push.",
            "33",
        ))
        return 0

    # -- sync __version__ in the package --------------------------------------
    # Read/write UTF-8 *without* a BOM: a BOM breaks marimo notebooks and the
    # repo is BOM-less. utf-8-sig on read tolerates a stray BOM; binary write
    # keeps the original (LF) line endings byte-for-byte, never adding one.
    original = INIT_PATH.read_bytes().decode("utf-8-sig")
    updated = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{new}"', original)
    if updated == original:
        fail(f"Could not find the __version__ line in {INIT_PATH}.")
    INIT_PATH.write_bytes(updated.encode("utf-8"))

    # -- checks ---------------------------------------------------------------
    if run(["uv", "run", "ruff", "check", "src", "tests"]).returncode != 0:
        fail("Lint failed; version files are modified but not committed.")
    if run(["uv", "run", "pytest", "-q"]).returncode != 0:
        fail("Tests failed; version files are modified but not committed.")

    # -- commit, tag, push ----------------------------------------------------
    run(["git", "add", "pyproject.toml", "uv.lock", "src/mooring/__init__.py"])
    if run(["git", "commit", "-m", f"release: v{new}"]).returncode != 0:
        fail("git commit failed.")
    if run(["git", "tag", "-a", f"v{new}", "-m", f"mooring v{new}"]).returncode != 0:
        fail("git tag failed.")
    if run(["git", "push", "origin", "master", f"v{new}"]).returncode != 0:
        fail(f"Push failed; local commit and tag v{new} exist - fix and push manually.")

    print()
    print(_paint(f"Released v{new} ({current} -> {new}).", "32"))
    print("CI will build artifacts, create the GitHub Release, and publish to PyPI:")
    print("  https://github.com/OpenAfterHours/mooring/actions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
