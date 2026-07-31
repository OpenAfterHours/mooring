"""Attended parameterised runs: one notebook, once per value, one artifact per value.

``mooring run notebooks/board.py --for region=EMEA,APAC,AMER`` is the one-click version of
the loop an analyst does by hand at month end — edit a value, run, Deliver, rename, repeat.
The analyst watches it happen, sees which value is running, and can stop it.

Four decisions carry this module:

**It is ATTENDED, and stays that way.** Nothing here touches :mod:`mooring.schedule`, and no
cadence can produce a fan-out. A schedule's contract is *one* notebook, *one* receipt, *one*
artifact whose staleness is arithmetic; N artifacts on a cadence is a different promise about
retention and freshness that has not been made. It is also, unlike a refresh, not something
that should happen while nobody is looking: it can take N × several minutes of a laptop's CPU.

**It is SEQUENTIAL, with no concurrency option at all.** N marimo kernels on an analyst's
laptop fight over CPU, over the workspace files the notebooks read, and over the single
throwaway render path — and the failure looks like "my machine froze", which is the worst
kind of support ticket. Sequential also makes cancellation exact: at most one kernel is ever
alive, so a cancel is one process-tree kill and the remaining values simply never start.

**No run in the fan-out can stop another.** A value that fails is recorded with a curated
reason and the next value starts. That is the difference between a fan-out and a script.

**Nothing here reaches the network or the AI.** A parameterised run does not pull (the
analyst is right there and has just pulled), does not push, and writes only inside the
sync-excluded ``.mooring/`` tree. Because there is no GitHub call, there is no ``str(exc)``
from a GitHub error that could reach a receipt, and telemetry gets counts only — never the
parameter name, a value, or a path.
"""

from __future__ import annotations

import contextlib
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mooring import activity, params, telemetry, verify
from mooring.app import deliver, notebook_run, notebooks, verify_run
from mooring.config import Config

# Per-value outcomes. Deliberately NOT reusing the schedule vocabulary: a fan-out has no
# cadence, so "degraded" (could not pull) and "checks_failed" (a cadence's red state) have
# no meaning here, and two of these states are ones a schedule cannot be in.
OK = "ok"
FAILED = "failed"
CANCELLED = "cancelled"  # this value's kernel was killed mid-run
SKIPPED = "skipped"  # the fan-out was cancelled before this value started


class FanOutRefused(Exception):
    """The fan-out must not start. ``str(exc)`` is the user-facing reason."""


class _PromoteFailed(Exception):
    """This value's artifact could not be written. Internal: it becomes a FAILED value, never
    a clean one (see :func:`_promote`)."""


@dataclass(frozen=True)
class ValueRun:
    """What happened for ONE parameter value. Value-free apart from the value itself, which
    the analyst typed and which is already in the artifact's filename."""

    value: str
    outcome: str
    ran: bool = False
    cells_failed: int | None = None
    reason: str = ""  # curated; never raw stderr, never a traceback
    artifact: str = ""  # workspace-relative outbox path, or ""
    ran_at: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == OK


@dataclass(frozen=True)
class FanOutResult:
    notebook: str
    param: str
    values: tuple[str, ...]
    runs: tuple[ValueRun, ...] = ()
    cancelled: bool = False
    delivered: bool = False

    @property
    def total(self) -> int:
        return len(self.values)

    @property
    def clean(self) -> int:
        return sum(1 for run in self.runs if run.ok)

    @property
    def failed(self) -> int:
        return sum(1 for run in self.runs if run.outcome == FAILED)

    @property
    def artifacts(self) -> list[str]:
        return [run.artifact for run in self.runs if run.artifact]

    @property
    def complete(self) -> bool:
        """Whether EVERY value ran clean. Anything else is a partial fan-out, and the
        summary line says so — a half-finished pack that reads as finished is the failure
        this feature would otherwise introduce."""
        return bool(self.runs) and self.clean == self.total


# -- the fan-out -------------------------------------------------------------


