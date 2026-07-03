"""Deliver a notebook as a self-contained stakeholder artifact.

An analyst's real output is a number + a chart a manager consumes — not a ``.py``.
:func:`deliver_html` renders a notebook to a self-contained HTML file (code hidden)
that can be double-clicked or emailed, writes it to the workspace's SYNC-EXCLUDED
``.mooring/outbox/``, and stamps a value-free provenance footer (which repo /
commit / notebook / date it came from, plus a View-on-GitHub link).

Keeping data OUT of the repo is structural, not a promise: ``.mooring/`` is excluded
by :func:`mooring.sync.is_synced_path` on BOTH the local scan and the remote tree,
so a rendered artifact — which embeds real data values — can never ride a push or
be adopted as a synced folder. Rendering runs LOCALLY (the notebook executes in the
team's locked env via ``marimo export html``); the values never leave the machine,
and no channel here reaches the AI copilot.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path

from mooring import activity, editor, inputs, manifest, paths
from mooring.app import notebooks
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
    out_path: Path  # absolute path to the rendered HTML
    out_rel: str  # workspace-relative POSIX path, for display / reveal
    commit: str  # short head commit the render is stamped with, or "" when unsynced


def outbox_dir(workspace: Path) -> Path:
    return workspace / ".mooring" / OUTBOX_DIRNAME


def _slug(rel_posix: str) -> str:
    stem = rel_posix[:-3] if rel_posix.endswith(".py") else rel_posix
    return stem.replace("/", "__")


def deliver_html(cfg: Config, rel_path: str) -> DeliverResult:
    """Render ``rel_path`` to a self-contained HTML snapshot in the outbox.

    Raises :class:`DeliverError` for a non-notebook target or a render failure, and
    ``ValueError`` / ``FileNotFoundError`` (from :func:`notebooks.ws_file`) for a
    bad path — the adapters translate these to their transport (a hub 4xx / a CLI
    message)."""
    workspace = cfg.workspace()
    target = notebooks.ws_file(workspace, rel_path, suffix=".py")
    try:
        kind = notebooks.openable_kind(target, rel_path)
    except notebooks.OpenRefused as exc:
        # A plain helper module (non-notebook .py): rendering it would run a module
        # that was never a notebook. Refuse with the shared explanation.
        raise DeliverError(str(exc)) from exc
    if kind != "notebook":  # e.g. a .pbip project — open it in Power BI Desktop instead
        raise DeliverError("Only marimo notebooks can be delivered.")

    rel_posix = rel_path.replace("\\", "/")
    # Make sure the kernel import path (.mooring/pylib + workspace root) is set, so
    # the notebook's cross-folder imports and any mooring_checks calls resolve during
    # export. theme=None preserves an open editor's appearance.
    editor.ensure_runtime_config(workspace)

    out_dir = outbox_dir(workspace) / _slug(rel_posix)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{Path(rel_posix).stem}-{datetime.now():%Y%m%d}.html"

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

    commit = _stamp_provenance(out_path, cfg, rel_posix, workspace)
    out_rel = out_path.relative_to(workspace).as_posix()
    activity.record(workspace, "deliver", path=rel_posix, out=out_rel)
    return DeliverResult(notebook_rel=rel_posix, out_path=out_path, out_rel=out_rel, commit=commit)


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


def _stamp_provenance(out_path: Path, cfg: Config, rel_posix: str, workspace: Path) -> str:
    """Append a value-free provenance footer to the rendered HTML. Returns the short
    head commit (or "").

    The "View on GitHub" link and the ``@<commit>`` origin are stamped ONLY when the
    notebook is actually tracked on the remote (present in the manifest) — a
    never-pushed notebook would otherwise get a 404 link and a false "at this commit"
    claim (blob_url's precondition is that the file exists remotely)."""
    mft = manifest.load(workspace)
    head = (mft.head_commit or "").strip()
    short = head[:7] if head else ""
    synced = mft.files.get(rel_posix) is not None  # tracked == present on the remote branch
    link = ""
    if not cfg.is_configured:
        origin = "a local workspace"
    elif synced:
        origin = f"{cfg.owner}/{cfg.repo}" + (f"@{short}" if short else "")
        link = blob_url(cfg.owner, cfg.repo, cfg.branch, rel_posix, host=cfg.host)
    else:
        origin = f"{cfg.owner}/{cfg.repo} (this notebook is not yet pushed)"
    # Value-free input fingerprints (if the notebook recorded any via mooring_inputs when
    # it just ran to render): the exact inputs behind these numbers — filename, content
    # hash, and shape — so a reader can answer "same inputs, same numbers?". No values.
    inputs_text = _inputs_summary(inputs.fingerprints(workspace, rel_posix))
    footer = _footer_html(origin, rel_posix, f"{datetime.now():%Y-%m-%d}", link, inputs_text)
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


def _inputs_summary(fps: list[dict]) -> str:
    """A value-free one-line summary of the fingerprinted inputs for the footer — each
    ``name (sha7, rows×cols)``, counts and a hash only. Empty when nothing was pinned."""
    parts: list[str] = []
    for fp in fps:
        label = fp.get("name") or fp.get("path") or "input"
        bits = []
        sha = fp.get("sha") or ""
        if sha:
            bits.append(sha[:7])
        rows, cols = fp.get("rows"), fp.get("cols")
        if isinstance(rows, int) and isinstance(cols, int) and (rows or cols):
            bits.append(f"{rows}×{cols}")
        parts.append(f"{label} ({', '.join(bits)})" if bits else str(label))
    return "; ".join(parts)


def _footer_html(origin: str, rel_posix: str, day: str, link: str, inputs_text: str = "") -> str:
    text = f"Generated by mooring from {escape(origin)} · notebook {escape(rel_posix)} · {day}"
    if link:
        text += f' · <a href="{escape(link, quote=True)}" style="color:inherit">View on GitHub</a>'
    if inputs_text:
        text += f"<br>Inputs: {escape(inputs_text)}"
    return (
        '<footer style="margin:2.5rem 0 1rem;padding:0.75rem 1rem;border-top:1px solid #8884;'
        'font:12px/1.5 system-ui,sans-serif;color:#8a8a8a;text-align:center">'
        f"{text}</footer>"
    )
