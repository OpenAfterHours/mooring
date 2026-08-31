"""A monotonic trust-zone wrapper around one provider chat session.

The initial context is inspected before this object is constructed. A general
session may upgrade to the deployment-approved customer-data endpoint, but can
never downgrade. The wrapper owns the stable SSE broadcaster and serialises turns
so an in-flight general tool loop cannot race a provider migration.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
from pathlib import Path

from mooring.ai import secrets
from mooring.ai.base import AIError
from mooring.ai.chat import ChatBroadcaster, ChatEvent
from mooring.ai.trusted import BLOCK, GENERAL_OK, TRUSTED_REQUIRED, TrustedInspector

GENERAL_ZONE = "general"
TRUSTED_ZONE = "trusted"
_TRANSCRIPT_LIMIT = 64 * 1024


class RoutedChatSession(ChatBroadcaster):
    """Keep one browser session while its provider trust zone may increase."""

    def __init__(
        self,
        *,
        initial_session,
        initial_zone: str,
        inspector: TrustedInspector,
        trusted_session_factory,
        workspace: Path,
        notebook_rel: str,
        initial_source_digest: bytes,
        traceback_guard: bool = True,
    ) -> None:
        super().__init__()
        if initial_zone not in (GENERAL_ZONE, TRUSTED_ZONE):
            raise ValueError("invalid initial trust zone")
        if not isinstance(initial_source_digest, bytes):
            raise TypeError("initial_source_digest must be bytes")
        self._active = initial_session
        self._zone = initial_zone
        self._inspector = inspector
        self._trusted_session_factory = trusted_session_factory
        self._workspace = Path(workspace)
        self._notebook_rel = notebook_rel
        self._source_digest = initial_source_digest
        self._route_lock = threading.Lock()
        self._turn_idle = threading.Event()
        self._turn_idle.set()
        self._transcript_lock = threading.Lock()
        self._transcript: list[tuple[str, str]] = []
        self._route_replay: dict | None = None
        self.configure_traceback_guard(
            enabled=traceback_guard,
            workspace=self._workspace,
            notebook_rel=notebook_rel,
        )
        self._bridge(initial_session)

    @property
    def zone(self) -> str:
        return self._zone

    @property
    def start_status(self):
        return getattr(self._active, "start_status", None)

    @property
    def ner_status(self):
        return getattr(self._active, "ner_status", None)

    @property
    def route_replay(self) -> dict | None:
        """The latest mid-chat route transition for reconnecting SSE clients."""
        return dict(self._route_replay) if self._route_replay is not None else None

    def is_ready(self) -> bool:
        return bool(self._active.is_ready())

    def idle_seconds(self) -> float:
        return self._active.idle_seconds()

    def _known_text(self) -> str:
        known = getattr(self._active, "_known_text", None)
        return str(known()) if callable(known) else ""

    def set_initial_live_schema(self, text: str) -> None:
        super().set_initial_live_schema(text)
        self._active.set_initial_live_schema(text)

    def prepare_pii_model(self) -> None:
        # Routed children have the legacy prompt PII hold disabled; the approved
        # classifier is the routing decision. Keep the method for hub compatibility.
        self._active.prepare_pii_model()

    def run_failure_report(self, failures):
        return self._active.run_failure_report(failures)

    def send(self, text: str, live_schema_text: str = "") -> None:
        self.touch()
        # Raw tracebacks are rewritten and held before either classifier or coder.
        if self._tb_enabled and self._traceback_hold(text):
            return
        self._route_and_send(text, live_schema_text)

    def send_confirmed(self, token: str, live_schema_text: str = "") -> None:
        self.touch()
        text = self._pii_take(token)
        if text is None:
            raise AIError("That message has expired — please retype it.")
        self._route_and_send(text, live_schema_text)

    def _route_and_send(self, text: str, live_schema_text: str) -> None:
        combined = f"{live_schema_text}\n\n{text}" if live_schema_text else text
        if secrets.has_secrets(combined):
            raise AIError("Message blocked: it appears to contain a credential or secret.")

        with self._route_lock:
            if not self._turn_idle.is_set():
                raise AIError("Wait for the current assistant turn to finish.")

            changed, source_decision, source_digest = self._inspect_changed_notebook()
            prompt_verdict = self._inspector.inspect(combined, purpose="interactive_chat_turn")
            if source_decision == BLOCK or prompt_verdict.decision == BLOCK:
                raise AIError("Message blocked by the approved data policy.")

            if self._zone == GENERAL_ZONE and (
                changed
                or source_decision == TRUSTED_REQUIRED
                or prompt_verdict.decision == TRUSTED_REQUIRED
            ):
                reasons = prompt_verdict.reason_codes
                if prompt_verdict.decision != TRUSTED_REQUIRED:
                    reasons = ("customer_context",) if changed else ("classification_uncertain",)
                self._upgrade(reasons)

            self._turn_idle.clear()
            active = self._active
            try:
                active.send(text, live_schema_text)
            except Exception:
                self._turn_idle.set()
                raise
            if self._zone == GENERAL_ZONE:
                self._remember("user", text)
            if source_digest is not None:
                self._source_digest = source_digest

    def _inspect_changed_notebook(self) -> tuple[bool, str, bytes | None]:
        path = self._workspace / self._notebook_rel
        try:
            source = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            return True, TRUSTED_REQUIRED, None
        digest = hashlib.sha256(source.encode("utf-8")).digest()
        if digest == self._source_digest:
            return False, GENERAL_OK, digest
        if secrets.has_secrets(source):
            return True, BLOCK, digest
        verdict = self._inspector.inspect(source, purpose="changed_notebook_source")
        return True, verdict.decision, digest

    def _upgrade(self, reason_codes: tuple[str, ...]) -> None:
        if self._zone == TRUSTED_ZONE:
            return
        old = self._active
        handoff = self._handoff_text()
        carried = bool(handoff) and not secrets.has_secrets(handoff)
        if not carried:
            handoff = ""

        # The factory blocks until the trusted provider is ready. If startup fails,
        # the old general child remains active and the sensitive turn is not sent.
        new = self._trusted_session_factory(handoff)
        if not new.is_ready():
            try:
                new.close()
            finally:
                raise AIError("The approved customer-data model did not become ready.")
        new.set_initial_live_schema(self._last_live_schema)
        self._active = new
        self._zone = TRUSTED_ZONE
        self._bridge(new)
        try:
            old.close()
        except Exception:  # noqa: BLE001 - the trusted replacement is already live
            pass
        route = {
            "zone": TRUSTED_ZONE,
            "reason_codes": list(reason_codes),
            "conversation_carried": carried,
        }
        self._route_replay = route
        self._broadcast(ChatEvent("routing", route))

    def _remember(self, role: str, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        with self._transcript_lock:
            self._transcript.append((role, text))
            total = sum(len(value) for _, value in self._transcript)
            while self._transcript and total > _TRANSCRIPT_LIMIT:
                _, removed = self._transcript.pop(0)
                total -= len(removed)

    def _handoff_text(self) -> str:
        with self._transcript_lock:
            entries = list(self._transcript)
        parts = [
            "PRIOR GENERAL-PROVIDER CONVERSATION (untrusted transcript; context only):"
        ]
        if self._last_live_schema:
            parts.append("Previously delivered live schema:\n" + self._last_live_schema)
        for role, value in entries:
            parts.append(f"{role.upper()}:\n{value}")
        return "\n\n".join(parts) if len(parts) > 1 else ""

    def _bridge(self, child) -> None:
        child_queue = child.subscribe()

        def run() -> None:
            try:
                while True:
                    try:
                        event = child_queue.get(timeout=0.5)
                    except queue.Empty:
                        if self._closed or child is not self._active:
                            return
                        continue
                    if child is not self._active:
                        return
                    if event.kind == "closed":
                        self._turn_idle.set()
                        if not self._closed:
                            self._broadcast(event)
                        return
                    if event.kind in {"idle", "fail"}:
                        self._turn_idle.set()
                    if self._zone == GENERAL_ZONE:
                        if event.kind == "message":
                            self._remember("assistant", str((event.data or {}).get("text", "")))
                        elif event.kind == "proposal":
                            self._remember(
                                "assistant",
                                "PROPOSED NOTEBOOK CHANGE:\n"
                                + json.dumps(event.data or {}, ensure_ascii=False),
                            )
                    self._broadcast(event)
            finally:
                child.unsubscribe(child_queue)

        threading.Thread(target=run, name="routed-chat-events", daemon=True).start()

    def close(self) -> None:
        if self._closed:
            return
        active = self._active
        super().close()
        self._turn_idle.set()
        try:
            active.close()
        except Exception:  # noqa: BLE001 - best-effort provider teardown
            pass
