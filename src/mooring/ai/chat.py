"""Chat-session plumbing shared by the stub (Phase 0) and the real Copilot
session (Phase 1).

A chat session is a fan-out broadcaster: the hub's SSE endpoint ``subscribe()``s
to receive :class:`ChatEvent`s, and ``send()`` feeds a user turn in. The transport
(SSE) lives here; the value-blind context assembler and the outbound scrubbers
live in :mod:`mooring.ai.egress` (the single privacy choke point), re-exported
here as :func:`build_system_context` for back-compat, and the prompt valve is
called as ``egress.guard_prompt`` so every egress routes through one module.
"""

from __future__ import annotations

import contextlib
import queue
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mooring.ai import egress
from mooring.ai.base import AIError

if TYPE_CHECKING:
    from mooring.ai.ner import ModelRef

# Re-exported for backward compatibility — the assembler itself now lives in
# mooring.ai.egress, the single outbound-scrub choke point.
from mooring.ai.egress import build_system_context  # noqa: F401

# Event kinds the frontend understands. "proposal" carries a cell the agent
# suggests; the analyst Applies it (we never inject autonomously). "applied"
# carries the value-free RECEIPT for a write the model made inside its own tool
# call (edit mode — see _apply_edit); it is the same hand-off the proposal card
# used to make, after the fact. "cancelled" says the analyst pressed stop and the
# turn is winding down — the turn's real end still arrives as the usual "idle".
# "pii" carries a value-free outbound-PII warning (and, when the turn is held, a
# confirm token). "traceback" carries a held turn's sanitised rewrite: the preview,
# value-free redaction/PII findings, and the one confirm token — never the raw paste.
_QUEUE_MAX = 1000

# The ONE cancellation wording, shared by the "cancelled" event and by the notice a
# provider loop ends its turn with, so the UI and the transcript agree.
CANCELLED_NOTICE = "(Stopped at your request.)"

# How many ``applied`` receipts a session keeps for replay to a reconnecting subscriber.
# Mirrors hub.sse.MAX_APPLIED_REPLAY: the consumer caps too, so this is belt and braces
# rather than one number two modules depend on being read the same way.
_APPLIED_REPLAY_MAX = 25


def _finding_dicts(findings) -> list[dict]:
    """Value-free serialisation of PII findings for the SSE channel — kinds only."""
    return [{"line": f.line, "kind": f.kind} for f in findings]


@dataclass
class ChatEvent:
    kind: str  # delta | message | proposal | applied | tool | idle | cancelled | error | closed
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _ApplyOutcome:
    """The apply outcome mooring synthesises when the injected applier could not run.

    A SHAPE, not a subclass: the real outcome object belongs to ``app/`` (L3.5) and
    ``ai/`` (L3) may never import it, so both are reached the same way — by reading
    ``.status`` / ``.text`` / ``.is_error``.
    """

    status: str
    text: str
    is_error: bool = True


def _event_payload(outcome) -> dict:
    """The value-free event body an apply outcome carries for the LOCAL UI, if any.

    Duck-typed for the same layering reason as the outcome itself: ``.payload`` is the
    name mooring's own applier uses (:class:`mooring.app.auto_apply.ApplyOutcome`), and
    it is the only one — an ``.data`` alias used to be accepted here for an outcome that
    named the body the other way, but nothing produces one, so it was a second contract
    that could only ever drift from the first. Anything else (or a non-dict) yields
    ``{}`` — the model still gets ``.text``; only the local receipt is thinner. The dict
    is COPIED, so a broadcast can never be mutated after the fact by whoever handed it
    over.
    """
    value = getattr(outcome, "payload", None)
    return dict(value) if isinstance(value, dict) else {}


