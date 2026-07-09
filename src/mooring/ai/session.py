"""A long-lived, streaming GitHub Copilot chat session — the interactive copilot.

The Copilot SDK is async, but a multi-turn chat must outlive any single call, so
ONE asyncio event loop runs on ONE dedicated daemon thread for the session's
whole life. Turns are submitted with ``run_coroutine_threadsafe``; the SDK's
(synchronous) ``session.on`` handler runs on that loop and only pushes
:class:`ChatEvent`s onto subscriber queues, which the hub's SSE endpoint drains
from threadpool workers. Starlette's loop and this loop never share state beyond
the thread-safe queues.

Privacy: the session is built from :func:`copilot.hardened_session_kwargs` (the
audited value-blind config) plus ``available_tools=TOOL_NAMES`` (only mooring's
value-free tools — the SDK's built-in file/shell tools are not in the allowlist)
and ``working_directory`` set to an empty temp dir (so even a stray file tool has
no data to read). The agent has no path to a data value.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mooring.ai.base import AIError, AINotConnectedError
from mooring.ai.chat import ChatBroadcaster, ChatEvent

if TYPE_CHECKING:
    from mooring.ai.ner import ModelRef

_START_TIMEOUT = 60.0
_SEND_TIMEOUT = 30.0

_TOOL_GUIDE = (
    "\n\nYou have tools to inspect this workspace WITHOUT ever seeing data values:\n"
    "- mooring_list_datasets — list available datasets\n"
    "- mooring_get_schema(dataset) — a dataset's column names + dtypes\n"
    "- mooring_read_notebook_source — the notebook's current code, with each cell's index\n"
    "- mooring_propose_cell(code, rationale) — propose a NEW cell appended at the end\n"
    "- mooring_propose_cell_edit(index, code, rationale) — propose REPLACING an existing "
    "cell's code (read the source first for the index)\n"
    "- mooring_propose_notebook_edit(edits, appends, deletes, rationale) — propose several "
    "cell changes at once as one patch\n"
    "- mooring_propose_notebook_rewrite(cells, rationale) — propose replacing the WHOLE "
    "notebook (use only for a wholesale rewrite; an edit is lighter).\n"
    "To CHANGE the notebook, call the matching propose tool — prefer editing an existing "
    "cell over appending a near-duplicate. Cell code is the BODY ONLY: top-level statements "
    "with NO '@app.cell', NO 'def _():', and NO trailing 'return (...)' (the displayed file "
    "source shows that wrapper, but mooring adds it for you — never copy it back). Every "
    "proposal is reviewed and applied by the analyst; you never write the file yourself and "
    "cannot read the data itself."
)

_DICT_TOOL_GUIDE = (
    "\n\nA team DATA DICTIONARY is available (metadata only — names/types/keys/"
    "descriptions, never values):\n"
    "- mooring_list_tables — list dictionary tables by domain\n"
    "- mooring_describe_table(table) — one table's columns, types, and foreign keys\n"
    "- mooring_search_dictionary(query) — find tables/columns by term.\n"
    "Use these to confirm table and column names (and join keys) BEFORE proposing "
    "code; a relevant slice may already be in your context."
)

_HELPER_TOOL_GUIDE = (
    "\n\nA team CODE LIBRARY is available (reusable helper modules — signatures, type "
    "hints, and docstrings, never a function body or any data value):\n"
    "- mooring_list_helpers — reusable functions/classes with signatures\n"
    "- mooring_describe_helper(name) — one helper's signature, docstring, and import line\n"
    "- mooring_search_helpers(query) — find helpers by name/term.\n"
    "Prefer REUSING an existing helper (import it via the exact `from ... import ...` line) "
    "over re-implementing it; check here before writing a utility yourself."
)

_MODEL_TOOL_GUIDE = (
    "\n\nA POWER BI SEMANTIC MODEL is available (tables, columns+types, "
    "relationships, and measure DAX — authored code, never any data value):\n"
    "- mooring_get_semantic_model — table names + measure NAMES (no DAX; cheap)\n"
    "- mooring_describe_model_table(table) — one table's columns and its measures' DAX\n"
    "- mooring_get_measure(measure) — one measure's full DAX + format string.\n"
    "Use these to translate business logic faithfully — e.g. recreate a measure in "
    "polars from its real DAX instead of guessing. Fetch only the tables/measures "
    "you need; never ask for the whole model at once."
)


class CopilotChatSession(ChatBroadcaster):
    def __init__(
        self,
        *,
        model: str,
        system_context: str,
        workspace,
        folders,
        notebook_rel: str,
        reasoning_effort: str | None = None,
        dictionary=None,
        semantic_models=None,
        helpers=None,
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
        # The traceback guard needs the workspace (to bound its source re-read)
        # and the notebook (for the known-token rescue) — both already travel
        # into this ctor, so no route/hub arming call exists to forget.
        self.configure_traceback_guard(
            enabled=traceback_guard, workspace=workspace, notebook_rel=notebook_rel
        )
        self._model = (model or "").strip()
        self._reasoning_effort = (reasoning_effort or "").strip() or None
        guide = _TOOL_GUIDE
        if dictionary is not None and not dictionary.is_empty():
            guide += _DICT_TOOL_GUIDE
        if helpers is not None and not helpers.is_empty():
            guide += _HELPER_TOOL_GUIDE
        if semantic_models:
            guide += _MODEL_TOOL_GUIDE
        self._system_context = system_context + guide
        self._workspace = Path(workspace)
        self._folders = tuple(folders)
        self._notebook_rel = notebook_rel
        self._dictionary = dictionary
        self._helpers = helpers
        self._semantic_models = list(semantic_models or [])
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client = None
        self._session = None
        self._workdir: str | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        # A real provider session is NOT ready until its loop thread has spawned the
        # Copilot CLI, checked auth, and created the session (see _aopen). The base
        # class defaults to "ready"; flip it so the hub can return the chat-open
        # response immediately and surface readiness over the SSE stream instead.
        self._mark_starting()

    def _known_text(self) -> str:
        # The system context (schema + notebook source + tool guide) the model has
        # already been shown — the traceback guard's known-token rescue source.
        return self._system_context

    # -- lifecycle ----------------------------------------------------------

    def start(self, block: bool = True) -> "CopilotChatSession":
        """Boot the session's loop thread (Copilot CLI + auth + create_session).

        ``block`` (default) waits for readiness and RAISES on a startup/auth error
        — the synchronous contract the CLI path and the unit tests rely on.
        ``block=False`` returns immediately; the loop thread broadcasts a
        ``ready``/``fail`` event (and ``start_status`` flips) when the handshake
        finishes, so the hub can stream readiness without holding the open request.
        """
        self._thread = threading.Thread(target=self._run_loop, name="copilot-chat", daemon=True)
        self._thread.start()
        if not block:
            return self
        # The loop thread now bounds the handshake itself (wait_for in _run_loop), so
        # _ready is always set within ~_START_TIMEOUT. Wait a touch longer here so that
        # loop-side deadline (with its precise message) wins over a redundant race.
        if not self._ready.wait(timeout=_START_TIMEOUT + 10):
            raise AIError("Copilot timed out starting up.")
        if self._start_error is not None:
            raise self._start_error
        return self

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            # Bound the handshake so a HUNG (not merely failed) Copilot CLI / network
            # can't leave a backgrounded session stuck "starting" forever — the
            # non-blocking open path has already returned and has no caller-side
            # timeout, so the deadline must live HERE. A timeout raises and is turned
            # into a "fail" event below, which re-enables the UI just like an error.
            loop.run_until_complete(asyncio.wait_for(self._aopen(), _START_TIMEOUT))
        except BaseException as exc:  # noqa: BLE001  # surfaced via start()/the stream
            from mooring.ai.copilot import friendly_error

            # A machine-readable reason lets the chat UI branch on "not signed in"
            # and render a sign-in button instead of a dead error string. Check the
            # typed subclass FIRST (it is an AIError too).
            reason: str | None = None
            if isinstance(exc, AINotConnectedError):
                err: AIError = exc
                reason = "not_connected"
            elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                err = AIError("Copilot timed out starting up.")
            elif isinstance(exc, AIError):
                err = exc
            else:
                err = AIError(friendly_error(str(exc)))
            self._start_error = err
            # Surface the failure on the stream too (the non-blocking open path has
            # already returned, so it can't raise); harmless in the blocking path
            # (no subscriber has attached before start() returns).
            self._mark_start_error(str(err), reason=reason)
            self._ready.set()
            self._teardown(loop)
            return
        self._mark_ready()  # flips start_status -> "ready" and emits a "ready" event
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            # _aclose() already ran in close() while the loop was live; here we
            # only close the loop and clean the temp dir.
            self._teardown(loop)

    def _teardown(self, loop: asyncio.AbstractEventLoop) -> None:
        with _suppress():
            loop.close()
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    async def _aopen(self) -> None:
        from copilot import CopilotClient

        from mooring.ai.copilot import is_authed, hardened_session_kwargs
        from mooring.ai.tools import build_tools

        # An empty working dir: even if a built-in file tool slipped the allowlist,
        # there are no data files here to read.
        self._workdir = tempfile.mkdtemp(prefix="mooring_copilot_")
        client = CopilotClient(use_logged_in_user=True, working_directory=self._workdir)
        await client.start()
        self._client = client
        auth = await client.get_auth_status()
        if not is_authed(auth):
            # Typed so the hub can offer an in-app "Sign in to Copilot" button (the
            # fail event carries reason="not_connected") rather than a dead error
            # telling a non-technical analyst to run a CLI command.
            raise AINotConnectedError(
                "You're not signed in to GitHub Copilot. Sign in to use the copilot."
            )
        tools = build_tools(
            workspace=self._workspace,
            folders=self._folders,
            notebook_rel=self._notebook_rel,
            emit_proposal=self._emit_proposal,
            emit_proposal_patch=self._emit_proposal_patch,
            dictionary=self._dictionary,
            semantic_models=self._semantic_models,
            code_index=self._helpers,
            pii_enabled=self._pii_enabled,
        )
        extra: dict[str, Any] = {}
        if self._reasoning_effort:
            extra["reasoning_effort"] = self._reasoning_effort
        self._session = await client.create_session(
            model=self._model or None,
            streaming=True,
            tools=tools,
            # Allowlist exactly the tools we built (mooring's safe set, plus the
            # dictionary tools when present) => the SDK's built-ins stay dropped.
            available_tools=[t.name for t in tools],
            working_directory=self._workdir,
            **extra,
            **hardened_session_kwargs(self._system_context),
        )
        self._session.on(self._on_event)

    async def _aclose(self) -> None:
        if self._session is not None:
            with _suppress():
                await self._session.disconnect()
        if self._client is not None:
            with _suppress():
                await self._client.stop()

    # -- events -------------------------------------------------------------

    def _emit_proposal(self, code: str, rationale: str = "") -> None:
        self._broadcast(ChatEvent("proposal", {"code": code, "rationale": rationale}))

    def _emit_proposal_patch(self, payload: dict) -> None:
        """Broadcast a structured proposal (edit / multi-cell patch / rewrite).

        ``payload`` carries ``kind`` + the normalized ``ops`` the Apply endpoint runs,
        plus value-free ``diffs`` for the local diff view. The cell ``anchor``s inside
        were read from the analyst's own notebook and go only to their browser — never
        to the model (which already saw the source) — so this opens no value channel.
        """
        self._broadcast(ChatEvent("proposal", payload))

    def _on_event(self, event) -> None:
        """SDK callback (runs on the loop thread). Non-blocking: queue and return."""
        from copilot import SessionEventType as ET

        etype = getattr(event, "type", None)
        data = getattr(event, "data", None)
        if etype == ET.ASSISTANT_MESSAGE_DELTA:
            self._broadcast(ChatEvent("delta", {"text": getattr(data, "delta_content", "") or ""}))
        elif etype == ET.ASSISTANT_MESSAGE:
            self._broadcast(ChatEvent("message", {"text": getattr(data, "content", "") or ""}))
        elif etype == ET.ASSISTANT_INTENT:
            intent = getattr(data, "intent", "") or ""
            if intent:
                self._broadcast(ChatEvent("intent", {"text": intent}))
        elif etype == ET.TOOL_EXECUTION_START:
            name = getattr(data, "tool_name", None) or getattr(data, "name", "") or ""
            self._broadcast(ChatEvent("tool", {"name": name}))
        elif etype == ET.TOOL_EXECUTION_PROGRESS:
            self._broadcast(
                ChatEvent("tool", {"progress": getattr(data, "progress_message", "") or ""})
            )
        elif etype == ET.TOOL_EXECUTION_COMPLETE:
            self._broadcast(
                ChatEvent("tool_done", {"success": bool(getattr(data, "success", True))})
            )
        elif etype == ET.SESSION_IDLE:
            self._broadcast(ChatEvent("idle"))
        elif etype == ET.SESSION_ERROR:
            self._broadcast(ChatEvent("fail", {"text": _event_text(data) or "Copilot error."}))

    # -- turns --------------------------------------------------------------

    def send(self, text: str, live_schema_text: str = "") -> None:
        self.touch()
        if self._loop is None or self._session is None:
            raise AIError("Chat session is not ready.")
        gated = self._pii_gate(text)
        if gated is None:
            return  # held pending the analyst's "Send anyway" (see send_confirmed)
        # The live-schema prefix is machine-rendered and already value-free, so it is
        # added AFTER the PII gate — it must not trip the warn-and-hold flow.
        self._forward(self._live_prefix(live_schema_text) + gated)

    def send_confirmed(self, token: str, live_schema_text: str = "") -> None:
        """Forward a prompt the analyst chose to send despite the PII warning."""
        self.touch()
        if self._loop is None or self._session is None:
            raise AIError("Chat session is not ready.")
        text = self._pii_take(token)
        if text is None:
            raise AIError("That message has expired — please retype it.")
        self._forward(self._live_prefix(live_schema_text) + text)

    def _forward(self, text: str) -> None:
        assert self._session is not None and self._loop is not None  # callers check first
        future = asyncio.run_coroutine_threadsafe(self._session.send(text), self._loop)
        try:
            future.result(timeout=_SEND_TIMEOUT)
        except Exception as exc:  # noqa: BLE001  # surface to the chat, don't crash the hub
            from mooring.ai.copilot import friendly_error

            self._broadcast(ChatEvent("fail", {"text": friendly_error(str(exc))}))

    def close(self) -> None:
        super().close()  # broadcast "closed" to subscribers (idempotent)
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        # Disconnect the SDK on the LIVE loop (its RPC needs the running executor),
        # THEN stop the loop — doing it after stop() caused "cannot schedule new
        # futures after shutdown".
        try:
            asyncio.run_coroutine_threadsafe(self._aclose(), loop).result(timeout=10)
        except Exception:  # noqa: BLE001  # best-effort teardown
            pass
        loop.call_soon_threadsafe(loop.stop)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True  # swallow everything (best-effort teardown)


def _event_text(data) -> str:
    for attr in ("message", "error", "text", "detail"):
        value = getattr(data, attr, None)
        if value:
            return str(value)
    return ""
