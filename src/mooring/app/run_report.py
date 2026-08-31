"""Run the notebook after an Apply and tell the copilot, value-safely, what broke.

The copilot's repair loop used to reach only the failures mooring could see *without
running anything* — a cell that would not parse, a patch that would not write. Everything
a weak model actually gets wrong (a column that isn't there, an API called the wrong way,
a name that resolves only at runtime) is invisible from there, because mooring never opens
a marimo websocket and so never sees a cell's output. This module is the one route back,
and it is deliberately narrow:

* **It runs the EXISTING verify smoke path.** :func:`mooring.app.verify_run.run_verified`
  — the same ``marimo export html`` run behind the trust badge, under the same workspace
  run lock, with the same SHA-before-run rule, the same value-bearing-render deletion, the
  same process-tree kill, and the same value-free receipt. Nothing here is a second way to
  run a notebook; a reported run IS a verify, badge and activity entry included.
* **Only a value-safe rewrite reaches the model.** The run hands back
  :func:`mooring.app.notebook_run.failure_lines`' ``(KIND, message)`` pairs and nothing else
  (never the stderr text, which carries the cells' own ``print`` output), and the session
  turns those into a message through ``egress.sanitize_traceback`` — the one gateway. This
  module never sees the finished text before the session composes it, and it opens no
  channel of its own.
* **The per-notebook AI opt-out is re-checked immediately before the text leaves.** The run
  takes minutes; a teammate's sync or a hub toggle can land inside that window, and the
  hand-off is the egress, so the gate is where the egress is (the same reasoning as
  ``/api/ai/chat/send``). Both entry points below share that one check.

**When it is automatic, and when it is not.** It used to be never — an analyst clicked a
button that said what it would do, because this path re-executes every cell, which is
exactly what the apply gate exists to make deliberate. :func:`run_and_report` (the
**Run & report** click) is still that. :func:`run_and_collect` is the new one, and it is
reached ONLY from :mod:`mooring.app.auto_apply` after a model-driven write, under ALL of:

1. ``[ai] auto_run_report`` is on (policy-folded, read fresh from disk at the write);
2. the observation of the running kernel says a name the change should have bound is NOT
   bound — i.e. there is a real failure to report, not a hunch;
3. the notebook scans ``clean`` under :mod:`mooring.ai.codeguard`. That band answers
   precisely the question being asked here — "is re-executing this safe?" — so it is the
   condition, not a heuristic standing in for one. A notebook holding a cell that deletes
   files, posts data, or overwrites a report is never re-run automatically;
4. the analyst has not cancelled the turn (re-checked before the run and honoured DURING
   it — ``cancel`` kills the process tree, it does not merely decline to start).

Neither entry point ever runs a notebook the per-notebook AI opt-out covers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from mooring import policy
from mooring.app import notebook_run, verify_run
from mooring.config import Config


class ReportError(Exception):
    """The notebook could not be RUN at all (busy workspace, broken environment,
    timeout, not a notebook). ``str(exc)`` is the user-facing reason. A notebook that
    runs and fails is NOT an error — it is the report this module exists to make."""


@dataclass(frozen=True)
class RunReport:
    ran_clean: bool
    cells_failed: int | None  # the receipt's value-free count, or None when unknown
    sent: str  # the EXACT text forwarded to the model ("" when nothing was)
    redactions: tuple[tuple[int, str], ...] = ()  # value-free (line, kind) rewrite findings
    cancelled: bool = False  # the analyst stopped the turn; the process tree was killed


def run_and_report(
    session, cfg: Config, notebook_rel: str, *, live_schema_text: str = ""
) -> RunReport:
    """Smoke-run ``notebook_rel`` and, if it failed, SEND ``session`` a value-safe summary.

    The attended path: the analyst clicked **Run & report**, so the summary arrives as a
    new turn in the transcript.

    ``session`` is a :class:`mooring.ai.chat.ChatBroadcaster` — passed in rather than
    reached for, so the one thing that can compose an outbound message stays the session.

    Raises :class:`ReportError` when the run could not happen, and ``PermissionError``
    ("notebook_disabled") when the copilot was turned off for this notebook while the run
    was in flight — the same signal :meth:`mooring.app.apply.ApplyGuard.apply_with_undo`
    raises, so the adapters answer it the one way they already do.
    """
    report = _run_and_compose(session, cfg, notebook_rel)
    if report.sent:
        session.send(report.sent, live_schema_text)
    return report


def run_and_collect(
    session, cfg: Config, notebook_rel: str, *, cancel: threading.Event | None = None
) -> RunReport:
    """The same run, composed for a TOOL RESULT instead of a message.

    The automatic path (see the module docstring for the four conditions the caller must
    have satisfied first). Identical in every respect that matters — same runner, same
    lock, same receipt, same ``egress.sanitize_traceback`` rewrite, same opt-out re-check
    immediately before the text is handed over — and differs only in WHERE the text goes:
    back to the model as the result of the write it just made, rather than into the
    transcript as a fresh turn. Nothing is broadcast, so the analyst reads the outcome on
    the receipt the write already drew.

    ``cancel`` is the analyst's stop, honoured mid-run: it becomes the runner's own
    process-tree kill. A cancelled run reports ``cancelled=True`` and sends nothing —
    there is nothing to tell the model about a run that was stopped on purpose.

    Raises the same :class:`ReportError` / ``PermissionError`` as :func:`run_and_report`.
    """
    return _run_and_compose(session, cfg, notebook_rel, cancel=cancel)


def _run_and_compose(
    session, cfg: Config, notebook_rel: str, *, cancel: threading.Event | None = None
) -> RunReport:
    """Run, compose, gate — everything both entry points share.

    Kept as ONE body rather than two similar ones because the value-safety here is a
    sequence, not a step: drop the console half, rewrite the message half through the one
    gateway, then re-read the opt-out at the last possible moment. Two copies of that
    would be two chances to get the order wrong.
    """
    workspace = cfg.workspace()
    failures: list[tuple[str, str]] = []
    try:
        result = _smoke_run(cfg, notebook_rel, failures.extend, cancel)
    except notebook_run.RunCancelled:
        # Not a failure to report: the analyst stopped it, and the process tree is gone.
        return RunReport(ran_clean=False, cells_failed=None, sent="", cancelled=True)

    if result.passed:
        # Nothing to report. The receipt (and the green badge) is the whole outcome; the
        # model is not told "it worked", because the observation already told it that.
        return RunReport(ran_clean=True, cells_failed=None, sent="")
    if not failures:
        # A non-zero exit whose stderr held no line we recognise. Saying so to the analyst
        # is honest; inventing a summary for the model would not be.
        return RunReport(ran_clean=False, cells_failed=result.cells_failed, sent="")

    text, findings = session.run_failure_report(failures)
    # LAST point before egress — see the module docstring. Same gate as every other AI
    # write/send path (policy.ai_gate unions the per-notebook opt-out with the admin
    # ai_off globs), re-read from disk here rather than trusted from chat-open.
    if policy.ai_disabled(Path(workspace), notebook_rel):
        raise PermissionError("notebook_disabled")
    return RunReport(
        ran_clean=False,
        cells_failed=result.cells_failed,
        sent=text,
        redactions=tuple((f.line, f.kind) for f in findings),
    )


def _smoke_run(cfg: Config, notebook_rel: str, on_failures, cancel):
    """:func:`mooring.app.verify_run.verify_notebook`, opened up just far enough to pass
    a ``cancel`` event through.

    Spelled out here rather than called because ``verify_notebook`` takes no ``cancel``
    and the automatic run must be stoppable — it is minutes long. Everything else is
    that function verbatim: the same ``ensure_runnable`` refusals, the same workspace run
    lock (two kernels on one CPU would also fight over the same throwaway render), and
    the same body, so a reported run stays byte-identical to a hand-verified one.
    """
    workspace = cfg.workspace()
    rel_posix = verify_run.ensure_runnable(workspace, notebook_rel, ReportError)
    try:
        with notebook_run.workspace_guard(workspace):
            return verify_run.run_verified(cfg, rel_posix, cancel=cancel, on_failures=on_failures)
    except notebook_run.RunBusy as exc:
        raise ReportError(str(exc)) from exc
    except notebook_run.RunCancelled:
        raise  # a stop is not a failure to run — _run_and_compose tells them apart
    except verify_run.VerifyError as exc:
        raise ReportError(str(exc)) from exc
