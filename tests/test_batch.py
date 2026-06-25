"""The deterministic batch planner, driven against stub/scripted copilot sessions.

No Copilot SDK or auth: a job's "builder" is a ``ChatBroadcaster`` whose ``send``
broadcasts a scripted event sequence, so we exercise the planner's control flow —
slug de-dup, concurrency, completion detection, failure isolation, and the
non-interactive PII policy — exactly as it would drive a real session.
"""

from __future__ import annotations

import pytest

from mooring import notebook_template
from mooring.ai.batch import BatchError, BatchJob, BatchPlanner, _name_from_brief
from mooring.ai.chat import ChatBroadcaster, ChatEvent, StubChatSession
from mooring.ai_config import BatchConfig, PiiConfig

# A Luhn-valid test PAN (same one the chat-session suite uses) — a checksum-confident
# hit the PII pre-flight must block on.
_CARD_BRIEF = "explain why 4012888888881881 fails validation"


class ScriptedSession(ChatBroadcaster):
    """A builder whose ``send`` broadcasts a fixed list of ``(kind, data)`` events."""

    def __init__(self, events, *, ready=True):
        super().__init__()
        self._events = events
        self.sent = []
        self.closed = False
        if not ready:
            self._mark_starting()

    def send(self, text, live_schema_text=""):
        self.sent.append(text)
        for kind, data in self._events:
            self._broadcast(ChatEvent(kind, data))

    def close(self):
        self.closed = True
        super().close()


def _proposal(code="result = df.head()"):
    return ("proposal", {"code": code, "rationale": "summary"})


def _make_planner(
    tmp_path, open_session, *, config=None, pii=None, is_disabled=None, on_progress=None
):
    return BatchPlanner(
        config=config or BatchConfig(enabled=True, job_timeout=2),
        pii=pii or PiiConfig(),
        make_notebook=lambda name: notebook_template.create_unique(tmp_path, name),
        build_context=lambda nb, ds: (f"CTX {nb} ds={ds}", None),
        open_session=open_session,
        is_disabled=is_disabled,
        on_progress=on_progress,
    )


def test_builds_a_notebook_from_a_brief_via_a_stub_session(tmp_path):
    # The stub session proposes one cell per turn -> status "built" with the proposal
    # captured for a human to Apply later.
    planner = _make_planner(tmp_path, lambda ctx, nb, m, e, d: StubChatSession(system_context=ctx))
    [result] = planner.run([BatchJob(name="sales", brief="summarise sales")])
    assert result.status == "built"
    assert result.notebook_rel == "notebooks/sales.py"
    assert len(result.proposals) == 1 and result.proposals[0]["code"]
    assert (tmp_path / "notebooks/sales.py").is_file()  # the skeleton was created


def test_fans_out_and_preserves_order(tmp_path):
    scripts = {
        "notebooks/a.py": [_proposal("a = 1")],
        "notebooks/b.py": [("idle", {})],  # idles without proposing -> empty
        "notebooks/c.py": [("fail", {"text": "kernel exploded"})],
    }
    planner = _make_planner(tmp_path, lambda ctx, nb, m, e, d: ScriptedSession(scripts[nb]))
    results = planner.run([BatchJob("a", "x"), BatchJob("b", "y"), BatchJob("c", "z")])
    assert [r.status for r in results] == ["built", "empty", "failed"]
    assert results[2].error == "kernel exploded"


def test_slug_collisions_get_distinct_notebooks(tmp_path):
    planner = _make_planner(tmp_path, lambda ctx, nb, m, e, d: ScriptedSession([_proposal()]))
    results = planner.run([BatchJob("Report", "one"), BatchJob("Report", "two")])
    rels = {r.notebook_rel for r in results}
    assert rels == {"notebooks/Report.py", "notebooks/Report-2.py"}
    assert all(r.status == "built" for r in results)


def test_a_pii_brief_blocks_just_that_job_under_block_job(tmp_path):
    planner = _make_planner(
        tmp_path,
        lambda ctx, nb, m, e, d: ScriptedSession([_proposal()]),
        pii=PiiConfig(enabled=True),
        config=BatchConfig(enabled=True, pii_policy="block_job", job_timeout=2),
    )
    results = planner.run([BatchJob("clean", "summarise"), BatchJob("leak", _CARD_BRIEF)])
    by_name = {r.job.name: r for r in results}
    assert by_name["clean"].status == "built"
    assert by_name["leak"].status == "pii_blocked"
    assert by_name["leak"].pii and by_name["leak"].pii[0]["kind"]  # value-free finding
    # The blocked job never created a notebook (no orphan skeleton).
    assert not (tmp_path / "notebooks/leak.py").exists()


