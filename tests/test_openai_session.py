"""The OpenAI chat session must run the tool loop correctly AND stay value-blind.

These drive :class:`mooring.ai.openai_session.OpenAIChatSession` with a FAKE OpenAI
client (injected via ``client_factory``), so they need no ``openai`` package and no
network. They pin the adversarial-review must-fixes: ``store=False`` on every
request, function-only tools (never a hosted tool), the ``SECRET`` fixture never on
the wire, the PII gate running BEFORE anything is enqueued, and fail-closed dispatch
of an unknown tool name.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import types

import polars as pl
import pytest

from mooring.ai.chat import ChatEvent  # noqa: F401  (documents the event shape)
from mooring.ai.openai_session import DEFAULT_MAX_TOOL_ITERS, OpenAIChatSession

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
VALID_CARD = "4012888888881881"  # Luhn-valid (shared with test_egress/test_pii)


# -- a scriptable fake of the OpenAI streaming client ---------------------------


def _chunk(content=None, tool_calls=None, finish=None, empty=False, **delta_extra):
    """One streamed chunk. ``delta_extra`` sets EXTRA attributes on the delta —
    ``reasoning_content=...`` / ``reasoning=...``, the non-standard fields LiteLLM,
    DeepSeek, Qwen and vLLM stream a model's thinking on. Passed through as **kwargs
    rather than named parameters on purpose: a plain ``_chunk()`` then leaves those
    attributes ABSENT (not None), which is what a canonical OpenAI chunk looks like and
    what every other test in this file keeps exercising."""
    if empty:  # Azure's leading content-filter chunk / a usage-only final chunk
        return types.SimpleNamespace(choices=[])
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls, **delta_extra)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta, finish_reason=finish)])


def _tc(index, tc_id=None, name=None, args=None):
    fn = types.SimpleNamespace(name=name, arguments=args)
    return types.SimpleNamespace(index=index, id=tc_id, function=fn)


class _ScriptedError(Exception):
    """A scripted API failure. A PLAIN Exception subclass on purpose: the session must
    recognise a rejected parameter without importing ``openai`` (an optional extra), and
    a gateway's error class is not knowable anyway. ``status_code`` is set only when
    asked for, so a front-end that exposes none can be scripted too."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


# The 400 gpt-5.6-sol returns for every request mooring makes (it always sends tools).
EFFORT_400 = (
    "Error code: 400 - {'error': {'message': \"Function tools with reasoning_effort are "
    "not supported for gpt-5.6-sol in /v1/chat/completions. To use function tools, use "
    "/v1/responses or set reasoning_effort to 'none'.\", 'type': 'invalid_request_error', "
    "'param': 'reasoning_effort', 'code': None}}"
)


def _play(chunks):
    """Yield the scripted chunks, RAISING any exception scripted among them — that is a
    stream which fails part-way, AFTER deltas have already gone out to subscribers."""
    for item in chunks:
        if isinstance(item, BaseException):
            raise item
        yield item


class _FakeCompletions:
    def __init__(self, scripted):
        self._scripted = scripted
        self.calls: list[dict] = []

    def create(self, **kwargs):
        idx = len(self.calls)
        self.calls.append(kwargs)
        entry = self._scripted[idx] if idx < len(self._scripted) else [_chunk(finish="stop")]
        if isinstance(entry, BaseException):
            raise entry  # a create() that fails before any chunk is produced
        return _play(entry)


class _FakeClient:
    def __init__(self, scripted):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(scripted))


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "data").mkdir()
    pl.DataFrame({"region": [SECRET], "amount": [123456]}).write_parquet(
        tmp_path / "data" / "sales.parquet"
    )
    (tmp_path / "nb.py").write_text("import marimo\n# notebook code\n", "utf-8")
    return tmp_path


def _session(ws, scripted, model="gpt-4o", **kw):
    client = _FakeClient(scripted)
    session = OpenAIChatSession(
        model=model,
        system_context="SYSTEM CONTEXT: schema + source only.",
        workspace=ws,
        folders=("data",),
        notebook_rel="nb.py",
        client_factory=lambda: client,
        **kw,
    )
    session.start(block=True)
    return session, client.chat.completions


