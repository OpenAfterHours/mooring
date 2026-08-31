"""Driving one case against one model, and turning the results into a capability
card.

The flow, per run:

1. :func:`evals.fixtures.materialise` writes a throwaway workspace into a temp dir
   — a fixture notebook plus a synthetic CSV header. Nothing touches a real
   workspace, so the sync engine never sees any of this.
2. :meth:`mooring.app.chat_service.ChatService.build_context` assembles the system
   context. The application's SOLE caller of the value-blindness choke point is
   reused deliberately: an eval that assembled its own prompt would be measuring a
   prompt mooring does not ship.
3. The opener returns a live ``ChatBroadcaster`` (a real provider session, or the
   scripted fake). Each turn is sent and the event stream drained to ``idle``.
4. The proposal is composed into a candidate notebook through
   :func:`mooring.ai.cellwrite.apply_wire_patch` — the same call the hub's Apply
   endpoint makes — and the case's checks are run against it.

The candidate is built from the LAST proposal of the run applied to the BASE
notebook: that is what an analyst who waited for the model to settle would apply.
Earlier proposals still count — they are what ``proposals`` and ``refusals``
report — but they are not what gets scored, because a model that corrects itself
should be judged on the correction.
"""

from __future__ import annotations

import json
import queue
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

from mooring import marimo_rt, schema
from mooring.ai import cellwrite
from mooring.ai.tools import WRITE_TOOL_NAMES
from mooring.app.chat_service import ChatService
from mooring.config import AppConfig

from evals import checks as chk
from evals.cases import Case
from evals.fixtures import NOTEBOOKS, materialise

# How long one analyst turn may take before the run is abandoned. Generous: a
# reasoning model with a tool loop is not quick, and a timeout scored as a failure
# would quietly turn "slow" into "incapable".
DEFAULT_TURN_TIMEOUT = 240.0

# The turn a case sends when its first one produced nothing usable and it still
# has turns left. Deliberately says nothing about WHAT was wrong: the propose gate
# already handed the model its diagnostics in-loop, so a nudge that repeated them
# would be scoring mooring's prompt rather than the model's ability to act on it.
RETRY_TURN = (
    "That did not land — mooring did not accept it. Look at what its check reported "
    "and propose something that works."
)


# -- what one run produced ----------------------------------------------------


