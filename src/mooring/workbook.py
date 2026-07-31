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

mooring never READS the workbook: it has no Excel reader and will not grow one (see
the no-base-dependency note in the runtime). It learns what happened from the
value-free receipt under ``.mooring/workbooks/`` — the sheet NAMES, the workbook's
path, and a mooring-authored failure reason.

It does, however, WRITE one part of it. :func:`stamp_provenance` replaces the
workbook's ``Provenance`` sheet after the run, by hand, with ``zipfile`` and a
few lines of XML — because the notebook is the party being vouched for and must not
author the record that vouches for it. The facts used to travel to the kernel in
environment variables, and a cell that rewrote ``os.environ`` could claim any repo
and commit it liked. The HTML delivery has always stamped its footer from mooring's
side after the render; this is the same rule, paid for in a little XML.

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
import os
import posixpath
import re
import zipfile
from collections.abc import Sequence
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from mooring.paths import safe_write_bytes

STATE_DIR = ".mooring"
PYLIB_DIRNAME = "pylib"
WORKBOOKS_DIRNAME = "workbooks"

# The sheet mooring owns. The runtime writes a placeholder under this exact name (and
# reserves it, so an analyst's own "Provenance" table is the one renamed) and
# stamp_provenance finds it by name afterwards.
PROVENANCE_SHEET = "Provenance"

# The whole environment mooring's own run hands the kernel: where to put the workbook,
# and which notebook the receipt belongs to. Deliberately NOT the provenance facts —
# see stamp_provenance. Kept here beside the installer so both halves of the contract
# are read in one place; the runtime is standalone and cannot import these names.
ENV_TARGET = "MOORING_DELIVER_XLSX"
ENV_NOTEBOOK = "MOORING_DELIVER_NOTEBOOK"

# The packaged payload (this file's sibling) and the importable name it is written
# out as in the notebook kernel.
_RUNTIME_SRC = "_workbook_runtime.py"
_MODULE_NAME = "mooring_deliver.py"