class ChatBroadcaster:
    """Fan-out of :class:`ChatEvent`s to any number of SSE subscribers.

    Subscribers each get their own bounded queue; a slow/dead subscriber drops
    events rather than back-pressuring the producer (the model loop).
    """

    def __init__(self) -> None:
        self._subs: set[queue.Queue[ChatEvent]] = set()
        self._lock = threading.Lock()
        self._last_active = time.monotonic()
        self._closed = False
        # Startup readiness, so the open response need not BLOCK on a provider's
        # (CLI-spawning, network) session handshake. A plain broadcaster (the stub,
        # the test QuickSession) is ready the instant it is constructed; a real
        # provider session resets this to "starting" and flips it to "ready"/"error"
        # from its loop thread, broadcasting a "ready"/"fail" event the SSE endpoint
        # also REPLAYS on connect (so a subscriber that attaches mid-startup, or
        # after it finished, still learns the outcome). See _mark_ready/_mark_start_error.
        self._start_state = "ready"  # "ready" | "starting" | "error"
        self._start_error_text = ""
        # A machine-readable cause for a startup error (e.g. "not_connected"), so a
        # late SSE subscriber and the chat UI can branch on it — offer a sign-in
        # button instead of a dead error string. None for a plain/unknown error.
        self._start_error_reason: str | None = None
        # Outbound-PII guard state (see _pii_gate). Off unless configure_pii says so.
        self._pii_enabled = False
        self._pii_block = True
        # Optional NER name detection (Phase 2) — see mooring.ai.ner. Off unless armed.
        self._pii_names = False
        self._pii_name_labels: tuple[str, ...] | None = None
        self._pii_name_threshold = 0.7
        self._pii_name_model: "ModelRef | str | None" = None
        self._pii_name_backend = "auto"  # resolved to "gliner"/"spacy" by configure_pii
        # NER model readiness, surfaced to the UI via "ner" events (prepare_pii_model):
        # the model downloads in the background on first use; until ready the name
        # pass is skipped (the prompt is still structurally scanned) rather than block.
        self._ner_ready = False
        self._ner_pct = -1
        self._ner_last_data: dict | None = None
        # Traceback guard state (see _traceback_hold). Off unless
        # configure_traceback_guard arms it; the workspace bounds the sanitiser's
        # source-line re-read, and the notebook feeds the known-token rescue.
        self._tb_enabled = False
        self._tb_workspace: Path | None = None
        self._tb_notebook_rel = ""
        self._pending: dict[str, str] = {}  # confirm-token -> held prompt text
        # The live-kernel schema the model has last been shown (the system-context
        # snapshot at open, then the most recent per-turn refresh). A turn re-injects
        # the live schema only when this changes — see _live_prefix.
        self._last_live_schema = ""
        # Callables run once when the session closes. The one caller today is the
        # investigate fan-out: work that OUTLIVES a turn (read-only sub-agent sessions on
        # their own pool) must stop when the chat is closed / idle-reaped / repo-switched,
        # or it runs to its branch timeout, burning spend the analyst tried to cancel.
        self._close_hooks: list = []
        # The analyst's stop button. An Event because the hub route that sets it runs on
        # Starlette's event loop while the turn runs on a worker/loop thread — see
        # request_cancel for why cancellation is a FLAG rather than an exception. Its own
        # lock (not the subscriber lock, which _broadcast holds) makes "raise it, and say
        # so ONCE" one atomic decision rather than a check followed by a set.
        self._cancel = threading.Event()
        self._cancel_lock = threading.Lock()
        # The optional in-turn write capability (see _apply_edit). None is the shipped
        # default and keeps the propose→analyst-Applies path byte-identical.
        self._applier = None
        # Receipts for writes this session has already made, oldest first, so an SSE
        # subscriber that reconnects mid-turn can be shown the changes it missed. Bounded
        # because it is a replay buffer, not a history: the transcript is the history.
        # Only ever the applier's own value-free payload — never a cell, never a value.
        self._applied_replay: list[dict] = []

    @property
    def applied_replay(self) -> list[dict]:
        """The value-free ``applied`` receipts broadcast this session, oldest first.

        Read by :func:`mooring.hub.sse.applied_replay` when a subscriber attaches. An
        EventSource reconnects on its own, and a write that already landed is not a lost
        suggestion — it is a change sitting in the analyst's notebook — so the receipt
        that offers Revert has to survive a dropped stream. The consumer replays only
        payloads carrying a non-empty ``id`` and de-duplicates on it.
        """
        with self._lock:
            return list(self._applied_replay)

    def subscribe(self) -> queue.Queue[ChatEvent]:
        q: queue.Queue[ChatEvent] = queue.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[ChatEvent]) -> None:
        with self._lock:
            self._subs.discard(q)

    def _broadcast(self, event: ChatEvent) -> None:
        # A streamed delta IS activity, so it resets the idle clock. Without this a turn
        # that is visibly producing text still ages toward `ai.chat_idle_timeout_sec`,
        # because only send / tool progress / proposals touched — and the reaper
        # (`hub/routes/chat.py`, on chat open) would then close a session mid-answer.
        # A reasoning model behind a gateway makes that reachable rather than theoretical:
        # its whole think arrives as deltas and nothing else, and the timeout knob's own
        # help text invites raising the request budget past the 900s idle default.
        # Deliberately the `delta` kind only: "text is arriving" is the one event that
        # means the model is working. Cheap enough to sit on the hot path — one clock
        # read, outside the subscriber lock.
        if event.kind == "delta":
            self.touch()
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def touch(self) -> None:
        self._last_active = time.monotonic()

    def emit_job(self, data) -> None:
        """Publish one value-free batch-job lifecycle event — the batch planner's
        progress channel. The PUBLIC entry point for the app layer (it used to
        reach into ``_broadcast`` directly); also touches the activity clock so a
        still-building run is never idle-reaped mid-build."""
        self.touch()
        self._broadcast(ChatEvent("job", data))

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_active

    def add_close_hook(self, fn) -> None:
        """Register a callable to run once when this session closes (best-effort).

        The seam for cancelling work that outlives a turn: an in-flight investigate
        fan-out registers its abort ``Event.set`` here, so closing / idle-reaping /
        repo-switching the chat stops its read-only sub-agents instead of letting each
        run to its full ``branch_timeout``."""
        self._close_hooks.append(fn)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A closing session's turn is over by definition: raise the stop flag (quietly —
        # "closed" is the event that says so) so a tool loop still in flight converges at
        # its next tool boundary instead of running on against a dead session.
        self._cancel.set()
        # Cancel outliving work FIRST, so a blocked tool sees the abort as early as
        # possible; a broken hook must never stop the rest of the teardown.
        for hook in self._close_hooks:
            with contextlib.suppress(Exception):
                hook()
        # Never retain a held (flagged) prompt's plaintext past the session's life.
        self._pending.clear()
        self._broadcast(ChatEvent("closed"))

    # -- startup readiness (so chat-open need not block on the handshake) ----

    @property
    def start_status(self) -> dict | None:
        """The current startup state for a late SSE subscriber to catch up on
        (the hub replays it on connect). ``None`` means there is nothing to wait
        for — but a session that has gone through a real startup reports ``ready``
        too, so a subscriber attaching after the handshake still gets unblocked."""
        if self._start_state == "error":
            data = {"state": "error", "text": self._start_error_text}
            if self._start_error_reason:
                data["reason"] = self._start_error_reason
            return data
        return {"state": self._start_state}  # "ready" | "starting"

    def is_ready(self) -> bool:
        """Whether a turn can be sent now (no provider handshake still pending)."""
        return self._start_state == "ready"

    def _mark_starting(self) -> None:
        self._start_state = "starting"

    def _mark_ready(self) -> None:
        self._start_state = "ready"
        self._broadcast(ChatEvent("ready"))

    def _mark_start_error(self, text: str, reason: str | None = None) -> None:
        self._start_state = "error"
        self._start_error_text = text
        self._start_error_reason = reason
        data = {"text": text}
        if reason:
            data["reason"] = reason
        self._broadcast(ChatEvent("fail", data))

    # -- cancellation (the analyst's stop button) ---------------------------

    def request_cancel(self) -> bool:
        """Ask the turn in flight to stop. Thread-safe, idempotent, never raises.

        Returns whether THIS call is the one that raised the flag — so a subclass with
        more to do on a stop (the Copilot session also asks the SDK to abort) acts
        exactly once without repeating the check-then-act race this method closes.

        Cancellation is a FLAG, not an exception, because the two providers stop in
        different places and only one of the loops is mooring's to break out of:

        * At the TOOL BOUNDARY — the portable mechanism, and the only one that works
          for GitHub Copilot, whose SDK drives its own tool loop from inside. Every
          tool call made after this returns a terminal "the analyst cancelled this;
          stop and reply" error (:func:`mooring.ai.tools.build_tool_specs` takes
          :meth:`cancel_requested` as its ``cancelled`` predicate), which converges the
          model's loop within a call or two instead of letting it run to completion.
        * In the loop mooring OWNS (:class:`mooring.ai.openai_session.OpenAIChatSession`)
          the flag additionally ends the turn at the next checkpoint and closes the open
          stream, so a cancel does not still pay for the whole completion.

        Broadcasts ONE ``cancelled`` event, so the UI can say the turn is stopping the
        moment the analyst asks rather than only once the model gets the message. Exactly
        one: raising the flag and deciding to announce it happen under one lock, because
        two near-simultaneous presses (two browser tabs, a double click) could otherwise
        both see it unset and both announce. The turn's real end still arrives as the
        usual ``idle``, from whichever provider finished it — nothing downstream needs a
        second "turn over" rule.
        """
        self.touch()
        with self._cancel_lock:
            first = not self._cancel.is_set()
            if first:
                self._cancel.set()
        if not first:
            return False
        # Outside the lock on purpose: _broadcast takes the subscriber lock, and a
        # subscriber's queue is not this method's business to hold a lock across.
        self._broadcast(ChatEvent("cancelled", {"text": CANCELLED_NOTICE}))
        return True

    def cancel_requested(self) -> bool:
        """Whether a stop is pending for the CURRENT turn — the tool-boundary predicate."""
        return self._cancel.is_set()

    def clear_cancel(self) -> None:
        """Re-arm for the next turn.

        Called at the START of every turn, never at the end: a cancel belongs to the
        turn it stopped, and a flag left standing would silently kill every turn after
        it (each tool call refusing to run, for a session the analyst thinks is live).
        """
        self._cancel.clear()

    # -- the model's own write (edit mode) ----------------------------------

    def _apply_edit(self, ops, rationale: str = ""):
        """Perform ONE model-driven notebook write and mirror it onto the chat's events.

        Wired into the write tool as ``apply_edit`` only when an applier was injected
        AND the session is not read-only — the same shape as ``emit_proposal``, and for
        the same reason: with no applier the tool stays in propose mode and the analyst
        still clicks Apply, which is what ``[ai] auto_apply = false`` and the policy
        escape hatch both rely on.

        The outcome object comes from ``app/`` and is DUCK-TYPED (``ai/`` is L3, ``app/``
        L3.5 — importing it would invert the layering). It is returned to the tool
        unchanged, so the model reads exactly the observation the applier wrote; what
        happens here is only the LOCAL echo:

        * ``applied`` → an ``applied`` receipt for the browser ("changed cell 3 · revert").
        * ``held``    → the EXISTING ``proposal`` event, so today's hold card appears
          exactly as it always did. There is deliberately no second hold UI.
        * anything else (``conflict`` / ``disabled`` / ``cancelled`` / ``error``) → no
          event at all: the model is told, the analyst is not interrupted by a card for
          a change that did not happen.

        Neither channel carries a data value: the receipt is the applier's own value-free
        payload, and it goes to the local browser, never to the model.

        What a cancel guarantees here, precisely: no NEW write is STARTED once the flag
        is up. The applier reads it before it begins (returning ``cancelled``), and every
        later tool call is refused at the boundary — but a cancel that lands after that
        read, while the write is already in flight, does not un-write it. That is what
        Revert is for, and why one turn's writes share ONE undo checkpoint. "A cancelled
        turn writes nothing" would be the stronger claim, and it is not the one the code
        makes.
        """
        applier = self._applier
        self.touch()  # a long self-correcting turn must never be idle-reaped mid-write
        if applier is None:  # defensive: the tool is only ever built when one exists
            return _ApplyOutcome("disabled", "Editing the notebook is switched off for this chat.")
        try:
            outcome = applier(list(ops or []), rationale or "")
        except Exception:  # noqa: BLE001 - a broken applier must not break the turn
            # Value-free on purpose: whatever the applier failed on is not something we
            # can safely repeat back to the model.
            return _ApplyOutcome("error", "The change could not be applied. Nothing was written.")
        status = str(getattr(outcome, "status", "") or "")
        if status == "applied":
            payload = _event_payload(outcome)
            self._remember_receipt(payload)
            self._broadcast(ChatEvent("applied", payload))
        elif status == "held":
            payload = _event_payload(outcome)
            if payload:
                self._broadcast(ChatEvent("proposal", payload))
        else:
            # conflict / disabled / cancelled / error. The analyst IS told the write did
            # not happen, because the alternative reads as silence: a tool row ending in a
            # cross says something went wrong without saying the notebook is untouched,
            # and "the write failed" and "the write worked and something else broke" are
            # the two readings they most need told apart. Value-free: ``text`` is the same
            # string the model is given, which the applier composes from fixed wording.
            self._broadcast(
                ChatEvent(
                    "apply_failed",
                    {"status": status, "text": str(getattr(outcome, "text", "") or "")},
                )
            )
        return outcome

    def _remember_receipt(self, payload: dict) -> None:
        """Keep one receipt for replay; the newest are kept when the buffer is full."""
        if not isinstance(payload, dict) or not payload:
            return
        with self._lock:
            self._applied_replay.append(payload)
            if len(self._applied_replay) > _APPLIED_REPLAY_MAX:
                del self._applied_replay[:-_APPLIED_REPLAY_MAX]

    # -- outbound PII guard (Channel A) -------------------------------------

    def configure_pii(
        self,
        *,
        enabled: bool,
        block: bool,
        names: bool = False,
        labels: tuple[str, ...] | None = None,
        threshold: float = 0.7,
        model: "ModelRef | str | None" = None,
        backend: str = "auto",
    ) -> None:
        """Arm the prompt guard for this session (called at construction).

        ``names`` (with ``labels``/``threshold``/``model``/``backend``) additionally
        enables the local NER name pass — see :func:`mooring.ai.pii.guard_prompt`.
        ``backend`` is ``"gliner"`` / ``"spacy"`` or ``"auto"``; it is resolved to a
        concrete backend here (only when ``names`` is armed, so a session without
        name detection never imports spaCy just to choose a backend).
        """
        self._pii_enabled = enabled
        self._pii_block = block
        self._pii_names = names
        self._pii_name_labels = labels
        self._pii_name_threshold = threshold
        self._pii_name_model = model
        if names:
            from mooring.ai import ner

            self._pii_name_backend = ner.resolve_backend(backend)
        else:
            self._pii_name_backend = (backend or "auto").strip().lower() or "auto"

    def _pii_gate(self, text: str) -> str | None:
        """THE shared outbound-prompt valve, used by every session class.

        Returns the text to forward, or ``None`` when the turn is HELD pending
        the analyst's confirmation. Two holds share one ``_pending`` token map and
        one confirm path (:meth:`_pii_take` via ``send_confirmed``):

        * a pasted TRACEBACK is sanitised and held with a ``traceback`` event —
          only the SANITISED rewrite is stored, so no code path can forward the
          raw paste (see :meth:`_traceback_hold`);
        * otherwise a PII hit under block mode holds the raw prompt with a
          ``pii`` event; the analyst's "Send anyway" forwards it verbatim.

        The PII scan fails OPEN on a scan error — but LOUD, broadcasting
        ``scan_error`` so the analyst sees the guard did not run.
        """
        if self._tb_enabled and self._traceback_hold(text):
            return None  # held; only the sanitised rewrite exists server-side now
        hold, findings, scan_error = self._scan_prompt(text)
        # Hold takes precedence over a scan error: act on an actionable (structured)
        # finding even when the optional name pass could not run — otherwise enabling
        # detect_names without the extra would silently bypass the structured guard.
        if hold:
            token = _secrets.token_urlsafe(9)
            self._pending[token] = text
            self._broadcast(
                ChatEvent("pii", {"findings": _finding_dicts(findings), "token": token})
            )
            return None
        if findings or scan_error:
            data = {"findings": _finding_dicts(findings)}
            if scan_error:  # fail-open but report WHICH scanner failed (see guard_prompt)
                data["scan_error"] = scan_error
            self._broadcast(ChatEvent("pii", data))
        return text

    def _scan_prompt(self, text: str) -> tuple[bool, list, str]:
        """Run the outbound-PII scan exactly as configured — shared by the plain
        valve and the traceback hold (which scans the SANITISED rewrite)."""
        names = self._pii_names
        if names:
            from mooring.ai import ner

            # Run the optional name pass ONLY when its backend is installed AND the
            # model is ready. Otherwise skip it for this turn rather than letting it
            # raise — the structured scan still runs, so the prompt is NOT unchecked,
            # and the topbar PII badge already shows "PII-partial" before the user
            # sends. (A model still downloading just isn't ready yet; the "ner"
            # prepare status covers that and the badge flips to green when it lands.)
            if not (ner.available(self._pii_name_backend) and self._names_ready()):
                names = False
        return egress.guard_prompt(
            text,
            enabled=self._pii_enabled,
            block=self._pii_block,
            names=names,
            labels=self._pii_name_labels,
            threshold=self._pii_name_threshold,
            model=self._pii_name_model,
            backend=self._pii_name_backend,
        )

    def _pii_take(self, token: str) -> str | None:
        """Pop the prompt held under ``token`` (forwarded verbatim, exactly once)."""
        return self._pending.pop(token, None)

    # -- traceback guard (the value-safe traceback fixer's valve) ------------

    def configure_traceback_guard(
        self, *, enabled: bool, workspace: "Path | str | None" = None, notebook_rel: str = ""
    ) -> None:
        """Arm the traceback sanitise-and-hold valve for this session (called at
        construction, mirroring :meth:`configure_pii`). ``workspace`` bounds the
        sanitiser's source-line re-read to the session's own workspace;
        ``notebook_rel`` feeds the notebook source into the known-token rescue."""
        self._tb_enabled = enabled
        self._tb_workspace = Path(workspace) if workspace is not None else None
        self._tb_notebook_rel = notebook_rel or ""

    def _known_text(self) -> str:
        """Text the model has ALREADY been shown this session (beyond the live
        schema) — the known-token source for the traceback guard's message rescue.
        Session classes return their system context; the base has none."""
        return ""

    def _traceback_known_text(self) -> str:
        # Rescue only from text already delivered to this provider. Re-reading the
        # mutable notebook here lets a post-open literal masquerade as in-channel.
        return "\n".join(
            part for part in (self._last_live_schema, self._known_text()) if part
        )

    def _traceback_hold(self, text: str) -> bool:
        """Sanitise a traceback-bearing prompt and HOLD it. Returns True when held.

        One COMBINED hold: the sanitised rewrite (never the raw paste) goes into
        ``_pending``, the PII scan runs over the SANITISED text so findings in the
        surrounding prose ride the same card, and a single ``traceback`` event
        carries {preview, redactions, pii_findings, token}. The analyst's one
        "Send sanitised" confirm re-enters through the existing ``send_confirmed``
        → :meth:`_pii_take` path, which can only ever forward the sanitised text —
        the raw paste is dropped HERE and never stored.
        """
        result = egress.sanitize_traceback(
            text, workspace=self._tb_workspace, known_text=self._traceback_known_text()
        )
        if not result.detected:
            return False
        pii_hold, findings, scan_error = self._scan_prompt(result.text)
        token = _secrets.token_urlsafe(9)
        self._pending[token] = result.text  # ONLY the sanitised rewrite, by construction
        data = {
            "preview": result.text,
            "redactions": _finding_dicts(result.findings),
            "pii_findings": _finding_dicts(findings),
            # Whether the PII guard (as configured) would have HELD this text on its
            # own — the sanitiser rewrites only the traceback block, so block-mode
            # PII in the surrounding prose must not be auto-confirmable by an
            # unattended consumer (the batch worker keys off this; an interactive
            # confirm already has the analyst looking at the same card).
            "pii_hold": bool(pii_hold),
            "token": token,
        }
        if scan_error:
            data["scan_error"] = scan_error
        self._broadcast(ChatEvent("traceback", data))
        return True

    # -- run-failure report (the applied cell actually blew up) --------------

    # How many failed cells one report names before it says "and N more". A notebook
    # whose graph broke can fail every cell downstream of the applied one; the first
    # few carry the cause, the rest are the same fact restated.
    RUN_REPORT_MAX = 8

    def run_failure_report(self, failures) -> tuple[str, list]:
        """The value-safe message describing a failed run, plus its redaction findings.

        ``failures`` is :func:`mooring.app.notebook_run.failure_lines`' output: pairs of a
        marimo error KIND (a constant from marimo's own closed taxonomy) and that error's
        **raw** message, which can quote a data value exactly as a pasted traceback can.

        The raw message never leaves this method. Each pair is composed into a minimal
        traceback and put through :func:`mooring.ai.egress.sanitize_traceback` — the ONE
        gateway, the same rewrite a pasted traceback gets — so the message survives only
        when it is provably value-free (a fixed interpreter message, or quoted tokens the
        model has already been shown this session: a column name it wrote is rescued,
        ``'ACME Ltd'`` is not) and otherwise becomes ``<redacted: N chars>``.

        Two things make the composition safe rather than merely convenient:

        * The synthetic ``Traceback (most recent call last):`` header exists only to make
          the sanitiser's block detector fire; it carries **no frame line**, so there is no
          path/line for a source re-read to key off and nothing is ever read from disk.
        * The header is then dropped and the surviving line is checked to still begin with
          the KIND constant we composed. An unexpected shape falls back to the kind alone —
          fail closed, in the sanitiser's own spirit.

        Note what this deliberately does NOT consult: ``[ai] traceback_guard``. That knob
        governs the analyst's own **paste** — text they wrote, can see, and may judge safe.
        Here they never see the raw message at all, so there is nothing for an off switch to
        hand back; the rewrite is unconditional.

        Returns ``(text, findings)``; the caller sends ``text`` through the ordinary
        :meth:`send` so the PII valve applies to it like any other turn.
        """
        all_failures = list(failures)
        kept = all_failures[: self.RUN_REPORT_MAX]
        more = len(all_failures) - len(kept)
        lines: list[str] = []
        findings: list = []
        for kind, message in kept:
            result = egress.sanitize_traceback(
                f"Traceback (most recent call last):\n{kind}: {message}",
                workspace=self._tb_workspace,
                known_text=self._traceback_known_text(),
            )
            findings.extend(result.findings)
            rewritten = result.text.splitlines()
            line = rewritten[1].strip() if result.detected and len(rewritten) == 2 else ""
            if not (line == kind or line.startswith(kind + ":")):
                line = f"{kind}: <redacted>"  # fail closed on any shape we didn't compose
            lines.append(line)
        body = "\n".join(lines)
        if more:
            body += f"\n…and {more} more failing cell(s), not shown."
        count = len(all_failures)
        cells = "cell" if count == 1 else "cells"
        return (
            "I applied your change and then ran the notebook locally, top to bottom. "
            f"It did not run clean: {count} {cells} failed.\n\n"
            f"{body}\n\n"
            "This is all mooring can tell you: it reads marimo's error lines only, and "
            "rewrites each message value-safe before showing it to you, so a message that "
            "quoted a data value arrives redacted. There is no cell index and no "
            "traceback — work out which cell it is from the notebook source you can "
            "already see. Re-propose a corrected version.",
            findings,
        )

    # -- live-kernel schema refresh -----------------------------------------

    def set_initial_live_schema(self, text: str) -> None:
        """Seed the live-schema snapshot already folded into the system context at
        chat-open, so the first turn re-injects only if the kernel changed since."""
        self._last_live_schema = (text or "").strip()

    def _live_prefix(self, live_schema_text: str) -> str:
        """A block to PREPEND to a turn when the kernel's dataframes changed since
        the model last saw them — otherwise ``""``.

        ``live_schema_text`` comes from the SAME ``introspect`` probe -> scrub ->
        ``format_live_schemas`` pipeline as the system context (column names +
        dtypes only, never a value), so re-stating it opens no new value channel.
        Updates the stored snapshot when it changes; a held/empty refresh leaves it
        untouched so a later turn still re-injects.
        """
        live = (live_schema_text or "").strip()
        if not live or live == self._last_live_schema:
            return ""
        self._last_live_schema = live
        return (
            "UPDATED LIVE NOTEBOOK DATAFRAMES (schema only) — the kernel changed "
            "since the last message; use this in place of any earlier live-dataframe "
            "list:\n" + live + "\n\n"
        )

    # -- NER model readiness (Phase 2 name detection) -----------------------

    @property
    def ner_status(self) -> dict | None:
        """The latest ``ner`` event payload, so a late SSE subscriber can catch up
        on a download already in progress (the hub replays it on connect)."""
        return self._ner_last_data

    def _set_ner(self, data: dict) -> None:
        self._ner_last_data = data
        self._broadcast(ChatEvent("ner", data))

    def _names_ready(self) -> bool:
        """Whether the NER model is loadable now (no download). Memoized once true."""
        if self._ner_ready:
            return True
        from mooring.ai import ner

        if ner.is_ready(self._pii_name_model, self._pii_name_backend):
            self._ner_ready = True
        return self._ner_ready

    def prepare_pii_model(self) -> None:
        """When name detection is armed, make the model ready in the background and
        report progress over the chat via ``ner`` events. Best-effort; never raises.

        Moves the (potentially large, one-time) model download out of the first chat
        turn — where it would hang silently — into a visible, streamed prepare step.
        """
        if not (self._pii_enabled and self._pii_names):
            return
        from mooring.ai import ner

        if not ner.available(self._pii_name_backend):
            return  # the prompt path surfaces scan_error loudly when the extra is missing
        if self._names_ready():
            # already present — warm the in-process load so the first prompt is snappy
            threading.Thread(target=self._warm_ner, name="ner-warm", daemon=True).start()
            return
        if self._pii_name_backend == "spacy":
            return  # spaCy models are install-time, never fetched at runtime — nothing to prepare
        mid = self._pii_name_model

        def run() -> None:
            self._ner_pct = -1
            self._set_ner({"state": "downloading"})
            try:
                ner.download_model(mid, on_progress=self._on_ner_progress)
                ner.load_model(mid)
                self._ner_ready = True
                self._set_ner({"state": "ready"})
            except Exception:  # noqa: BLE001  # report, never crash the session
                self._set_ner({"state": "error"})

        threading.Thread(target=run, name="ner-prepare", daemon=True).start()

    def _warm_ner(self) -> None:
        try:
            if self._pii_name_backend == "spacy":
                from mooring.ai import ner_spacy

                ner_spacy.load(
                    self._pii_name_model if isinstance(self._pii_name_model, str) else ""
                )
            else:
                from mooring.ai import ner

                ner.load_model(self._pii_name_model)
            self._ner_ready = True
        except Exception:  # noqa: BLE001  # best-effort warm-up
            pass

    def _on_ner_progress(self, done: int, total: int) -> None:
        if not total:
            return
        pct = int(done * 100 / total)
        if pct == self._ner_pct:
            return  # throttle to whole-percent changes so we don't flood SSE
        self._ner_pct = pct
        self._set_ner({"state": "downloading", "pct": pct})


