"""Parallel "investigate" fan-out coordinator.

The copilot's ``mooring_investigate`` tool hands a list of independent, value-free
sub-questions here. :class:`InvestigatePlanner` opens ONE read-only, value-blind session
per branch on a bounded worker pool, drives each to a final text finding (via
:mod:`mooring.ai.fanout`), and returns the ordered results; :func:`merge_findings` scrubs
each and concatenates them into one value-free block the parent model reads back as the
tool result — then turns into ONE proposal the analyst Applies (the only human gate).

Pure + injected, exactly like :class:`mooring.ai.batch.BatchPlanner`: ``build_context`` and
``open_session`` are supplied by the app layer (they need the provider + config), so this
module imports neither the hub nor a provider. Two invariants hold BY CONSTRUCTION:

* every sub-agent is READ-ONLY — the caller opens it with no propose/edit tool — so a
  branch can never write; and
* ``mooring_investigate`` is never in a sub-agent's toolset (only the interactive parent
  gets it), so an investigation cannot recurse.

A finding is value-free because a read-only sub-agent has no data-value channel; the scrub
on merge (:func:`mooring.ai.egress.scrub_text`) is the checksum-PII floor BENEATH that
structural guarantee, not the guarantee itself.
"""

from __future__ import annotations

import contextlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from mooring.ai import fanout

# AUTO concurrency (``[ai.investigate] max_concurrency = 0``), per provider. A branch's
# cost is provider-shaped: a Copilot branch spawns a ~150 MB CLI subprocess AND spends a
# premium request, so keep it small; an OpenAI/LiteLLM branch is just an HTTP stream, so a
# wider fan-out is cheap. An explicitly configured value always wins over these.
_AUTO_CONCURRENCY = {"copilot": 2}
_AUTO_CONCURRENCY_DEFAULT = 6


def resolve_concurrency(configured: int, provider: str) -> int:
    """The effective number of branches to run at once.

    ``configured > 0`` is an explicit operator override and always wins. ``0`` means AUTO:
    a provider-aware default (see :data:`_AUTO_CONCURRENCY`), because the per-branch cost
    differs by an order of magnitude between the Copilot and OpenAI/LiteLLM backends."""
    if configured and configured > 0:
        return configured
    return _AUTO_CONCURRENCY.get((provider or "").strip().lower(), _AUTO_CONCURRENCY_DEFAULT)


@dataclass(frozen=True)
class BranchJob:
    """One independent sub-question. Every field is value-free: ``question`` is a
    plain-English ask, ``notebook_rel`` / ``dataset_rel`` are workspace-relative PATHS."""

    question: str
    notebook_rel: str = ""
    dataset_rel: str = ""
    model: str = ""
    reasoning_effort: str = ""


@dataclass
class BranchResult:
    """The value-free outcome of one branch (see :class:`mooring.ai.fanout.BranchOutcome`
    for the ``status`` vocabulary; ``not_run`` is added here for a branch skipped before
    it opened a session)."""

    question: str
    status: str
    finding: str = ""
    error: str = ""
    pii: list[dict] = field(default_factory=list)