def _drain(q, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    events = []
    while time.monotonic() < deadline:
        try:
            ev = q.get(timeout=0.2)
        except queue.Empty:
            continue
        events.append(ev)
        if ev.kind == until:
            break
    return events


# -- the tool-calling loop ------------------------------------------------------


def test_full_turn_tool_call_then_message(ws):
    scripted = [
        [  # 1st completion: the model asks for the schema (args streamed in fragments)
            _chunk(tool_calls=[_tc(0, tc_id="call_1", name="mooring_get_schema", args='{"dataset"')]),
            _chunk(tool_calls=[_tc(0, args=': "data/sales.parquet"}')]),
            _chunk(finish="tool_calls"),
        ],
        [_chunk(content="Here "), _chunk(content="you go."), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted)
    q = session.subscribe()
    session.send("show me the schema")
    events = _drain(q, until="idle")
    session.close()

    kinds = [e.kind for e in events]
    assert "tool" in kinds and "tool_done" in kinds and "message" in kinds and "idle" in kinds
    deltas = "".join(e.data["text"] for e in events if e.kind == "delta")
    assert deltas == "Here you go."
    [msg] = [e for e in events if e.kind == "message"]
    assert msg.data["text"] == "Here you go."
    tool_ev = next(e for e in events if e.kind == "tool")
    assert tool_ev.data["name"] == "mooring_get_schema"

    # Two requests: the tool round-trip then the final answer.
    assert len(completions.calls) == 2
    # The 2nd request carries the assistant tool_calls turn + the tool RESULT message,
    # and that result holds the schema (column names) but never the data value.
    second = completions.calls[1]["messages"]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_1"
    assert "region" in tool_msgs[0]["content"] and "amount" in tool_msgs[0]["content"]


def test_every_request_is_value_blind(ws):
    scripted = [[_chunk(content="hi"), _chunk(finish="stop")]]
    session, completions = _session(ws, scripted)
    q = session.subscribe()
    session.send("hello")
    _drain(q, until="idle")
    session.close()

    for call in completions.calls:
        # store=False (the OpenAI analogue of enable_session_store=False) — pinned,
        # not left to a default a gateway/Azure base_url could change.
        assert call["store"] is False
        # Only mooring's own function tools; NEVER a hosted data-reaching tool.
        for tool in call.get("tools", []):
            assert tool["type"] == "function"
            assert tool["function"]["name"].startswith("mooring_")
        # Exactly one system message, first, and it is the assembled context.
        messages = call["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == session._system_context
        assert sum(1 for m in messages if m["role"] == "system") == 1
        # The data value never rides any outbound field.
        assert SECRET not in json.dumps(call, default=str)


def test_the_propose_tool_emits_an_apply_proposal(ws):
    # The Apply card is driven by a "proposal" event, which fires ONLY when the model
    # calls a propose tool. Prove the OpenAI loop wires that end-to-end (the flow the
    # user reported missing): a mooring_propose_notebook_edit call -> a proposal event
    # with code. The flat `code` shape is deliberate: it is the ONE tool's shape
    # tolerance for a model that reaches for the retired append tool's arguments.
    scripted = [
        [
            _chunk(
                tool_calls=[
                    _tc(
                        0,
                        tc_id="p1",
                        name="mooring_propose_notebook_edit",
                        args='{"code": "summary = df.describe()", "rationale": "summarise"}',
                    )
                ]
            ),
            _chunk(finish="tool_calls"),
        ],
        [_chunk(content="Proposed a cell."), _chunk(finish="stop")],
    ]
    session, _ = _session(ws, scripted)
    q = session.subscribe()
    session.send("summarise the data")
    events = _drain(q, until="idle")
    session.close()
    proposals = [e for e in events if e.kind == "proposal"]
    assert proposals, "a propose-tool call must emit a proposal event (the Apply card)"
    assert proposals[0].data["code"] == "summary = df.describe()"
    assert proposals[0].data.get("rationale") == "summarise"


def test_store_omitted_for_a_custom_endpoint(ws):
    # The provider passes store=None for a custom base_url (an OpenAI-compatible server
    # may reject the unknown field); the request then omits it entirely.
    stop = [[_chunk(content="ok"), _chunk(finish="stop")]]
    session, completions = _session(ws, stop, store=None)
    q = session.subscribe()
    session.send("hi")
    _drain(q, until="idle")
    session.close()
    assert "store" not in completions.calls[0]


def test_unknown_tool_is_refused_fail_closed(ws):
    scripted = [
        [_chunk(tool_calls=[_tc(0, tc_id="c1", name="evil_shell", args="{}")]), _chunk(finish="tool_calls")],
        [_chunk(content="ok"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted)
    q = session.subscribe()
    session.send("run something")
    events = _drain(q, until="idle")
    session.close()

    done = next(e for e in events if e.kind == "tool_done")
    assert done.data["success"] is False
    tool_msg = [m for m in completions.calls[1]["messages"] if m.get("role") == "tool"][0]
    assert "unknown tool" in tool_msg["content"] and "evil_shell" in tool_msg["content"]


def test_empty_choices_chunk_is_tolerated(ws):
    # Azure emits a leading empty-choices chunk and a usage-only final one; neither
    # must crash the stream loop.
    scripted = [[_chunk(empty=True), _chunk(content="safe"), _chunk(finish="stop"), _chunk(empty=True)]]
    session, _ = _session(ws, scripted)
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="idle")
    session.close()
    assert "".join(e.data["text"] for e in events if e.kind == "delta") == "safe"


# -- the inherited PII gate runs BEFORE the wire --------------------------------


def test_pii_prompt_is_held_and_never_forwarded(ws):
    scripted = [[_chunk(content="ok"), _chunk(finish="stop")]]
    session, completions = _session(ws, scripted, pii_enabled=True, pii_block=True)
    q = session.subscribe()
    session.send(f"look at card {VALID_CARD}")
    # The gate runs synchronously in send(): a checksum hit is HELD (a pii event with
    # a confirm token) and nothing is enqueued — so no request reaches the wire.
    pii = _drain(q, until="pii")
    token = next(e.data["token"] for e in pii if e.kind == "pii" and "token" in e.data)
    time.sleep(0.3)
    assert completions.calls == []  # the raw prompt never left

    session.send_confirmed(token)  # the analyst's "Send anyway"
    _drain(q, until="idle")
    session.close()
    assert len(completions.calls) == 1
    assert VALID_CARD in json.dumps(completions.calls[0]["messages"])  # forwarded verbatim on confirm


def test_traceback_raw_never_reaches_the_wire(ws):
    # The inherited traceback valve sanitises-and-holds: only the value-safe rewrite
    # can ever be forwarded, and only after the analyst confirms.
    scripted = [[_chunk(content="fixed"), _chunk(finish="stop")]]
    session, completions = _session(ws, scripted, traceback_guard=True)
    q = session.subscribe()
    session.send(
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        f"KeyError: '{SECRET}'"
    )
    tb = _drain(q, until="traceback")
    ev = next(e for e in tb if e.kind == "traceback")
    assert SECRET not in ev.data["preview"]  # the held rewrite is value-safe
    time.sleep(0.3)
    assert completions.calls == []  # nothing forwarded yet

    session.send_confirmed(ev.data["token"])
    _drain(q, until="idle")
    session.close()
    wire = json.dumps(completions.calls[0]["messages"])
    assert SECRET not in wire  # the raw paste is dropped by construction
    assert "redacted" in wire  # the sanitised rewrite is what went out


def test_tool_result_is_scrubbed_on_the_wire(ws):
    # A checksum value in the notebook source must not ride the tool RESULT message
    # to the model — the handler scrubs, and to_openai_tool_message is the gateway.
    (ws / "nb.py").write_text(f"import marimo\nacct = {VALID_CARD}\n", "utf-8")
    scripted = [
        [_chunk(tool_calls=[_tc(0, tc_id="r1", name="mooring_read_notebook_source", args="{}")]), _chunk(finish="tool_calls")],
        [_chunk(content="ok"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted)
    q = session.subscribe()
    session.send("read the notebook")
    _drain(q, until="idle")
    session.close()
    tool_msg = [m for m in completions.calls[1]["messages"] if m.get("role") == "tool"][0]
    assert VALID_CARD not in tool_msg["content"]  # the checksum line was withheld


def test_reasoning_effort_only_for_reasoning_models(ws):
    # The name-prefix check is an ADVISORY pre-filter (the ladder below is the
    # authority), but it still earns its keep: it spares an obvious chat model a
    # wasted 400 round-trip.
    stop = [[_chunk(content="ok"), _chunk(finish="stop")]]
    # A plain chat model must NOT receive reasoning_effort (it would 400).
    plain, plain_calls = _session(ws, stop, model="gpt-4o", reasoning_effort="high")
    q = plain.subscribe()
    plain.send("hi")
    _drain(q, until="idle")
    plain.close()
    assert "reasoning_effort" not in plain_calls.calls[0]

    # A reasoning model DOES.
    reasoning, reasoning_calls = _session(ws, stop, model="o3-mini", reasoning_effort="high")
    q = reasoning.subscribe()
    reasoning.send("hi")
    _drain(q, until="idle")
    reasoning.close()
    assert reasoning_calls.calls[0]["reasoning_effort"] == "high"


# -- streamed reasoning: shown to the human, never part of the conversation -----
# Behind a custom base_url (LiteLLM / Azure APIM / OpenRouter / vLLM / Ollama) a
# reasoning model streams its THINKING on `delta.reasoning_content` — a gateway
# extension, not OpenAI's schema — and mooring used to drop it, so the window sat dead
# for the whole think. It is now forwarded as a delta flagged `reasoning`. The invariant
# these tests exist to pin: it is DISPLAY-ONLY. It never joins the assistant's text,
# never reaches self._messages, and so never rides back out on the next request.

REASONING = "REASONING_TRACE_DO_NOT_KEEP"


def _texts(events, *, reasoning):
    """The delta text on ONE channel: the reasoning asides, or the answer."""
    return "".join(
        e.data["text"]
        for e in events
        if e.kind == "delta" and (e.data.get("reasoning") is True) == reasoning
    )


def test_reasoning_deltas_are_broadcast_flagged_and_never_join_the_answer(ws):
    scripted = [
        [
            _chunk(reasoning_content="Let me "),
            _chunk(reasoning_content=REASONING),
            _chunk(content="The answer "),
            _chunk(content="is 42."),
            _chunk(finish="stop"),
        ]
    ]
    session, completions = _session(ws, scripted)
    q = session.subscribe()
    session.send("think about it")
    events = _drain(q, until="idle")
    session.close()

    # It reached the UI, on its own channel...
    assert _texts(events, reasoning=True) == "Let me " + REASONING
    # ...and the answer channel is exactly what it was before this existed. Ordinary
    # content deltas carry NO `reasoning` key at all, so a consumer that never heard of
    # the flag is untouched.
    answer_deltas = [e for e in events if e.kind == "delta" and "reasoning" not in e.data]
    assert "".join(e.data["text"] for e in answer_deltas) == "The answer is 42."

    # The final message the UI swaps in is the answer, with no trace of the think.
    [msg] = [e for e in events if e.kind == "message"]
    assert msg.data["text"] == "The answer is 42."
    assert REASONING not in msg.data["text"]

    # And nothing of it is on the wire — not this request, and not a later one.
    assert REASONING not in json.dumps(completions.calls)


def test_reasoning_never_enters_the_conversation_sent_on_the_next_request(ws):
    """After a turn that streamed reasoning, the conversation this session carries
    forward — the exact payload of the NEXT request — is byte-for-byte what it would
    have been had the gateway streamed no thinking at all.

    The tool call in turn 1 is load-bearing, not scene-dressing. ``_run_turn`` appends an
    assistant message **only** on the tool-call branch: a plain question-and-answer
    completion is never recorded (OpenAI is stateless, so mooring re-sends only what it
    must). Compared across two no-tool turns this assertion has nothing to compare — both
    payloads are ``['system', 'user', 'user']`` no matter what the model said, so even a
    TOTAL leak of the assistant's own text passes it. That was the first shape of this
    test, and it was vacuous. The role assertion below is what keeps it honest: if the
    assistant turn ever stops being recorded, this fails loudly instead of going quiet.
    """

    def script(*, thinking):
        think = [_chunk(reasoning_content=REASONING)] if thinking else []
        return [
            [
                *think,
                _chunk(content="Reading the schema first."),
                _chunk(
                    tool_calls=[
                        _tc(
                            0,
                            tc_id="call_1",
                            name="mooring_get_schema",
                            args='{"dataset": "data/sales.parquet"}',
                        )
                    ]
                ),
                _chunk(finish="tool_calls"),
            ],
            [_chunk(content="Two columns."), _chunk(finish="stop")],
        ]

    payloads = []
    for thinking in (True, False):
        session, completions = _session(ws, script(thinking=thinking))
        q = session.subscribe()
        session.send("what columns?")
        _drain(q, until="idle")
        session.close()
        # The SECOND request carries the whole recorded conversation: the assistant's
        # tool-call turn and the tool reply that answers it.
        payloads.append(completions.calls[1]["messages"])

    # Anti-vacuity, first: the assistant's own words really are in what we compare.
    assert [m["role"] for m in payloads[0]] == ["system", "user", "assistant", "tool"]
    assert payloads[0][2]["content"] == "Reading the schema first."
    # …and now the comparison means something.
    assert payloads[0] == payloads[1]
    assert REASONING not in json.dumps(payloads[0])


def test_a_reasoning_field_that_is_none_or_absent_is_tolerated(ws):
    # Gateways vary: most chunks carry neither field, many carry it explicitly None
    # alongside real content, and a couple send a structured block instead of a string.
    # None of those may raise, and none may be rendered.
    scripted = [
        [
            _chunk(content=None, reasoning_content=None),  # present-but-None
            _chunk(content="ok", reasoning_content=None),  # None beside real content
            _chunk(content="!", reasoning=[{"type": "thinking"}]),  # not a string
            _chunk(finish="stop"),  # the field absent entirely
        ]
    ]
    session, _ = _session(ws, scripted)
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="idle")
    session.close()

    assert _texts(events, reasoning=True) == ""  # nothing to show, nothing shown
    [msg] = [e for e in events if e.kind == "message"]
    assert msg.data["text"] == "ok!"


def test_the_reasoning_field_fallback_name_is_read_too(ws):
    # A few front-ends spell it `reasoning`. `reasoning_content` wins where both are
    # sent (it is the more common and the more specific), so this pins the fallback.
    scripted = [
        [
            _chunk(reasoning="via the fallback name"),
            _chunk(reasoning_content="preferred", reasoning="ignored"),
            _chunk(content="done"),
            _chunk(finish="stop"),
        ]
    ]
    session, _ = _session(ws, scripted)
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="idle")
    session.close()

    assert _texts(events, reasoning=True) == "via the fallback namepreferred"
    assert _texts(events, reasoning=False) == "done"


def test_reasoning_interleaved_with_a_tool_call_leaves_the_call_intact(ws):
    # A reasoning model thinks, calls a tool, thinks again, then answers. The tool-call
    # accumulator keys on chunk index and must not be disturbed by chunks that carry
    # thinking and nothing else — including one arriving mid-arguments.
    scripted = [
        [
            _chunk(reasoning_content="I should look at the schema. "),
            _chunk(tool_calls=[_tc(0, tc_id="call_1", name="mooring_get_schema", args='{"dataset"')]),
            _chunk(reasoning_content=REASONING),  # thinking BETWEEN argument fragments
            _chunk(tool_calls=[_tc(0, args=': "data/sales.parquet"}')]),
            _chunk(finish="tool_calls"),
        ],
        [
            _chunk(reasoning_content="Now I can answer. "),
            _chunk(content="Two columns."),
            _chunk(finish="stop"),
        ],
    ]
    session, completions = _session(ws, scripted)
    q = session.subscribe()
    session.send("what columns?")
    events = _drain(q, until="idle")
    session.close()

    tool_ev = next(e for e in events if e.kind == "tool")
    assert tool_ev.data["name"] == "mooring_get_schema"
    tool_msgs = [m for m in completions.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_1"
    assert "region" in tool_msgs[0]["content"]  # the arguments reassembled correctly

    [msg] = [e for e in events if e.kind == "message"]
    assert msg.data["text"] == "Two columns."
    # The assistant tool-call turn records content=None, not the think that preceded it.
    assistant = [
        m for m in completions.calls[1]["messages"] if m.get("role") == "assistant"
    ][0]
    assert assistant["content"] is None
    assert REASONING not in json.dumps(completions.calls)


class _AgingCompletions:
    """A stream that ages the session's idle clock BEFORE its first chunk, standing in
    for the long silence a reasoning model spends thinking behind a gateway."""

    def __init__(self, box, chunks, aged_by):
        self._box = box
        self._chunks = list(chunks)
        self._aged_by = aged_by
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        def gen():
            self._box[0]._last_active = time.monotonic() - self._aged_by
            yield from self._chunks

        return gen()


def _aging_session(ws, chunks, aged_by=10_000.0):
    box: list = [None]
    completions = _AgingCompletions(box, chunks, aged_by)
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    session = OpenAIChatSession(
        model="gpt-4o",
        system_context="SYSTEM CONTEXT: schema + source only.",
        workspace=ws,
        folders=("data",),
        notebook_rel="nb.py",
        client_factory=lambda: client,
    )
    box[0] = session
    session.start(block=True)
    return session


def test_a_streamed_delta_keeps_the_turn_from_being_idle_reaped(ws):
    # The hub reaps idle chats when a chat is opened (`hub/routes/chat.py`), on
    # `ai.chat_idle_timeout_sec` (900s by default). Only send / tool progress / proposals
    # used to touch the clock, so a turn whose whole output is STREAMED TEXT — a long
    # think behind a gateway is the extreme case, and the timeout knob's help invites
    # raising the request budget past 900s — aged as if nothing were happening, and
    # opening a second chat could close it mid-answer. A delta is activity; it touches.
    #
    # This is a `ChatBroadcaster` behaviour, so it holds for every provider; it is driven
    # through the OpenAI session because that is the one with an injectable client.
    session = _aging_session(ws, [_chunk(content="the answer"), _chunk(finish="stop")])
    q = session.subscribe()
    session.send("hi")
    _drain(q, until="idle")
    idle_after_deltas = session.idle_seconds()
    session.close()

    # The teeth: a completion carrying NO delta leaves the aged clock exactly as it was,
    # so the assertion above is measuring the delta and nothing else.
    silent = _aging_session(ws, [_chunk(finish="stop")])
    q2 = silent.subscribe()
    silent.send("hi")
    _drain(q2, until="idle")
    idle_without_deltas = silent.idle_seconds()
    silent.close()

    assert idle_after_deltas < 60
    assert idle_without_deltas > 9_000


# -- the reasoning-effort ladder: the SERVER settles it, not the model name -----


def _notices(events):
    """The value-free ladder notices among a drained event stream."""
    return [
        e.data["text"]
        for e in events
        if e.kind == "message" and "reasoning" in e.data["text"].lower()
    ]


def test_reasoning_effort_400_retries_with_the_param_dropped(ws):
    # gpt-5.6-sol matches the "gpt-5" prefix but rejects reasoning_effort whenever
    # function tools are attached — and mooring ALWAYS attaches them, so without the
    # ladder every single request of the session fails.
    scripted = [
        _ScriptedError(EFFORT_400, 400),
        [_chunk(content="ok"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="idle")
    session.close()

    assert len(completions.calls) == 2
    assert completions.calls[0]["reasoning_effort"] == "high"
    # Rung (a): the param is DROPPED (the model keeps its own default reasoning);
    # everything else about the request is the same one, re-sent.
    assert "reasoning_effort" not in completions.calls[1]
    assert completions.calls[1]["model"] == "gpt-5.6-sol"
    assert completions.calls[1]["messages"] == completions.calls[0]["messages"]
    assert completions.calls[1]["tools"] == completions.calls[0]["tools"]

    # The turn completes normally: an answer, no failure, and ONE value-free notice
    # so the analyst can see why the effort setting did not apply.
    assert not [e for e in events if e.kind == "fail"]
    texts = [e.data["text"] for e in events if e.kind == "message"]
    assert "ok" in texts
    [notice] = _notices(events)
    assert str(ws) not in notice and SECRET not in notice
    assert SECRET not in json.dumps(completions.calls, default=str)


def test_the_effort_ladder_is_remembered_for_the_session(ws):
    scripted = [
        _ScriptedError(EFFORT_400, 400),
        [_chunk(content="one"), _chunk(finish="stop")],
        [_chunk(content="two"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("first")
    first = _drain(q, until="idle")
    session.send("second")
    second = _drain(q, until="idle")
    session.close()

    # ONE probe for the whole session: 2 creates for turn 1, exactly 1 for turn 2.
    assert len(completions.calls) == 3
    assert "reasoning_effort" not in completions.calls[2]
    assert not [e for e in first + second if e.kind == "fail"]
    assert len(_notices(first + second)) == 1  # told once, not once per turn


def test_the_effort_ladder_falls_back_to_none(ws):
    # Dropping the param is not always enough: the model's OWN default effort can be
    # what conflicts with tools, and the server says so again — so rung (b) sends the
    # remedy the error message itself suggests.
    scripted = [
        _ScriptedError(EFFORT_400, 400),
        _ScriptedError(EFFORT_400, 400),
        [_chunk(content="ok"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="idle")
    session.close()

    assert len(completions.calls) == 3
    assert completions.calls[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in completions.calls[1]
    assert completions.calls[2]["reasoning_effort"] == "none"
    assert not [e for e in events if e.kind == "fail"]
    assert len(_notices(events)) == 1


def test_a_gateway_error_without_a_status_code_still_ladders(ws):
    # Azure / LiteLLM / OpenAI-compatible front-ends reword the body and may expose no
    # status_code at all, so detection is string-first and must not require one.
    scripted = [
        _ScriptedError("BadRequest: unsupported parameter 'reasoning effort' for tool use"),
        [_chunk(content="ok"), _chunk(finish="stop")],
    ]
    assert not hasattr(scripted[0], "status_code")
    session, completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="idle")
    session.close()

    assert len(completions.calls) == 2
    assert "reasoning_effort" not in completions.calls[1]
    assert not [e for e in events if e.kind == "fail"]


def test_an_unrelated_400_is_not_retried(ws):
    # An error that does not blame the parameter is NOT ours to fix: it must fail on
    # exactly one request, never be turned into a double request.
    scripted = [
        _ScriptedError(
            "Error code: 400 - {'error': {'message': 'The model `nope` does not exist.', "
            "'type': 'invalid_request_error', 'param': None, 'code': 'model_not_found'}}",
            400,
        ),
        [_chunk(content="never reached"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="fail")
    session.close()

    assert len(completions.calls) == 1
    assert [e for e in events if e.kind == "fail"]
    assert not _notices(events)


def test_a_mid_stream_failure_is_never_retried(ws):
    # The ladder retries the create() CALL only. Once chunks are flowing the deltas
    # have been broadcast and cannot be un-broadcast, so a failure during iteration
    # surfaces as-is rather than re-issuing the request and replaying the text.
    scripted = [
        [_chunk(content="Half a sentence"), _ScriptedError(EFFORT_400, 400)],
        [_chunk(content="Half a sentence and the rest"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="fail")
    session.close()

    assert len(completions.calls) == 1  # the 2nd script entry is never reached
    deltas = "".join(e.data["text"] for e in events if e.kind == "delta")
    assert deltas == "Half a sentence"  # streamed once, not duplicated
    assert [e for e in events if e.kind == "fail"]


@pytest.mark.parametrize("sentinel", ["default", "Default", "auto", " AUTO "])
def test_the_default_effort_sentinel_sends_no_param(ws, sentinel):
    # The provider's effort picker offers "default" for "leave it to the model". The
    # session __init__ is the one choke point every path flows through, so normalising
    # the sentinel there covers hub chat, batch and investigate alike.
    stop = [[_chunk(content="ok"), _chunk(finish="stop")]]
    session, completions = _session(ws, stop, model="o3-mini", reasoning_effort=sentinel)
    q = session.subscribe()
    session.send("hi")
    _drain(q, until="idle")
    session.close()

    assert session._reasoning_effort is None
    assert "reasoning_effort" not in completions.calls[0]  # nothing on the wire


def test_the_ladder_arms_when_the_request_carried_no_effort_at_all(ws):
    # The SHIPPED default configures no reasoning_effort, so the request sends none —
    # and gpt-5.6-sol 400s anyway, because what it refuses alongside tools is a
    # non-"none" effort and the model's OWN default is one. Gating the ladder on "we
    # sent the param" therefore disarmed it in exactly the configuration most people
    # run: one request, one 400, no probe at all.
    scripted = [
        _ScriptedError(EFFORT_400, 400),
        [_chunk(content="ok"), _chunk(finish="stop")],
        [_chunk(content="again"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted, model="gpt-5.6-sol")
    q = session.subscribe()
    session.send("hi")
    first = _drain(q, until="idle")
    session.send("again")
    second = _drain(q, until="idle")
    session.close()

    assert "reasoning_effort" not in completions.calls[0]
    # Dropping a param that was never sent would re-send the IDENTICAL request, so the
    # only rung that can differ here is "none" — and it is the one taken.
    assert completions.calls[1]["reasoning_effort"] == "none"
    assert not [e for e in first + second if e.kind == "fail"]
    # ...and remembered: turn two is ONE request, still on the settled rung. (Consulting
    # the config before the settled mode would evaluate "none" back to "send nothing"
    # and re-probe every single turn.)
    assert len(completions.calls) == 3
    assert completions.calls[2]["reasoning_effort"] == "none"
    assert len(_notices(first + second)) == 1


# -- detection is STRICT: naming the field is not the same as refusing it -------


def test_a_transient_error_that_merely_lists_the_fields_is_not_a_rejection(ws):
    # A blip whose body enumerates the request's fields NAMES reasoning_effort without
    # refusing it. Arming on that is expensive, because the ladder's answer is sticky
    # for the session: one such blip would silently disable the analyst's setting for
    # the rest of the chat and blame the model for it in the notice.
    blip = (
        "Error code: 400 - Upstream backend hiccup while validating request "
        "(fields: model, messages, tools, reasoning_effort)"
    )
    scripted = [_ScriptedError(blip, 400)] + [
        [_chunk(content="ok"), _chunk(finish="stop")] for _ in range(3)
    ]
    session, completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("one")
    events = _drain(q, until="fail")
    for text in ("two", "three", "four"):
        session.send(text)
        events += _drain(q, until="idle")
    session.close()

    # One request for the blip (not a three-rung probe), and "high" still rides every
    # later turn — NOT ['high', absent, absent, absent].
    assert [c.get("reasoning_effort") for c in completions.calls] == ["high"] * 4
    assert not _notices(events)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_a_non_400_that_echoes_the_request_body_is_not_a_rejection(ws, status):
    # Gateways echo the request back inside auth / throttle / upstream errors, so the
    # body names the parameter and even reads as "Invalid ...". Only a 400 is THIS
    # REQUEST being refused; anything else must fail once and be reported as itself.
    echoed = (
        "Error code: %d - {'error': {'message': 'Invalid authentication. Request was: "
        "{model: gpt-5.6-sol, tools: [...], reasoning_effort: high}'}}" % status
    )
    scripted = [
        _ScriptedError(echoed, status),
        [_chunk(content="never reached"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="fail")
    session.close()

    assert len(completions.calls) == 1
    assert [e for e in events if e.kind == "fail"]
    assert not _notices(events)  # never misreported to the analyst as an effort fault


def test_a_structured_param_field_identifies_the_rejection(ws):
    # A front-end may name the offending parameter STRUCTURALLY rather than in the
    # prose (the OpenAI SDK lifts "param" out of the error body onto the exception).
    # Read duck-typed, because this module never imports openai.
    err = _ScriptedError("Error code: 400 - unsupported parameter for this model.", 400)
    err.param = "reasoning_effort"
    scripted = [err, [_chunk(content="ok"), _chunk(finish="stop")]]
    session, completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="idle")
    session.close()

    assert len(completions.calls) == 2
    assert "reasoning_effort" not in completions.calls[1]
    assert not [e for e in events if e.kind == "fail"]


def test_a_400_whose_body_was_lost_is_left_alone(ws):
    # A KNOWN, ACCEPTED blind spot, pinned here so nobody "fixes" it by loosening the
    # matcher back to a bare substring test: when the SDK cannot read the error body it
    # builds the exception with body=None, so the message is bare ("Error code: 400")
    # and .param is None too. Nothing identifies the parameter, so the request fails
    # exactly as it would have before the ladder existed.
    scripted = [
        _ScriptedError("Error code: 400", 400),
        [_chunk(content="never reached"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="fail")
    session.close()

    assert len(completions.calls) == 1
    assert [e for e in events if e.kind == "fail"]


def test_the_notice_does_not_assert_a_cause_it_cannot_know(ws):
    # The server may be refusing the PARAMETER, or only the configured VALUE. This body
    # is the second kind: 'high' is out of range where 'low' would be accepted. A notice
    # asserting "this model rejected the setting when tools are in play" is simply wrong
    # here, and it talks the analyst out of the one fix that would work.
    value_400 = (
        "Error code: 400 - {'error': {'message': \"Invalid value for 'reasoning_effort': "
        "'high'. Supported values are: 'low' and 'medium'.\", 'type': "
        "'invalid_request_error', 'param': 'reasoning_effort'}}"
    )
    scripted = [_ScriptedError(value_400, 400), [_chunk(content="ok"), _chunk(finish="stop")]]
    session, _completions = _session(ws, scripted, model="gpt-5.6-sol", reasoning_effort="high")
    q = session.subscribe()
    session.send("hi")
    events = _drain(q, until="idle")
    session.close()

    [notice] = _notices(events)
    assert "tools" not in notice.lower()  # no claim about tool calling
    assert "not that value" in notice  # both causes left open
    # Fixed text: nothing echoed back from the server, no value, no path, no model name.
    assert "high" not in notice and "gpt-5.6-sol" not in notice
    assert str(ws) not in notice and SECRET not in notice


def test_parallel_tool_calls_are_each_answered_before_the_next_request(ws):
    # OpenAI can return several tool_calls in one assistant turn; every tool_call_id
    # must be answered by exactly one role:"tool" message before the next request.
    scripted = [
        [
            _chunk(
                tool_calls=[
                    _tc(0, tc_id="a", name="mooring_list_datasets", args="{}"),
                    _tc(1, tc_id="b", name="mooring_list_datasets", args="{}"),
                ]
            ),
            _chunk(finish="tool_calls"),
        ],
        [_chunk(content="done"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted)
    q = session.subscribe()
    session.send("list twice")
    events = _drain(q, until="idle")
    session.close()

    assert sum(1 for e in events if e.kind == "tool") == 2
    second = completions.calls[1]["messages"]
    assistant = [m for m in second if m.get("role") == "assistant" and m.get("tool_calls")][0]
    assert [tc["id"] for tc in assistant["tool_calls"]] == ["a", "b"]
    tool_ids = [m["tool_call_id"] for m in second if m.get("role") == "tool"]
    assert tool_ids == ["a", "b"]  # both answered, in order, before the 2nd request


# -- cancellation (the analyst's stop button) -----------------------------------


class _BlockingStream:
    """One streamed completion that STALLS part-way until the test releases it.

    The point is to press Cancel while the worker thread really is mid-stream, from
    another thread, rather than to simulate that from inside the loop being tested.
    Records ``closed`` so a test can prove the abandoned stream was closed and is not
    still being billed.
    """

    def __init__(self, chunks, opened, release):
        self._chunks = list(chunks)
        self._opened = opened
        self._release = release
        self.closed = False

    def __iter__(self):
        for i, chunk in enumerate(self._chunks):
            if i == 1:  # one chunk has been delivered; hand control to the test
                self._opened.set()
                self._release.wait(timeout=5)
            yield chunk

    def close(self):
        self.closed = True


class _StreamCompletions:
    """Like ``_FakeCompletions`` but scripted with whole STREAM objects, so a test can
    supply one that blocks (and one that records ``close()``)."""

    def __init__(self, streams):
        self._streams = list(streams)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        idx = len(self.calls)
        self.calls.append(kwargs)
        if idx < len(self._streams):
            return self._streams[idx]
        return _play([_chunk(finish="stop")])


def _stream_session(ws, streams, **kw):
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_StreamCompletions(streams)))
    session = OpenAIChatSession(
        model="gpt-4o",
        system_context="SYSTEM CONTEXT: schema + source only.",
        workspace=ws,
        folders=("data",),
        notebook_rel="nb.py",
        client_factory=lambda: client,
        **kw,
    )
    session.start(block=True)
    return session, client.chat.completions


def test_cancel_stops_an_in_flight_turn_and_the_session_takes_the_next_message(ws):
    # The whole point of "no small iteration cap, a Cancel button instead": a stop must
    # actually stop THIS turn (closing the open stream rather than paying it out), and
    # must leave the session usable for the very next message.
    opened, release = threading.Event(), threading.Event()
    blocking = _BlockingStream(
        [_chunk(content="working"), _chunk(content=" on it"), _chunk(finish="stop")],
        opened,
        release,
    )
    session, completions = _stream_session(
        ws, [blocking, [_chunk(content="second answer"), _chunk(finish="stop")]]
    )
    q = session.subscribe()
    try:
        session.send("do something long")
        assert opened.wait(timeout=5), "the worker never reached the stream"
        session.request_cancel()
        release.set()

        events = _drain(q, until="idle")
        kinds = [e.kind for e in events]
        assert "cancelled" in kinds  # the UI is told the moment the analyst asks
        assert kinds[-1] == "idle"  # ...and the turn ends the ordinary way
        texts = [e.data.get("text", "") for e in events if e.kind == "message"]
        assert "(Stopped at your request.)" in texts
        assert "working" in texts  # the text already streamed is kept, not thrown away
        assert blocking.closed is True  # the abandoned completion was closed
        assert len(completions.calls) == 1  # no further round-trip was paid for

        # The session is still alive: the NEXT message runs a full, uncancelled turn.
        session.send("and now?")
        follow = _drain(q, until="idle")
        assert "second answer" in [
            e.data.get("text", "") for e in follow if e.kind == "message"
        ]
        assert session.cancel_requested() is False  # re-armed at the start of that turn
    finally:
        session.close()


def test_a_stop_during_a_think_keeps_the_reasoning_out_of_the_partial_answer(ws):
    # A cancel mid-stream KEEPS whatever the model had already said and replays it as
    # the turn's partial `message`. Reasoning is not part of that: a stop must not be
    # the side door through which display-only thinking lands in the transcript as the
    # assistant's own words (and, from there, in front of whoever reads the notebook).
    opened, release = threading.Event(), threading.Event()
    blocking = _BlockingStream(
        # One chunk carrying BOTH, which is what a gateway sends on the hand-over from
        # thinking to answering — and the only shape that puts the two in the partial.
        [
            _chunk(content="working", reasoning_content=REASONING),
            _chunk(content=" on it"),
            _chunk(finish="stop"),
        ],
        opened,
        release,
    )
    session, _ = _stream_session(ws, [blocking])
    q = session.subscribe()
    try:
        session.send("think hard")
        assert opened.wait(timeout=5), "the worker never reached the stream"
        session.request_cancel()
        release.set()
        events = _drain(q, until="idle")
    finally:
        session.close()

    texts = [e.data.get("text", "") for e in events if e.kind == "message"]
    assert "working" in texts  # the answer so far is kept, exactly as before
    assert not any(REASONING in t for t in texts)
    # The think DID reach the UI on its own channel — it is dropped from the answer,
    # not swallowed. (The stop lands after the first chunk, so exactly one arrives.)
    assert _texts(events, reasoning=True) == REASONING


def test_a_stop_pressed_while_a_turn_waits_in_the_queue_still_stops_it(ws):
    # The race the analyst actually saw: `send` returns, they press Stop, the UI says
    # "cancelled" — and the turn ran to completion anyway, because the flag was cleared
    # on the WORKER thread when it finally picked the turn up. The flag is re-armed when
    # a turn is ENQUEUED now (as the Copilot backend has always done), so a stop in that
    # window ends the queued turn before a single completion is paid for.
    opened, release = threading.Event(), threading.Event()
    blocking = _BlockingStream(
        [_chunk(content="one"), _chunk(content=" more"), _chunk(finish="stop")],
        opened,
        release,
    )
    session, completions = _stream_session(
        ws, [blocking, [_chunk(content="second answer"), _chunk(finish="stop")]]
    )
    q = session.subscribe()
    try:
        session.send("first")
        assert opened.wait(timeout=5), "the worker never reached the stream"
        session.send("second")  # queued behind the turn in flight
        session.request_cancel()  # ...and stopped before the worker reaches it
        release.set()

        events, idles = [], 0
        deadline = time.monotonic() + 5
        while idles < 2 and time.monotonic() < deadline:
            try:
                ev = q.get(timeout=0.2)
            except queue.Empty:
                continue
            events.append(ev)
            idles += ev.kind == "idle"
        assert idles == 2, "the queued turn never ended"
        assert len(completions.calls) == 1  # the queued turn paid for no completion
        texts = [e.data.get("text", "") for e in events if e.kind == "message"]
        assert texts.count("(Stopped at your request.)") == 2
        assert "second answer" not in texts
    finally:
        session.close()


def test_a_cancel_never_poisons_the_following_turn(ws):
    # The flag is cleared at the START of a turn, not at the end of the cancelled one:
    # a stop pressed with nothing running (or one that raced the end of a turn) must not
    # silently kill every turn after it.
    session, completions = _session(
        ws, [[_chunk(content="fresh"), _chunk(finish="stop")]]
    )
    q = session.subscribe()
    try:
        session.request_cancel()
        assert session.cancel_requested() is True
        session.send("hello")
        events = _drain(q, until="idle")
        assert "fresh" in [e.data.get("text", "") for e in events if e.kind == "message"]
        assert session.cancel_requested() is False
        assert len(completions.calls) == 1  # the turn really ran
    finally:
        session.close()


def test_cancel_between_tool_rounds_ends_the_turn_with_every_tool_call_answered(ws):
    # A cancel that lands while tools are being dispatched still has to leave the
    # conversation well-formed — every tool_call_id answered — or the NEXT turn is
    # rejected by the API for a half-finished one.
    scripted = [
        [
            _chunk(tool_calls=[_tc(0, tc_id="c1", name="mooring_list_datasets", args="{}")]),
            _chunk(finish="tool_calls"),
        ],
        [_chunk(content="never reached"), _chunk(finish="stop")],
    ]
    session, completions = _session(ws, scripted)
    q = session.subscribe()
    try:
        # The analyst presses stop while that tool is running: wrap the handler so the
        # cancel lands at exactly that point every run, instead of racing the loop.
        original = session._dispatch["mooring_list_datasets"]

        def cancel_during(invocation):
            out = original(invocation)
            session.request_cancel()
            return out

        session._dispatch["mooring_list_datasets"] = cancel_during
        session.send("list them")
        events = _drain(q, until="idle")
        assert "(Stopped at your request.)" in [
            e.data.get("text", "") for e in events if e.kind == "message"
        ]
        assert len(completions.calls) == 1  # the second completion was never requested
        # Every tool_call_id the assistant asked for has exactly one answer on record.
        asked = [
            tc["id"]
            for m in session._messages
            if m.get("role") == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]
        ]
        answered = [m["tool_call_id"] for m in session._messages if m.get("role") == "tool"]
        assert asked == answered == ["c1"]
    finally:
        session.close()


def test_closing_a_session_raises_the_stop_flag(ws):
    # A closed session's turn is over by definition; the flag makes an in-flight tool
    # loop converge at its next tool boundary instead of running on against a dead chat.
    session, _ = _session(ws, [[_chunk(content="hi"), _chunk(finish="stop")]])
    assert session.cancel_requested() is False
    session.close()
    assert session.cancel_requested() is True


# -- the runaway ceiling (a safety limit, not a work budget) --------------------


def test_the_tool_iteration_ceiling_comes_from_the_caller(ws):
    # The ceiling is the policy-folded `[ai] max_tool_iters`, passed IN by the caller.
    tool_round = [
        _chunk(tool_calls=[_tc(0, tc_id="c", name="mooring_list_datasets", args="{}")]),
        _chunk(finish="tool_calls"),
    ]
    session, completions = _session(ws, [tool_round, tool_round, tool_round], max_tool_iters=2)
    q = session.subscribe()
    try:
        session.send("loop forever")
        events = _drain(q, until="idle")
        assert len(completions.calls) == 2  # stopped at the ceiling the caller set
        [budget] = [
            e for e in events if e.kind == "message" and e.data.get("notice")
        ]
        text = budget.data["text"]
        assert "2 steps" in text
        # Abnormal stop, not a finished answer — and continuing is one message away.
        assert "runaway ceiling" in text and "not a finished answer" in text
        assert "continue" in text
    finally:
        session.close()


def test_the_default_ceiling_is_the_config_default(ws):
    # Nothing passed in falls back to the SAME number `[ai] max_tool_iters` ships, so a
    # default install and an un-wired caller can never disagree about the ceiling.
    from mooring.ai_config import AiConfig

    assert DEFAULT_MAX_TOOL_ITERS == AiConfig().max_tool_iters == 200
    session, _ = _session(ws, [[_chunk(content="hi"), _chunk(finish="stop")]])
    try:
        assert session._max_tool_iters == DEFAULT_MAX_TOOL_ITERS
    finally:
        session.close()


@pytest.mark.parametrize("bad", [0, -5, None])
def test_a_degenerate_ceiling_falls_back_rather_than_ending_every_turn(ws, bad):
    # A ceiling of 0 would end every turn before its first request. Fall back instead.
    session, _ = _session(
        ws, [[_chunk(content="hi"), _chunk(finish="stop")]], max_tool_iters=bad
    )
    try:
        assert session._max_tool_iters == DEFAULT_MAX_TOOL_ITERS
    finally:
        session.close()


def _two_calls(a="a", b="b"):
    return [
        _chunk(
            tool_calls=[
                _tc(0, tc_id=a, name="mooring_list_datasets", args="{}"),
                _tc(1, tc_id=b, name="mooring_list_datasets", args="{}"),
            ]
        ),
        _chunk(finish="tool_calls"),
    ]


def test_the_ceiling_counts_a_tool_call_once_not_once_per_layer(ws):
    # The ceiling is enforced on BOTH backends now, from one number. This loop spends it
    # here and the tool wrapper does not spend it again (`build_openai_tools` takes no
    # budget) — charging in both places would silently halve the analyst's setting. Two
    # calls per completion, a ceiling of 4: four calls, all of them really run.
    session, completions = _session(
        ws, [_two_calls("a1", "a2"), _two_calls("b1", "b2"), _two_calls()], max_tool_iters=4
    )
    q = session.subscribe()
    try:
        session.send("loop")
        _drain(q, until="idle")
        assert len(completions.calls) == 2
        replies = [m for m in session._messages if m.get("role") == "tool"]
        assert len(replies) == 4
        assert not any("runaway" in m["content"] for m in replies)
    finally:
        session.close()


def test_a_call_past_the_ceiling_is_answered_but_never_run(ws):
    # Every tool_call_id still needs exactly one reply or the API rejects the next turn —
    # but a batch of parallel calls must not RUN on a budget that has one step left.
    session, completions = _session(ws, [_two_calls("c1", "c2")], max_tool_iters=1)
    ran = []
    original = session._dispatch["mooring_list_datasets"]
    session._dispatch["mooring_list_datasets"] = lambda inv: (ran.append(1), original(inv))[1]
    q = session.subscribe()
    try:
        session.send("loop")
        _drain(q, until="idle")
        replies = [m["content"] for m in session._messages if m.get("role") == "tool"]
        assert len(replies) == 2 and len(ran) == 1
        assert "runaway ceiling" in replies[1] and "runaway" not in replies[0]
    finally:
        session.close()


# -- the write capability (edit mode) ------------------------------------------


def _captured_tool_kwargs(ws, monkeypatch, **kw):
    """Build a session, capturing what it hands :func:`build_openai_tools`.

    Returns ``(captured, session)``; the caller closes the session (closing it raises
    the stop flag, so the cancel predicate must be read before that)."""
    captured: dict = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr("mooring.ai.openai_session.build_openai_tools", fake_build)
    session, _ = _session(ws, [[_chunk(content="hi"), _chunk(finish="stop")]], **kw)
    return captured, session


def test_no_applier_leaves_the_write_tool_in_propose_mode(ws, monkeypatch):
    # `[ai] auto_apply = false` and the policy escape hatch both ride on this: with no
    # applier the tool proposes and the analyst clicks Apply, exactly as it shipped.
    captured, session = _captured_tool_kwargs(ws, monkeypatch)
    try:
        assert captured["apply_edit"] is None
        assert captured["emit_proposal"] is not None
        assert captured["emit_proposal_patch"] is not None
    finally:
        session.close()


def test_an_applier_is_wired_into_the_write_tool(ws, monkeypatch):
    captured, session = _captured_tool_kwargs(ws, monkeypatch, applier=lambda ops, why: None)
    try:
        assert captured["apply_edit"] == session._apply_edit
        assert captured["emit_proposal"] is not None  # propose events still exist
    finally:
        session.close()


def test_a_read_only_session_gets_no_write_capability_at_all(ws, monkeypatch):
    # The load-bearing privacy gate, extended: a read-only investigate sub-agent gets no
    # proposal callbacks AND no applier — even when one is mis-wired in, as here.
    captured, session = _captured_tool_kwargs(
        ws, monkeypatch, read_only=True, applier=lambda ops, why: "should never run"
    )
    try:
        assert captured["apply_edit"] is None
        assert captured["emit_proposal"] is None
        assert captured["emit_proposal_patch"] is None
        assert session._applier is None  # dropped at the ctor, not just at the builder
    finally:
        session.close()


def test_the_tool_boundary_always_gets_the_cancel_predicate(ws, monkeypatch):
    # Both providers stop the same way at the tool boundary; a session that did not hand
    # its predicate over would have no stop at all on the Copilot path.
    captured, session = _captured_tool_kwargs(ws, monkeypatch)
    try:
        assert captured["cancelled"] == session.cancel_requested
        assert captured["cancelled"]() is False
        session.request_cancel()
        assert captured["cancelled"]() is True  # the tools see the stop immediately
    finally:
        session.close()
