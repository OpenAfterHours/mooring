"""Deliver a notebook as a self-contained stakeholder artifact.

An analyst's real output is a number + a chart a manager consumes — not a ``.py``.
:func:`deliver_html` renders a notebook to a self-contained HTML file (code hidden)
that can be double-clicked or emailed, writes it to the workspace's SYNC-EXCLUDED
``.mooring/outbox/``, and stamps a value-free provenance footer (which repo /
commit / notebook / date it came from, plus a View-on-GitHub link).

:func:`deliver_excel` is the same last mile for the large part of a finance audience
that lives in Excel: it runs the notebook and collects the tables the notebook named
via the injected ``mooring_deliver`` helper (:mod:`mooring.workbook`) into one
``.xlsx`` in the same outbox, with the same provenance on its own sheet.

Keeping data OUT of the repo is structural, not a promise: ``.mooring/`` is excluded
by :func:`mooring.sync.is_synced_path` on BOTH the local scan and the remote tree,
so a delivered artifact — which embeds real data values — can never ride a push or
be adopted as a synced folder. Delivery runs LOCALLY (the notebook executes in the
team's locked env via ``marimo export html``); the values never leave the machine,
and no channel here reaches the AI copilot.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path

from mooring import activity, editor, manifest, paths, workbook
from mooring.app import notebook_run, notebooks
from mooring.config import Config
from mooring.github import blob_url

OUTBOX_DIRNAME = "outbox"
# marimo EXECUTES the notebook to capture outputs, so bound the wait generously.
_EXPORT_TIMEOUT = 300

_BODY_CLOSE = re.compile(r"</body\s*>", re.IGNORECASE)


class DeliverError(Exception):
    """Rendering the notebook failed; ``str(exc)`` is the user-facing reason."""


@dataclass
class DeliverResult:
    notebook_rel: str
    out_path: Path  # absolute path to the delivered artifact
    out_rel: str  # workspace-relative POSIX path, for display / reveal
    commit: str  # short head commit the artifact is stamped with, or "" when unsynced
    sheets: tuple[str, ...] = field(default_factory=tuple)  # Excel delivery only


def outbox_dir(workspace: Path) -> Path:
    return workspace / ".mooring" / OUTBOX_DIRNAME


def _slug(rel_posix: str) -> str:
    stem = rel_posix[:-3] if rel_posix.endswith(".py") else rel_posix
    return stem.replace("/", "__")


def outbox_target(workspace: Path, rel_posix: str, ext: str = ".html") -> Path:
    """Where ``rel_posix``'s delivered artifact lands: one dated file per notebook per day.

    Public so the scheduled refresh (:mod:`mooring.app.refresh`) puts its artifact exactly
    where an attended Deliver would — one naming scheme, not two. ``ext`` picks the last
    mile (``.html`` or ``.xlsx``); both sit in the same per-notebook folder so a stakeholder
    pack for one notebook stays together."""
    out_dir = outbox_dir(workspace) / _slug(rel_posix)
    return out_dir / f"{Path(rel_posix).stem}-{datetime.now():%Y%m%d}{ext}"


def _ensure_notebook(workspace: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` to a deliverable marimo notebook, or raise.

    Shared by both last miles so they refuse the same things for the same reasons: a
    plain helper module (delivering it would run something that was never a notebook)
    and a non-notebook target such as a ``.pbip`` project."""
    target = notebooks.ws_file(workspace, rel_path, suffix=".py")
    try:
        kind = notebooks.openable_kind(target, rel_path)
    except notebooks.OpenRefused as exc:
        raise DeliverError(str(exc)) from exc
    if kind != "notebook":  # e.g. a .pbip project — open it in Power BI Desktop instead
        raise DeliverError("Only marimo notebooks can be delivered.")
    return target


