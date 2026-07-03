"""Install and read the value-free input-data fingerprints.

mooring injects a stdlib-only helper module (``mooring_inputs``, the source in
:mod:`mooring._inputs_runtime`) into ``<workspace>/.mooring/pylib/`` and puts that
directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can
``import mooring_inputs`` and pin the exact inputs a run read::

    mi.fingerprint(sales_df, "sales", path="data/sales.csv")

Each call records a VALUE-FREE fingerprint — the input file's content HASH, its SHAPE
(row/column counts), and its SCHEMA (column names + dtypes) — under ``.mooring/inputs/``,
and flags the input when it differs from the previous run. This extends mooring's
three-way-SHA reproducibility story to the INPUT axis (code and environment are already
pinned): it answers the auditor's "same inputs, same numbers?" without ever seeing a
data value.

Everything here is value-free and stays in the sync-excluded ``.mooring`` dir: the
receipts never ride a push and are never handed to the AI copilot. Lean-core leaf — it
imports only :mod:`mooring.paths` and the standard library, so it carries no path to
marimo / the Copilot SDK / spaCy. Mirrors :mod:`mooring.checks`.
"""

from __future__ import annotations

import json
from pathlib import Path

from mooring.paths import safe_write_bytes

STATE_DIR = ".mooring"
PYLIB_DIRNAME = "pylib"
INPUTS_DIRNAME = "inputs"

# The packaged payload (this file's sibling) and the importable name it is written out
# as in the notebook kernel.
_RUNTIME_SRC = "_inputs_runtime.py"
_MODULE_NAME = "mooring_inputs.py"


def pylib_dir(workspace: Path | str) -> Path:
    """The directory added to the notebook kernel's import path, holding the injected
    ``mooring_inputs`` module (shared with ``mooring_checks``)."""
    return Path(workspace) / STATE_DIR / PYLIB_DIRNAME


def inputs_dir(workspace: Path | str) -> Path:
    return Path(workspace) / STATE_DIR / INPUTS_DIRNAME


def _payload_source() -> bytes:
    return Path(__file__).with_name(_RUNTIME_SRC).read_bytes()


def install_runtime(workspace: Path | str) -> None:
    """Write the fingerprint payload to ``<ws>/.mooring/pylib/mooring_inputs.py``.

    Best-effort and idempotent (only rewrites when the bytes differ, so it is cheap to
    call on every editor start) and never raises — a failure just means
    ``import mooring_inputs`` is unavailable, which surfaces as a clear ImportError in
    the analyst's cell rather than a broken editor."""
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
    """Map notebook rel-path -> ``{total, changed, updated}`` from the
    ``.mooring/inputs/*.json`` receipts.

    Value-free (input NAMES + counts only; the per-input hash/shape/schema stays in the
    receipt). ``total`` = inputs fingerprinted; ``changed`` = how many differ from the
    previous run. Best-effort: unreadable / foreign / corrupt files are skipped; a
    receipt whose notebook no longer exists on disk is dropped (a stale badge for a
    deleted file is worse than none); a malformed (non-dict) input entry is ignored."""
    out: dict[str, dict] = {}
    ws = Path(workspace)
    directory = inputs_dir(workspace)
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
        inputs = data.get("inputs")
        if not isinstance(rel, str) or not isinstance(inputs, dict):
            continue
        if rel != "_notebook" and not (ws / rel).is_file():
            continue  # the notebook was deleted — don't badge a file that's gone
        entries = [e for e in inputs.values() if isinstance(e, dict)]
        total = len(entries)
        if total == 0:
            continue  # nothing well-formed to report
        changed = sum(1 for entry in entries if entry.get("changed"))
        out[rel] = {
            "total": total,
            "changed": changed,
            "updated": data.get("updated", ""),
        }
    return out


def clear(workspace: Path | str, rel: str | None = None) -> int:
    """Delete recorded fingerprint receipts — all of them, or just ``rel``'s. Returns
    the number removed. Best-effort; never raises."""
    directory = inputs_dir(workspace)
    removed = 0
    try:
        files = list(directory.glob("*.json"))
    except OSError:
        return 0
    want = rel.replace("\\", "/") if rel is not None else None
    for path in files:
        if want is not None:
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if not (isinstance(data, dict) and data.get("notebook") == want):
                continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def copilot_guide() -> str:
    """A short, value-free capability note for the AI system context, so the copilot can
    AUTHOR input fingerprints on request. It reads no receipt and no data value — it only
    tells the model the ``mooring_inputs`` API exists and how to call it, so it can
    propose a fingerprint cell from the schema it already sees."""
    return (
        "INPUT FINGERPRINTS (value-free): the notebook can `import mooring_inputs as mi` to "
        'pin its inputs for reproducibility — mi.fingerprint(df, "name", path="data/x.csv") '
        "records the file's content hash + shape + column schema (never a value) and flags the "
        "input if it changed since the last run. When asked to pin / fingerprint / track inputs "
        "or check reproducibility, propose ONE cell that begins with `mi.reset()` (so a removed "
        "input does not linger) and then fingerprints each input dataframe right after it is "
        "loaded. ALWAYS pass path= to the source file — that is what gives the content guarantee; "
        "without it only shape+schema are compared. Pick the name and path from the source, and "
        "never request data values. Each call records only a value-free receipt."
    )
