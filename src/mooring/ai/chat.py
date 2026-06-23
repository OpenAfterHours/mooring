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

import queue
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field

from mooring.ai import egress
from mooring.ai.base import AIError

# Re-exported for backward compatibility — the assembler itself now lives in
# mooring.ai.egress, the single outbound-scrub choke point.
from mooring.ai.egress import build_system_context  # noqa: F401

# Event kinds the frontend understands. "proposal" carries a cell the agent
# suggests; the analyst Applies it (we never inject autonomously). "pii" carries
# a value-free outbound-PII warning (and, when the turn is held, a confirm token).
_QUEUE_MAX = 1000


def _finding_dicts(findings) -> list[dict]:
    """Value-free serialisation of PII findings for the SSE channel — kinds only."""
    return [{"line": f.line, "kind": f.kind} for f in findings]


@dataclass
class ChatEvent:
    kind: str  # delta | message | proposal | tool | idle | error | closed
    data: dict = field(default_factory=dict)


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
        self._pii_name_model: str | None = None
        self._pii_name_backend = "auto"  # resolved to "gliner"/"spacy" by configure_pii
        # NER model readiness, surfaced to the UI via "ner" events (prepare_pii_model):
        # the model downloads in the background on first use; until ready the name
        # pass is skipped (the prompt is still structurally scanned) rather than block.
        self._ner_ready = False
        self._ner_pct = -1
        self._ner_last_data: dict | None = None
        self._pending: dict[str, str] = {}  # confirm-token -> held prompt text
        # The live-kernel schema the model has last been shown (the system-context
        # snapshot at open, then the most recent per-turn refresh). A turn re-injects
        # the live schema only when this changes — see _live_prefix.
        self._last_live_schema = ""

    def subscribe(self) -> queue.Queue[ChatEvent]:
        q: queue.Queue[ChatEvent] = queue.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[ChatEvent]) -> None:
        with self._lock:
            self._subs.discard(q)

    def _broadcast(self, event: ChatEvent) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def touch(self) -> None:
        self._last_active = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_active

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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

    # -- outbound PII guard (Channel A) -------------------------------------

    def configure_pii(
        self,
        *,
        enabled: bool,
        block: bool,
        names: bool = False,
        labels: tuple[str, ...] | None = None,
        threshold: float = 0.7,
        model: str | None = None,
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
        the analyst's confirmation (a ``pii`` event carrying a value-free finding
        list and a one-time ``token`` is broadcast; the analyst's "Send anyway"
        re-enters via :meth:`_pii_take`). Fails OPEN on a scan error — but LOUD,
        broadcasting ``scan_error`` so the analyst sees the guard did not run.
        """
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
        hold, findings, scan_error = egress.guard_prompt(
            text,
            enabled=self._pii_enabled,
            block=self._pii_block,
            names=names,
            labels=self._pii_name_labels,
            threshold=self._pii_name_threshold,
            model=self._pii_name_model,
            backend=self._pii_name_backend,
        )
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

    def _pii_take(self, token: str) -> str | None:
        """Pop the prompt held under ``token`` (forwarded verbatim, exactly once)."""
        return self._pending.pop(token, None)

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
            except Exception:  # noqa: BLE001 - report, never crash the session
                self._set_ner({"state": "error"})

        threading.Thread(target=run, name="ner-prepare", daemon=True).start()

    def _warm_ner(self) -> None:
        try:
            if self._pii_name_backend == "spacy":
                from mooring.ai import ner_spacy

                ner_spacy.load(self._pii_name_model if isinstance(self._pii_name_model, str) else "")
            else:
                from mooring.ai import ner

                ner.load_model(self._pii_name_model)
            self._ner_ready = True
        except Exception:  # noqa: BLE001 - best-effort warm-up
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