def deliver_html(cfg: Config, rel_path: str) -> DeliverResult:
    """Render ``rel_path`` to a self-contained HTML snapshot in the outbox.

    Raises :class:`DeliverError` for a non-notebook target or a render failure, and
    ``ValueError`` / ``FileNotFoundError`` (from :func:`notebooks.ws_file`) for a
    bad path — the adapters translate these to their transport (a hub 4xx / a CLI
    message)."""
    workspace = cfg.workspace()
    _ensure_notebook(workspace, rel_path)

    rel_posix = rel_path.replace("\\", "/")
    # Make sure the kernel import path (.mooring/pylib + workspace root) is set, so
    # the notebook's cross-folder imports and any mooring_checks calls resolve during
    # export. theme=None preserves an open editor's appearance.
    editor.ensure_runtime_config(workspace)

    out_path = outbox_target(workspace, rel_posix)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd, env = editor.export_html_command(workspace, rel_posix, out_path, include_code=False)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=_EXPORT_TIMEOUT,
        )
    except FileNotFoundError as exc:  # marimo/uv not found on PATH
        raise DeliverError(f"Could not run the renderer: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeliverError("Rendering timed out — the notebook took too long to run.") from exc
    if proc.returncode != 0 or not out_path.is_file():
        raise DeliverError(_render_error(proc))

    commit = stamp_provenance(out_path, cfg, rel_posix, workspace)
    out_rel = out_path.relative_to(workspace).as_posix()
    activity.record(workspace, "deliver", path=rel_posix, out=out_rel)
    return DeliverResult(notebook_rel=rel_posix, out_path=out_path, out_rel=out_rel, commit=commit)


def deliver_excel(cfg: Config, rel_path: str) -> DeliverResult:
    """Run ``rel_path`` and collect the tables it named into one ``.xlsx`` in the outbox.

    The workbook is written by the NOTEBOOK, not by mooring: the injected
    ``mooring_deliver`` helper (:mod:`mooring.workbook`) writes it from the kernel using
    whichever Excel engine the repo's own environment has. mooring supplies only the
    target path, reads back the value-free receipt to learn what the run did, and then
    stamps the provenance sheet itself. It never READS the workbook: it has no Excel
    reader, and the whole point of the design is that it does not need one.

    Four things have to hold before this hands anything over, because every one of them
    is a way to ship a wrong number in a board pack:

    1. the run completed cleanly — a failed cell means the numbers may be wrong;
    2. the receipt records no failed table — a workbook quietly missing a sheet looks
       exactly like a complete one once forwarded;
    3. the receipt says THIS run wrote the file at THIS path — a file merely existing
       there is the previous delivery, which is what an ``os.replace`` blocked by the
       analyst's own open Excel window leaves behind;
    4. mooring's provenance replaced the notebook's placeholder.

    Raises :class:`DeliverError` when any of them fails, and ``ValueError`` /
    ``FileNotFoundError`` (from :func:`notebooks.ws_file`) for a bad path."""
    workspace = cfg.workspace()
    _ensure_notebook(workspace, rel_path)
    rel_posix = rel_path.replace("\\", "/")

    out_path = outbox_target(workspace, rel_posix, ext=".xlsx")
    # Clear both sides BEFORE the run, so what is found afterwards can only be this
    # run's. Otherwise a run that writes nothing would happily "deliver" yesterday's
    # workbook — the same staleness trap the runtime's reset() guards against.
    had_previous = _clear(out_path)
    workbook.clear_receipt(workspace, rel_posix)

    # The kernel is told WHERE to write and WHICH notebook it is, and nothing else. The
    # provenance facts deliberately do not travel this way: a cell can rewrite
    # os.environ, and then the notebook would be authoring its own vouching record.
    env_extra = {workbook.ENV_TARGET: str(out_path), workbook.ENV_NOTEBOOK: rel_posix}
    render = workbook.render_target(workspace, rel_posix)
    try:
        outcome = notebook_run.run(
            workspace, rel_posix, render, keep_on_success=False, env_extra=env_extra
        )
    except notebook_run.RunError as exc:
        # A run that stopped early may have left the sheets it got to. A delivery mooring
        # REFUSED must leave nothing deliverable behind — a half-populated workbook in the
        # outbox is indistinguishable from a good one to whoever forwards it.
        _clear(out_path)
        raise DeliverError(str(exc)) from exc
    if not outcome.ok:
        _clear(out_path)
        raise DeliverError(_failed_run(outcome))

    receipt = workbook.read_receipt(workspace, rel_posix)
    failures = receipt.get("failures") or []
    if failures:
        _clear(out_path)
        raise DeliverError(_partial(failures))
    if not _run_wrote(receipt, workspace, out_path):
        raise DeliverError(_no_workbook(receipt, had_previous))

    origin, link, short = provenance(cfg, rel_posix, workspace)
    rows = _provenance_rows(origin, link, rel_posix, receipt)
    try:
        workbook.stamp_provenance(out_path, rows)
    except workbook.StampError as exc:
        # Without mooring's stamp the sheet still holds whatever the notebook put there.
        # Shipping an unverified claim is worse than shipping nothing.
        _clear(out_path)
        raise DeliverError(f"The workbook's provenance could not be recorded: {exc}") from exc

    out_rel = out_path.relative_to(workspace).as_posix()
    activity.record(workspace, "deliver", path=rel_posix, out=out_rel)
    return DeliverResult(
        notebook_rel=rel_posix,
        out_path=out_path,
        out_rel=out_rel,
        commit=short,
        sheets=tuple(receipt.get("sheets") or ()),
    )


def _run_wrote(receipt: dict, workspace: Path, out_path: Path) -> bool:
    """Whether the run itself wrote the workbook now sitting at ``out_path``.

    "A file is there" is NOT the same claim and was the difference between delivering
    today's numbers and yesterday's: when the analyst has the previous workbook open,
    the kernel's final ``os.replace`` fails with a sharing violation and the old file
    survives, with the same name and the same date stamp. The receipt is written only
    on a completed write, and it names the path — so both are checked."""
    written = (receipt.get("workbook") or "").strip()
    if not written or not out_path.is_file():
        return False
    try:
        return (workspace / written).resolve() == out_path.resolve()
    except OSError:
        return False


def _provenance_rows(origin: str, link: str, rel_posix: str, receipt: dict) -> list[tuple[str, str]]:
    """The rows mooring stamps onto the Provenance sheet — the workbook's counterpart to
    the HTML footer, built from the same :func:`provenance` call, so a never-pushed
    notebook gets no commit and no link here either."""
    rows = [
        ("Generated by", "mooring"),
        ("Source", origin),
        ("Notebook", rel_posix),
        ("Date", f"{datetime.now():%Y-%m-%d}"),
    ]
    if link:
        rows.append(("View on GitHub", link))
    sheets = receipt.get("sheets") or []
    if sheets:
        rows.append(("Sheets", ", ".join(sheets)))
    if receipt.get("utc_normalised"):
        # A timestamp column whose zone changed is undiscoverable by the reader, so the
        # workbook says so itself rather than leaving it to the docs.
        rows.append(("Timestamps", "UTC (timezone-aware values were normalised)"))
    return rows


def _clear(out_path: Path) -> bool:
    """Remove a previous workbook at ``out_path``; True if one was there."""
    try:
        out_path.unlink()
        return True
    except OSError:
        return False


def _partial(failures: list[dict]) -> str:
    """Why a delivery with a lost table is refused outright. Names the sheets (the
    analyst's own labels) and the recorded reason, both mooring-authored strings."""
    named = [f["sheet"] for f in failures if f.get("sheet")]
    which = ", ".join(named) if named else f"{len(failures)} table(s)"
    reason = next((f["reason"] for f in failures if f.get("reason")), "")
    tail = f" ({reason})" if reason else ""
    return (
        f"The workbook was not delivered: {which} could not be written{tail}. "
        "A workbook missing a sheet looks complete once it is forwarded, so mooring "
        "delivers all of it or none of it."
    )


def _failed_run(outcome: notebook_run.RunOutcome) -> str:
    """Why an Excel delivery was refused after the notebook ran. Built from the
    value-free failed-cell COUNT only — the stderr text can quote a data value."""
    if outcome.cells_failed:
        cells = "cell" if outcome.cells_failed == 1 else "cells"
        return (
            f"{outcome.cells_failed} {cells} failed to run, so the workbook was not "
            "delivered — open the notebook to see which."
        )
    return "The notebook failed to run, so the workbook was not delivered."


def _no_workbook(receipt: dict, had_previous: bool) -> str:
    """The notebook ran clean but no workbook appeared. The reason recorded by the
    runtime (a fixed, mooring-authored string — most often "no Excel writer") is far
    more useful than anything inferable here, so it leads when present."""
    reason = (receipt.get("reason") or "").strip()
    if reason:
        return f"The workbook was not written: {reason}"
    stale = " The previous workbook was removed." if had_previous else ""
    return (
        "The notebook ran, but it named no tables to deliver. Add a cell with "
        '`import mooring_deliver as md` then `md.reset()` and `md.table(df, "Summary")` '
        "for each result you want in the workbook." + stale
    )


def _render_error(proc: subprocess.CompletedProcess) -> str:
    """A short, local-only reason from a failed export. Never recorded to telemetry
    or the activity ledger (marimo's stderr can quote a value); shown only to the
    analyst on their own machine."""
    tail = ""
    for line in reversed((proc.stderr or "").splitlines()):
        if line.strip():
            tail = line.strip()
            break
    base = "The notebook could not be rendered — it may have failed to run."
    return f"{base} ({tail})" if tail else base


def provenance(cfg: Config, rel_posix: str, workspace: Path) -> tuple[str, str, str]:
    """``(origin, link, short_commit)`` — where a delivered artifact came from.

    The ONE place the "don't claim what we can't stand behind" rule lives, shared by
    every last mile (the HTML footer and the workbook's Provenance sheet). The
    ``@<commit>`` origin and the "View on GitHub" link appear ONLY when the notebook is
    actually tracked on the remote (present in the manifest): a never-pushed notebook
    would otherwise get a 404 link and a false "at this commit" claim, and blob_url's
    precondition is that the file exists remotely."""
    mft = manifest.load(workspace)
    head = (mft.head_commit or "").strip()
    short = head[:7] if head else ""
    synced = mft.files.get(rel_posix) is not None  # tracked == present on the remote branch
    if not cfg.is_configured:
        return "a local workspace", "", short
    if synced:
        origin = f"{cfg.owner}/{cfg.repo}" + (f"@{short}" if short else "")
        return origin, blob_url(cfg.owner, cfg.repo, cfg.branch, rel_posix, host=cfg.host), short
    return f"{cfg.owner}/{cfg.repo} (this notebook is not yet pushed)", "", short


def stamp_provenance(
    out_path: Path, cfg: Config, rel_posix: str, workspace: Path, *, freshness: str = ""
) -> str:
    """Append a value-free provenance footer to the rendered HTML. Returns the short
    head commit (or "").

    The "View on GitHub" link and the ``@<commit>`` origin are stamped ONLY when the
    notebook is actually tracked on the remote (present in the manifest) — a
    never-pushed notebook would otherwise get a 404 link and a false "at this commit"
    claim (blob_url's precondition is that the file exists remotely).

    ``freshness`` (see :func:`mooring.schedule.freshness_note`) adds the scheduled
    cadence and the next-due date for an artifact produced by a scheduled refresh. That
    clause is what makes staleness travel WITH the output: a stakeholder holding the
    emailed HTML weeks later can see it is overdue without access to mooring, the repo,
    or the analyst. An attended Deliver passes "" — it makes no recurrence claim."""
    origin, link, short = provenance(cfg, rel_posix, workspace)
    footer = _footer_html(origin, rel_posix, f"{datetime.now():%Y-%m-%d}", link, freshness)
    try:
        content = out_path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return short
    if _BODY_CLOSE.search(content):
        content = _BODY_CLOSE.sub(lambda m: footer + m.group(0), content, count=1)
    else:
        content += footer
    try:
        paths.safe_write_text(out_path, content)
    except OSError:
        pass
    return short


def _footer_html(origin: str, rel_posix: str, day: str, link: str, freshness: str = "") -> str:
    text = f"Generated by mooring from {escape(origin)} · notebook {escape(rel_posix)} · {day}"
    if freshness:
        text += f" · {escape(freshness)}"
    if link:
        text += f' · <a href="{escape(link, quote=True)}" style="color:inherit">View on GitHub</a>'
    return (
        '<footer style="margin:2.5rem 0 1rem;padding:0.75rem 1rem;border-top:1px solid #8884;'
        'font:12px/1.5 system-ui,sans-serif;color:#8a8a8a;text-align:center">'
        f"{text}</footer>"
    )
