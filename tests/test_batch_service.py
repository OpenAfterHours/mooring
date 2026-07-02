"""app/batch_service — the typed BatchRun invariants and the hub teardown order.

BatchRun carries the two invariants that used to be raw-dict fields mutated in
place across nine handlers: the applied-once set of stable proposal ids, and the
open/closed status with its teardown. These pins are deterministic (fakes, no
timing) — see the architecture plan's P5.
"""

from __future__ import annotations

import threading

from mooring.app.batch_service import BatchRun, BatchService


class _FakePlanner:
    def __init__(self, idle=True):
        self._idle = idle
        self.closed_with: list[bool] = []

    def is_idle(self):
        return self._idle

    def close(self, cancel=False):
        self.closed_with.append(cancel)


class _FakeBroadcaster:
    def __init__(self, idle_s=0.0):
        self._idle = idle_s
        self.closed = 0

    def idle_seconds(self):
        return self._idle

    def close(self):
        self.closed += 1


def _run(*, idle=True, idle_s=0.0) -> BatchRun:
    return BatchRun(
        broadcaster=_FakeBroadcaster(idle_s),
        abort=threading.Event(),
        planner=_FakePlanner(idle),
        workspace="ws",
    )


def test_applied_once_is_a_method_under_the_runs_lock():
    run = _run()
    assert run.already_applied("p1") is False
    run.mark_applied("p1")
    assert run.already_applied("p1") is True
    assert run.applied_pids() == {"p1"}
    # None is never tracked (a proposal without a stable id can't be deduped).
    run.mark_applied(None)
    assert run.already_applied(None) is False
    assert run.applied_pids() == {"p1"}


def test_is_reapable_transitions():
    fresh = _run(idle=True, idle_s=0.0)
    assert fresh.is_reapable(timeout=10) is False  # idle but recently active
    stale = _run(idle=True, idle_s=99.0)
    assert stale.is_reapable(timeout=10) is True
    building = _run(idle=False, idle_s=99.0)
    assert building.is_reapable(timeout=10) is False  # never reap a building run
    closed = _run(idle=True, idle_s=99.0)
    closed.close()
    assert closed.is_reapable(timeout=10) is False  # already torn down


def test_close_cancel_fires_abort_and_cancels_the_planner():
    run = _run()
    run.close(cancel=True)
    assert run.status == "closed"
    assert run.abort.is_set()
    assert run.planner.closed_with == [True]
    assert run.broadcaster.closed == 1


def test_close_without_cancel_drains_quietly():
    # The reap path: no abort (the run is idle anyway), a plain planner close.
    run = _run()
    run.close()
    assert run.status == "closed"
    assert not run.abort.is_set()
    assert run.planner.closed_with == [False]


def test_service_cancel_keeps_the_entry_and_is_idempotent():
    svc = BatchService()
    bid = svc.register(_FakeBroadcaster(), threading.Event(), _FakePlanner(), "ws")
    assert svc.cancel(bid) is True
    assert svc.get(bid) is not None  # kept: the tray answers "closed", not 404
    assert svc.get(bid).status == "closed"
    assert svc.cancel(bid) is True  # close() is idempotent
    assert svc.cancel("nope") is False


def test_abort_all_clears_the_registry_with_cancel_semantics():
    svc = BatchService()
    bid = svc.register(_FakeBroadcaster(), threading.Event(), _FakePlanner(), "ws")
    run = svc.get(bid)
    svc.abort_all()
    assert svc.get(bid) is None
    assert run.abort.is_set() and run.planner.closed_with == [True]


def test_hub_teardown_order_is_chats_then_batches_then_editors(tmp_path, monkeypatch):
    """shutdown() tears down chats -> batches -> editors (the editor step owns the
    Windows process-group kill), and reload() tears down chats -> batches ->
    provider but NEVER the editors — switching repos must not kill marimo tabs
    open against the previous workspace. Deterministic: recorded fakes, no timing."""
    from mooring import config
    from mooring.hub.server import Hub

    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws"))
    order: list[str] = []

    class _Editor:
        def shutdown(self):
            order.append("editors")

    hub.editors["ws"] = _Editor()
    monkeypatch.setattr(hub.chat, "close_all", lambda: order.append("chats"))
    monkeypatch.setattr(hub.batch, "abort_all", lambda: order.append("batches"))

    hub.shutdown()
    assert order == ["chats", "batches", "editors"]

    order.clear()
    monkeypatch.setattr(config, "load_app_config", lambda: hub.app_cfg)
    hub.reload()
    assert order == ["chats", "batches"]  # editors survive a repo switch
    assert hub._provider is None  # the provider cache was dropped