def fan_out(
    cfg: Config,
    rel_path: str,
    spec: params.ParamSpec,
    *,
    do_deliver: bool = True,
    on_event=None,
    cancel: threading.Event | None = None,
) -> FanOutResult:
    """Run ``rel_path`` once per value in ``spec``, sequentially, and report per value.

    ``on_event`` is called from the calling thread with small value-free-shaped dicts as the
    run progresses (see :func:`_emit`), which is how both adapters show per-value progress
    without either of them owning a transport. ``cancel`` stops the fan-out: the in-flight
    kernel is killed through the runner's process-tree kill and the remaining values are
    recorded as skipped.

    Raises :class:`FanOutRefused` for a target that must not be fanned out,
    :class:`mooring.app.notebook_run.RunBusy` when another run holds the workspace, and
    ``ValueError`` / ``FileNotFoundError`` for a bad path."""
    workspace = cfg.workspace()
    rel_posix = verify_run.ensure_runnable(workspace, rel_path, FanOutRefused)
    _refuse_if_the_notebook_ignores_the_parameter(workspace, rel_posix, spec)

    # The one workspace run lock, which Verify, Deliver and the scheduled refresh also take —
    # so no two whole-notebook runs can overlap, in any order, including across processes (a
    # background refresh agent).
    with notebook_run.workspace_guard(workspace):
        return _run_values(cfg, rel_posix, spec, do_deliver, on_event, cancel)


def _run_values(
    cfg: Config,
    rel_posix: str,
    spec: params.ParamSpec,
    do_deliver: bool,
    on_event,
    cancel: threading.Event | None,
) -> FanOutResult:
    workspace = cfg.workspace()
    total = len(spec)
    runs: list[ValueRun] = []
    cancelled = False
    _emit(on_event, "start", notebook=rel_posix, param=spec.name, values=list(spec.values))

    for index, value in enumerate(spec.values, start=1):
        if cancelled or (cancel is not None and cancel.is_set()):
            cancelled = True
            runs.append(
                ValueRun(value=value, outcome=SKIPPED, reason="cancelled before this value ran")
            )
            _emit(on_event, "value", index=index, total=total, run=asdict(runs[-1]))
            continue
        _emit(on_event, "running", index=index, total=total, value=value)
        try:
            run = _run_one(cfg, rel_posix, spec, value, index, total, do_deliver, cancel)
        except KeyboardInterrupt:
            # Ctrl+C on the CLI, where the fan-out runs on the main thread. The runner has
            # already killed the process tree and removed the render (its own rules 1 and 2),
            # so nothing is left running — record the cancel and carry on marking the rest
            # skipped, rather than losing the whole report to an unwind. An interrupted pack
            # must still be able to say what it did and did not produce.
            if cancel is not None:
                cancel.set()
            run = ValueRun(value=value, outcome=CANCELLED, reason="cancelled part-way through")
        runs.append(run)
        if run.outcome == CANCELLED:
            cancelled = True
        _emit(on_event, "value", index=index, total=total, run=asdict(run))

    result = FanOutResult(
        notebook=rel_posix,
        param=spec.name,
        values=spec.values,
        runs=tuple(runs),
        cancelled=cancelled,
        delivered=do_deliver,
    )
    activity.record(
        workspace,
        "run",
        path=rel_posix,
        param=spec.name,  # the NAME is authored code, like a path; the values are not recorded
        values=total,
        ok=result.complete,
    )
    # Value-free: counts only. Not the parameter name, not a value, not a path.
    telemetry.log_event(
        "param_run",
        values=total,
        clean=result.clean,
        failed=result.failed,
        cancelled=int(cancelled),
    )
    return result


