"""A long-lived, streaming GitHub Copilot chat session — the interactive copilot.

The Copilot SDK is async, but a multi-turn chat must outlive any single call, so
ONE asyncio event loop runs on ONE dedicated daemon thread for the session's
whole life. Turns are submitted with ``run_coroutine_threadsafe``; the SDK's
(synchronous) ``session.on`` handler runs on that loop and only pushes
:class:`ChatEvent`s onto subscriber queues, which the hub's SSE endpoint drains
from threadpool workers. Starlette's loop and this loop never share state beyond
the thread-safe queues.

Privacy: the session is built from :func:`copilot.hardened_session_kwargs` (the
audited value-blind config) plus an ``available_tools`` allowlist derived from the
tools actually built — so it stays in lock-step with them, including the write
tool's two names (only mooring's
value-free tools; the SDK's built-in file/shell tools are not in the allowlist)
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
from mooring.ai.tools import EDIT_TOOL_NAME, write_tool_name

if TYPE_CHECKING:
    from mooring.ai.ner import ModelRef

_START_TIMEOUT = 60.0
_SEND_TIMEOUT = 30.0
# How long to wait on the SDK's own turn abort. Short on purpose: it is a best-effort
# BONUS on top of the tool-boundary stop flag, which has already taken effect, so the
# analyst's Cancel must never block the hub waiting for it.
_ABORT_TIMEOUT = 5.0
# Reasoning-effort picker sentinels meaning "send no reasoning_effort at all" — leave
# it to the model's own default. Defined HERE (the lowest ai/ session module) and
# imported by :mod:`mooring.ai.openai_session`, so both providers read one list: the
# hub's `ai.reasoning_effort` is a free-text Settings field and the word "default"
# now appears in the picker, so either session can be handed it.
_EFFORT_SENTINELS = frozenset({"", "default", "auto"})

# What happens AFTER the write tool is called — the one thing that genuinely differs
# between the tool's two modes, so it is the only part of the guides below that varies.
# Mirrors ``tools._PROPOSE_MODE`` / ``tools._EDIT_MODE`` (the tool's own description) in
# the guide's voice; the two must agree, because the model reads both every turn.
_PROPOSE_OUTCOME = (
    "Every proposal is reviewed and applied by the analyst; you never write the file "
    "yourself and cannot read the data itself."
)
_EDIT_OUTCOME = (
    "Your change is written into the analyst's open notebook and marimo runs it straight "
    "away — there is no Apply click. The call comes back with a value-free OBSERVATION "
    "of what actually happened (whether the cell ran, the error if it did not, and the "
    "column names and dtypes of what it produced): READ it, fix anything wrong with "
    "another call, and keep going until the analysis is right rather than handing back "
    "a half-finished notebook. Only names, types and status ever come back — you still "
    "cannot read the data itself."
)


def _outcome(write_tool: str) -> str:
    return _EDIT_OUTCOME if write_tool == EDIT_TOOL_NAME else _PROPOSE_OUTCOME


def _tool_guide(write_tool: str) -> str:
    """The read + write tool guide, naming the write tool as THIS session registered it.

    A function rather than a constant because the ONE write tool has two names, one per
    mode (:data:`mooring.ai.tools.WRITE_TOOL_NAMES`), and this text rides the system
    message on EVERY turn — a hard-coded name here is a standing instruction to call a
    tool that does not exist in the other mode. Callers pass
    :func:`mooring.ai.tools.write_tool_name`, the same helper ``build_tool_specs`` uses
    to REGISTER it, so the prompt and the toolset cannot disagree.
    """
    return (
        "\n\nYou have tools to inspect this workspace WITHOUT ever seeing data values:\n"
        "- mooring_list_datasets — list available datasets\n"
        "- mooring_get_schema(dataset) — a dataset's column names + dtypes\n"
        "- mooring_read_notebook_source — the notebook's code as it is RIGHT NOW, with each "
        "cell's current index\n"
        f"- {write_tool}(edits, appends, deletes, cells, rationale) — the ONE "
        "tool that changes the notebook, whatever the change is. `appends` adds brand-new "
        "cells at the end; `edits` replaces existing ones; `deletes` removes them; `cells` "
        "rewrites the notebook wholesale (rare — it discards every cell you do not carry "
        "over). Mix edits, appends and deletes freely in one call; they arrive as one patch "
        "the analyst reviews together.\n"
        "  PREFER EDITING the cell that already does the thing over appending a near-"
        "duplicate: two cells defining the same name stop BOTH of them and everything "
        "downstream.\n"
        "  Every `edits` and `deletes` entry carries `expect`: the first line of the cell you "
        "believe is at that index, copied from the cell view. mooring compares it with the "
        "real cell and refuses the whole change unless it matches that cell AND NO OTHER — "
        "that is what stops a stale index writing over a cell you never read, so send what "
        "you actually saw, never a guess. When the first line does not single a cell out — "
        "every markdown cell begins `mo.md(\"\"\"`, so one line never does — send the next line "
        "or two as well. Take indices from the cell view you were given, but that view is a "
        "SNAPSHOT: "
        "once anything has been applied this session, call mooring_read_notebook_source "
        "first, because inserting or deleting a cell renumbers the ones after it. A `cells` "
        "rewrite carries `expect_cells` instead — how many cells you believe the notebook has "
        "right now.\n"
        f"To CHANGE the notebook, call {write_tool}. Cell code is the BODY "
        "ONLY: top-level statements "
        "with NO '@app.cell', NO 'def _():', and NO trailing 'return (...)' (mooring adds that "
        "wrapper for you). WHERE the notebook is shown to you as indexed cells (each under a "
        "'# === cell N ===' line), those are already in exactly that body-only form — write "
        "them back the same way; where it is instead shown raw, it says so. "
        + _outcome(write_tool)
    )


# Propose mode's sentence here is byte-for-byte the one this guide has always carried:
# it says what the call PRODUCES, which the main guide's shared outcome sentence does
# not need to (that guide has just described the fields). Edit mode reuses the shared
# one, since what it produces is the observation.
_WRITE_ONLY_PROPOSE = "It creates a local, reviewable patch and never writes by itself."


def _write_only_guide(write_tool: str) -> str:
    """The guide for the general-provider conversation, which gets the write tool and
    no workspace read tools. Mode-aware for the same reason :func:`_tool_guide` is: the
    hub wires an applier into BOTH routed children, so this path reaches edit mode too."""
    return (
        f"\n\nYou have one tool: {write_tool}. "
        + (_EDIT_OUTCOME if write_tool == EDIT_TOOL_NAME else _WRITE_ONLY_PROPOSE)
        + " Workspace read tools are disabled for this general-provider conversation; "
        "use only the notebook snapshot already in your context."
    )

_INVESTIGATOR_GUIDE = (
    "\n\nYou are INVESTIGATING on another assistant's behalf. Use the read tools to inspect "
    "this workspace WITHOUT ever seeing data values:\n"
    "- mooring_list_datasets — list available datasets\n"
    "- mooring_get_schema(dataset) — a dataset's column names + dtypes\n"
    "- mooring_read_notebook_source — the notebook's current code, with each cell's index\n"
    "Then ANSWER the question in prose, citing the schema / column / table / measure NAMES "
    "and code facts you found. You have NO way to write, propose, or apply anything — do not "
    "try, and never ask for a data value. Keep your answer focused and self-contained; it is "
    "handed straight back as findings for the main assistant to act on."
)

_INVESTIGATE_GUIDE = (
    "\n\nWhen a task splits into INDEPENDENT parts (understand several notebooks, map several "
    "tables / semantic models, or plan a join across datasets), call mooring_investigate with "
    "a list of value-free sub-questions: separate read-only assistants research them IN "
    "PARALLEL and their findings come back merged, so you can then propose ONE change. Prefer "
    "it over asking many read questions yourself one at a time. Never put a data value in a "
    "sub-question — only names / paths and plain-English asks."
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

_CATALOG_TOOL_GUIDE = (
    "\n\nA repo-wide NOTEBOOK CATALOG is available (every notebook's title, description, "
    "imports, declared inputs/checks, and SQL tables — never another notebook's code, its "
    "outputs, or any data value):\n"
    "- mooring_list_notebooks — every notebook with its title\n"
    "- mooring_search_notebooks(query) — find notebooks by metric / dataset / table term\n"
    "- mooring_describe_notebook(notebook) — one notebook's title, inputs, and checks.\n"
    "Before proposing a NEW analysis, search the catalog: if a teammate already built it, "
    "say so and point at that notebook instead of duplicating the work."
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
        catalog=None,
        read_only: bool = False,
        run_investigation=None,
        applier=None,
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
        # The traceback guard needs the workspace (to bound its source re-read)
        # and the notebook (for the known-token rescue) — both already travel
        # into this ctor, so no route/hub arming call exists to forget.
        self.configure_traceback_guard(
            enabled=traceback_guard, workspace=workspace, notebook_rel=notebook_rel
        )
        self._model = (model or "").strip()
        # Normalise the picker's "leave it to the model" sentinels to None here, in the
        # ctor every Copilot path flows through: otherwise "default" rides on as a
        # LITERAL reasoning_effort into client.create_session (see _aopen), which is not
        # a value the SDK accepts. The hub's ai.reasoning_effort is free text, so the
        # word can reach us from Settings as well as from the picker.
        effort = (reasoning_effort or "").strip()
        self._reasoning_effort = None if effort.lower() in _EFFORT_SENTINELS else effort
        self._read_only = read_only
        self._allow_read_tools = bool(allow_read_tools)
        self._trusted_customer_data = bool(trusted_customer_data)
        self._output_guard = output_guard
        # A read-only investigate sub-agent: no propose/edit tool and no
        # mooring_investigate (so it cannot write or recurse). Forced off under read_only
        # even if a run_investigation were mis-wired — belt-and-suspenders for depth-1.
        self._run_investigation = None if read_only else run_investigation
        # The in-turn write capability, wired the SAME way as emit_proposal and gated by
        # the same read_only rule (see _aopen). None -> the tool stays in propose mode.
        self._applier = None if read_only else applier
        # The name the write tool will actually be REGISTERED under below (_aopen passes
        # the same `self._applier is not None` through to build_tools), resolved by the
        # one helper both sides share so the prompt cannot name a tool the session does
        # not have.
        write_tool = write_tool_name(self._applier is not None)
        guide = (
            _INVESTIGATOR_GUIDE
            if read_only
            else (
                _tool_guide(write_tool)
                if self._allow_read_tools
                else _write_only_guide(write_tool)
            )
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
            # A read-only session registers NO write surface (the load-bearing privacy
            # gate): no propose/edit tool, only the value-free read tools.
            emit_proposal=None if self._read_only else self._emit_proposal,
            emit_proposal_patch=None if self._read_only else self._emit_proposal_patch,
            # ...and the same gate again for the WRITE capability, which is strictly
            # stronger than a proposal: no applier (or a read-only sub-agent) leaves the
            # tool in propose mode, exactly as it shipped.
            apply_edit=None if self._read_only or self._applier is None else self._apply_edit,
            # The portable stop signal. The SDK owns this tool loop, so the analyst's
            # Cancel is enforced at the boundary of every call it makes.
            cancelled=self.cancel_requested,
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

    def _emit_tool_progress(self, text: str) -> None:
        """A value-free in-flight cue for a long-running tool (the investigate fan-out),
        on the SAME ``tool`` progress channel the SDK's TOOL_EXECUTION_PROGRESS uses. It
        carries counts/statuses only, goes to the local UI (never the model), and touches
        the activity clock so a multi-minute investigation is never idle-reaped."""
        self.touch()
        self._broadcast(ChatEvent("tool", {"progress": text}))

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
        # The start of a turn — the ONE place both send paths meet, so this is where the
        # stop flag is re-armed. Clearing it anywhere later would let one Cancel go on
        # refusing every tool call in every turn that followed it.
        self.clear_cancel()
        future = asyncio.run_coroutine_threadsafe(self._session.send(text), self._loop)
        try:
            future.result(timeout=_SEND_TIMEOUT)
        except Exception as exc:  # noqa: BLE001  # surface to the chat, don't crash the hub
            from mooring.ai.copilot import friendly_error

            self._broadcast(ChatEvent("fail", {"text": friendly_error(str(exc))}))

    # -- cancellation -------------------------------------------------------

    def request_cancel(self) -> None:
        """Stop the turn in flight: raise the portable flag, then ALSO ask the SDK.

        The flag (base class) is what actually guarantees a stop here — the Copilot SDK
        runs its own tool loop and mooring cannot break out of it, so every tool call the
        agent makes from now on comes back as a terminal "cancelled" error and the loop
        converges. The SDK's own ``session.abort()`` is a genuine bonus on top: it ends
        the turn the runtime is processing right now (mid-completion, before the next
        tool call), and its docstring is explicit that the session stays valid for new
        messages afterwards — which is the property the next turn depends on.
        """
        already = self.cancel_requested()
        super().request_cancel()
        if not already:
            self._abort_sdk_turn()

    def _abort_sdk_turn(self) -> None:
        """Best-effort ``session.abort()`` on the session's own loop thread.

        Duck-typed and fully swallowed: the SDK is an optional extra whose surface can
        move, and this is an EXTRA stop, not the mechanism. If it is missing or fails,
        the tool-boundary flag has already taken effect and the turn still ends.
        """
        session, loop = self._session, self._loop
        if session is None or loop is None or not loop.is_running():
            return
        abort = getattr(session, "abort", None)
        if not callable(abort):
            return
        try:
            asyncio.run_coroutine_threadsafe(abort(), loop).result(timeout=_ABORT_TIMEOUT)
        except Exception:  # noqa: BLE001 - never let a best-effort abort break Cancel
            pass

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
