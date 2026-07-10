"""The parallel investigate fan-out, driven against stub read-only sessions.

No provider: a branch's "sub-agent" is a ``ChatBroadcaster`` whose ``send`` broadcasts a
scripted event sequence, so we exercise the planner + the shared drive loop (fan-out order,
completion, timeout, the non-interactive PII block) and the value-blindness of the merge —
exactly as they would drive a real read-only session, but with no model.
"""

from __future__ import annotations

from mooring.ai.chat import ChatBroadcaster, ChatEvent
from mooring.ai.investigate import (
    BranchJob,
    BranchResult,
    InvestigatePlanner,
    merge_findings,
    resolve_concurrency,
)
from mooring.ai_config import InvestigateConfig, PiiConfig

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
# A Luhn-valid test PAN — the checksum-confident hit the pre-flight must block on.
CARD = "4012888888881881"


class ScriptedSession(ChatBroadcaster):
    """A read-only sub-agent whose ``send`` broadcasts a fixed ``(kind, data)`` list."""

    def __init__(self, events):
        super().__init__()
        self._events = events
        self.sent: list[str] = []
        self.closed = False

    def send(self, text, live_schema_text=""):
        self.sent.append(text)
        for kind, data in self._events:
            self._broadcast(ChatEvent(kind, data))

    def close(self):
        self.closed = True
        super().close()


class SilentSession(ChatBroadcaster):
    """Never answers -> the branch hits its deadline."""

    def __init__(self):
        super().__init__()
        self.sent: list[str] = []
        self.closed = False

    def send(self, text, live_schema_text=""):
        self.sent.append(text)

    def close(self):
        self.closed = True
        super().close()


class HoldingSession(ChatBroadcaster):
    """Holds its question with a tokened ``pii`` event -> the branch is BLOCKED (there is
    no human at a sub-agent, so it is never auto-confirmed)."""

    def __init__(self, findings=None):
        super().__init__()
        self._findings = findings or [{"line": 1, "kind": "person name"}]
        self.sent: list[str] = []
        self.confirmed: list[str] = []
        self.closed = False

    def send(self, text, live_schema_text=""):
        self.sent.append(text)
        self._broadcast(ChatEvent("pii", {"token": "tok", "findings": self._findings}))

    def send_confirmed(self, token, live_schema_text=""):
        self.confirmed.append(token)  # must NEVER be called for an unattended sub-agent

    def close(self):
        self.closed = True
        super().close()


def _msg(text):
    return [("message", {"text": text}), ("idle", {})]


def _planner(sessions, *, config=None, pii=None, opened=None, on_progress=None):
    it = iter(sessions)

    def open_session(ctx, nb, model, effort):
        if opened is not None:
            opened.append((ctx, nb, model, effort))
        return next(it)

    return InvestigatePlanner(
        # max_concurrency=2: the factory resolves AUTO(0) before constructing a planner,
        # so a directly-built planner must pass a concrete cap or it degrades to serial.
        config=config or InvestigateConfig(enabled=True, branch_timeout=2, max_concurrency=2),
        pii=pii or PiiConfig(),
        build_context=lambda nb, ds: f"CTX nb={nb} ds={ds}",
        open_session=open_session,
        default_notebook_rel="notebooks/current.py",
        on_progress=on_progress,
    )


def test_a_branch_collects_the_final_message_as_its_finding():
    planner = _planner([ScriptedSession(_msg("orders has columns id, ts, amount"))])
    [r] = planner.run([BranchJob(question="what columns does orders have?")])
    assert r.status == "finding"
    assert "orders has columns id, ts, amount" in r.finding


def test_a_branch_falls_back_to_accumulated_deltas():
    events = [("delta", {"text": "part-"}), ("delta", {"text": "one"}), ("idle", {})]
    [r] = _planner([ScriptedSession(events)]).run([BranchJob(question="q")])
    assert r.status == "finding" and r.finding == "part-one"


def test_fan_out_preserves_submission_order():
    sessions = [ScriptedSession(_msg(f"finding {i}")) for i in range(4)]
    results = _planner(sessions).run([BranchJob(question=f"q{i}") for i in range(4)])
    assert [r.finding for r in results] == [f"finding {i}" for i in range(4)]


def test_default_notebook_is_used_when_a_branch_names_none():
    opened = []
    _planner([ScriptedSession(_msg("x"))], opened=opened).run([BranchJob(question="q")])
    # build_context was called with the analyst's current notebook as the branch focus.
    assert opened and "nb=notebooks/current.py" in opened[0][0]


def test_a_silent_branch_times_out_as_failed():
    planner = _planner([SilentSession()], config=InvestigateConfig(enabled=True, branch_timeout=1, max_concurrency=2))
    [r] = planner.run([BranchJob(question="q")])
    assert r.status == "failed"


def test_a_held_branch_is_blocked_never_confirmed():
    holding = HoldingSession()
    [r] = _planner([holding]).run([BranchJob(question="q")])
    assert r.status == "pii_blocked"
    assert holding.confirmed == []  # unattended: never auto-confirms a hold
    assert holding.closed  # the session is always closed