def _run_one(
    cfg: Config,
    rel_posix: str,
    spec: params.ParamSpec,
    value: str,
    index: int,
    total: int,
    do_deliver: bool,
    cancel: threading.Event | None,
) -> ValueRun:
    """One value: render into the sync-excluded throwaway path, promote on success only.

    The promote-only-on-success discipline is copied from the scheduled refresh for the same
    reason: a failed value must never overwrite the artifact a previous good run produced for
    that value, so anything sitting in the outbox is always a complete run."""
    workspace = cfg.workspace()
    # Render into the sync-excluded throwaway path ALWAYS and promote only a completed run,
    # so a failed value never even opens the artifact a previous good run left behind. The
    # promotion happens inside this try (as it does in the scheduled refresh) because the
    # `finally` below removes whatever value-bearing render is left, on every path.
    #
    # The path is per-VALUE, not just per-notebook: were it shared, a render written by
    # anything else could be promoted as this value's artifact — a file labelled EMEA holding
    # somebody else's numbers. (The workspace run lock should prevent that; this makes it
    # structural rather than dependent on the lock being correct.)
    render = verify.render_target(workspace, rel_posix, variant=spec.variant(value))
    ran_at = ""
    artifact = ""
    try:
        outcome = notebook_run.run(
            workspace,
            rel_posix,
            render,
            keep_on_success=do_deliver,
            env_extra=spec.env_for(value),
            cancel=cancel,
        )
        ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if outcome.ok and do_deliver:
            artifact = _promote(workspace, cfg, rel_posix, render, spec, value, index, total)
    except notebook_run.RunCancelled:
        return ValueRun(value=value, outcome=CANCELLED, reason="cancelled part-way through")
    except notebook_run.RunError as exc:
        # str(exc) here is the runner's OWN curated sentence (timed out / environment broke),
        # never marimo's stderr — which the runner refuses to store because it can quote a
        # data value.
        return ValueRun(value=value, outcome=FAILED, reason=str(exc))
    except _PromoteFailed as exc:
        # The notebook ran fine but its artifact could not be written — most often because
        # the PREVIOUS one is open in the analyst's own viewer, which on Windows makes
        # os.replace raise and leaves the old file sitting there under today's date. Counting
        # that as clean is how a pack ships January's numbers labelled February: the value is
        # reported FAILED so `complete` is false and the summary says so.
        return ValueRun(value=value, outcome=FAILED, ran=True, reason=str(exc), ran_at=ran_at)
    finally:
        with contextlib.suppress(OSError):
            render.unlink()

    if not outcome.ok:
        return ValueRun(
            value=value,
            outcome=FAILED,
            ran=True,
            cells_failed=outcome.cells_failed,
            reason=_describe_cells(outcome.cells_failed),
            ran_at=ran_at,
        )
    return ValueRun(value=value, outcome=OK, ran=True, artifact=artifact, ran_at=ran_at)


def _promote(
    workspace: Path,
    cfg: Config,
    rel_posix: str,
    render: Path,
    spec: params.ParamSpec,
    value: str,
    index: int,
    total: int,
) -> str:
    """Move this value's completed render into the outbox under its OWN name and stamp the
    provenance footer with which value it is and where it sat in the fan-out.

    Raises :class:`_PromoteFailed` when the move does not happen. It deliberately does NOT
    degrade to "ran clean, no artifact": the file at that path is then the PREVIOUS delivery
    — which is exactly what an ``os.replace`` blocked by the analyst's own open viewer
    leaves behind — so reporting the value clean would present last month's numbers under
    this month's name in a pack the summary calls complete. This is the lesson the Excel
    last mile learned in ``deliver._run_wrote``, applied to the same failure here."""
    final = deliver.outbox_target(workspace, rel_posix, variant=spec.variant(value))
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(render, final)
    except OSError as exc:
        stale = " The file already there is from an earlier run." if final.exists() else ""
        raise _PromoteFailed(
            f"the notebook ran, but its artifact could not be written ({type(exc).__name__})"
            f" — close it if it is open in another program, then run this value again.{stale}"
        ) from exc
    deliver.stamp_provenance(
        final, cfg, rel_posix, workspace, note=spec.note(value, index, total)
    )
    return final.relative_to(workspace).as_posix()