@dataclass
class Attempt:
    """Everything one run of a case produced, plus the static views over it."""

    workspace: Path
    notebook_rel: str
    base_source: str
    known_columns: tuple[str, ...]
    proposals: tuple[dict, ...] = ()
    tool_calls: tuple[str, ...] = ()
    tool_results: tuple[tuple[str, bool], ...] = ()
    replies: tuple[str, ...] = ()
    turns_used: int = 0
    first_proposal_turn: int = 0
    error: str = ""
    _candidate: str | None = field(default=None, repr=False)
    _composed: bool = field(default=False, repr=False)

    @property
    def refusals(self) -> int:
        """Write calls the gate handed back instead of letting through. The direct
        measure of whether the in-loop diagnostics are doing anything.

        Matched against :data:`~mooring.ai.tools.WRITE_TOOL_NAMES` — BOTH names the one
        write tool is registered under — not a ``mooring_propose`` prefix: in edit mode
        the tool is ``mooring_edit_notebook``, and a prefix test silently scored every
        such run as zero refusals, which reads as "the gate never fired"."""
        return sum(1 for name, ok in self.tool_results if name in WRITE_TOOL_NAMES and not ok)

    def last_proposal(self) -> dict | None:
        return self.proposals[-1] if self.proposals else None

    def kind_of(self, proposal: dict) -> str:
        """``append`` / ``edit`` / ``patch`` / ``rewrite``.

        There is one write tool, but it still emits a payload per SHAPE: a lone
        append keeps the legacy ``{code, rationale}`` event (no ``kind``, so it
        reads as ``append``), a lone edit is ``kind: "edit"``, a ``cells`` rewrite
        ``"rewrite"``, and anything else ``"patch"``.
        """
        return str(proposal.get("kind") or "append")

    def ops_of(self, proposal: dict) -> list[dict]:
        """The proposal as wire op-dicts — the exact shape Apply consumes."""
        ops = proposal.get("ops")
        if isinstance(ops, list):
            return [op for op in ops if isinstance(op, dict)]
        return [{"op": "append", "code": str(proposal.get("code", ""))}]

    def proposed_cells(self) -> list[tuple[str, str]]:
        """``(label, code)`` for every cell body the LAST proposal would write."""
        last = self.last_proposal()
        if last is None:
            return []
        out: list[tuple[str, str]] = []
        for op in self.ops_of(last):
            kind = op.get("op")
            if kind == "append":
                out.append(("the new cell", str(op.get("code", ""))))
            elif kind == "edit":
                out.append((f"cell {op.get('index')}", str(op.get("code", ""))))
            elif kind == "replace_all":
                for i, code in enumerate(op.get("cells") or []):
                    out.append((f"cell {i}", str(code)))
        return out

    def candidate(self) -> str | None:
        """The notebook the last proposal would produce, or ``None`` if it could
        not be applied at all. Composed through the real Apply path."""
        if self._composed:
            return self._candidate
        self._composed = True
        last = self.last_proposal()
        if last is None:
            return None
        with tempfile.TemporaryDirectory(prefix="mooring-eval-apply-") as tmp:
            target = Path(tmp) / "candidate.py"
            target.write_text(self.base_source, "utf-8", newline="\n")
            try:
                cellwrite.apply_wire_patch(target, self.ops_of(last))
            except (cellwrite.CellWriteError, ValueError, SyntaxError):
                return None
            self._candidate = target.read_text("utf-8")
        return self._candidate

    def introduced_diagnostics(self) -> list[marimo_rt.Diagnostic]:
        """Blocking diagnostics the proposal ADDED, by the gate's own accounting.

        Mirrors ``ai/tools.py``'s ``_split_by_blame``: compared by count, not
        membership, because three of marimo's five allowlisted rules carry a
        constant message — a set test would let one pre-existing instance whitelist
        every new one. A base that could not be checked attributes nothing.
        """
        if self.last_proposal() is None:
            return []  # nothing was changed, so nothing was introduced
        candidate = self.candidate()
        if candidate is None:
            # A proposal that exists but will not compose. Reported as a real fault,
            # because it IS one: Apply would have failed the same way.
            return [
                marimo_rt.Diagnostic(
                    code=marimo_rt.DIAG_CELL_SYNTAX,
                    name="not-applicable",
                    message="the proposal could not be applied to the notebook",
                )
            ]
        diagnostics = marimo_rt.validate_notebook_source(candidate)
        blocking = [d for d in diagnostics if d.code not in chk.NON_BLOCKING]
        if not blocking:
            return []
        base = marimo_rt.validate_notebook_source(self.base_source)
        if any(d.code in (marimo_rt.DIAG_VALIDATOR_UNAVAILABLE, marimo_rt.DIAG_TOO_LARGE)
               for d in base):
            return []  # the base was not checked, so nothing can be blamed on the change
        existing: Counter = Counter(_diag_key(d) for d in base)
        seen: Counter = Counter()
        introduced = []
        for d in blocking:
            key = _diag_key(d)
            seen[key] += 1
            if seen[key] > existing[key]:
                introduced.append(d)
        return introduced


def _diag_key(d: marimo_rt.Diagnostic) -> tuple:
    return (d.code, d.message, len(d.lines))


# -- one scored run -----------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    check: str
    reason: str


@dataclass(frozen=True)
class RunResult:
    case_id: str
    bucket: str
    passed: bool
    failures: tuple[Failure, ...] = ()
    proposals: int = 0
    refusals: int = 0
    turns_used: int = 0
    tool_calls: tuple[str, ...] = ()
    seconds: float = 0.0
    error: str = ""
    # Whether ANYTHING came back — a proposal or words. Reported separately from
    # `passed` because a reasoned decline and an empty response are different
    # things, and a card that showed only a rate would read them the same.
    answered: bool = True

    def as_dict(self) -> dict:
        return {
            "case": self.case_id,
            "bucket": self.bucket,
            "passed": self.passed,
            "answered": self.answered,
            "failures": [{"check": f.check, "reason": f.reason} for f in self.failures],
            "proposals": self.proposals,
            "refusals": self.refusals,
            "turns_used": self.turns_used,
            "tool_calls": list(self.tool_calls),
            "seconds": round(self.seconds, 2),
            "error": self.error,
        }