# The three OOXML namespaces needed to walk from a sheet NAME to the zip entry holding
# it: the sheet list in xl/workbook.xml carries a relationship id, which xl/_rels/
# workbook.xml.rels resolves to a part name.
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


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
    """``{workbook, sheets, failures, reason, utc_normalised, updated}`` for one
    notebook's last delivery.

    ``workbook`` is the path the run actually WROTE — empty when it wrote nothing, which
    is what lets a caller tell "this run produced the file at that path" from "a file
    happens to be there". ``failures`` lists every table that did not make it, so a
    partial workbook can be refused rather than forwarded.

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
    failures = data.get("failures")
    return {
        "workbook": data.get("workbook") if isinstance(data.get("workbook"), str) else "",
        "sheets": [s for s in sheets if isinstance(s, str)] if isinstance(sheets, list) else [],
        "failures": _failures(failures),
        "reason": data.get("reason") if isinstance(data.get("reason"), str) else "",
        "utc_normalised": bool(data.get("utc_normalised")),
        "updated": data.get("updated") if isinstance(data.get("updated"), str) else "",
    }


def _failures(raw) -> list[dict]:
    """The recorded per-table failures, normalised to ``{sheet, reason}`` strings. A
    malformed entry is kept as an unnamed failure rather than dropped: it still means a
    table did not make it, and forgetting that is how a partial workbook ships."""
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            out.append({"sheet": "", "reason": ""})
            continue
        sheet = entry.get("sheet")
        reason = entry.get("reason")
        out.append(
            {
                "sheet": sheet if isinstance(sheet, str) else "",
                "reason": reason if isinstance(reason, str) else "",
            }
        )
    return out


class StampError(Exception):
    """The provenance record could not be written. ``str(exc)`` is the reason. Fatal to
    a delivery: an unstamped workbook still carries whatever the notebook put on that
    sheet, and shipping an unverified claim is worse than shipping nothing."""


def stamp_provenance(path: Path, rows: Sequence[tuple[str, str]]) -> None:
    """Replace the workbook's ``Provenance`` sheet with mooring's own record.

    THE anti-forgery step. The notebook wrote this workbook, so anything it put on that
    sheet is a claim by the party being vouched for — in review, a cell that rewrote
    ``os.environ`` produced a sheet claiming a repo and commit that mooring had
    reported as never pushed. This overwrites the sheet afterwards, from
    :func:`mooring.app.deliver.provenance`, and it is the LAST write to the file.

    Done with ``zipfile`` and a few lines of XML rather than an Excel library, because
    mooring adds no dependency for this feature — an ``.xlsx`` is a zip of XML parts,
    and one part is replaced by name. The rows are written as inline strings, so the
    new sheet depends on nothing else in the workbook. (Entries the old sheet had in
    ``sharedStrings.xml`` are simply left unreferenced; Excel treats the counts there
    as hints and rewrites them on save.)

    Raises :class:`StampError` and leaves the workbook untouched if anything is off —
    a missing or duplicated ``Provenance`` sheet included, since either means the
    notebook has been doing something to the file that mooring did not ask for."""
    try:
        with zipfile.ZipFile(path) as book:
            infos = book.infolist()
            entries = {info.filename: book.read(info.filename) for info in infos}
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise StampError(f"the workbook could not be read back: {exc}") from exc

    part = _provenance_part(entries)
    entries[part] = _sheet_xml([("Field", "Value"), *rows])

    tmp = path.with_name(path.name + ".stamp.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            for info in infos:
                out.writestr(_fresh_entry(info), entries[info.filename])
        with zipfile.ZipFile(tmp) as check:
            if check.namelist() != [info.filename for info in infos]:
                raise StampError("the stamped workbook lost a part")
        os.replace(tmp, path)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        _unlink(tmp)
        raise StampError(f"the provenance record could not be written: {exc}") from exc
    except StampError:
        _unlink(tmp)
        raise


def _provenance_part(entries: dict[str, bytes]) -> str:
    """The zip entry holding the ``Provenance`` sheet: sheet name -> relationship id
    (``xl/workbook.xml``) -> part name (``xl/_rels/workbook.xml.rels``)."""
    try:
        book = ElementTree.fromstring(entries["xl/workbook.xml"])
        rels = ElementTree.fromstring(entries["xl/_rels/workbook.xml.rels"])
    except (KeyError, ElementTree.ParseError) as exc:
        raise StampError(f"the workbook is not a readable .xlsx: {exc}") from exc
    ids = [
        sheet.get(f"{{{_REL_NS}}}id")
        for sheet in book.iter(f"{{{_MAIN_NS}}}sheet")
        if sheet.get("name") == PROVENANCE_SHEET
    ]
    if len(ids) != 1 or not ids[0]:
        raise StampError(f"the workbook has no single {PROVENANCE_SHEET} sheet")
    for rel in rels.iter(f"{{{_PKG_REL_NS}}}Relationship"):
        if rel.get("Id") != ids[0]:
            continue
        target = (rel.get("Target") or "").replace("\\", "/")
        part = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
        if part in entries:
            return part
        break
    raise StampError(f"the {PROVENANCE_SHEET} sheet could not be located in the workbook")


def _fresh_entry(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """A new zip entry carrying only the attributes worth preserving. Re-using the
    original ``ZipInfo`` would carry its ``flag_bits`` across too — including the
    data-descriptor bit, which ``writestr`` does not clear and which would then
    describe a record we are not writing."""
    fresh = zipfile.ZipInfo(info.filename, info.date_time)
    fresh.compress_type = info.compress_type
    fresh.external_attr = info.external_attr
    fresh.internal_attr = info.internal_attr
    fresh.create_system = info.create_system
    return fresh


def _sheet_xml(rows: Sequence[tuple[str, str]]) -> bytes:
    """A minimal two-column worksheet part. Inline strings (``t="inlineStr"``) so the
    sheet is self-contained — nothing to keep in step with ``sharedStrings.xml``."""
    body = []
    for index, cells in enumerate(rows, start=1):
        line = "".join(
            f'<c r="{column}{index}" t="inlineStr"><is><t xml:space="preserve">'
            f"{escape(_CONTROL_CHARS.sub('', str(value)))}</t></is></c>"
            for column, value in zip(("A", "B"), cells)
        )
        body.append(f'<row r="{index}">{line}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_MAIN_NS}"><sheetData>{"".join(body)}</sheetData></worksheet>'
    ).encode("utf-8")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


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
