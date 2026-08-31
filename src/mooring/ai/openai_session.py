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

Which request fields a given model accepts is SERVER-side policy that no name
prefix can predict, so ``reasoning_effort`` is settled by asking rather than by
guessing: :meth:`OpenAIChatSession._create_stream` walks a short ladder when the
server REJECTS a request over the param (send -> drop -> ``"none"``, or straight to
``"none"`` when the request carried no effort at all) and REMEMBERS the answer on
the session, so the probe costs one extra round-trip once, not once per turn. The
detector that arms it (:func:`_blames_reasoning_effort`) is deliberately strict: a
false positive would disable the analyst's setting for the whole session.
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
from mooring.ai.session import (
    _CATALOG_TOOL_GUIDE,
    _DICT_TOOL_GUIDE,
    _EFFORT_SENTINELS,
    _HELPER_TOOL_GUIDE,
    _INVESTIGATE_GUIDE,
    _INVESTIGATOR_GUIDE,
    _MODEL_TOOL_GUIDE,
    _PROPOSE_ONLY_GUIDE,
    _TOOL_GUIDE,
)
from mooring.ai.tools import build_openai_tools

if TYPE_CHECKING:
    from mooring.ai.ner import ModelRef

_START_TIMEOUT = 60.0
_MAX_TOOL_ITERS = 12  # backstop: bound the model's tool round-trips per turn
# The model when ``[ai] model`` is unset — a CURRENT id on purpose, not merely a widely
# available one: this loop's output is code the analyst can Apply, and an applied cell RUNS,
# so a model that mis-authors a marimo cell costs a broken notebook, not just a weak
# suggestion. Set ``[ai] model`` (the dropdown lists what the account can use) to pick
# another. Only reached when nothing is configured; a custom endpoint always names its own.
_DEFAULT_MODEL = "gpt-5.1"
_TOOL_BUDGET_MSG = (
    "(Stopped: reached the tool-call limit for one turn. Ask me to continue if needed.)"
)
_STOP = object()  # sentinel queued by close() to end the worker loop
# What the ladder settled on, remembered for the life of the session (_effort_mode):
#   "send" - the configured effort rides every request (the un-probed default)
#   "drop" - the server rejected the param; omit it and keep the model's own default
#   "none" - the server rejects any other effort here; send "none"
# The two rungs that CHANGE behaviour each say so once, in fixed value-free text.
# The wording states only what we OBSERVED (a rejection, and what we re-sent): the
# server may be refusing the parameter itself, or only the configured value, and the
# error body is not something we can safely repeat back or reliably classify.
_EFFORT_NOTICE = {
    "drop": (
        "(Note: the server rejected the reasoning-effort setting for this chat — it may "
        "not accept the setting at all, or not that value — so the request was re-sent "
        "without it and the model's own default reasoning applies.)"
    ),
    "none": (
        "(Note: the server rejected the reasoning-effort setting for this chat — it may "
        "not accept the setting at all, or not that value — so the request was re-sent "
        "with reasoning effort set to 'none'.)"
    ),
}
# Words that mark an error as a REJECTION of the request, rather than a transient
# failure that merely happens to name the field (see _blames_reasoning_effort).
_REJECTION_MARKERS = (
    "not supported",
    "unsupported",
    "does not support",
    "not permitted",
    "not allowed",
    "invalid",
)


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
        store: bool | None = False,
        reasoning_effort: str | None = None,
        dictionary=None,
        semantic_models=None,
        helpers=None,
        catalog=None,
        read_only: bool = False,
        run_investigation=None,
        pii_enabled: bool = False,
        pii_block: bool = True,
        pii_names: bool = False,
        pii_name_labels: tuple[str, ...] | None = None,
        pii_name_threshold: float = 0.7,
        pii_name_model: "ModelRef | str | None" = None,
        pii_name_backend: str = "auto",
        traceback_guard: bool = False,
        allow_read_tools: bool = True,
        trusted_customer_data: bool = False,
        output_guard=None,
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
        # The provider's effort picker offers "default" (and "auto") to mean "don't send
        # the param at all". Normalised HERE because __init__ is the ONE choke point every
        # path flows through (hub chat, batch, investigate), so no caller can forget it.
        # The sentinel list is shared with CopilotChatSession, which needs the same guard.
        effort = (reasoning_effort or "").strip()
        self._reasoning_effort = None if effort.lower() in _EFFORT_SENTINELS else effort
        # Where the reasoning-effort ladder has got to for this session (see
        # _create_stream): probed once on the first rejection, then reused.
        self._effort_mode = "send"
        self._effort_notified = False
        self._read_only = read_only
        self._allow_read_tools = bool(allow_read_tools)
        self._trusted_customer_data = bool(trusted_customer_data)
        self._output_guard = output_guard
        # A read-only investigate sub-agent: no propose/edit tool and no
        # mooring_investigate (so it cannot write or recurse). Force it off even if a
        # run_investigation were mis-wired in — belt-and-suspenders for the depth-1 rule.
        self._run_investigation = None if read_only else run_investigation
        guide = (
            _INVESTIGATOR_GUIDE
            if read_only
            else (_TOOL_GUIDE if self._allow_read_tools else _PROPOSE_ONLY_GUIDE)
        )
        if self._allow_read_tools and dictionary is not None and not dictionary.is_empty():
            guide += _DICT_TOOL_GUIDE
        if self._allow_read_tools and helpers is not None and not helpers.is_empty():
            guide += _HELPER_TOOL_GUIDE
        if self._allow_read_tools and catalog is not None and not catalog.is_empty():
            guide += _CATALOG_TOOL_GUIDE
        if self._allow_read_tools and semantic_models:
            guide += _MODEL_TOOL_GUIDE
        if self._allow_read_tools and self._run_investigation is not None:
            guide += _INVESTIGATE_GUIDE
        self._system_context = system_context + guide
        self._workspace = Path(workspace)
        self._folders = tuple(folders)
        self._notebook_rel = notebook_rel
        self._dictionary = dictionary
        self._helpers = helpers
        self._catalog = catalog
        self._semantic_models = list(semantic_models or [])
        self._pii_enabled = pii_enabled
        self._client_factory = client_factory
        self._store = store
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
                # A read-only session registers NO write surface (the load-bearing
                # privacy gate): no propose/edit tool, so only the value-free read tools
                # (+ dictionary/model/helper reads) are built.
                emit_proposal=None if self._read_only else self._emit_proposal,
                emit_proposal_patch=None if self._read_only else self._emit_proposal_patch,
                dictionary=self._dictionary,
                semantic_models=self._semantic_models,
                code_index=self._helpers,
                catalog=self._catalog,
                run_investigation=self._run_investigation,
                emit_tool_progress=self._emit_tool_progress,
                pii_enabled=self._pii_enabled,
                allow_read_tools=self._allow_read_tools,
                trusted_customer_data=self._trusted_customer_data,
                output_guard=self._output_guard,
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

    def _emit_tool_progress(self, text: str) -> None:
        """A value-free in-flight cue for a long-running tool (the investigate fan-out),
        on the chat's EXISTING ``tool`` progress channel. It carries counts/statuses only,
        goes to the local UI (never the model), and touches the activity clock so a
        multi-minute investigation is never idle-reaped mid-flight. Called from the
        planner's worker threads — ``_broadcast`` fans out onto thread-safe queues."""
        self.touch()
        self._broadcast(ChatEvent("tool", {"progress": text}))

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
        }
        # No server-side retention (the OpenAI analogue of enable_session_store=False):
        # conversation state lives here in self._messages only. Sent for canonical
        # OpenAI; omitted (store is None) for a custom endpoint that may reject the
        # unknown field — there is no OpenAI-side retention to control there.
        if self._store is not None:
            kwargs["store"] = self._store
        if self._tool_specs:
            kwargs["tools"] = self._tool_specs
            kwargs["tool_choice"] = "auto"
        effort = self._effort_for_request()
        if effort is not None:
            kwargs["reasoning_effort"] = effort

        # Only the create() call is retryable; the iteration below is NOT, because a
        # delta broadcast from a half-consumed stream can never be un-broadcast.
        stream = self._create_stream(kwargs)
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

    # -- the reasoning-effort ladder (the server is the authority) -----------

    def _effort_for_request(self) -> str | None:
        """The ``reasoning_effort`` this request should carry, or None to omit it."""
        # A settled ladder wins over BOTH the config and the name pre-filter. It has to:
        # the "none" rung can be reached from a request that sent no param at all (the
        # shipped default), and if config were consulted first that rung would evaluate
        # back to "send nothing" and the session would re-probe on every single turn.
        if self._effort_mode == "drop":
            return None
        if self._effort_mode == "none":
            return "none"
        # _is_reasoning_model is an ADVISORY pre-filter: it spares a plain chat model a
        # wasted 400, but it never decides that a model DOES accept the param — the
        # ladder in _create_stream does, from the server's own answer.
        if not (self._reasoning_effort and _is_reasoning_model(self._model or _DEFAULT_MODEL)):
            return None
        return self._reasoning_effort

    def _create_stream(self, kwargs: dict[str, Any]):
        """Open ONE streamed completion, letting the server settle ``reasoning_effort``.

        Some models reject the param outright, and some reject a non-``"none"`` effort
        *alongside function tools* — which mooring always sends. That is server-side
        policy, not something the model NAME can be read for, so when the server
        REJECTS the request over ``reasoning_effort`` we re-ask, and which rungs we
        walk depends on what the failed request carried:

        * we sent an effort -> (a) the same request with the param DROPPED (preferred:
          it keeps the model's own default reasoning), then (b) ``"none"``.
        * we sent none -> only (b). Dropping is a no-op here, so re-sending the
          identical request would just buy the identical 400; and the premise of rung
          (b) is precisely that the model's OWN default effort is what the server
          refuses alongside tools, which is the state this request was already in.

        The first success is remembered in ``self._effort_mode``, so a session pays the
        extra round-trip once. If every rung fails the error propagates as it always did.

        Returns the stream object WITHOUT iterating it: consuming chunks stays in the
        caller so a retry can never replay a delta that has already been broadcast.
        """
        try:
            return self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised below unless it is ours
            # Hard guard: only an actual REJECTION over this param is ours to retry.
            # Anything else (auth, model-not-found, rate limit, a transient blip that
            # merely lists the request's fields) fails on one request, as before.
            if not _blames_reasoning_effort(exc):
                raise
        rungs = ("drop", "none") if "reasoning_effort" in kwargs else ("none",)
        for i, mode in enumerate(rungs):
            attempt = dict(kwargs)
            if mode == "drop":
                attempt.pop("reasoning_effort", None)
            else:
                attempt["reasoning_effort"] = "none"
            try:
                stream = self._client.chat.completions.create(**attempt)
            except Exception as exc:  # noqa: BLE001
                # Still a rejection over the param with it dropped means the model's own
                # default effort is the problem — fall through to the next rung. The last
                # rung, and any other error, propagates.
                if i == len(rungs) - 1 or not _blames_reasoning_effort(exc):
                    raise
                continue
            self._settle_effort(mode)
            return stream
        raise AssertionError("unreachable: the last rung either returns or raises")

    def _settle_effort(self, mode: str) -> None:
        """Remember the ladder's answer for the rest of the session and say so ONCE, so
        a silently different reasoning setting is visible.

        The notice is fixed text — no values, no paths, no model name, nothing echoed
        back from the server — and rides the ``message`` channel flagged ``notice``, so
        consumers that collect the assistant's ANSWER can tell mooring's own aside apart
        from it: the chat UI renders it like any message, the investigate fan-out skips
        it (it is not a finding), and the batch tray records it on the job's result."""
        self._effort_mode = mode
        if self._effort_notified:
            return
        self._effort_notified = True
        self._broadcast(ChatEvent("message", {"text": _EFFORT_NOTICE[mode], "notice": True}))

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
    """ADVISORY only: does this model name LOOK like a reasoning model?

    A name prefix cannot know a server's per-model rules — ``gpt-5.6-sol`` matches
    "gpt-5" yet rejects ``reasoning_effort`` whenever function tools are attached,
    which mooring always attaches. So this is kept purely as a cheap pre-filter that
    spares an obvious chat model (``gpt-4o``) a wasted 400; the authority on whether
    the param is accepted is the server, via the ladder in
    :meth:`OpenAIChatSession._create_stream`. Deliberately NOT a per-model list: the
    next model would break it again.
    """
    m = (model or "").lower()
    return m.startswith(("o1", "o3", "o4", "o5", "gpt-5")) or "reasoning" in m


