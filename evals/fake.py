"""A scripted provider: replays a recorded model output with no network and no
``openai`` package.

This is what makes the eval trustworthy. The harness's scoring logic is ordinary
code and can be ordinary-code wrong — a predicate that never fires would report a
weak model as capable, and nobody would notice, because the only way to check it
would be to run a weak model. So the harness is driven in the offline pytest suite
against outputs whose correct verdict is already known
(``tests/test_eval_harness.py``), and only the real-model sweep needs a key.

The fake replaces the HTTP client, not the session. A scripted turn is fed to a
real :class:`mooring.ai.openai_session.OpenAIChatSession` through its
``client_factory`` seam — the same seam ``tests/test_openai_session.py`` uses — so
the real tool loop, the real value-free tool handlers, the real propose gate and
the real egress minters all run. Nothing about mooring is stubbed except the model
itself. That matters: a fake that emitted proposal EVENTS directly would score a
harness against a pipeline the product does not have, and would sail straight past
a regression in the gate.
"""

from __future__ import annotations

import json
import types
from dataclasses import dataclass
from typing import Iterable

# -- the script DSL -----------------------------------------------------------


@dataclass(frozen=True)
class Say:
    """One completion that is plain assistant text and then stops."""

    text: str


@dataclass(frozen=True)
class Call:
    """One completion that asks for a tool, by name, with these arguments."""

    name: str
    args: dict


# THE one write tool. There used to be four; the three that expressed a change the
# general patch could already express were retired, so a scripted "wrong tool" is no
# longer a thing a model can do — the equivalent mistake is now the wrong FIELD, or a
# bad ``expect``. Both are scriptable through the helpers below.
PROPOSE = "mooring_propose_notebook_edit"


def propose_cell(code: str, rationale: str = "") -> Call:
    """A one-cell append, through the flat ``code`` form the tool still accepts.

    ``code`` is the RAW model output, wrapper leftovers and all: normalising it is
    mooring's job, and whether it succeeds is exactly what a case is measuring.
    """
    return Call(PROPOSE, {"code": code, "rationale": rationale})


def propose_cell_edit(index: int, expect: str, code: str, rationale: str = "") -> Call:
    """One edit. ``expect`` is REQUIRED and positional on purpose.

    ``expect`` is the model's CLAIM about what is at ``index`` — the first line of
    that cell as it last saw it — and mooring refuses the whole change if the claim
    is wrong. So the script has to carry it: a fake that looked the line up in the
    fixture would hand the scripted model information a real one has to earn by
    reading, and would make "the model got the index wrong" unscriptable, which is
    precisely the failure this bucket now measures. Pass ``""`` to script a model
    that omitted the claim.
    """
    return Call(
        PROPOSE,
        {"edits": [{"index": index, "expect": expect, "code": code}], "rationale": rationale},
    )


def propose_notebook_edit(
    edits: Iterable[dict] = (),
    appends: Iterable[str] = (),
    deletes: Iterable[dict | int] = (),
    rationale: str = "",
) -> Call:
    """Any mix, as one patch. Each ``edits`` entry is ``{index, expect, code}`` and
    each ``deletes`` entry ``{index, expect}`` (a bare int is accepted by the tool
    only so it can answer that an ``expect`` is needed)."""
    return Call(
        PROPOSE,
        {
            "edits": list(edits),
            "appends": list(appends),
            "deletes": list(deletes),
            "rationale": rationale,
        },
    )


def propose_notebook_rewrite(cells: Iterable[str], expect_cells: int) -> Call:
    """A wholesale rewrite. ``expect_cells`` is REQUIRED and positional for the same
    reason ``expect`` is: a rewrite discards every cell, so the model has to say how
    many it believes are there, or it would be deleting cells it never saw."""
    return Call(PROPOSE, {"cells": list(cells), "expect_cells": expect_cells})


def read_source() -> Call:
    """The read a model must do before it can write a correct ``expect``."""
    return Call("mooring_read_notebook_source", {})


def get_schema(dataset: str) -> Call:
    return Call("mooring_get_schema", {"dataset": dataset})


Step = Say | Call
Script = list[Step]


# -- the streaming-chunk shapes the session parses ----------------------------


def _content_chunk(text: str, finish: str | None = None):
    delta = types.SimpleNamespace(content=text, tool_calls=None)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta, finish_reason=finish)]
    )


def _tool_chunk(call_id: str, name: str, args: dict, finish: str | None = None):
    fn = types.SimpleNamespace(name=name, arguments=json.dumps(args))
    tc = types.SimpleNamespace(index=0, id=call_id, function=fn)
    delta = types.SimpleNamespace(content=None, tool_calls=[tc])
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta, finish_reason=finish)]
    )


def _chunks_for(step: Step, ordinal: int) -> list:
    if isinstance(step, Say):
        return [_content_chunk(step.text), _content_chunk("", finish="stop")]
    return [
        _tool_chunk(f"call_{ordinal}", step.name, step.args),
        _content_chunk("", finish="tool_calls"),
    ]


class _Completions:
    """The ``client.chat.completions`` the session calls, one entry per request."""

    def __init__(self, script: Script) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        if index >= len(self._script):
            # The script ran out: end the turn rather than loop. A model that stops
            # talking is a real thing, and the harness must score it as one.
            return iter([_content_chunk("", finish="stop")])
        return iter(_chunks_for(self._script[index], index))


class FakeClient:
    """A stand-in for the ``openai`` client, shaped only where the session looks."""

    def __init__(self, script: Script) -> None:
        self.chat = types.SimpleNamespace(completions=_Completions(script))

    @property
    def calls(self) -> list[dict]:
        return self.chat.completions.calls


def open_scripted_session(
    script: Script,
    *,
    system_context: str,
    workspace,
    folders,
    notebook_rel: str,
    **_ignored,
):
    """A live :class:`~mooring.ai.openai_session.OpenAIChatSession` driven by
    ``script`` instead of by a model. Needs no ``openai`` package and no network."""
    from mooring.ai.openai_session import OpenAIChatSession

    client = FakeClient(script)
    session = OpenAIChatSession(
        model="scripted",
        system_context=system_context,
        workspace=workspace,
        folders=folders,
        notebook_rel=notebook_rel,
        client_factory=lambda: client,
    )
    session.start(block=True)
    return session


def scripted_opener(scripts: dict[str, Script]):
    """A :data:`~evals.harness.SessionOpener` serving one script per case id.

    A case with no script gets an empty one, so it plays out as a model that
    answered nothing — which is itself a verdict the harness must produce.
    """

    def opener(*, case, system_context, workspace, folders, notebook_rel, **_ignored):
        return open_scripted_session(
            scripts.get(case.id, []),
            system_context=system_context,
            workspace=workspace,
            folders=folders,
            notebook_rel=notebook_rel,
        )

    return opener