def _describe_cells(cells_failed: int | None) -> str:
    if cells_failed:
        noun = "cell" if cells_failed == 1 else "cells"
        return f"{cells_failed} {noun} failed to run"
    return "the notebook failed to run"


def _refuse_if_the_notebook_ignores_the_parameter(
    workspace: Path, rel_posix: str, spec: params.ParamSpec
) -> None:
    """Refuse a fan-out over a notebook that never reads this parameter.

    This is the single most valuable guard in the feature. A notebook that ignores the
    parameter produces N IDENTICAL renders under N different names — an artifact labelled
    ``APAC`` holding EMEA's numbers, which a stakeholder cannot possibly detect and which is
    strictly worse than no artifact at all. The commonest cause is a typo in the parameter
    name (``--for regoin=…``), which is otherwise completely invisible.

    Refusing costs one file read and happens BEFORE any notebook runs, so the analyst learns
    in a second rather than after six minutes of rendering.

    :func:`mooring.params.reads_parameter` is an AST scan and is fail-closed, so it also
    refuses a notebook whose ``get`` call it cannot SEE — one hidden behind a helper module,
    or with a computed name. That is the right side to err on here (a refusal costs a
    minute; a mislabelled board pack costs trust), and the message says so rather than
    leaving a puzzled analyst to guess."""
    target = notebooks.ws_file(workspace, rel_posix, suffix=".py")
    try:
        source = target.read_text("utf-8", errors="replace")
    except OSError as exc:
        raise FanOutRefused(f"Could not read the notebook: {exc}") from exc
    if params.reads_parameter(source, spec.name):
        return
    raise FanOutRefused(
        f"{rel_posix} has no visible read of a parameter called {spec.name!r}, so running it "
        f"once per value would write {len(spec)} identically-numbered artifacts under "
        f"{len(spec)} different names. Add the read to the notebook itself:\n"
        "    import mooring_params\n"
        f'    {spec.name} = mooring_params.get("{spec.name}", "{spec.values[0]}")\n'
        "(with the default there, the notebook still runs exactly as it does today when you "
        "open it or verify it.)\n"
        "mooring looks for that call IN THIS FILE with the name written out in full, and "
        "refuses when it cannot see one — so a read that lives in a helper module, or whose "
        "name is computed, has to be spelled out here before a fan-out will run."
    )


def _emit(on_event, kind: str, **fields) -> None:
    """Publish one progress event, best-effort: a broken listener must never take down a run
    that is otherwise going fine."""
    if on_event is None:
        return
    with contextlib.suppress(Exception):
        on_event({"kind": kind, **fields})


# -- reporting ---------------------------------------------------------------


def describe_run(run: ValueRun) -> str:
    """One human line per value — shared by both adapters so the hub and the CLI can never
    word an outcome differently."""
    head = f"{run.value} — "
    if run.outcome == OK:
        return head + (f"ran clean → {run.artifact}" if run.artifact else "ran clean.")
    if run.outcome == CANCELLED:
        return head + "cancelled part-way through; no artifact was written."
    if run.outcome == SKIPPED:
        return head + "not run (the fan-out was cancelled)."
    return head + f"did not run — {run.reason}"


def describe_result(result: FanOutResult) -> str:
    """The summary line. A PARTIAL fan-out never reads as a complete one: the counts lead,
    and the reason it stopped short is named."""
    head = f"{result.notebook} — {result.clean} of {result.total} value(s) ran clean"
    if result.complete:
        return head + ("; artifacts are in .mooring/outbox." if result.delivered else ".")
    bits = []
    if result.failed:
        bits.append(f"{result.failed} failed")
    missing = result.total - result.clean - result.failed
    if missing:
        bits.append(f"{missing} did not run")
    tail = f" ({', '.join(bits)})" if bits else ""
    stopped = " — cancelled" if result.cancelled else ""
    return f"{head}{tail}{stopped}. This pack is INCOMPLETE."