def test_a_pii_brief_aborts_the_whole_batch_under_block_batch(tmp_path):
    planner = _make_planner(
        tmp_path,
        lambda ctx, nb, m, e, d: ScriptedSession([_proposal()]),
        pii=PiiConfig(enabled=True),
        config=BatchConfig(enabled=True, pii_policy="block_batch", job_timeout=2),
    )
    results = planner.run([BatchJob("clean", "summarise"), BatchJob("leak", _CARD_BRIEF)])
    by_name = {r.job.name: r for r in results}
    assert by_name["leak"].status == "pii_blocked"
    assert by_name["clean"].status == "not_run"  # the clean job is held back too
    assert not (tmp_path / "notebooks").exists()  # nothing was created


def test_a_held_pii_event_from_the_session_blocks_the_job(tmp_path):
    # Even if the pre-flight misses it, the session's own block-mode guard holds the
    # brief and emits a tokened "pii" event -> the planner marks the job blocked.
    held = [("pii", {"token": "t1", "findings": [{"line": 1, "kind": "email"}]})]
    planner = _make_planner(tmp_path, lambda ctx, nb, m, e, d: ScriptedSession(held))
    [result] = planner.run([BatchJob("nb", "do a thing")])
    assert result.status == "pii_blocked"
    assert result.proposals == []


def test_disabled_target_is_skipped(tmp_path):
    planner = _make_planner(
        tmp_path,
        lambda ctx, nb, m, e, d: ScriptedSession([_proposal()]),
        is_disabled=lambda nb: nb == "notebooks/off.py",
    )
    results = planner.run([BatchJob("off", "x"), BatchJob("on", "y")])
    by_name = {r.job.name: r for r in results}
    assert by_name["off"].status == "skipped_disabled"
    assert by_name["on"].status == "built"


def test_times_out_without_a_proposal(tmp_path):
    # A session that never emits anything after the brief -> failed at the deadline.
    planner = _make_planner(
        tmp_path,
        lambda ctx, nb, m, e, d: ScriptedSession([]),
        config=BatchConfig(enabled=True, job_timeout=1),
    )
    [result] = planner.run([BatchJob("slow", "x")])
    assert result.status == "failed"
    assert "timed out" in result.error.lower()


def test_a_proposal_then_failure_still_keeps_the_proposal(tmp_path):
    script = [_proposal("kept = 1"), ("fail", {"text": "late error"})]
    planner = _make_planner(tmp_path, lambda ctx, nb, m, e, d: ScriptedSession(script))
    [result] = planner.run([BatchJob("nb", "x")])
    assert result.status == "built"
    assert result.proposals[0]["code"] == "kept = 1"


def test_exceeding_max_jobs_raises(tmp_path):
    planner = _make_planner(
        tmp_path,
        lambda ctx, nb, m, e, d: ScriptedSession([_proposal()]),
        config=BatchConfig(enabled=True, max_jobs=1),
    )
    with pytest.raises(BatchError):
        planner.run([BatchJob("a", "x"), BatchJob("b", "y")])


def test_progress_events_are_value_free(tmp_path):
    events = []
    planner = _make_planner(
        tmp_path,
        lambda ctx, nb, m, e, d: StubChatSession(system_context=ctx),
        on_progress=events.append,
    )
    planner.run([BatchJob("nb", "summarise the data")])
    statuses = [e["status"] for e in events]
    assert "queued" in statuses and "building" in statuses and "built" in statuses
    # No event carries a data value — only index/name/status/notebook/counts/queue depth.
    allowed = {"index", "name", "status", "notebook", "n_proposals", "error", "pending", "total"}
    for e in events:
        assert set(e).issubset(allowed)


def test_await_ready_returns_false_immediately_on_abort(tmp_path):
    # A builder whose handshake hangs (never ready) must not pin a worker until the
    # job deadline when the batch is cancelled — abort short-circuits the wait.
    import time

    sess = ScriptedSession([], ready=False)
    planner = _make_planner(tmp_path, lambda ctx, nb, m, e, d: sess)
    planner._abort.set()
    q = sess.subscribe()
    deadline = time.monotonic() + 60  # large: abort, not the deadline, must end the wait
    assert planner._await_ready(sess, q, deadline) is False


