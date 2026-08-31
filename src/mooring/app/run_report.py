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
* **The outbound PII valve applies to both entry points, and to the automatic one it
  applies FAIL-CLOSED.** The attended path gets it from ``session.send``; the automatic
  path never touches ``send``, so :func:`run_and_collect` runs the session's own scan
  itself and, where block mode would hold the turn, sends NOTHING (there is no analyst
  at a tool result to confirm a hold). The same call also puts the exact text that was
  sent into the analyst's transcript, which is otherwise the one thing the automatic
  path loses by not being a turn.

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

import contextlib
import threading
from dataclasses import dataclass, replace
from pathlib import Path

from mooring import policy
from mooring.app import notebook_run, verify_run
from mooring.config import Config

# What the ANALYST is shown when mooring runs the notebook on the model's behalf. The
# summary itself travels verbatim in the event's `sent` field, so "you are shown exactly
# what was sent" is literally true; these two say, without quoting anything, why there
# is no summary to show.
_HELD_NOTICE = (
    "mooring ran this notebook after the assistant's change, and the outbound PII "
    "guard held the failure summary — so nothing was sent to the assistant. Open the "
    "notebook to see the error yourself."
)
_UNREADABLE_NOTICE = (
    "mooring ran this notebook after the assistant's change. It did not run clean, but "
    "no line matching marimo's own error taxonomy came back, so there was nothing "
    "value-safe to send the assistant."
)
_STOPPED_NOTICE = "You stopped the turn, so mooring stopped the run it had started."
_FAILED_NOTICE = "mooring could not run this notebook to diagnose the change:"


def _one_line(exc: BaseException) -> str:
    """A raised reason, bounded to one line. These messages are mooring's own (a busy
    workspace, an unrunnable notebook, a broken environment) and they go to the
    ANALYST's transcript, never to the model."""
    return " ".join(str(exc).split())[:200]


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
    held: bool = False  # the outbound PII valve held the summary; NOTHING was sent


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
    transcript as a fresh turn.

    ``cancel`` is the analyst's stop, honoured mid-run: it becomes the runner's own
    process-tree kill. A cancelled run reports ``cancelled=True`` and sends nothing —
    there is nothing to tell the model about a run that was stopped on purpose.

    Two things the attended path gets from ``session.send`` have to be done explicitly
    here, because this text does NOT go through it:

    * **the outbound PII valve.** The same scan the analyst's own turns get
      (``ChatBroadcaster._scan_prompt`` -> ``egress.guard_prompt``), and where block
      mode would HOLD the turn this **fails closed**: ``held=True``, ``sent=""``,
      nothing forwarded. A hold is a request for a human decision, and there is no
      human at a tool result — auto-confirming one would make "block mode" mean
      "warn mode" on the one path nobody watches. A session that cannot be scanned at
      all is held for the same reason.
    * **the transcript entry.** The attended report appears in the transcript because
      it IS a turn; this one is a tool result the analyst never sees. So the run
      announces itself before it starts and reports itself afterwards over value-free
      ``run_report`` events (see :func:`_run_event` for why that NAME is a contract) —
      the docs promise "you are shown exactly what was sent", and a summary that reaches
      only the model would make that false.

    Raises the same :class:`ReportError` / ``PermissionError`` as :func:`run_and_report`.
    """
    # The analyst's own notebook is about to be re-executed, which takes minutes: say so
    # before it starts, not once it is over.
    _run_event(session, {"state": "running"})
    try:
        report = _run_and_compose(session, cfg, notebook_rel, cancel=cancel)
    except Exception as exc:
        # Every exit path ends the cue. A run that announced itself and then said
        # nothing would leave "running your notebook…" on screen for ever.
        _run_event(session, {"text": f"{_FAILED_NOTICE} {_one_line(exc)}".strip()})
        raise
    if report.cancelled:
        _run_event(session, {"text": _STOPPED_NOTICE})
        return report
    if not report.sent:
        _run_event(session, {"ran_clean": True} if report.ran_clean else {"text": _UNREADABLE_NOTICE})
        return report
    hold, findings, scan_error = _scan_outbound(session, report.sent)
    _emit_pii_event(session, findings, scan_error, held=hold)
    if hold:
        _run_event(session, {"held": True, "text": _HELD_NOTICE})
        return replace(report, sent="", held=True)
    # The summary VERBATIM, in the field the chat page renders with its existing
    # "this is exactly what was sent" block — the same promise the attended path keeps
    # by being a turn.
    _run_event(
        session,
        {"sent": report.sent, "redactions": [{"line": ln, "kind": k} for ln, k in report.redactions]},
    )
    return report


def _scan_outbound(session, text: str) -> tuple[bool, list, str]:
    """The session's OWN outbound scan, run without sending. Fails closed.

    Deliberately the session's method rather than a second call to
    ``egress.guard_prompt`` from here: the scan's configuration (enabled, block mode,
    whether the optional name pass is armed AND its model actually ready) lives on the
    session, and a copy of that decision in the app layer is a copy that can drift from
    the one the analyst's own turns get. Duck-typed across the layer boundary for the
    same reason ``run_failure_report`` is.

    A session that does not offer the scan, or one whose scan raises, is treated as a
    HOLD: this is the unattended path, so "the guard could not run" must stop the text,
    not wave it through.
    """
    scan = getattr(session, "_scan_prompt", None)
    if not callable(scan):
        return True, [], "unavailable"
    try:
        hold, findings, scan_error = scan(text)
    except Exception:  # noqa: BLE001 — a guard that breaks must not become a bypass
        return True, [], "unavailable"
    return bool(hold), list(findings or []), str(scan_error or "")


def _emit_pii_event(session, findings, scan_error: str, *, held: bool) -> None:
    """The same value-free ``pii`` event the attended valve broadcasts — kinds and line
    numbers only, never the matched text. No confirm token: an automatic report is never
    resumable, so there is nothing for the analyst to release."""
    if not (findings or scan_error):
        return
    data: dict = {"findings": [{"line": f.line, "kind": f.kind} for f in findings]}
    if scan_error:
        data["scan_error"] = scan_error
    if held:
        data["held"] = True
    _broadcast(session, "pii", data)


def _run_event(session, data: dict) -> None:
    """One value-free ``run_report`` event for the analyst's transcript (best-effort).

    The NAME matters and is a contract with the chat page: ``EventSource`` has no
    wildcard listener, so an event sent under any other name is dropped by the browser
    with no error anywhere. The page understands ``{state}``, ``{ran_clean}`` and
    ``{sent, redactions}``, and falls back to the first string among
    ``text``/``detail``/``message``/``summary``/``error`` for anything else — so extra
    keys are safe, a new NAME is not (``note`` is the only other one it listens for).
    """
    _broadcast(session, "run_report", data)


def _broadcast(session, kind: str, data: dict) -> None:
    from mooring.ai.chat import ChatEvent

    fan_out = getattr(session, "_broadcast", None)
    if not callable(fan_out):
        return
    with contextlib.suppress(Exception):
        fan_out(ChatEvent(kind, data))


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