def exit_code(result: FanOutResult) -> int:
    """0 every value clean · 1 at least one value failed · 4 cancelled (partial).
    Documented on ``mooring run --help`` so a wrapper script can branch on it."""
    if result.failed:
        return 1
    if not result.complete:
        return 4
    return 0


# -- the hub's handle --------------------------------------------------------


@dataclass
class RunHandle:
    """A fan-out running on its own thread, with a snapshot the hub can poll and a cancel.

    The hub needs progress and a stop button; the CLI needs neither (it prints from the same
    ``on_event`` callback as it goes). So the threading lives HERE, once, rather than in the
    route — and the app layer still knows nothing about HTTP, SSE, or polling."""

    notebook: str
    param: str
    values: tuple[str, ...]
    cancel: threading.Event = field(default_factory=threading.Event)
    _runs: list[dict] = field(default_factory=list)
    _running: str = ""
    _done: bool = False
    _error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def on_event(self, event: dict) -> None:
        kind = event.get("kind")
        with self._lock:
            if kind == "running":
                self._running = str(event.get("value", ""))
            elif kind == "value":
                self._runs.append(dict(event.get("run") or {}))
                self._running = ""

    def finish(self, error: str = "") -> None:
        with self._lock:
            self._done = True
            self._running = ""
            self._error = error

    def snapshot(self) -> dict:
        """Everything the UI needs, in one poll. A fan-out emits at most one transition per
        notebook run (tens of seconds), so polling is the honest transport here — an SSE
        stream would carry one frame a minute and add a broadcaster, a registry and a
        replay path to maintain."""
        with self._lock:
            return {
                "notebook": self.notebook,
                "param": self.param,
                "values": list(self.values),
                "total": len(self.values),
                "runs": [dict(run) for run in self._runs],
                "running": self._running,
                "done": self._done,
                "cancelling": self.cancel.is_set() and not self._done,
                "error": self._error,
            }


def start(
    cfg: Config,
    rel_path: str,
    spec: params.ParamSpec,
    *,
    do_deliver: bool = True,
) -> tuple[RunHandle, threading.Thread]:
    """Validate, then run the fan-out on a background thread.

    Every refusal (bad path, non-notebook, ignores the parameter, workspace busy) is raised
    HERE, synchronously, so the caller can answer with a real error instead of minting a run
    that immediately dies.

    "Workspace busy" is checked by TAKING the lock and letting it go again — the worker
    re-takes it for real a moment later. That leaves a hair of a race in which someone else
    grabs it in between; that one lands on the handle's ``error`` instead of the caller's
    status code, which is why :meth:`RunHandle.finish` is reached from a catch-all below."""
    workspace = cfg.workspace()
    rel_posix = verify_run.ensure_runnable(workspace, rel_path, FanOutRefused)
    _refuse_if_the_notebook_ignores_the_parameter(workspace, rel_posix, spec)
    with notebook_run.workspace_guard(workspace):
        pass  # probe: raises RunBusy here, where the caller can still answer with a 409
    handle = RunHandle(notebook=rel_posix, param=spec.name, values=spec.values)

    def _work() -> None:
        try:
            fan_out(
                cfg,
                rel_posix,
                spec,
                do_deliver=do_deliver,
                on_event=handle.on_event,
                cancel=handle.cancel,
            )
        except (FanOutRefused, notebook_run.RunBusy, ValueError, FileNotFoundError) as exc:
            handle.finish(str(exc))
        except BaseException as exc:  # noqa: BLE001
            # A catch-all, because the alternative is worse than any exception: an escape
            # leaves this handle permanently not-done, and the hub's single run slot then
            # refuses every future start with "a run is already going" for the life of the
            # process. Reported as a TYPE, never str(exc) — an arbitrary exception's message
            # is not a curated, value-free string.
            handle.finish(f"The run stopped unexpectedly ({type(exc).__name__}).")
        else:
            handle.finish()

    thread = threading.Thread(target=_work, name="mooring-param-run", daemon=True)
    thread.start()
    return handle, thread
