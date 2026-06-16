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

from mooring.ai.base import AIError
from mooring.ai.chat import ChatBroadcaster, ChatEvent

_START_TIMEOUT = 60.0
_SEND_TIMEOUT = 30.0

_TOOL_GUIDE = (
    "\n\nYou have tools to inspect this workspace WITHOUT ever seeing data values:\n"
    "- mooring_list_datasets — list available datasets\n"
    "- mooring_get_schema(dataset) — a dataset's column names + dtypes\n"
    "- mooring_read_notebook_source — the notebook's current code\n"
    "- mooring_propose_cell(code, rationale) — propose a cell for the analyst to apply.\n"
    "When you want to add code to the notebook, CALL mooring_propose_cell so the analyst can "
    "review and apply it. You have no other tools and cannot read the data itself."
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
    ) -> None:
        super().__init__()
        self._model = (model or "").strip()
        self._reasoning_effort = (reasoning_effort or "").strip() or None
        guide = _TOOL_GUIDE
        if dictionary is not None and not dictionary.is_empty():
            guide += _DICT_TOOL_GUIDE
        self._system_context = system_context + guide
        self._workspace = Path(workspace)
        self._folders = tuple(folders)
        self._notebook_rel = notebook_rel
        self._dictionary = dictionary
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client = None
        self._session = None
        self._workdir: str | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "CopilotChatSession":
        self._thread = threading.Thread(target=self._run_loop, name="copilot-chat", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=_START_TIMEOUT):
            raise AIError("Copilot timed out starting up.")
        if self._start_error is not None:
            raise self._start_error
        return self

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._aopen())
        except BaseException as exc:  # noqa: BLE001 - surfaced via start()
            from mooring.ai.copilot import _friendly_error

            self._start_error = (
                exc if isinstance(exc, AIError) else AIError(_friendly_error(str(exc)))
            )
            self._ready.set()
            self._teardown(loop)
            return
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

        from mooring.ai.copilot import _is_authed, hardened_session_kwargs
        from mooring.ai.tools import build_tools

        # An empty working dir: even if a built-in file tool slipped the allowlist,
        # there are no data files here to read.
        self._workdir = tempfile.mkdtemp(prefix="mooring_copilot_")
        client = CopilotClient(use_logged_in_user=True, working_directory=self._workdir)
        await client.start()
        self._client = client
        auth = await client.get_auth_status()
        if not _is_authed(auth):
            raise AIError("Copilot isn't connected. Run `mooring ai login` to sign in.")
        tools = build_tools(
            workspace=self._workspace,
            folders=self._folders,
            notebook_rel=self._notebook_rel,
            emit_proposal=self._emit_proposal,
            dictionary=self._dictionary,
        )
        extra = {}
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

    def send(self, text: str) -> None:
        self.touch()
        if self._loop is None or self._session is None:
            raise AIError("Chat session is not ready.")
        future = asyncio.run_coroutine_threadsafe(self._session.send(text), self._loop)
        try:
            future.result(timeout=_SEND_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - surface to the chat, don't crash the hub
            from mooring.ai.copilot import _friendly_error

            self._broadcast(ChatEvent("fail", {"text": _friendly_error(str(exc))}))

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
        except Exception:  # noqa: BLE001 - best-effort teardown
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