def test_non_built_jobs_discard_their_orphan_skeleton(tmp_path):
    discarded = []
    scripts = {"notebooks/keep.py": [_proposal()], "notebooks/drop.py": [("idle", {})]}
    planner = BatchPlanner(
        config=BatchConfig(enabled=True, job_timeout=2),
        pii=PiiConfig(),
        make_notebook=lambda name: notebook_template.create_unique(tmp_path, name),
        build_context=lambda nb, ds: ("CTX", None),
        open_session=lambda ctx, nb, m, e, d: ScriptedSession(scripts[nb]),
        discard_notebook=discarded.append,
    )
    results = planner.run([BatchJob("keep", "x"), BatchJob("drop", "y")])
    by_name = {r.job.name: r for r in results}
    # The built job keeps its notebook; the empty one is discarded and its path nulled.
    assert by_name["keep"].status == "built" and by_name["keep"].notebook_rel == "notebooks/keep.py"
    assert by_name["drop"].status == "empty" and by_name["drop"].notebook_rel is None
    assert discarded == ["notebooks/drop.py"]


_BUILT = [_proposal(), ("idle", {})]  # proposes then idles -> "built" fast (no timeout wait)


def test_add_appends_to_a_running_queue(tmp_path):
    # The whole point: open the queue, add a job, then add MORE later — same run,
    # indices keep growing, results accumulate in order.
    planner = BatchPlanner(
        config=BatchConfig(enabled=True, job_timeout=3),
        pii=PiiConfig(),
        make_notebook=lambda name: notebook_template.create_unique(tmp_path, name),
        build_context=lambda nb, ds: ("CTX", None),
        open_session=lambda ctx, nb, m, e, d: ScriptedSession(_BUILT),
    )
    planner.start()
    first = planner.add([BatchJob("a", "x")])
    planner.wait_idle()
    second = planner.add([BatchJob("b", "y"), BatchJob("c", "z")])
    planner.wait_idle()
    planner.close()
    snap = planner.snapshot()
    assert first == [0] and second == [1, 2]
    assert [r.job.name for r in snap] == ["a", "b", "c"]
    assert all(r.status == "built" for r in snap)


def test_add_enforces_cumulative_max_jobs_across_calls(tmp_path):
    planner = BatchPlanner(
        config=BatchConfig(enabled=True, max_jobs=2, job_timeout=3),
        pii=PiiConfig(),
        make_notebook=lambda name: notebook_template.create_unique(tmp_path, name),
        build_context=lambda nb, ds: ("CTX", None),
        open_session=lambda ctx, nb, m, e, d: ScriptedSession(_BUILT),
    )
    planner.start()
    planner.add([BatchJob("a", "x")])
    planner.add([BatchJob("b", "y")])  # cumulative 2 == cap
    with pytest.raises(BatchError):
        planner.add([BatchJob("c", "z")])  # would exceed the cumulative cap
    planner.wait_idle()
    planner.close()
    assert planner.total == 2


def test_submit_after_shutdown_records_not_run_and_balances_pending(tmp_path):
    # A close(cancel=True) that shuts the pool down mid-add (a reload/shutdown racing an
    # in-flight add) must NOT leak _pending — which would wedge wait_idle / idle-reap and
    # leak the pool — nor let a bare RuntimeError escape. The job is recorded as cancelled.
    planner = BatchPlanner(
        config=BatchConfig(enabled=True, job_timeout=3),
        pii=PiiConfig(),
        make_notebook=lambda name: notebook_template.create_unique(tmp_path, name),
        build_context=lambda nb, ds: ("CTX", None),
        open_session=lambda ctx, nb, m, e, d: ScriptedSession(_BUILT),
    )
    planner.start()
    job = BatchJob("x", "do x")
    [idx] = planner._reserve_all([job])
    planner.close(cancel=True)  # shuts the pool, so the next submit() will raise
    planner._submit(idx, job, "notebooks/x.py")  # must not raise, must balance pending
    assert planner.pending == 0
    assert planner.is_idle() is True
    assert planner.snapshot()[idx].status == "not_run"