# The opener the runner is handed: it takes an assembled system context and the
# workspace the case built, and returns a live ChatBroadcaster. The real-model and
# scripted implementations both live in evals.providers.
SessionOpener = Callable[..., object]


def app_config(**ai_overrides) -> AppConfig:
    """A default :class:`~mooring.config.AppConfig` for a case.

    Built in memory from the dataclass defaults, never loaded from disk: an eval
    must not inherit the operator's ``config.toml`` — a machine with the notebook
    catalog switched on would send a different prompt and score a different model.
    """
    cfg = AppConfig()
    return replace(cfg, ai=replace(cfg.ai, **ai_overrides)) if ai_overrides else cfg


def run_case(
    case: Case,
    opener: SessionOpener,
    *,
    root: Path,
    turn_timeout: float = DEFAULT_TURN_TIMEOUT,
) -> RunResult:
    """Run one case once and score it. Never raises: a broken run is a failed run
    with the reason attached, because one flaky model call must not end a sweep."""
    started = time.monotonic()
    try:
        attempt = _drive(case, opener, root=root, turn_timeout=turn_timeout)
    except Exception as exc:  # noqa: BLE001  # a sweep survives one bad run
        return RunResult(
            case_id=case.id,
            bucket=case.bucket,
            passed=False,
            failures=(Failure("run", f"{type(exc).__name__}: {exc}"),),
            seconds=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )
    failures = tuple(
        Failure(check.name, reason)
        for check in case.checks
        if (reason := check.run(attempt))
    )
    if attempt.error:
        failures = (Failure("session", attempt.error), *failures)
    return RunResult(
        case_id=case.id,
        bucket=case.bucket,
        passed=not failures,
        failures=failures,
        proposals=len(attempt.proposals),
        refusals=attempt.refusals,
        turns_used=attempt.turns_used,
        tool_calls=attempt.tool_calls,
        seconds=time.monotonic() - started,
        error=attempt.error,
        answered=bool(attempt.proposals or any(r.strip() for r in attempt.replies)),
    )


def _drive(case: Case, opener: SessionOpener, *, root: Path, turn_timeout: float) -> Attempt:
    with tempfile.TemporaryDirectory(dir=root, prefix=f"{case.id.replace('/', '-')}-") as tmp:
        fixture = NOTEBOOKS[case.notebook]
        workspace, notebook_rel, dataset_rel, folders = materialise(fixture, Path(tmp))
        base_source = (workspace / notebook_rel).read_text("utf-8")
        cfg = app_config(**case.ai_config)
        context, dictionary, _banner, _live, models, code_index, catalog = (
            ChatService().build_context(cfg, workspace, notebook_rel, dataset_rel, folders)
        )
        attempt = Attempt(
            workspace=workspace,
            notebook_rel=notebook_rel,
            base_source=base_source,
            known_columns=_known_columns(workspace, fixture, base_source),
        )
        session = opener(
            case=case,
            app_cfg=cfg,
            system_context=context,
            workspace=workspace,
            folders=folders,
            notebook_rel=notebook_rel,
            dictionary=dictionary,
            semantic_models=models,
            helpers=code_index,
            catalog=catalog,
        )
        try:
            _send_turns(session, case, attempt, turn_timeout)
        finally:
            try:
                session.close()  # ty: ignore[unresolved-attribute]
            except Exception:  # noqa: BLE001  # teardown must not mask the result
                pass
        return attempt


def _send_turns(session, case: Case, attempt: Attempt, turn_timeout: float) -> None:
    q = session.subscribe()
    try:
        turns = list(case.turns)
        while len(turns) < case.max_turns:
            turns.append(RETRY_TURN)
        for number, prompt in enumerate(turns[: case.max_turns], start=1):
            attempt.turns_used = number
            session.send(prompt)
            _collect(q, attempt, number, turn_timeout)
            if attempt.error:
                return
            if attempt.proposals and not attempt.introduced_diagnostics():
                return  # a proposal that works — no reason to spend another turn
    finally:
        session.unsubscribe(q)


