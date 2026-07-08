"""A long-lived, streaming OpenAI chat session — the copilot on Chat Completions.

The OpenAI Chat Completions API is stateless (message-in / message-out) and runs
NO tool loop of its own, so — unlike :class:`mooring.ai.session.CopilotChatSession`,
which only observes the Copilot agent's events — this session OWNS the multi-turn
agent loop. It keeps its own ``messages`` list (system + user + assistant + tool
turns) and, per turn, streams a completion, accumulates assistant text and any
streamed ``tool_calls`` (by index), dispatches each tool to mooring's value-free
handler, appends the results, and re-calls until a completion with no tool calls.

Threading mirrors the Copilot session's "one thread per session" property without
its asyncio: the loop is a blocking generator, so ONE dedicated daemon worker
thread pulls turns off a queue and runs them serialized (protecting the shared
``messages`` list), pushing :class:`ChatEvent`s onto the same subscriber queues the
hub's SSE endpoint drains. Starlette's loop never touches this thread.

Privacy: the class subclasses :class:`ChatBroadcaster`, so the outbound-PII guard,
the traceback sanitise-and-hold valve, the send/confirm flow, the live-schema
refresh, and idle reaping are INHERITED unchanged — ``send`` runs ``_pii_gate``
before anything is enqueued to the wire. The only tools ever registered are
mooring's own value-free functions (:func:`mooring.ai.tools.build_openai_tools`);
no hosted tool (web_search / file_search / code_interpreter) is ever attached, and
``store=False`` is sent on every request so nothing is retained server-side. Every
tool result crosses to the model through the egress minter
(:func:`mooring.ai.egress.to_openai_tool_message`).
"""

from __future__ import annotations

import json
import queue
import threading
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mooring.ai import egress
from mooring.ai.base import AIError, AINotConnectedError
from mooring.ai.chat import ChatBroadcaster, ChatEvent
from mooring.ai.session import _DICT_TOOL_GUIDE, _MODEL_TOOL_GUIDE, _TOOL_GUIDE
from mooring.ai.tools import build_openai_tools

if TYPE_CHECKING:
    from mooring.ai.ner import ModelRef

_START_TIMEOUT = 60.0
_MAX_TOOL_ITERS = 12  # backstop: bound the model's tool round-trips per turn
_DEFAULT_MODEL = "gpt-4o"
_TOOL_BUDGET_MSG = (
    "(Stopped: reached the tool-call limit for one turn. Ask me to continue if needed.)"
)
_STOP = object()  # sentinel queued by close() to end the worker loop


