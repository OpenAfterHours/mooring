"""Chat-session plumbing shared by the stub (Phase 0) and the real Copilot
session (Phase 1).

A chat session is a fan-out broadcaster: the hub's SSE endpoint ``subscribe()``s
to receive :class:`ChatEvent`s, and ``send()`` feeds a user turn in. The transport
(SSE) and the value-blind context-building live here so both session kinds share
one contract — and one privacy choke point (:func:`build_system_context`).
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

# Event kinds the frontend understands. "proposal" carries a cell the agent
# suggests; the analyst Applies it (we never inject autonomously).
_QUEUE_MAX = 1000


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
        self._broadcast(ChatEvent("closed"))


class StubChatSession(ChatBroadcaster):
    """A no-LLM stand-in used in Phase 0 to prove the chat → Apply → run loop.

    It echoes the user's turn and proposes a fixed, schema-agnostic cell, so the
    whole pipeline (SSE streaming + the Apply→/api/kernel/run injection) can be
    exercised without the Copilot SDK or the org policy.
    """

    def __init__(self, *, system_context: str = "") -> None:
        super().__init__()
        self.system_context = system_context  # stored so tests can prove it's value-free

    def send(self, text: str) -> None:
        self.touch()
        reply = "Here is a cell that summarises the dataframe:"
        for word in reply.split():
            self._broadcast(ChatEvent("delta", {"text": word + " "}))
        code = "summary = df.describe()\nsummary"
        self._broadcast(ChatEvent("message", {"text": reply}))
        self._broadcast(
            ChatEvent("proposal", {"code": code, "rationale": "describe the dataframe"})
        )
        self._broadcast(ChatEvent("idle"))


def build_system_context(*, schema_text: str, notebook_source: str, notebook_rel: str) -> str:
    """Assemble the value-blind context handed to the assistant.

    THE PRIVACY CHOKE POINT for chat context. Only two things go in: the dataset
    SCHEMA (column names + dtypes from ``schema.format_for_ai`` — never a value)
    and the notebook's `.py` SOURCE (code; data is loaded at runtime, so the
    source holds no values). Nothing here may carry a cell output or data value.
    """
    parts = [
        "You are a careful data-analysis coding assistant inside a financial "
        "institution's notebook tool. You help an analyst write code for a marimo "
        "(Python) notebook, using Polars (imported as `pl`).",
        "STRICT PRIVACY RULES:",
        "- You are given ONLY the dataset SCHEMA (column names and types) and the "
        "notebook SOURCE. For privacy/regulatory reasons you can NEVER see the "
        "actual data values, and must not ask for them or try to read any file.",
        "- When you propose code, return it in a ```python fenced block so the "
        "analyst can apply it to the notebook.",
    ]
    if schema_text.strip():
        parts.append("DATASET SCHEMA:\n" + schema_text.strip())
    parts.append(f"CURRENT NOTEBOOK ({notebook_rel}) SOURCE:\n{notebook_source.strip()}")
    return "\n\n".join(parts)