def _blames_reasoning_effort(exc: BaseException) -> bool:
    """Is this API error the server REJECTING the ``reasoning_effort`` parameter?

    Deliberately STRICT, because a false positive is expensive: arming the ladder
    settles ``_effort_mode`` for the life of the session, so one transient failure that
    merely happened to name the field would disable the analyst's setting for the rest
    of the chat and blame the model for it. Three things must all hold:

    1. the parameter is IDENTIFIED — its name in the message, or a duck-typed
       structured ``.param`` (the OpenAI SDK's ``APIError.__init__`` lifts ``param``
       out of the error body; a gateway wrapper may expose it without repeating the
       name in the text);
    2. the message carries a genuine REJECTION marker. A bare mention is not enough:
       ``"Upstream hiccup while validating request (fields: model, messages, tools,
       reasoning_effort)"`` names the field and is a transient fault, not a verdict;
    3. the status, when exposed, is exactly 400. A 401/403 from a gateway that echoes
       the request body back, a 429, a 5xx — none of those are our request being
       refused. A front-end that exposes no status at all is still allowed through,
       since some do; the rejection marker then carries the weight on its own.

    KNOWN AND ACCEPTED BLIND SPOT — do not "fix" it by loosening this back to a bare
    substring test. When the SDK cannot read the error body (its
    ``response.is_closed and not response.is_stream_consumed`` branch) it builds the
    exception with ``body=None``, so ``str(exc)`` is just ``"Error code: 400"`` and
    ``.param`` is ``None`` too: nothing identifies the parameter and the ladder stays
    disarmed. That request fails, as it would have before the ladder existed. A
    matcher loose enough to catch it would fire on unrelated 400s.

    String-first and duck-typed because this module never imports ``openai`` (the SDK
    is an optional extra, and the injected client may be any OpenAI-compatible
    stand-in), and Azure / LiteLLM / gateway front-ends reword the body freely.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status != 400:
        return False
    text = str(exc).lower()
    named = (
        "reasoning_effort" in text
        or "reasoning effort" in text
        or getattr(exc, "param", None) == "reasoning_effort"
    )
    return named and any(marker in text for marker in _REJECTION_MARKERS)