def _collect(q: "queue.Queue", attempt: Attempt, turn: int, timeout: float) -> None:
    """Drain one turn's events off the subscriber queue, into ``attempt``."""
    deadline = time.monotonic() + timeout
    pending: list[str] = []
    while time.monotonic() < deadline:
        try:
            event = q.get(timeout=0.25)
        except queue.Empty:
            continue
        kind = getattr(event, "kind", "")
        data = getattr(event, "data", {}) or {}
        if kind == "proposal":
            attempt.proposals = (*attempt.proposals, dict(data))
            attempt._composed = False  # a later proposal replaces the scored candidate
            attempt._candidate = None
            if not attempt.first_proposal_turn:
                attempt.first_proposal_turn = turn
        elif kind == "tool" and data.get("name"):
            name = str(data["name"])
            attempt.tool_calls = (*attempt.tool_calls, name)
            pending.append(name)
        elif kind == "tool_done":
            name = pending.pop() if pending else "?"
            attempt.tool_results = (*attempt.tool_results, (name, bool(data.get("success", True))))
        elif kind == "message":
            attempt.replies = (*attempt.replies, str(data.get("text", "")))
        elif kind in ("fail", "error"):
            attempt.error = str(data.get("text", "")) or kind
            return
        elif kind in ("idle", "closed"):
            return
    attempt.error = f"the model did not finish turn {turn} within {timeout:.0f}s"


def attempt_for(notebook: str, proposals: Iterable[dict], root: Path) -> Attempt:
    """An :class:`Attempt` built directly from proposal payloads, with no model.

    The seam that lets the SCORING be tested apart from the propose gate. Driving a
    case end to end proves the gate refused a bad proposal; this proves the checks
    would have caught it even if the gate had let it through — which is the failure
    mode a regression in ``ai/tools.py`` would actually produce, and the one no
    end-to-end run can reach.

    ``root`` must outlive the returned attempt: the fixture workspace is written
    inside it and :meth:`Attempt.candidate` reads the base source from memory but
    :attr:`Attempt.workspace` points at the files.
    """
    fixture = NOTEBOOKS[notebook]
    workspace, notebook_rel, _dataset_rel, _folders = materialise(fixture, root)
    base_source = (workspace / notebook_rel).read_text("utf-8")
    return Attempt(
        workspace=workspace,
        notebook_rel=notebook_rel,
        base_source=base_source,
        known_columns=_known_columns(workspace, fixture, base_source),
        proposals=tuple(proposals),
        first_proposal_turn=1,
        turns_used=1,
    )


def _known_columns(workspace: Path, fixture, base_source: str) -> tuple[str, ...]:
    """Column names a proposal may legitimately reference: every dataset's schema,
    plus every column the notebook it is editing already names (a derived frame's
    columns are real, and are not in any file's schema)."""
    names: set[str] = set()
    for ds in fixture.datasets:
        try:
            extracted = schema.extract_schema(workspace / ds.rel)
        except (ValueError, OSError):
            names |= set(ds.columns)
            continue
        names |= {column for column, _ in extracted.columns}
    try:
        cells = marimo_rt.read_cells(base_source)
    except (ValueError, SyntaxError, marimo_rt.MarimoTooOld):
        cells = []
    for _, code in cells:
        referenced, created = chk.column_names(code)
        names |= referenced | created
    return tuple(sorted(names))


# -- the capability card ------------------------------------------------------


@dataclass(frozen=True)
class BucketScore:
    bucket: str
    cases: int
    runs: int
    passes: int

    @property
    def rate(self) -> float:
        return self.passes / self.runs if self.runs else 0.0


