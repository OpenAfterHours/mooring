"""Install and read the value-free tie-out checks runtime.

mooring injects a stdlib-only helper module (``mooring_checks``, the source in
:mod:`mooring._checks_runtime`) into ``<workspace>/.mooring/pylib/`` and puts that
directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can
``import mooring_checks`` and assert its numbers tie out. Each check writes a
VALUE-FREE receipt (``{name, kind, passed, note, ts}`` — counts and booleans, never
a data value) under ``.mooring/checks/``, which the hub reads to badge a notebook
green/red.

Everything here is value-free and stays in the sync-excluded ``.mooring`` dir: the
receipts never ride a push and are never handed to the AI copilot. This module is a
lean-core leaf — it imports only :mod:`mooring.paths` and the standard library, so
it carries no path to marimo / the Copilot SDK / spaCy.
"""

from __future__ import annotations

import json
from pathlib import Path

from mooring.paths import safe_write_bytes

STATE_DIR = ".mooring"
PYLIB_DIRNAME = "pylib"
CHECKS_DIRNAME = "checks"

# The packaged payload (this file's sibling) and the importable name it is written
# out as in the notebook kernel.
_RUNTIME_SRC = "_checks_runtime.py"
_MODULE_NAME = "mooring_checks.py"


def pylib_dir(workspace: Path | str) -> Path:
    """The directory added to the notebook kernel's import path, holding the
    injected ``mooring_checks`` module."""
    return Path(workspace) / STATE_DIR / PYLIB_DIRNAME


def checks_dir(workspace: Path | str) -> Path:
    return Path(workspace) / STATE_DIR / CHECKS_DIRNAME


def _payload_source() -> bytes:
    return Path(__file__).with_name(_RUNTIME_SRC).read_bytes()


def install_runtime(workspace: Path | str) -> None:
    """Write the checks payload to ``<ws>/.mooring/pylib/mooring_checks.py``.

    Best-effort and idempotent: only rewrites when the bytes differ (so it is cheap
    to call on every editor start), and never raises — a failure just means
    ``import mooring_checks`` is unavailable in the kernel, which surfaces as a
    clear ImportError in the analyst's cell rather than a broken editor."""
    try:
        src = _payload_source()
    except OSError:
        return
    target = pylib_dir(workspace) / _MODULE_NAME
    try:
        if target.is_file() and target.read_bytes() == src:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_write_bytes(target, src)
    except OSError:
        pass


def read_results(workspace: Path | str) -> dict[str, dict]:
    """Map notebook rel-path -> ``{total, passed, failed, updated}`` from the
    ``.mooring/checks/*.json`` receipts.

    Value-free (check NAMES + counts only, never a note is surfaced here).
    Best-effort: unreadable / foreign / corrupt files are skipped; a receipt whose
    notebook no longer exists on disk is dropped (a stale badge for a deleted file
    is worse than none); a malformed (non-dict) check entry is NOT counted as
    passing — it is ignored, so it can never give false assurance."""
    out: dict[str, dict] = {}
    ws = Path(workspace)
    directory = checks_dir(workspace)
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        rel = data.get("notebook")
        checks = data.get("checks")
        if not isinstance(rel, str) or not isinstance(checks, dict):
            continue
        if rel != "_notebook" and not (ws / rel).is_file():
            continue  # the notebook was deleted — don't badge a file that's gone
        entries = [e for e in checks.values() if isinstance(e, dict)]
        total = len(entries)
        if total == 0:
            continue  # nothing well-formed to report
        failed = sum(1 for entry in entries if not entry.get("passed"))
        out[rel] = {
            "total": total,
            "failed": failed,
            "passed": total - failed,
            "updated": data.get("updated", ""),
        }
    return out


def clear(workspace: Path | str, rel: str | None = None) -> int:
    """Delete recorded check receipts — all of them, or just ``rel``'s. Returns the
    number removed. Lets an analyst reset a stale badge after removing a checks cell
    (the notebook stopped running its checks, so nothing re-writes the receipt).
    Best-effort; never raises."""
    directory = checks_dir(workspace)
    removed = 0
    try:
        files = list(directory.glob("*.json"))
    except OSError:
        return 0
    for path in files:
        if rel is not None:
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if not (isinstance(data, dict) and data.get("notebook") == rel):
                continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def copilot_guide() -> str:
    """A short, value-free capability note for the AI system context, so the copilot
    can AUTHOR tie-out checks on request. It reads no receipt and no data value — it
    only tells the model the ``mooring_checks`` API exists and how to call it, so it
    can propose a checks cell from the schema it already sees. Deliberately terse: the
    full template rides the on-demand ``/checks`` command, so this stays cheap on
    every chat's context."""
    return (
        "DATA-QUALITY CHECKS (value-free): the notebook can `import mooring_checks as mc` to "
        'assert tie-outs — mc.reconciles(a, b, tol=), mc.unique_key(df, "id"), '
        'mc.no_fanout(left, right, on="k"), mc.row_delta(df, prior), mc.not_null(df, ...), '
        "mc.expect(cond, name=). When asked to check / validate / reconcile / tie out, propose a "
        "checks cell (begin it with mc.reset()); pick columns and keys from the schema, and "
        "never request data values. Each call records only a pass/fail receipt, never a value."
    )