def test_add_to_a_closed_queue_raises(tmp_path):
    planner = BatchPlanner(
        config=BatchConfig(enabled=True, job_timeout=3),
        pii=PiiConfig(),
        make_notebook=lambda name: notebook_template.create_unique(tmp_path, name),
        build_context=lambda nb, ds: ("CTX", None),
        open_session=lambda ctx, nb, m, e, d: ScriptedSession(_BUILT),
    )
    planner.start()
    planner.close()
    with pytest.raises(BatchError):
        planner.add([BatchJob("a", "x")])


def _planner_with_sessions(tmp_path, sessions, *, pii=None, discarded=None):
    it = iter(sessions)
    return BatchPlanner(
        config=BatchConfig(enabled=True, job_timeout=3),
        pii=pii or PiiConfig(),
        make_notebook=lambda name: notebook_template.create_unique(tmp_path, name),
        build_context=lambda nb, ds: ("CTX", None),
        open_session=lambda ctx, nb, m, e, d: ScriptedSession(next(it)),
        discard_notebook=(discarded.append if discarded is not None else None),
    )


def test_refine_replaces_the_proposal_and_accumulates_the_brief(tmp_path):
    # Build a notebook, then refine it: the revised proposal replaces the old one and the
    # note is folded into the brief — the notebook file is never touched.
    planner = _planner_with_sessions(
        tmp_path,
        [[_proposal("v1 = 1"), ("idle", {})], [_proposal("v2 = 2"), ("idle", {})]],
    )
    planner.start()
    planner.add([BatchJob("rev", "chart revenue")])
    planner.wait_idle()
    assert planner.snapshot()[0].proposals[0]["code"] == "v1 = 1"
    planner.refine(0, "use a bar chart instead")
    planner.wait_idle()
    planner.close()
    res = planner.snapshot()[0]
    assert res.status == "built" and res.proposals[0]["code"] == "v2 = 2"
    assert "use a bar chart instead" in res.job.brief  # accumulated
    assert planner.refining_indices() == set()
    assert (tmp_path / res.notebook_rel).is_file()  # same notebook, never deleted


def test_refine_that_produces_nothing_keeps_the_previous_proposal(tmp_path):
    discarded = []
    planner = _planner_with_sessions(
        tmp_path,
        [[_proposal("kept = 1"), ("idle", {})], [("idle", {})]],  # 2nd build idles empty
        discarded=discarded,
    )
    planner.start()
    planner.add([BatchJob("x", "do x")])
    planner.wait_idle()
    nb_rel = planner.snapshot()[0].notebook_rel
    planner.refine(0, "tweak it")
    planner.wait_idle()
    planner.close()
    res = planner.snapshot()[0]
    assert res.status == "built" and res.proposals[0]["code"] == "kept = 1"  # preserved
    assert res.notebook_rel == nb_rel and discarded == []  # notebook NOT discarded
    assert "didn't change anything" in res.error.lower()  # the no-op is surfaced to the tray


def test_refine_rejects_a_still_building_job(tmp_path):
    # A revision may only target a FINISHED, built proposal — never a job whose initial
    # build is still running (which would spawn a 2nd session on the same notebook).
    planner = _planner_with_sessions(tmp_path, [])
    planner.start()
    [idx] = planner._reserve_all([BatchJob("x", "do x")])
    planner._results[idx] = planner._make_result(
        BatchJob("x", "do x"), "notebooks/x.py", "building"
    )
    with pytest.raises(BatchError):
        planner.refine(idx, "tweak it")
    planner.close()


def test_refine_blocks_a_pii_note_and_rejects_bad_targets(tmp_path):
    planner = _planner_with_sessions(
        tmp_path, [[_proposal(), ("idle", {})]], pii=PiiConfig(enabled=True)
    )
    planner.start()
    planner.add([BatchJob("x", "do x")])
    planner.wait_idle()
    with pytest.raises(BatchError):
        planner.refine(0, "use card 4012888888881881 as the example value")  # checksum PII
    with pytest.raises(BatchError):
        planner.refine(0, "   ")  # empty note
    with pytest.raises(BatchError):
        planner.refine(99, "no such job")
    planner.close()


def test_name_from_brief_falls_back():
    assert _name_from_brief("chart monthly revenue by region") == "chart monthly revenue by region"
    assert _name_from_brief("!!!") == "notebook"
    assert _name_from_brief("") == "notebook"
