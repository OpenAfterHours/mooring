"""Deterministic fan-out engine for unattended batch notebook generation.

The orchestrator the user drives with a *list of jobs* ("build a notebook that does
X"). This module owns the CONTROL FLOW — slug de-duplication, a hard concurrency
cap, per-job timeout / failure-isolation, and the NON-interactive PII policy — while
the model only ever runs inside the EXISTING per-notebook copilot: one value-free
``ChatBroadcaster`` session per job, injected via ``open_session``. It is therefore
testable end-to-end against a stub session with no real Copilot, and it preserves
every privacy invariant by construction:

* one session is bound to exactly one notebook (invariant 1) — the planner makes N
  sessions, never one multi-notebook session;
* builders only PROPOSE; this module never writes a notebook (invariant 2) — it
  collects proposals for a human to Apply;
* all model egress goes through the session's own value-free context + prompt guard
  (invariant 3); the planner adds ONE new outbound decision — the brief — and runs
  it through the same :func:`mooring.ai.egress.guard_prompt` BEFORE dispatch. By
  default a structured-PII hit BLOCKS the job (per ``pii_policy``) rather than
  leaking, and the session's own guard (forced to block mode by the caller) is the
  backstop for anything the pre-flight misses. The one human-gated escape hatch is
  :meth:`BatchPlanner.force` ("Build anyway"): the analyst reviewing the tray may
  override a block per job, which re-runs it auto-confirming the held brief — the
  batch analogue of the chat's "Send anyway", and the only path that forwards a
  flagged brief.

Nothing here imports the hub or the Copilot SDK: the four operations that touch the
workspace / provider are injected, so the engine is a pure coordinator.
"""

from __future__ import annotations

import contextlib
import queue
import re
import secrets
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mooring.ai.chat import ChatBroadcaster

# Statuses a job can finish in (all value-free; surfaced to the tray + SSE).
#   built            — the builder proposed >=1 change; queued for human review
#   empty            — the builder finished without proposing anything
#   failed           — context/create/timeout/session error
#   skipped_disabled — the target notebook has the copilot turned off
#   pii_blocked      — the brief tripped the outbound PII guard (no human to confirm)
#   not_run          — block_batch aborted, or the batch was cancelled, before this job

_POLL = 0.5  # seconds between wall-clock deadline checks while draining a session
_FOLLOW_UP_PROMPT = (
    "Continue building this notebook: add any remaining loading, analysis or "
    "visualisation cells the brief calls for, then stop. Propose the changes."
)


class BatchError(Exception):
    """A batch could not be started (e.g. it exceeds the configured job cap)."""


@dataclass(frozen=True)
class BatchJob:
    """One unit of work: build a notebook named ``name`` that does ``brief``.

    ``name`` may be empty — a readable name is then derived from the brief.
    ``dataset_rel`` optionally points the builder at one dataset's schema. No field
    ever carries a data value: a brief is a natural-language instruction and
    ``dataset_rel`` is a workspace-relative PATH.
    """

    name: str
    brief: str
    dataset_rel: str = ""
    model: str = ""
    reasoning_effort: str = ""


@dataclass
class BatchResult:
    """The value-free outcome of one job, for the review tray + per-notebook Apply.

    ``proposals`` are the raw proposal payloads the builder emitted (each already
    value-free source code — an ``{code, rationale}`` append or a
    ``{kind, ops, diffs}`` patch); the hub turns the human's chosen one into a write
    through the EXISTING single-notebook Apply path. ``pii`` carries value-free
    findings (line + kind) when the job was blocked.
    """

    job: BatchJob
    notebook_rel: str | None
    status: str
    proposals: list[dict] = field(default_factory=list)
    error: str = ""
    pii: list[dict] = field(default_factory=list)


def _name_from_brief(brief: str) -> str:
    """A readable notebook name from a brief when the job gave none — the first few
    word-ish tokens, so ``create_unique`` has something to slugify."""
    words = re.findall(r"[A-Za-z0-9]+", brief or "")[:6]
    return " ".join(words) or "notebook"


