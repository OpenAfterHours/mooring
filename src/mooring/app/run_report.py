"""Run the notebook after an Apply and tell the copilot, value-safely, what broke.

The copilot's repair loop used to reach only the failures mooring could see *without
running anything* — a cell that would not parse, a patch that would not write. Everything
a weak model actually gets wrong (a column that isn't there, an API called the wrong way,
a name that resolves only at runtime) is invisible from there, because mooring never opens
a marimo websocket and so never sees a cell's output. This module is the one route back,
and it is deliberately narrow:

* **It runs the EXISTING verify smoke path.** :func:`mooring.app.verify_run.verify_notebook`
  — the same ``marimo export html`` run behind the trust badge, with the same workspace run
  lock, the same SHA-before-run rule, the same value-bearing-render deletion, the same
  process-tree kill, and the same value-free receipt. Nothing here is a second way to run a
  notebook; a reported run IS a verify, badge and activity entry included.
* **It is never automatic.** An analyst clicks a button that says what it will do. That
  matters because this path re-executes every cell — exactly what the apply gate exists to
  make deliberate — so it must never ride an Apply, a timer, or a page load.
* **Only a value-safe rewrite reaches the model.** The run hands back
  :func:`mooring.app.notebook_run.failure_lines`' ``(KIND, message)`` pairs and nothing else
  (never the stderr text, which carries the cells' own ``print`` output), and the session
  turns those into a message through ``egress.sanitize_traceback`` — the one gateway. This
  module never sees the finished text before the session composes it, and it opens no
  channel of its own.
* **The per-notebook AI opt-out is re-checked immediately before the send.** The run takes
  minutes; a teammate's sync or a hub toggle can land inside that window, and the send is
  the egress, so the gate is where the egress is (the same reasoning as ``/api/ai/chat/send``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mooring import policy
from mooring.app import verify_run
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


def run_and_report(
    session, cfg: Config, notebook_rel: str, *, live_schema_text: str = ""
) -> RunReport:
    """Smoke-run ``notebook_rel`` and, if it failed, send ``session`` a value-safe summary.

    ``session`` is a :class:`mooring.ai.chat.ChatBroadcaster` — passed in rather than
    reached for, so the one thing that can compose an outbound message stays the session.

    Raises :class:`ReportError` when the run could not happen, and ``PermissionError``
    ("notebook_disabled") when the copilot was turned off for this notebook while the run
    was in flight — the same signal :meth:`mooring.app.apply.ApplyGuard.apply_with_undo`
    raises, so the adapters answer it the one way they already do.
    """
    workspace = cfg.workspace()
    failures: list[tuple[str, str]] = []
    try:
        result = verify_run.verify_notebook(cfg, notebook_rel, on_failures=failures.extend)
    except verify_run.VerifyError as exc:
        raise ReportError(str(exc)) from exc

    if result.passed:
        # Nothing to report. The receipt (and the green badge) is the whole outcome; the
        # model is not told "it worked", because it was never told the change was applied.
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
    session.send(text, live_schema_text)
    return RunReport(
        ran_clean=False,
        cells_failed=result.cells_failed,
        sent=text,
        redactions=tuple((f.line, f.kind) for f in findings),
    )
