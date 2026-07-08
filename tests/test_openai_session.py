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
import time
import types

import polars as pl
import pytest

from mooring.ai.chat import ChatEvent  # noqa: F401  (documents the event shape)
from mooring.ai.openai_session import OpenAIChatSession

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
VALID_CARD = "4012888888881881"  # Luhn-valid (shared with test_egress/test_pii)


# -- a scriptable fake of the OpenAI streaming client ---------------------------


def _chunk(content=None, tool_calls=None, finish=None, empty=False):
    if empty:  # Azure's leading content-filter chunk / a usage-only final chunk
        return types.SimpleNamespace(choices=[])
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta, finish_reason=finish)])


def _tc(index, tc_id=None, name=None, args=None):
    fn = types.SimpleNamespace(name=name, arguments=args)
    return types.SimpleNamespace(index=index, id=tc_id, function=fn)


class _FakeCompletions:
    def __init__(self, scripted):
        self._scripted = scripted
        self.calls: list[dict] = []

    def create(self, **kwargs):
        idx = len(self.calls)
        self.calls.append(kwargs)
        chunks = self._scripted[idx] if idx < len(self._scripted) else [_chunk(finish="stop")]
        return iter(chunks)


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