class StubChatSession(ChatBroadcaster):
    """A no-LLM stand-in used in Phase 0 to prove the chat → Apply → run loop.

    It echoes the user's turn and proposes a fixed, schema-agnostic cell, so the
    whole pipeline (SSE streaming + the Apply→/api/kernel/run injection) can be
    exercised without the Copilot SDK or the org policy.
    """

    def __init__(
        self,
        *,
        system_context: str = "",
        pii_enabled: bool = False,
        pii_block: bool = True,
        pii_names: bool = False,
        pii_name_labels: tuple[str, ...] | None = None,
        pii_name_threshold: float = 0.7,
        pii_name_model: str | None = None,
        pii_name_backend: str = "auto",
        traceback_guard: bool = False,
        workspace: "Path | str | None" = None,
        notebook_rel: str = "",
    ) -> None:
        super().__init__()
        self.system_context = system_context  # stored so tests can prove it's value-free
        self.last_sent = ""  # exact text forwarded, incl. any live-schema prefix (tests)
        self.configure_pii(
            enabled=pii_enabled,
            block=pii_block,
            names=pii_names,
            labels=pii_name_labels,
            threshold=pii_name_threshold,
            model=pii_name_model,
            backend=pii_name_backend,
        )
        self.configure_traceback_guard(
            enabled=traceback_guard, workspace=workspace, notebook_rel=notebook_rel
        )

    def _known_text(self) -> str:
        return self.system_context

    def send(self, text: str, live_schema_text: str = "") -> None:
        self.touch()
        gated = self._pii_gate(text)
        if gated is None:
            return  # held pending the analyst's confirmation
        self.last_sent = self._live_prefix(live_schema_text) + gated
        self._reply()

    def send_confirmed(self, token: str, live_schema_text: str = "") -> None:
        self.touch()
        text = self._pii_take(token)
        if text is None:
            raise AIError("That message has expired — please retype it.")
        self.last_sent = self._live_prefix(live_schema_text) + text
        self._reply()

    def _reply(self) -> None:
        reply = "Here is a cell that summarises the dataframe:"
        for word in reply.split():
            self._broadcast(ChatEvent("delta", {"text": word + " "}))
        code = "summary = df.describe()\nsummary"
        self._broadcast(ChatEvent("message", {"text": reply}))
        self._broadcast(
            ChatEvent("proposal", {"code": code, "rationale": "describe the dataframe"})
        )
        self._broadcast(ChatEvent("idle"))