class InvestigatePlanner:
    """Fan out N read-only branches on a bounded pool and return ordered results.

    The four injected operations keep the engine hub/provider-free:

    * ``build_context(notebook_rel, dataset_rel) -> ctx`` — the value-free per-notebook
      context (opaque here; whatever ``open_session`` needs).
    * ``open_session(ctx, notebook_rel, model, effort) -> ChatBroadcaster`` — open a
      READ-ONLY value-blind session (no propose/edit tool, no ``mooring_investigate``).

    ``on_progress(event: dict)`` (optional) receives VALUE-FREE lifecycle events —
    ``{"phase": "start"|"branch"|"done", "done": int, "total": int, "status": str}`` — so a
    caller can stream a progress cue while the parent turn blocks. It carries counts and
    statuses only, never a sub-question or a finding.
    """

    def __init__(
        self,
        *,
        config,
        pii,
        build_context: Callable,
        open_session: Callable,
        default_notebook_rel: str = "",
        on_progress: Callable[[dict], None] | None = None,
        abort: threading.Event | None = None,
    ) -> None:
        self._cfg = config
        self._pii = pii
        self._build_context = build_context
        self._open_session = open_session
        self._default_notebook = default_notebook_rel
        self._on_progress = on_progress
        self._abort = abort or threading.Event()

    def _emit(self, **data) -> None:
        """Publish one value-free progress event; a broken sink never sinks a branch."""
        if self._on_progress is None:
            return
        with contextlib.suppress(Exception):
            self._on_progress(data)

    def run(self, branches) -> list[BranchResult]:
        """Run every branch concurrently (at most ``max_concurrency`` at once, at most
        ``max_branches`` total) and return results in SUBMISSION order.

        ``max_concurrency`` must already be resolved by the caller (see
        :func:`resolve_concurrency`); a 0 here degrades safely to serial. Progress is
        emitted as each branch FINISHES (not in submission order), so a caller can stream
        an honest "k of N done" cue while the parent turn blocks."""
        jobs = list(branches)[: max(1, self._cfg.max_branches)]
        if not jobs:
            return []
        # Value-free PII pre-flight on every sub-question BEFORE opening any session, so a
        # checksum-PII branch never spends a session. block_investigation aborts them all.
        blocked: dict[int, list[dict]] = {}
        for i, job in enumerate(jobs):
            hit, findings = fanout.preflight_pii(job.question, self._pii)
            if hit:
                blocked[i] = findings
        abort_all = bool(blocked) and str(self._cfg.pii_policy).strip() == "block_investigation"

        results: list[BranchResult | None] = [None] * len(jobs)
        futures: dict = {}
        with ThreadPoolExecutor(
            max_workers=max(1, self._cfg.max_concurrency), thread_name_prefix="investigate"
        ) as pool:
            for i, job in enumerate(jobs):
                if i in blocked:
                    results[i] = BranchResult(job.question, "pii_blocked", pii=blocked[i])
                    continue
                if abort_all or self._abort.is_set():
                    results[i] = BranchResult(
                        job.question, "not_run", error="Investigation cancelled."
                    )
                    continue
                futures[pool.submit(self._run_branch, job)] = i
            total = len(futures)
            self._emit(phase="start", done=0, total=total)
            done = 0
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:  # noqa: BLE001 - one branch must not sink the rest
                    results[i] = BranchResult(jobs[i].question, "failed", error=str(exc))
                done += 1
                self._emit(phase="branch", done=done, total=total, status=results[i].status)
        found = sum(1 for r in results if r is not None and r.status == "finding")
        self._emit(phase="done", done=total, total=total, found=found)
        return [r for r in results if r is not None]

    def _run_branch(self, job: BranchJob) -> BranchResult:
        if self._abort.is_set():
            return BranchResult(job.question, "not_run", error="Investigation cancelled.")
        notebook_rel = job.notebook_rel or self._default_notebook
        try:
            ctx = self._build_context(notebook_rel, job.dataset_rel)
        except Exception as exc:  # noqa: BLE001 - a branch's context failure isolates to it
            return BranchResult(job.question, "failed", error=f"Could not read context: {exc}")
        try:
            session = self._open_session(ctx, notebook_rel, job.model, job.reasoning_effort)
        except Exception as exc:  # noqa: BLE001
            return BranchResult(job.question, "failed", error=str(exc))
        try:
            deadline = time.monotonic() + max(1, self._cfg.branch_timeout)
            outcome = fanout.drive_to_finding(
                session, job.question, deadline=deadline, abort=self._abort
            )
        finally:
            with contextlib.suppress(Exception):
                session.close()
        return BranchResult(
            job.question,
            outcome.status,
            finding=outcome.finding,
            error=outcome.error,
            pii=outcome.pii,
        )


def merge_findings(results) -> str:
    """Scrub each branch's finding and concatenate into ONE value-free block for the
    parent model to read as the tool result.

    Only branches that actually answered contribute; blocked/failed branches are counted
    so the model knows coverage was partial. Each finding passes
    :func:`mooring.ai.egress.scrub_text` (the checksum-PII floor) as defence-in-depth — the
    finding is already value-free by construction (a read-only sub-agent has no value
    channel), so this is a floor, not the guarantee."""
    from mooring.ai import egress

    parts: list[str] = []
    blocked = 0
    failed = 0
    for r in results:
        if r.status == "finding" and r.finding.strip():
            scrubbed, _ = egress.scrub_text(r.finding.strip())
            if scrubbed.strip():
                parts.append(f"## {r.question.strip()}\n{scrubbed.strip()}")
        elif r.status == "pii_blocked":
            blocked += 1
        elif r.status in ("failed", "empty", "cancelled", "not_run"):
            failed += 1
    if not parts:
        return ""
    out = [
        "Findings from investigating these questions in parallel (schema/code only — no "
        "data values). Use them to propose ONE change now with the propose tools.",
        "\n\n".join(parts),
    ]
    notes = []
    if blocked:
        notes.append(
            f"{blocked} branch(es) were blocked because a sub-question looked like it "
            "contained sensitive data"
        )
    if failed:
        notes.append(f"{failed} branch(es) returned nothing")
    if notes:
        out.append("(" + "; ".join(notes) + ".)")
    return "\n\n".join(out)
