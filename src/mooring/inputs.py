"""Install and read the value-free input/output data fingerprints.

mooring injects a stdlib-only helper module (``mooring_inputs``, the source in
:mod:`mooring._inputs_runtime`) into ``<workspace>/.mooring/pylib/`` and puts that
directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can
``import mooring_inputs`` and pin exactly what a run read and wrote::

    mi.fingerprint(sales_df, "sales", path="data/sales.csv")   # read
    mi.output(monthly_df, "monthly", path="data/monthly.csv")  # written

Each call records a VALUE-FREE fingerprint — the file's content HASH, its SHAPE
(row/column counts), and its SCHEMA (column names + dtypes) — under ``.mooring/inputs/``,
and flags it when it differs from the previous run. This extends mooring's
three-way-SHA reproducibility story to the DATA axis (code and environment are already
pinned): it answers the auditor's "same inputs, same numbers?" without ever seeing a
data value. Because both sides are recorded, the same receipts also carry the workspace's
lineage — see :mod:`mooring.lineage`, which joins one notebook's output path to another's
input path.

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

# The receipt's two sections. Receipts written before outputs existed have only INPUTS_KEY,
# so a missing OUTPUTS_KEY means "none recorded", never "malformed" — the format only grows.
INPUTS_KEY = "inputs"
OUTPUTS_KEY = "outputs"

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


def read_receipts(workspace: Path | str) -> list[dict]:
    """Every usable receipt under ``.mooring/inputs/``, normalised to
    ``{"notebook": rel, "inputs": [entries], "outputs": [entries], "updated": str}``.

    The single floor under :func:`read_results` and :mod:`mooring.lineage`, so the badge
    and the lineage graph can never disagree about which receipts count. Best-effort:
    unreadable / foreign / corrupt files are skipped; a receipt whose notebook no longer
    exists on disk is DROPPED (a lineage edge or a badge for a deleted notebook is worse
    than none); a malformed (non-dict) entry is ignored; a missing section reads as
    empty, which is how a receipt written before outputs existed still reads cleanly."""
    out: list[dict] = []
    ws = Path(workspace)
    try:
        files = sorted(inputs_dir(workspace).glob("*.json"))
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
        if not isinstance(rel, str) or not rel:
            continue
        if rel != "_notebook" and not (ws / rel).is_file():
            continue  # the notebook was deleted — don't badge (or route) a file that's gone
        sections = {}
        for key in (INPUTS_KEY, OUTPUTS_KEY):
            bucket = data.get(key)
            sections[key] = (
                [e for e in bucket.values() if isinstance(e, dict)]
                if isinstance(bucket, dict)
                else []
            )
        if not sections[INPUTS_KEY] and not sections[OUTPUTS_KEY]:
            continue  # nothing well-formed to report
        out.append({"notebook": rel, "updated": data.get("updated", ""), **sections})
    return out


def summarize(receipts: list[dict]) -> dict[str, dict]:
    """Map notebook rel-path -> ``{total, changed, outputs, outputs_changed, updated}``.

    Value-free (counts only; the per-entry hash/shape/schema stays in the receipt).
    ``total``/``changed`` are the inputs fingerprinted and how many differ from the
    previous run; ``outputs``/``outputs_changed`` are the same for what the notebook
    wrote. The output counts are additive — a receipt with no outputs reports zero, so
    an older receipt renders exactly as it did before.

    Takes receipts rather than a workspace so a caller that ALSO builds the lineage graph
    from them (the hub, on every /api/state poll) reads the directory once."""
    out: dict[str, dict] = {}
    for receipt in receipts:
        entries = receipt[INPUTS_KEY]
        produced = receipt[OUTPUTS_KEY]
        out[receipt["notebook"]] = {
            "total": len(entries),
            "changed": sum(1 for entry in entries if entry.get("changed")),
            "outputs": len(produced),
            "outputs_changed": sum(1 for entry in produced if entry.get("changed")),
            "updated": receipt["updated"],
        }
    return out


def read_results(workspace: Path | str) -> dict[str, dict]:
    """:func:`summarize` over every receipt in ``workspace`` — the one-shot form."""
    return summarize(read_receipts(workspace))


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
        "INPUT/OUTPUT FINGERPRINTS (value-free): the notebook can `import mooring_inputs as mi` to "
        'pin what it reads and writes — mi.fingerprint(df, "name", path="data/x.csv") for an '
        'INPUT and mi.output(df, "name", path="data/y.csv") for a file the notebook WRITES. Both '
        "record the file's content hash + shape + column schema (never a value) and flag it if it "
        "changed since the last run. When asked to pin / fingerprint / track inputs or outputs, or "
        "to check reproducibility, propose ONE cell that begins with `mi.reset()` (so a removed "
        "entry does not linger) and then fingerprints each input dataframe right after it is "
        "loaded; put each mi.output(...) call AFTER the write that produces the file. ALWAYS pass "
        "path= — that is what gives the content guarantee (without it only shape+schema are "
        "compared) and, for an output, it is what lets mooring link this notebook to the one "
        "downstream that reads the same file. Pick the name and path from the source, and never "
        "request data values. Each call records only a value-free receipt."
    )