def test_preflight_blocks_a_checksum_pii_subquestion_before_opening_a_session():
    opened = []
    planner = _planner(
        [ScriptedSession(_msg("never reached"))], pii=PiiConfig(enabled=True), opened=opened
    )
    [r] = planner.run([BranchJob(question=f"why does {CARD} fail validation?")])
    assert r.status == "pii_blocked"
    assert opened == []  # no session opened for a blocked branch — no spend


def test_block_investigation_policy_aborts_all_branches():
    cfg = InvestigateConfig(
        enabled=True, pii_policy="block_investigation", branch_timeout=2, max_concurrency=2
    )
    opened = []
    planner = _planner(
        [ScriptedSession(_msg("x"))], pii=PiiConfig(enabled=True), config=cfg, opened=opened
    )
    results = planner.run(
        [BranchJob(question=f"card {CARD}"), BranchJob(question="a clean question")]
    )
    assert results[0].status == "pii_blocked"
    assert results[1].status == "not_run"
    assert opened == []


def test_max_branches_caps_the_fan_out():
    cfg = InvestigateConfig(enabled=True, max_branches=2, branch_timeout=2, max_concurrency=2)
    sessions = [ScriptedSession(_msg(f"f{i}")) for i in range(2)]
    results = _planner(sessions, config=cfg).run([BranchJob(question=f"q{i}") for i in range(5)])
    assert len(results) == 2


def test_merge_scrubs_concatenates_and_notes_gaps():
    results = [
        BranchResult("what columns?", "finding", finding="orders: id, ts, amount"),
        BranchResult("blocked one", "pii_blocked", pii=[{"line": 1, "kind": "card number"}]),
        BranchResult("join keys?", "finding", finding="join on customer_id"),
        BranchResult("empty one", "empty"),
    ]
    merged = merge_findings(results)
    assert "## what columns?" in merged and "orders: id, ts, amount" in merged
    assert "## join keys?" in merged and "join on customer_id" in merged
    assert "propose ONE change" in merged  # instructs the model to act
    assert "blocked" in merged and "returned nothing" in merged  # coverage is honest


def test_merge_drops_a_checksum_pii_line_from_a_finding():
    # Defence-in-depth: even though a read-only sub-agent is structurally value-blind, the
    # merge still applies the checksum-PII floor to each finding.
    r = BranchResult("q", "finding", finding=f"here is a card {CARD}\nand a safe line")
    merged = merge_findings([r])
    assert CARD not in merged
    assert "a safe line" in merged


def test_only_a_truly_empty_result_set_merges_to_the_empty_string():
    # No branches at all -> nothing to say. But a branch that FAILED must still be
    # reported (see test_merge_reports_the_notes_even_when_nothing_usable_came_back),
    # otherwise the model cannot tell "nothing ran" from "everything failed".
    assert merge_findings([]) == ""
    failed = merge_findings([BranchResult("q", "failed", error="boom")])
    assert "no usable findings" in failed and "returned nothing" in failed


def test_planner_refuses_an_unresolved_auto_concurrency_instead_of_going_serial():
    # 0 = AUTO is a CONFIG sentinel only the app factory can resolve (it knows the
    # provider). Collapsing it to 1 would silently make the fan-out serial.
    import pytest

    with pytest.raises(ValueError, match="resolved max_concurrency"):
        InvestigatePlanner(
            config=InvestigateConfig(enabled=True),  # max_concurrency defaults to 0
            pii=PiiConfig(),
            build_context=lambda nb, ds: "CTX",
            open_session=lambda *a: None,
        )


def test_abort_stops_in_flight_branches_and_skips_the_rest():
    import threading

    abort = threading.Event()
    abort.set()  # the parent session closed before the fan-out started
    it = iter([ScriptedSession(_msg("never")), ScriptedSession(_msg("never"))])
    opened = []
    planner = InvestigatePlanner(
        config=InvestigateConfig(enabled=True, branch_timeout=2, max_concurrency=2),
        pii=PiiConfig(),
        build_context=lambda nb, ds: "CTX",
        open_session=lambda *a: (opened.append(1), next(it))[1],
        abort=abort,
    )
    results = planner.run([BranchJob(question="q0"), BranchJob(question="q1")])
    assert [r.status for r in results] == ["not_run", "not_run"]
    assert opened == []  # nothing spawned once the parent is gone


def test_close_hook_fires_once_and_a_broken_hook_does_not_stop_teardown():
    calls = []
    s = ScriptedSession([])
    s.add_close_hook(lambda: (_ for _ in ()).throw(RuntimeError("boom")))  # broken hook
    s.add_close_hook(lambda: calls.append("aborted"))
    s.close()
    s.close()  # idempotent
    assert calls == ["aborted"]


def test_a_cut_short_branch_is_marked_truncated_and_labelled_in_the_merge():
    # Deltas arrive but the session never goes idle -> the deadline cuts it off. The partial
    # text must NOT read to the parent model as a complete finding.
    events = [("delta", {"text": "revenue joins to orders on"})]
    planner = _planner(
        [ScriptedSession(events)],
        config=InvestigateConfig(enabled=True, branch_timeout=1, max_concurrency=2),
    )
    [r] = planner.run([BranchJob(question="how does revenue join?")])
    assert r.status == "finding" and r.truncated is True
    merged = merge_findings([r])
    assert "revenue joins to orders on" in merged
    assert "INCOMPLETE" in merged