class BatchPlanner:
    """Run a list of :class:`BatchJob` to completion, returning a
    :class:`BatchResult` per job (in the original order).

    The four injected operations keep the engine free of hub/SDK imports:

    * ``make_notebook(name) -> notebook_rel`` — create one fresh notebook file
      (collision-safe, e.g. :func:`mooring.notebook_template.create_unique`).
    * ``build_context(notebook_rel, dataset_rel) -> (system_context, dictionary)`` —
      the value-free per-notebook context (the single egress assembler).
    * ``open_session(system_context, notebook_rel, model, effort, dictionary) ->
      ChatBroadcaster`` — open the existing value-free copilot bound to that notebook.
    * ``is_disabled(notebook_rel) -> bool`` — the per-notebook AI opt-out.
    * ``discard_notebook(notebook_rel)`` (optional) — best-effort remove the skeleton
      a job created but never built (pii-blocked / failed / empty), so a batch does
      not litter the workspace with empty notebooks.

    ``on_progress(event: dict)`` (optional) receives value-free lifecycle events for
    streaming. ``abort`` (optional) lets a caller cancel an in-flight batch.
    """

    def __init__(
        self,
        *,
        config,
        pii,
        make_notebook: Callable[[str], str],
        build_context: Callable[[str, str], tuple[str, object]],
        open_session: Callable[..., ChatBroadcaster],
        is_disabled: Callable[[str], bool] | None = None,
        discard_notebook: Callable[[str], None] | None = None,
        on_progress: Callable[[dict], None] | None = None,
        abort: threading.Event | None = None,
    ) -> None:
        self._cfg = config
        self._pii = pii
        self._make_notebook = make_notebook
        self._build_context = build_context
        self._open_session = open_session
        self._is_disabled = is_disabled or (lambda _rel: False)
        self._discard_notebook = discard_notebook
        self._on_progress = on_progress
        self._abort = abort or threading.Event()
        # Appendable-queue state: ONE bounded worker pool for the run's whole life, a
        # growing results list (index = submission order), and a pending counter so the
        # caller can tell when the queue is caught up. add() may be called many times
        # while earlier jobs are still building, so the user can keep writing.
        self._results: list[BatchResult] = []
        self._results_lock = threading.Lock()
        self._cond = threading.Condition()
        self._pending = 0
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False
        # Jobs currently being REVISED (re-built from the analyst's feedback). The
        # previous result is stashed so a revision that fails/empties restores it rather
        # than wiping a good proposal — the notebook file is never touched either way.
        self._refining: set[int] = set()
        self._refining_prev: dict[int, BatchResult] = {}
        # Jobs being force-rebuilt after the analyst chose "Build anyway" on a PII block.
        # Same stash/restore discipline as a revision: a forced build that produces nothing
        # restores the blocked state so the tray's button comes back.
        self._forcing: set[int] = set()
        self._forcing_prev: dict[int, BatchResult] = {}

    # -- lifecycle (an appendable queue) ------------------------------------

    def start(self) -> "BatchPlanner":
        """Create the bounded worker pool (idempotent). Call before :meth:`add`."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=max(1, self._cfg.max_concurrency), thread_name_prefix="batch-build"
            )
        return self

    def add(self, jobs) -> list[int]:
        """Queue more jobs onto the started run; return their indices.

        Each call independently runs the value-free PII pre-flight over ITS OWN jobs
        (so ``block_batch`` within a call still creates nothing and aborts the run),
        then creates a notebook and submits a builder for each clean job. Safe to call
        repeatedly while earlier jobs build. Raises :class:`BatchError` if the run is
        closed or the cumulative job count would exceed ``max_jobs``.
        """
        jobs = list(jobs)
        if not jobs:
            return []
        if self._closed or self._executor is None:
            raise BatchError("This batch queue is closed.")
        # Reserve all this call's slots atomically with the cumulative-cap check, so two
        # concurrent add() calls can't both pass the cap against the same measured length.
        indices = self._reserve_all(jobs)

        # Pre-flight this call's briefs up front so block_batch aborts before any file
        # is created (same one-shot guarantee a single add() had before).
        blocked: dict[int, list[dict]] = {}
        for offset, job in enumerate(jobs):
            hit, findings = self._preflight_pii(job.brief)
            if hit:
                blocked[offset] = findings
        abort_batch = bool(blocked) and self._policy() == "block_batch"
        if abort_batch:
            self._abort.set()

        for offset, job in enumerate(jobs):
            index = indices[offset]
            if abort_batch:
                if offset in blocked:
                    self._record(index, job, None, "pii_blocked", pii=blocked[offset])
                else:
                    self._record(
                        index,
                        job,
                        None,
                        "not_run",
                        error="Batch aborted: a checksum-validated PII value was found in "
                        "another job's brief.",
                    )
                continue
            if offset in blocked:  # block_job: skip just this one (no file created)
                self._record(index, job, None, "pii_blocked", pii=blocked[offset])
                continue
            if self._abort.is_set():
                self._record(index, job, None, "not_run", error="Batch cancelled.")
                continue
            try:
                notebook_rel = self._make_notebook(job.name.strip() or _name_from_brief(job.brief))
            except (ValueError, OSError) as exc:
                self._record(
                    index, job, None, "failed", error=f"Could not create the notebook: {exc}"
                )
                continue
            if self._is_disabled(notebook_rel):
                self._record(index, job, notebook_rel, "skipped_disabled")
                continue
            self._emit(
                index=index, name=job.name, status="queued", notebook=notebook_rel, n_proposals=0
            )
            self._submit(index, job, notebook_rel)
        return indices

    def run(self, jobs) -> list[BatchResult]:
        """One-shot convenience: start, add all jobs, wait for them, close, return the
        ordered results. Equivalent to the old fixed-batch behaviour (used by tests and
        any headless caller that doesn't need to stream/append)."""
        self.start()
        try:
            self.add(jobs)
        except BatchError:
            self.close(cancel=True)
            raise
        self.wait_idle()
        self.close()
        return self.snapshot()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until every submitted job has finished (the queue is caught up)."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending == 0, timeout)

    @property
    def pending(self) -> int:
        """How many builds are queued or in flight right now."""
        with self._cond:
            return self._pending

    @property
    def total(self) -> int:
        with self._results_lock:
            return len(self._results)

    def is_idle(self) -> bool:
        return self.pending == 0

    def snapshot(self) -> list[BatchResult]:
        """A copy of the results so far (for the review tray), newest-last."""
        with self._results_lock:
            return list(self._results)

    def close(self, *, cancel: bool = False) -> None:
        """Stop accepting new jobs and shut the pool down. ``cancel`` (used on a repo
        switch / shutdown) aborts in-flight builds and drops queued ones; otherwise
        running builds finish first."""
        self._closed = True
        if cancel:
            self._abort.set()
        if self._executor is not None:
            self._executor.shutdown(wait=not cancel, cancel_futures=cancel)

    # -- submission ---------------------------------------------------------

    def _reserve_all(self, jobs) -> list[int]:
        """Append a "queued" placeholder per job and return their indices, checking the
        cumulative cap under the SAME lock hold — so the check and the reservation are
        atomic and two concurrent add() calls can't both slip past the cap."""
        cap = self._cfg.max_jobs
        with self._results_lock:
            existing = len(self._results)
            if cap and existing + len(jobs) > cap:
                raise BatchError(
                    f"This batch already has {existing} job(s); adding {len(jobs)} more would "
                    f"exceed the limit of {cap} (raise [ai.batch] max_jobs to allow more)."
                )
            indices = []
            for job in jobs:
                indices.append(len(self._results))
                self._results.append(BatchResult(job=job, notebook_rel=None, status="queued"))
            return indices

    def _submit(self, index: int, job: BatchJob, notebook_rel: str) -> None:
        # Submit and bump _pending together under the lock, and ONLY on success — so a
        # concurrent close(cancel=True) that shut the pool down (executor.submit then
        # raises RuntimeError) can't leak _pending and wedge wait_idle / idle-reap. A
        # shut pool means the job is cancelled: record it (which also discards its
        # orphan notebook) rather than letting a bare RuntimeError escape add() as a 500.
        try:
            with self._cond:
                if self._closed:
                    raise RuntimeError("batch queue closed")
                assert self._executor is not None  # a live (non-closed) queue has a pool
                future = self._executor.submit(self._run_job, index, job, notebook_rel)
                self._pending += 1
        except RuntimeError:
            self._record(index, job, notebook_rel, "not_run", error="Batch cancelled.")
            return
        future.add_done_callback(
            lambda f, i=index, j=job, r=notebook_rel: self._on_done(f, i, j, r)
        )

    def _on_done(self, future, index: int, job: BatchJob, notebook_rel: str) -> None:
        # _run_job already stored its own result via _record; only handle the abnormal
        # cases (a cancelled queued job, or a worker that died before recording).
        try:
            if future.cancelled():
                self._record(index, job, notebook_rel, "not_run", error="Batch cancelled.")
            else:
                future.result()
        except CancelledError:
            self._record(index, job, notebook_rel, "not_run", error="Batch cancelled.")
        except Exception as exc:  # noqa: BLE001  # defensive; _run_job is meant to catch
            self._record(index, job, notebook_rel, "failed", error=f"The build failed: {exc}")
        with self._cond:
            self._pending -= 1
            self._cond.notify_all()

    # -- per-job ------------------------------------------------------------

    def _run_job(self, index: int, job: BatchJob, notebook_rel: str) -> BatchResult:
        # Reflect "building" (with the notebook) in the stored result so the live tray
        # shows it building, not just "queued", until the final result lands.
        with self._results_lock:
            if 0 <= index < len(self._results):
                self._results[index] = self._make_result(job, notebook_rel, "building")
        self._emit(
            index=index, name=job.name, status="building", notebook=notebook_rel, n_proposals=0
        )
        return self._commit(index, self._build(index, job, notebook_rel))

    def _build(
        self,
        index: int,
        job: BatchJob,
        notebook_rel: str,
        *,
        force_pii: bool = False,
        abort: threading.Event | None = None,
    ) -> BatchResult:
        """Build one notebook and RETURN its result (UNSTORED) — the shared core of an
        initial build, a revision, and a forced ("Build anyway") rebuild. build_context ->
        open one value-free copilot -> drive it to a proposal; the session is always closed.

        ``abort`` defaults to the batch-wide cancel event; a forced build passes its OWN
        (never-set) event so it can run even after a ``block_batch`` aborted the run.
        ``force_pii`` makes the drive auto-confirm a held brief (forward it verbatim,
        recording the overridden findings) instead of blocking the job."""
        abort = abort if abort is not None else self._abort
        if abort.is_set():
            return self._make_result(job, notebook_rel, "not_run", error="Batch cancelled.")
        try:
            system_context, dictionary = self._build_context(notebook_rel, job.dataset_rel)
        except Exception as exc:  # noqa: BLE001  # one job's failure must not abort the batch
            return self._make_result(
                job, notebook_rel, "failed", error=f"Could not read the notebook context: {exc}"
            )
        try:
            session = self._open_session(
                system_context, notebook_rel, job.model, job.reasoning_effort, dictionary
            )
        except Exception as exc:  # noqa: BLE001
            return self._make_result(job, notebook_rel, "failed", error=str(exc))
        try:
            return self._drive(index, job, notebook_rel, session, force_pii=force_pii, abort=abort)
        finally:
            with contextlib.suppress(Exception):
                session.close()

    # -- revision (iterate on a proposal before applying) -------------------

    def refine(self, index: int, feedback: str) -> None:
        """Re-build ONE notebook's proposal with the analyst's revision note folded into
        its brief — so a proposal can be tweaked in the review tray BEFORE it is Applied.
        The notebook file is never written; only the in-memory proposal changes, and a
        revision that produces nothing keeps the previous proposal. Respects the pool
        (one builder, the concurrency cap) and the non-interactive PII gate on the note.

        A job that was force-built ("Build anyway") stays overridden: its revisions run in
        force mode too, so iterating on it doesn't re-hit the PII wall on the same flagged
        brief. The revision NOTE is still scanned for high-confidence (checksum) PII the
        analyst may have pasted afresh — the override is scoped to the reviewed brief, not
        a blanket licence to type a new card into the note."""
        feedback = (feedback or "").strip()
        if not feedback:
            raise BatchError("Add a note describing the change you want.")
        if self._closed or self._executor is None:
            raise BatchError("This batch queue is closed.")
        hit, _findings = self._preflight_pii(feedback)
        if hit:
            raise BatchError(
                "Your revision note looks like it contains sensitive data (a card / IBAN / "
                "NHS number), so it was not sent."
            )
        with self._results_lock:
            if not 0 <= index < len(self._results):
                raise BatchError("No such job.")
            prev = self._results[index]
            # Only a FINISHED, built proposal can be revised. A still-building / queued
            # job has a non-None notebook_rel too, so without this a revision would spawn
            # a SECOND session on the same notebook and race the initial build's commit /
            # discard — corrupting the proposal and the file.
            if prev.status != "built" or prev.notebook_rel is None:
                raise BatchError("This notebook isn't ready to revise yet.")
            if index in self._refining:
                raise BatchError("This notebook is already being revised.")
            # A built job carries findings only when it was force-built (see _drive); a
            # revision of such a job inherits the override so the flagged brief still goes,
            # instead of the session re-holding it and the revision silently no-op'ing.
            force = bool(prev.pii)
            self._refining.add(index)
            self._refining_prev[index] = prev
        refined_job = replace(
            prev.job, brief=prev.job.brief + "\n\nRevision requested by the analyst: " + feedback
        )
        notebook_rel = prev.notebook_rel
        with self._cond:
            self._pending += 1
        try:
            future = self._executor.submit(
                self._run_refine, index, refined_job, notebook_rel, force
            )
        except RuntimeError:  # pool shut down by a concurrent close
            with self._results_lock:
                self._refining.discard(index)
                self._refining_prev.pop(index, None)
            with self._cond:
                self._pending -= 1
                self._cond.notify_all()
            raise BatchError("This batch queue is closed.")
        future.add_done_callback(lambda f, i=index: self._on_refine_done(f, i))
        self._emit(
            index=index,
            name=refined_job.name,
            status="refining",
            notebook=notebook_rel,
            n_proposals=len(prev.proposals),
        )

    def _run_refine(
        self, index: int, job: BatchJob, notebook_rel: str, force_pii: bool = False
    ) -> BatchResult:
        # Build WITHOUT storing — _on_refine_done adopts the result only if it actually
        # built, so a poor revision never wipes the existing proposal or its notebook.
        # force_pii carries a force-built job's override into its revisions.
        return self._build(index, job, notebook_rel, force_pii=force_pii)

    def _on_refine_done(self, future, index: int) -> None:
        with self._results_lock:
            self._refining.discard(index)
            prev = self._refining_prev.pop(index, None)
        try:
            result = None if future.cancelled() else future.result()
        except Exception:  # noqa: BLE001  # defensive; _build is meant to catch
            result = None
        if result is not None and result.status == "built":
            self._commit(index, result)  # adopt the revised proposal
        elif prev is not None:
            # The revision produced nothing usable — keep the previous proposal, but
            # ANNOTATE it: the tray is pull-based, so a transient emit alone is invisible.
            # _run_refine never stored over the slot, so results[index] is still prev.
            if result is not None and result.status == "pii_blocked":
                # A non-overridden job whose note tripped the guard (e.g. a name the note
                # introduced): keep the proposal and say why, so the analyst can edit the
                # note rather than wonder why nothing changed.
                kinds = ", ".join(sorted({str(f.get("kind", "")) for f in result.pii}))
                why = (
                    f"the revision looks like it contains {kinds or 'sensitive data'} — "
                    "edit the note or remove the flagged data"
                )
            else:
                why = "" if result is None else (result.error or "the revision proposed nothing")
            note = f"Revision didn't change anything: {why}" if why else ""
            with self._results_lock:
                if note and 0 <= index < len(self._results) and self._results[index] is prev:
                    prev.error = note
            self._emit(
                index=index,
                name=prev.job.name,
                status=prev.status,
                notebook=prev.notebook_rel,
                n_proposals=len(prev.proposals),
                error=prev.error,
            )
        with self._cond:
            self._pending -= 1
            self._cond.notify_all()

    def refining_indices(self) -> set[int]:
        """The job indices being revised right now (for a 'revising…' badge in the tray)."""
        with self._results_lock:
            return set(self._refining)

    # -- force (build anyway, overriding the PII block) ---------------------

    def force(self, index: int) -> None:
        """Re-build ONE pii-blocked job, overriding the outbound-PII guard — the tray's
        "Build anyway", the batch analogue of the chat's "Send anyway".

        A blocked job created no notebook (pre-flight) or had its skeleton discarded
        (session-held), so a fresh one is minted, and the build auto-confirms the held
        brief — forwarding it verbatim regardless of the PII kind. It runs even after a
        ``block_batch`` aborted the run (its own never-set cancel event), while a repo
        switch / shutdown still stops it via the closed-queue guard. A forced build that
        produces nothing restores the blocked state so the button is offered again."""
        if self._closed or self._executor is None:
            raise BatchError("This batch queue is closed.")
        with self._results_lock:
            if not 0 <= index < len(self._results):
                raise BatchError("No such job.")
            prev = self._results[index]
            if prev.status != "pii_blocked":
                raise BatchError("This job isn't blocked.")
            if index in self._forcing:
                raise BatchError("This job is already building.")
            self._forcing.add(index)
            self._forcing_prev[index] = prev
        try:
            notebook_rel = self._make_notebook(
                prev.job.name.strip() or _name_from_brief(prev.job.brief)
            )
        except (ValueError, OSError) as exc:
            with self._results_lock:
                self._forcing.discard(index)
                self._forcing_prev.pop(index, None)
            raise BatchError(f"Could not create the notebook: {exc}") from exc
        with self._cond:
            self._pending += 1
        try:
            future = self._executor.submit(self._run_force, index, prev.job, notebook_rel)
        except RuntimeError:  # pool shut down by a concurrent close
            self._discard(notebook_rel)
            with self._results_lock:
                self._forcing.discard(index)
                self._forcing_prev.pop(index, None)
            with self._cond:
                self._pending -= 1
                self._cond.notify_all()
            raise BatchError("This batch queue is closed.")
        future.add_done_callback(lambda f, i=index, nb=notebook_rel: self._on_force_done(f, i, nb))
        self._emit(
            index=index, name=prev.job.name, status="building", notebook=notebook_rel, n_proposals=0
        )

    def _run_force(self, index: int, job: BatchJob, notebook_rel: str) -> BatchResult:
        # Build WITHOUT storing — _on_force_done adopts only a built result, and uses its
        # OWN never-set abort so a block_batch-aborted run can still honour the override.
        return self._build(index, job, notebook_rel, force_pii=True, abort=threading.Event())

    def _on_force_done(self, future, index: int, notebook_rel: str) -> None:
        with self._results_lock:
            self._forcing.discard(index)
            prev = self._forcing_prev.pop(index, None)
        try:
            result = None if future.cancelled() else future.result()
        except Exception:  # noqa: BLE001  # defensive; _build is meant to catch
            result = None
        if result is not None and result.status == "built":
            self._commit(index, result)  # adopt the forced proposal (keeps its notebook)
        else:
            # Nothing usable — bin the skeleton this build created and restore the blocked
            # state (annotated) so the tray's "Build anyway" is offered again.
            self._discard(notebook_rel)
            if prev is not None:
                why = "" if result is None else (result.error or "nothing was proposed")
                note = f"Build anyway didn't produce a notebook: {why}" if why else ""
                with self._results_lock:
                    if note and 0 <= index < len(self._results) and self._results[index] is prev:
                        prev.error = note
                self._emit(
                    index=index,
                    name=prev.job.name,
                    status=prev.status,
                    notebook=prev.notebook_rel,
                    n_proposals=len(prev.proposals),
                    error=prev.error,
                )
        with self._cond:
            self._pending -= 1
            self._cond.notify_all()

    def forcing_indices(self) -> set[int]:
        """Job indices being force-rebuilt right now (for a 'building…' badge in the tray)."""
        with self._results_lock:
            return set(self._forcing)

    def _discard(self, notebook_rel: str | None) -> None:
        """Best-effort remove a skeleton (the discard hook is optional)."""
        if notebook_rel and self._discard_notebook is not None:
            with contextlib.suppress(Exception):
                self._discard_notebook(notebook_rel)

    def _drive(
        self,
        index: int,
        job: BatchJob,
        notebook_rel: str,
        session,
        *,
        force_pii: bool = False,
        abort: threading.Event,
    ) -> BatchResult:
        # Drives one session to a terminal result and RETURNS it (does NOT store) — the
        # caller (_run_job commits it; _run_refine / _run_force stage it). Progress is
        # emitted live. ``abort`` is this build's cancel event (the batch-wide one, or a
        # forced build's private never-set one). ``force_pii`` means the analyst chose
        # "Build anyway": a held brief is auto-confirmed (forwarded verbatim) rather than
        # blocking the job, exactly like the chat's "Send anyway".
        q = session.subscribe()
        deadline = time.monotonic() + max(1, self._cfg.job_timeout)
        if not self._await_ready(session, q, deadline, abort):
            return self._make_result(
                job, notebook_rel, "failed", error="The assistant did not become ready in time."
            )
        try:
            session.send(job.brief, "")
        except Exception as exc:  # noqa: BLE001
            return self._make_result(job, notebook_rel, "failed", error=str(exc))

        proposals: list[dict] = []
        forced_pii: list[dict] = []  # findings the analyst overrode, carried onto the result
        follow_ups = max(0, self._cfg.follow_up_turns)
        while True:
            if abort.is_set():
                status = "built" if proposals else "not_run"
                return self._make_result(
                    job,
                    notebook_rel,
                    status,
                    proposals=proposals,
                    error="" if proposals else "Batch cancelled.",
                    pii=forced_pii,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "built" if proposals else "failed"
                return self._make_result(
                    job,
                    notebook_rel,
                    status,
                    proposals=proposals,
                    error="" if proposals else "Timed out before the assistant proposed anything.",
                    pii=forced_pii,
                )
            try:
                event = q.get(timeout=min(remaining, _POLL))
            except queue.Empty:
                continue
            kind = getattr(event, "kind", "")
            data = getattr(event, "data", {}) or {}
            if kind == "proposal":
                proposals.append(data)
                self._emit(
                    index=index,
                    name=job.name,
                    status="building",
                    notebook=notebook_rel,
                    n_proposals=len(proposals),
                )
            elif kind == "pii":
                # A tokened "pii" event means the session's block-mode guard HELD the
                # brief. Normally there is no human mid-build, so the job is blocked; but
                # if the analyst already chose "Build anyway" (force_pii), confirm the hold
                # so the brief is forwarded verbatim — recording the overridden findings
                # (value-free) so the tray can show "built despite flagged data". A pii
                # event WITHOUT a token is warn-only (the brief already went); keep going.
                token = data.get("token")
                if token and force_pii:
                    forced_pii = list(data.get("findings", []))
                    try:
                        session.send_confirmed(token, "")
                    except Exception as exc:  # noqa: BLE001
                        return self._make_result(
                            job, notebook_rel, "failed", error=str(exc), pii=forced_pii
                        )
                elif token:
                    return self._make_result(
                        job, notebook_rel, "pii_blocked", pii=data.get("findings", [])
                    )
            elif kind == "fail":
                status = "built" if proposals else "failed"
                return self._make_result(
                    job,
                    notebook_rel,
                    status,
                    proposals=proposals,
                    error=str(data.get("text", "") or "The assistant reported an error."),
                    pii=forced_pii,
                )
            elif kind == "idle":
                if follow_ups > 0:
                    follow_ups -= 1
                    try:
                        session.send(_FOLLOW_UP_PROMPT, "")
                    except Exception:  # noqa: BLE001  # finish with whatever we have
                        return self._make_result(
                            job,
                            notebook_rel,
                            "built" if proposals else "empty",
                            proposals=proposals,
                            pii=forced_pii,
                        )
                    continue
                return self._make_result(
                    job,
                    notebook_rel,
                    "built" if proposals else "empty",
                    proposals=proposals,
                    pii=forced_pii,
                )
            elif kind == "closed":
                return self._make_result(
                    job,
                    notebook_rel,
                    "built" if proposals else "failed",
                    proposals=proposals,
                    error="" if proposals else "The session closed before proposing anything.",
                    pii=forced_pii,
                )

    def _await_ready(self, session, q, deadline: float, abort: threading.Event) -> bool:
        """Wait until the (possibly still-starting) session can take a turn. The stub
        and an already-warm session report ready immediately; a real provider session
        announces ``ready`` (or ``fail``) over the stream once its handshake lands."""
        if session.is_ready():
            return True
        while True:
            if abort.is_set():
                return False  # honour cancel during startup, not just after the brief
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                event = q.get(timeout=min(remaining, _POLL))
            except queue.Empty:
                if session.is_ready():
                    return True
                continue
            kind = getattr(event, "kind", "")
            if kind == "ready":
                return True
            if kind in ("fail", "closed"):
                return False
            # Nothing else is emitted before readiness on a real session; ignore.

    # -- helpers ------------------------------------------------------------

    def _policy(self) -> str:
        return "block_batch" if str(self._cfg.pii_policy).strip() == "block_batch" else "block_job"

    def _preflight_pii(self, brief: str) -> tuple[bool, list[dict]]:
        """Deterministic, value-free PII gate on a brief BEFORE any session opens.

        Blocks only on the checksum-validated kinds (card / IBAN / NHS) — the same
        high-confidence set :func:`mooring.ai.egress.scrub_text` will silently drop —
        so a legitimate email or product code in a brief does not abort a batch here;
        the session's own block-mode guard remains the backstop for the rest.
        """
        if not getattr(self._pii, "enabled", False):
            return False, []
        from mooring.ai import egress, pii as pii_mod

        _hold, findings, _scan_error = egress.guard_prompt(brief, enabled=True, block=True)
        hits = [f for f in findings if f.kind in pii_mod.CHECKSUM_KINDS]
        if hits:
            return True, [{"line": f.line, "kind": f.kind} for f in hits]
        return False, []

    def _record(
        self,
        index: int,
        job: BatchJob,
        notebook_rel: str | None,
        status: str,
        *,
        proposals: list[dict] | None = None,
        error: str = "",
        pii: list[dict] | None = None,
    ) -> BatchResult:
        return self._commit(
            index,
            self._make_result(job, notebook_rel, status, proposals=proposals, error=error, pii=pii),
        )

    def _make_result(
        self,
        job: BatchJob,
        notebook_rel: str | None,
        status: str,
        *,
        proposals: list[dict] | None = None,
        error: str = "",
        pii: list[dict] | None = None,
    ) -> BatchResult:
        """Build a BatchResult WITHOUT storing/emitting/discarding — pure, so the caller
        decides whether to adopt it (an initial build always; a revision only if built)."""
        return BatchResult(
            job=job,
            notebook_rel=notebook_rel,
            status=status,
            proposals=list(proposals or []),
            error=error,
            pii=list(pii or []),
        )

    def _commit(self, index: int, result: BatchResult) -> BatchResult:
        """Store a finished result at its reserved slot + emit. A non-built job's empty
        skeleton is discarded (and its path nulled) so a batch doesn't litter the
        workspace — safe because the builder only ever PROPOSED into that fresh file."""
        if result.status != "built" and result.notebook_rel and self._discard_notebook is not None:
            with contextlib.suppress(Exception):
                self._discard_notebook(result.notebook_rel)
            result.notebook_rel = None
        # Stamp a stable id on each proposal so applied-state survives a refine. The hub
        # tracks "applied" by pid, NOT by (job, position): a refine REPLACES the proposal
        # list, and a positional key would let the new proposal inherit the old one's
        # "applied" flag (blocking re-apply). setdefault keeps a kept-prev proposal's id
        # stable across a no-op refine, so it stays applied and can't be double-written.
        for p in result.proposals:
            p.setdefault("pid", secrets.token_urlsafe(6))
        with self._results_lock:
            if 0 <= index < len(self._results):
                self._results[index] = result
        self._emit(
            index=index,
            name=result.job.name,
            status=result.status,
            notebook=result.notebook_rel,
            n_proposals=len(result.proposals),
            error=result.error,
        )
        return result

    def _emit(self, **data) -> None:
        if self._on_progress is None:
            return
        # Carry the live queue depth so a streaming UI can show "N building · M queued"
        # and detect "all caught up" without a separate terminal event.
        data.setdefault("pending", self.pending)
        data.setdefault("total", self.total)
        with contextlib.suppress(Exception):
            self._on_progress(data)