class OpenAIChatSession(ChatBroadcaster):
    def __init__(
        self,
        *,
        model: str,
        system_context: str,
        workspace,
        folders,
        notebook_rel: str,
        client_factory,
        reasoning_effort: str | None = None,
        dictionary=None,
        semantic_models=None,
        pii_enabled: bool = False,
        pii_block: bool = True,
        pii_names: bool = False,
        pii_name_labels: tuple[str, ...] | None = None,
        pii_name_threshold: float = 0.7,
        pii_name_model: "ModelRef | str | None" = None,
        pii_name_backend: str = "auto",
        traceback_guard: bool = False,
    ) -> None:
        super().__init__()
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
        self._model = (model or "").strip()
        self._reasoning_effort = (reasoning_effort or "").strip() or None
        guide = _TOOL_GUIDE
        if dictionary is not None and not dictionary.is_empty():
            guide += _DICT_TOOL_GUIDE
        if semantic_models:
            guide += _MODEL_TOOL_GUIDE
        self._system_context = system_context + guide
        self._workspace = Path(workspace)
        self._folders = tuple(folders)
        self._notebook_rel = notebook_rel
        self._dictionary = dictionary
        self._semantic_models = list(semantic_models or [])
        self._pii_enabled = pii_enabled
        self._client_factory = client_factory
        self._client: Any = None
        self._tool_specs: list[dict] = []
        self._dispatch: dict = {}
        # The conversation state OpenAI does not keep for us: one system message,
        # then user/assistant/tool turns appended as the chat proceeds.
        self._messages: list[dict] = [{"role": "system", "content": self._system_context}]
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        # A real session is not ready until its worker has built the client + tools;
        # the base defaults to "ready", so flip it (mirrors CopilotChatSession).
        self._mark_starting()

    def _known_text(self) -> str:
        # The system context (schema + notebook source + tool guide) the model has
        # already seen — the traceback guard's known-token rescue source.
        return self._system_context

    # -- lifecycle ----------------------------------------------------------

    def start(self, block: bool = True) -> "OpenAIChatSession":
        """Boot the worker thread (build the client + the tool specs/dispatch).

        ``block`` (default) waits for readiness and RAISES on a startup/auth error —
        the synchronous contract the CLI path and unit tests rely on. ``block=False``
        returns immediately; the worker broadcasts ``ready``/``fail`` (and flips
        ``start_status``) when the handshake finishes, so the hub can stream
        readiness without holding the open request.
        """
        self._thread = threading.Thread(target=self._worker, name="openai-chat", daemon=True)
        self._thread.start()
        if not block:
            return self
        if not self._ready.wait(timeout=_START_TIMEOUT + 5):
            raise AIError("OpenAI chat timed out starting up.")
        if self._start_error is not None:
            raise self._start_error
        return self

    def _worker(self) -> None:
        # Build the client (resolves the key; may raise AINotConnectedError) and the
        # value-free tools, THEN serve turns off the queue until close() stops us.
        try:
            self._client = self._client_factory()
            self._tool_specs, self._dispatch = build_openai_tools(
                workspace=self._workspace,
                folders=self._folders,
                notebook_rel=self._notebook_rel,
                emit_proposal=self._emit_proposal,
                emit_proposal_patch=self._emit_proposal_patch,
                dictionary=self._dictionary,
                semantic_models=self._semantic_models,
                pii_enabled=self._pii_enabled,
            )
        except AINotConnectedError as exc:
            self._start_error = exc
            self._mark_start_error(str(exc), reason="not_connected")
            self._ready.set()
            return
        except BaseException as exc:  # noqa: BLE001 - surfaced via start()/the stream
            from mooring.ai.openai_provider import friendly_error

            err = AIError(friendly_error(str(exc)))
            self._start_error = err
            self._mark_start_error(str(err))
            self._ready.set()
            return
        self._mark_ready()
        self._ready.set()
        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            try:
                self._run_turn(item)
            except Exception as exc:  # noqa: BLE001 - surface to the chat, don't crash the worker
                from mooring.ai.openai_provider import friendly_error

                self._broadcast(ChatEvent("fail", {"text": friendly_error(str(exc))}))
        close = getattr(self._client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass

    # -- events -------------------------------------------------------------

    def _emit_proposal(self, code: str, rationale: str = "") -> None:
        self._broadcast(ChatEvent("proposal", {"code": code, "rationale": rationale}))

    def _emit_proposal_patch(self, payload: dict) -> None:
        self._broadcast(ChatEvent("proposal", payload))

    # -- the agent loop (mooring drives it; OpenAI keeps no state) -----------

    def _run_turn(self, user_text: str) -> None:
        self._messages.append({"role": "user", "content": user_text})
        for _ in range(_MAX_TOOL_ITERS):
            full_text, calls = self._stream_once()
            if not calls:
                if full_text:
                    self._broadcast(ChatEvent("message", {"text": full_text}))
                self._broadcast(ChatEvent("idle"))
                return
            # Record the assistant tool-call turn verbatim — every tool_call_id here
            # MUST be answered by exactly one role:"tool" message before the next
            # request, or the API rejects the conversation.
            self._messages.append(
                {
                    "role": "assistant",
                    "content": full_text or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["args"]},
                        }
                        for c in calls
                    ],
                }
            )
            for c in calls:
                self._broadcast(ChatEvent("tool", {"name": c["name"]}))
                out = self._dispatch_call(c["name"], c["args"])
                self._broadcast(ChatEvent("tool_done", {"success": not out.is_error}))
                self._messages.append(egress.to_openai_tool_message(c["id"], out))
        # Exceeded the per-turn tool budget — end the turn rather than loop forever.
        self._broadcast(ChatEvent("message", {"text": _TOOL_BUDGET_MSG}))
        self._broadcast(ChatEvent("idle"))

    def _stream_once(self) -> tuple[str, list[dict]]:
        """One streamed completion. Emits ``delta`` events, returns
        ``(assistant_text, tool_calls)`` — ``tool_calls`` is non-empty only when the
        model finished asking for tools."""
        kwargs: dict[str, Any] = {
            "model": self._model or _DEFAULT_MODEL,
            "messages": self._messages,
            "stream": True,
            # No server-side retention (the OpenAI analogue of enable_session_store=
            # False): conversation state lives here, in self._messages, only.
            "store": False,
        }
        if self._tool_specs:
            kwargs["tools"] = self._tool_specs
            kwargs["tool_choice"] = "auto"
        # reasoning_effort is only accepted by reasoning models (o-series / gpt-5);
        # sending it to a plain chat model errors, so gate it by model AND config.
        if self._reasoning_effort and _is_reasoning_model(self._model or _DEFAULT_MODEL):
            kwargs["reasoning_effort"] = self._reasoning_effort

        stream = self._client.chat.completions.create(**kwargs)
        text_parts: list[str] = []
        acc: dict[int, dict] = {}
        finish: str | None = None
        for chunk in stream:
            # Azure emits a leading content-filter chunk with empty choices, and a
            # usage-only final chunk also has choices == []; skip both.
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None and getattr(delta, "content", None):
                text_parts.append(delta.content)
                self._broadcast(ChatEvent("delta", {"text": delta.content}))
            for tc in (getattr(delta, "tool_calls", None) or []) if delta is not None else []:
                slot = acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["args"] += fn.arguments
            if choice.finish_reason:
                finish = choice.finish_reason
        calls = [acc[i] for i in sorted(acc)] if finish == "tool_calls" else []
        return "".join(text_parts), calls

    def _dispatch_call(self, name: str, args_json: str) -> "egress.ToolOutput":
        """Run one tool call through mooring's value-free handler. FAIL-CLOSED: an
        unrecognised tool name is refused, never executed (the loop is mooring code,
        so this is the choke point that replaces the copilot deny-all backstop)."""
        handler = self._dispatch.get(name)
        if handler is None:
            return egress.ToolOutput(text=f"unknown tool {name!r}", is_error=True)
        try:
            args = json.loads(args_json) if args_json else {}
        except ValueError:
            args = {}
        invocation = types.SimpleNamespace(arguments=args)
        try:
            return handler(invocation)
        except Exception as exc:  # noqa: BLE001 - a handler error still yields a well-formed turn
            return egress.ToolOutput(text=f"tool {name} failed: {exc}", is_error=True)

    # -- turns --------------------------------------------------------------

    def send(self, text: str, live_schema_text: str = "") -> None:
        self.touch()
        if self._closed:
            raise AIError("Chat session is closed.")
        gated = self._pii_gate(text)
        if gated is None:
            return  # held pending the analyst's "Send anyway" (see send_confirmed)
        # The live-schema prefix is machine-rendered and already value-free, so it is
        # added AFTER the PII gate — it must not trip the warn-and-hold flow.
        self._queue.put(self._live_prefix(live_schema_text) + gated)

    def send_confirmed(self, token: str, live_schema_text: str = "") -> None:
        self.touch()
        if self._closed:
            raise AIError("Chat session is closed.")
        text = self._pii_take(token)
        if text is None:
            raise AIError("That message has expired — please retype it.")
        self._queue.put(self._live_prefix(live_schema_text) + text)

    def close(self) -> None:
        super().close()  # broadcast "closed" (idempotent); clears any held prompt
        self._queue.put(_STOP)


def _is_reasoning_model(model: str) -> bool:
    m = (model or "").lower()
    return m.startswith(("o1", "o3", "o4", "o5", "gpt-5")) or "reasoning" in m