@dataclass(frozen=True)
class Card:
    model: str
    provider: str
    repeat: int
    results: tuple[RunResult, ...]

    @property
    def buckets(self) -> list[BucketScore]:
        order: list[str] = []
        for r in self.results:
            if r.bucket not in order:
                order.append(r.bucket)
        return [
            BucketScore(
                bucket=name,
                cases=len({r.case_id for r in self.results if r.bucket == name}),
                runs=sum(1 for r in self.results if r.bucket == name),
                passes=sum(1 for r in self.results if r.bucket == name and r.passed),
            )
            for name in order
        ]

    @property
    def rate(self) -> float:
        return sum(1 for r in self.results if r.passed) / len(self.results) if self.results else 0.0

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "provider": self.provider,
            "repeat": self.repeat,
            "runs": len(self.results),
            "passes": sum(1 for r in self.results if r.passed),
            "rate": round(self.rate, 4),
            "buckets": [
                {
                    "bucket": b.bucket,
                    "cases": b.cases,
                    "runs": b.runs,
                    "passes": b.passes,
                    "rate": round(b.rate, 4),
                }
                for b in self.buckets
            ],
            "results": [r.as_dict() for r in self.results],
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)


BUCKET_LABELS = {
    "format": "format",
    "tool": "tool choice",
    "dag": "DAG hygiene",
    "schema": "schema fidelity",
    "sql": "sql cells",
    "repair": "repair",
}


def render_card(card: Card, *, show_failures: bool = True) -> str:
    """The human-readable capability card."""
    # ASCII only, on purpose: Windows is the primary platform and a console still
    # running cp1252 turns an em dash into a replacement character mid-table.
    lines = [
        "",
        f"  Capability card: {card.model or '(default model)'} via {card.provider}",
        f"  {len(card.results)} runs | {card.repeat} per case | "
        f"{len({r.case_id for r in card.results})} cases",
        "",
        f"  {'bucket':<18}{'cases':>6}{'runs':>6}{'pass':>6}{'rate':>7}   ",
    ]
    lines.append("  " + "-" * 55)
    for b in card.buckets:
        label = BUCKET_LABELS.get(b.bucket, b.bucket)
        bar = _bar(b.rate)
        lines.append(
            f"  {label:<18}{b.cases:>6}{b.runs:>6}{b.passes:>6}{b.rate:>6.0%}   {bar}"
        )
    lines.append("  " + "-" * 55)
    passes = sum(1 for r in card.results if r.passed)
    lines.append(
        f"  {'OVERALL':<18}{len({r.case_id for r in card.results}):>6}{len(card.results):>6}"
        f"{passes:>6}{card.rate:>6.0%}   {_bar(card.rate)}"
    )
    weakest = min(card.buckets, key=lambda b: b.rate, default=None)
    if weakest is not None and weakest.rate < 1.0:
        lines += ["", f"  Weakest: {BUCKET_LABELS.get(weakest.bucket, weakest.bucket)} "
                      f"({weakest.rate:.0%})."]
    gated = sum(r.refusals for r in card.results)
    if gated:
        lines.append(
            f"  The propose gate refused {gated} proposal(s) across the sweep: each one a "
            "break the analyst never saw."
        )
    # Stated on its own line, never folded into the rate: "declined, with a reason"
    # and "returned nothing" are different capabilities, and a model that cannot call
    # tools at all should be unmistakable on its own card rather than looking merely
    # cautious.
    mute = sum(1 for r in card.results if not r.answered)
    if mute:
        lines.append(
            f"  {mute} run(s) produced NOTHING at all - no proposal and no reply. A model "
            "that cannot call tools looks cautious until you count these."
        )
    if show_failures:
        lines += _failure_lines(card.results)
    lines.append("")
    return "\n".join(lines)


def _failure_lines(results: Iterable[RunResult]) -> list[str]:
    by_case: dict[str, list[Failure]] = {}
    for r in results:
        if not r.passed:
            by_case.setdefault(r.case_id, []).extend(r.failures)
    if not by_case:
        return ["", "  No failures."]
    lines = ["", "  Failures:"]
    for case_id, failures in sorted(by_case.items()):
        seen: list[str] = []
        for f in failures:
            entry = f"{f.check}: {f.reason}"
            if entry not in seen:
                seen.append(entry)
        lines.append(f"    {case_id}")
        for entry in seen[:3]:
            lines.append(f"      - {entry}")
    return lines


def _bar(rate: float, width: int = 10) -> str:
    filled = round(rate * width)
    return "#" * filled + "." * (width - filled)
