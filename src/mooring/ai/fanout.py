"""Shared fan-out drive helpers — running one value-blind session to a terminal outcome.

Extracted from the batch orchestrator's drive loop (:meth:`mooring.ai.batch.BatchPlanner._drive`)
so a second fan-out coordinator (:mod:`mooring.ai.investigate`) reuses the SAME
readiness/timeout logic and the SAME PII / traceback value-safety branches instead of
re-implementing them. It is deliberately outcome-agnostic at the boundary:
:func:`drive_to_finding` collects a READ-ONLY session's final assistant MESSAGE (the
investigate case); the batch planner keeps its own proposal-collecting drive for now (a
later phase migrates it onto this module — see docs/developers and the investigate spec).

Pure + leaf: imports only stdlib + ``ai.egress`` / ``ai.pii`` (both ai/ leaves), never the
hub, a provider, or marimo. Every event a session emits is value-free by construction; this
module only decides WHEN to stop and whether a checksum-PII sub-question blocks a branch.
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field

_POLL = 0.5  # seconds between wall-clock deadline checks while draining a session


@dataclass
class BranchOutcome:
    """The value-free result of driving one branch session to a terminal state.

    ``status`` is one of ``finding`` (collected a non-empty answer), ``empty`` (the
    session went idle with nothing), ``failed`` (startup/send/timeout/error),
    ``pii_blocked`` (a block-mode hold — unattended, never auto-confirmed), or
    ``cancelled``. ``finding`` is the value-free answer text; ``pii`` are value-free
    ``(line, kind)`` findings when blocked."""

    status: str
    finding: str = ""
    error: str = ""
    pii: list[dict] = field(default_factory=list)


def await_ready(session, q, deadline: float, abort) -> bool:
    """Wait until a (possibly still-starting) session can take a turn.

    A warm session / stub reports ready immediately; a real provider session announces
    ``ready`` (or ``fail`` / ``closed``) over the stream once its handshake lands. Lifted
    verbatim from the batch planner so both coordinators share one readiness gate."""
    if session.is_ready():
        return True
    while True:
        if abort.is_set():
            return False
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


def preflight_pii(text: str, pii_cfg) -> tuple[bool, list[dict]]:
    """Deterministic, value-free PII gate on a sub-question BEFORE any session opens.

    Blocks only on the checksum-validated kinds (card / IBAN / NHS) — the same
    high-confidence set :func:`mooring.ai.egress.scrub_text` silently drops — so a
    legitimate email or product code does not block a branch; the session's own
    block-mode guard is the backstop for the rest. Identical policy to the batch
    planner's ``_preflight_pii`` (this is the shared home for it)."""
    if not getattr(pii_cfg, "enabled", False):
        return False, []
    from mooring.ai import egress, pii as pii_mod

    _hold, findings, _scan_error = egress.guard_prompt(text, enabled=True, block=True)
    hits = [f for f in findings if f.kind in pii_mod.CHECKSUM_KINDS]
    if hits:
        return True, [{"line": f.line, "kind": f.kind} for f in hits]
    return False, []


def drive_to_finding(session, brief: str, *, deadline: float, abort) -> BranchOutcome:
    """Drive one READ-ONLY session to a terminal :class:`BranchOutcome`, collecting its
    FINAL assistant message as the finding.

    Reuses the batch drive loop's readiness / deadline / abort handling and its ``pii`` /
    ``traceback`` value-safety branches, but collects the ``message`` event (falling back
    to accumulated ``delta`` text) instead of proposals, and terminates on the first
    ``idle``. A sub-agent is UNATTENDED, so a block-mode PII hold blocks the branch (it is
    never auto-confirmed); a PII-clean traceback hold is auto-confirmed so a stray guard
    hold can't hang the branch to its deadline."""
    q = session.subscribe()
    if not await_ready(session, q, deadline, abort):
        return BranchOutcome("failed", error="The assistant did not become ready in time.")
    try:
        session.send(brief, "")
    except Exception as exc:  # noqa: BLE001 - surface as a failed branch, never crash the pool
        return BranchOutcome("failed", error=str(exc))

    message = ""
    deltas: list[str] = []

    def _text() -> str:
        return message or "".join(deltas)

    while True:
        if abort.is_set():
            return BranchOutcome("cancelled", finding=_text())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            text = _text()
            return BranchOutcome(
                "finding" if text.strip() else "failed",
                finding=text,
                error="" if text.strip() else "Timed out before the assistant answered.",
            )
        try:
            event = q.get(timeout=min(remaining, _POLL))
        except queue.Empty:
            continue
        kind = getattr(event, "kind", "")
        data = getattr(event, "data", {}) or {}
        if kind == "delta":
            deltas.append(str(data.get("text", "")))
        elif kind == "message":
            message = str(data.get("text", "")) or message
        elif kind == "pii":
            # A tokened hold under block mode: no human at a sub-agent, so block the
            # branch (never auto-confirm). A warn-only "pii" event (no token) is ignored.
            if data.get("token"):
                return BranchOutcome("pii_blocked", pii=list(data.get("findings", [])))
        elif kind == "traceback":
            # The guard sanitised+held a traceback. A PII hold on the sanitised text
            # blocks (unattended); an otherwise-clean hold is auto-confirmed so the
            # branch continues rather than hanging to the deadline.
            token = data.get("token")
            if token:
                if data.get("pii_hold"):
                    return BranchOutcome("pii_blocked", pii=list(data.get("pii_findings", [])))
                try:
                    session.send_confirmed(token, "")
                except Exception as exc:  # noqa: BLE001
                    return BranchOutcome("failed", error=str(exc))
        elif kind == "idle":
            text = _text()
            return BranchOutcome("finding" if text.strip() else "empty", finding=text)
        elif kind == "fail":
            text = _text()
            return BranchOutcome(
                "finding" if text.strip() else "failed",
                finding=text,
                error=str(data.get("text", "") or "The assistant reported an error."),
            )
        elif kind == "closed":
            text = _text()
            return BranchOutcome(
                "finding" if text.strip() else "failed",
                finding=text,
                error="" if text.strip() else "The session closed before answering.",
            )