def test_a_clean_idle_branch_is_not_marked_truncated():
    [r] = _planner([ScriptedSession(_msg("complete answer"))]).run([BranchJob(question="q")])
    assert r.status == "finding" and r.truncated is False
    assert "INCOMPLETE" not in merge_findings([r])


def test_a_finding_the_scrub_empties_is_counted_as_withheld_not_dropped():
    # The whole answer is a checksum-PII line, so scrub_text removes it. The model must be
    # told the branch contributed nothing, not silently believe coverage was complete.
    r = BranchResult("q", "finding", finding=CARD)
    merged = merge_findings([r, BranchResult("q2", "finding", finding="real answer")])
    assert CARD not in merged
    assert "withheld by the privacy scrub" in merged


def test_merge_reports_the_notes_even_when_nothing_usable_came_back():
    # Every branch PII-blocked: returning "" would tell the model nothing, so it might
    # silently retry. It must learn WHY and rephrase.
    results = [
        BranchResult("q1", "pii_blocked", pii=[{"line": 1, "kind": "card number"}]),
        BranchResult("q2", "pii_blocked", pii=[{"line": 1, "kind": "card number"}]),
    ]
    merged = merge_findings(results)
    assert merged  # not the empty string
    assert "no usable findings" in merged and "sensitive data" in merged


def test_context_is_built_once_per_target_across_branches():
    # 4 angles on the SAME notebook must not re-parse the semantic model 4 times.
    builds = []

    def build_context(nb, ds):
        builds.append((nb, ds))
        return f"CTX {nb}"

    planner = InvestigatePlanner(
        config=InvestigateConfig(enabled=True, branch_timeout=2, max_concurrency=4),
        pii=PiiConfig(),
        build_context=build_context,
        open_session=lambda *a: ScriptedSession(_msg("ok")),
        default_notebook_rel="nb.py",
    )
    planner.run([BranchJob(question=f"q{i}") for i in range(4)])
    assert builds == [("nb.py", "")]  # built once, reused by the other three


def test_resolve_concurrency_is_provider_aware_and_explicit_wins():
    # AUTO (0): a Copilot branch is a subprocess + a premium request, so keep it small;
    # an OpenAI/LiteLLM branch is just an HTTP stream, so a wider fan-out is cheap.
    assert resolve_concurrency(0, "copilot") == 2
    assert resolve_concurrency(0, "openai") == 6
    assert resolve_concurrency(0, "litellm") == 6
    assert resolve_concurrency(0, "") == 6  # unknown provider -> the light default
    assert resolve_concurrency(0, "COPILOT") == 2  # case-insensitive
    # An explicitly configured value always wins over AUTO, for every provider.
    assert resolve_concurrency(4, "copilot") == 4
    assert resolve_concurrency(1, "openai") == 1


def test_planner_emits_value_free_progress_as_branches_finish():
    events: list[dict] = []
    sessions = [ScriptedSession(_msg(f"f{i}")) for i in range(3)]
    _planner(sessions, on_progress=events.append).run(
        [BranchJob(question=f"q{i}") for i in range(3)]
    )
    assert events[0] == {"phase": "start", "done": 0, "total": 3}
    assert [e["done"] for e in events if e["phase"] == "branch"] == [1, 2, 3]
    assert events[-1] == {"phase": "done", "done": 3, "total": 3, "found": 3}
    # Value-free: counts + statuses only — never a sub-question or a finding.
    blob = repr(events)
    assert "q0" not in blob and "f0" not in blob


def test_planner_progress_counts_only_answered_branches_as_found():
    events: list[dict] = []
    sessions = [ScriptedSession(_msg("answered")), ScriptedSession([("idle", {})])]
    _planner(sessions, on_progress=events.append).run(
        [BranchJob(question="q0"), BranchJob(question="q1")]
    )
    assert events[-1]["found"] == 1 and events[-1]["total"] == 2


def test_planner_survives_a_broken_progress_sink():
    def boom(_event):
        raise RuntimeError("sink exploded")

    [r] = _planner([ScriptedSession(_msg("ok"))], on_progress=boom).run(
        [BranchJob(question="q")]
    )
    assert r.status == "finding"  # a broken cue never sinks the investigation


def test_readonly_openai_session_drops_run_investigation_so_it_cannot_recurse():
    # Depth-1 guarantee at the SESSION layer: even if a run_investigation is passed to a
    # read_only session, it is forced off, so a sub-agent never gets mooring_investigate.
    from mooring.ai.openai_session import OpenAIChatSession

    s = OpenAIChatSession(
        model="",
        system_context="ctx",
        workspace=".",
        folders=(),
        notebook_rel="n.py",
        client_factory=lambda: None,
        read_only=True,
        run_investigation=lambda branches: "should never be used",
    )
    assert s._read_only is True
    assert s._run_investigation is None
