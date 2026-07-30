"""Install the Excel-delivery runtime and read back what a run delivered.

mooring injects a stdlib-only helper module (``mooring_deliver``, the source in
:mod:`mooring._workbook_runtime`) into ``<workspace>/.mooring/pylib/`` and puts that
directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can name the tables a
stakeholder should get::

    import mooring_deliver as md
    md.reset()
    md.table(summary, "Summary")

**Deliver as Excel** (:func:`mooring.app.deliver.deliver_excel`) then runs the
notebook and one ``.xlsx`` lands in ``.mooring/outbox/`` beside the HTML delivery.
Unlike the checks / inputs runtimes this one writes real data VALUES — that is the
product — so the sync exclusion of ``.mooring`` is the whole safety story, and the
runtime re-checks the target against that directory before writing.

mooring never opens the workbook itself: it has no Excel reader and will not grow
one (see the no-base-dependency note in the runtime). It learns what happened from
the value-free receipt under ``.mooring/workbooks/`` — the sheet NAMES, the
workbook's path, and a mooring-authored failure reason.

The mooring-side module is ``workbook`` while the notebook-side name is
``mooring_deliver``: the analyst's import should match the Deliver action they
clicked, and a top-level ``mooring.deliver`` would collide with the existing
:mod:`mooring.app.deliver`.

Lean-core leaf: imports only :mod:`mooring.paths` and the standard library, so it
carries no path to marimo / the Copilot SDK / spaCy (locked by the
``frozen-core-is-lean`` import contract). Mirrors :mod:`mooring.checks`.
"""

from __future__ import annotations

import json
from pathlib import Path

from mooring.paths import safe_write_bytes

STATE_DIR = ".mooring"
PYLIB_DIRNAME = "pylib"
WORKBOOKS_DIRNAME = "workbooks"

# The environment mooring's own run hands the kernel: where to put the workbook, and
# the provenance facts only mooring knows (the manifest lives on this side). Absent
# for an interactive run, and then the runtime claims no origin at all. Kept here
# beside the installer so both halves of the contract are read in one place — the
# runtime is standalone and cannot import these names.
ENV_TARGET = "MOORING_DELIVER_XLSX"
ENV_ORIGIN = "MOORING_DELIVER_ORIGIN"
ENV_LINK = "MOORING_DELIVER_LINK"
ENV_NOTEBOOK = "MOORING_DELIVER_NOTEBOOK"
ENV_DAY = "MOORING_DELIVER_DAY"

# The packaged payload (this file's sibling) and the importable name it is written
# out as in the notebook kernel.
_RUNTIME_SRC = "_workbook_runtime.py"
_MODULE_NAME = "mooring_deliver.py"


def pylib_dir(workspace: Path | str) -> Path:
    """The directory added to the notebook kernel's import path, holding the injected
    ``mooring_deliver`` module (shared with ``mooring_checks``)."""
    return Path(workspace) / STATE_DIR / PYLIB_DIRNAME


def workbooks_dir(workspace: Path | str) -> Path:
    """The sync-excluded dir holding the per-notebook delivery receipts (and the
    throwaway HTML the run renders into and deletes)."""
    return Path(workspace) / STATE_DIR / WORKBOOKS_DIRNAME


def slug(rel_posix: str) -> str:
    """A filesystem-safe, INJECTIVE receipt name: escape ``_`` first, THEN map ``/``
    to ``__``, so two different paths (``a_b.py`` vs ``a/b.py``) can never collide on
    one receipt. The same scheme the runtime uses on the kernel side."""
    return rel_posix.replace("_", "_u").replace("/", "__")


def render_target(workspace: Path | str, rel_posix: str) -> Path:
    """The throwaway ``.html`` path an Excel delivery renders into before deleting it.

    marimo executes a notebook only as a side effect of exporting it, so a run always
    produces an HTML render; for Excel delivery that render is pure waste AND embeds
    the values a second time, so it goes to this sync-excluded scratch path and
    :mod:`mooring.app.notebook_run` deletes it on every path."""
    return workbooks_dir(workspace) / f"{slug(rel_posix)}.html"


def _payload_source() -> bytes:
    return Path(__file__).with_name(_RUNTIME_SRC).read_bytes()


def install_runtime(workspace: Path | str) -> None:
    """Write the payload to ``<ws>/.mooring/pylib/mooring_deliver.py``.

    Best-effort and idempotent (only rewrites when the bytes differ, so it is cheap to
    call on every editor start) and never raises — a failure just means
    ``import mooring_deliver`` is unavailable, which surfaces as a clear ImportError in
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


def read_receipt(workspace: Path | str, rel_posix: str) -> dict:
    """``{workbook, sheets, reason, updated}`` for one notebook's last delivery.

    Empty when there is no receipt, so a caller can treat "nothing recorded" and
    "recorded nothing" the same way. Best-effort: an unreadable / corrupt / foreign
    receipt reads as empty rather than raising, and a receipt for a different notebook
    is ignored (the slug could only collide via a hand-edited file)."""
    path = workbooks_dir(workspace) / f"{slug(rel_posix)}.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("notebook") != rel_posix:
        return {}
    sheets = data.get("sheets")
    return {
        "workbook": data.get("workbook") if isinstance(data.get("workbook"), str) else "",
        "sheets": [s for s in sheets if isinstance(s, str)] if isinstance(sheets, list) else [],
        "reason": data.get("reason") if isinstance(data.get("reason"), str) else "",
        "updated": data.get("updated") if isinstance(data.get("updated"), str) else "",
    }


def clear_receipt(workspace: Path | str, rel_posix: str) -> None:
    """Remove one notebook's delivery receipt. Called BEFORE a run so what is read
    afterwards can only describe that run — a leftover receipt would otherwise report
    yesterday's sheets for a run that wrote none. Best-effort; never raises."""
    try:
        (workbooks_dir(workspace) / f"{slug(rel_posix)}.json").unlink()
    except OSError:
        pass


def copilot_guide() -> str:
    """A short, value-free capability note for the AI system context, so the copilot
    can AUTHOR the ``md.table(...)`` cell on request. It reads no workbook, no receipt
    and no data value — it only tells the model the ``mooring_deliver`` API exists and
    how to call it, so it can propose a delivery cell from the schema it already sees."""
    return (
        "EXCEL DELIVERY (value-free): the notebook can `import mooring_deliver as md` to name "
        'the result tables a stakeholder should get — md.table(summary, "Summary"), one sheet '
        "per call, replacing any sheet of the same name. When asked to export / hand over / "
        "send something to Excel or to a stakeholder, propose ONE cell that begins with "
        "md.reset() and then calls md.table() on each finished result frame, with a short "
        "sheet name for each. Put the headline summary first — it is the sheet the reader "
        "opens on. Pick the frames and names from the notebook source you can see, and never "
        "request data values. mooring's Deliver as Excel action then runs the notebook and "
        "writes one .xlsx into the local, never-synced outbox."
    )
