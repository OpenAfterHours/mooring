"""The model's own write: apply the change, run it, and hand back what happened.

This is the service behind ``mooring_edit_notebook``. In propose mode the copilot
emits a card and stops; the analyst reads code, clicks Apply, and finds out whether it
worked by looking at the notebook. That loop caps how hard a piece of analysis the
model can carry, because the model never learns anything: it writes blind and is told
nothing back. Here the write LANDS inside the tool call, marimo runs it, and the tool
result is a value-free OBSERVATION of the running kernel — which names the change
should have bound, which of them are actually there, and the schema of the frames that
came back. The model reads its own work and corrects itself in the same turn.

What that buys is only safe because of what it does NOT change:

* **The apply gate stays, exactly as it was.** Every write goes through
  :meth:`mooring.app.apply.ApplyGuard.apply_with_undo`, so a cell that deletes files,
  runs a program, or overwrites a report is still HELD for the analyst — ``ask`` and
  ``floor`` are untouched. What has gone is the unconditional click on a *reversible*
  change, not the confirmation on an irreversible one.
* **A hold is a hold.** ``auto_apply = false`` mid-session, and a codeguard hold,
  return the SAME ``held`` status carrying the SAME proposal payload. From the model's
  side they are one thing — "a human must act" — and from the analyst's side they are
  the ordinary Apply card they already know. No second hold UI, no new state.
* **Nothing that leaves is a value.** The observation is names, dtypes, row counts and
  this module's own words (:func:`mooring.ai.introspect.format_observation`); the
  receipt payload is cell numbers and the same one-line summary. The optional run
  report goes through ``egress.sanitize_traceback`` — the one gateway — inside
  :mod:`mooring.app.run_report`, never a second route to the sanitiser.
* **The analyst keeps the stop and the way back.** A cancelled turn writes nothing, and
  every write in one turn shares ONE undo checkpoint, so Revert puts the notebook back
  the way it was before the assistant started — not one write at a time.

Layering note: this is L3.5 (``app/``) and the tool that calls it is L3 (``ai/``), so
the callback is injected downwards and the outcome is read back by DUCK TYPING
(``.status`` / ``.text`` / ``.is_error`` / ``.payload``). ``ai/`` importing
:class:`ApplyOutcome` would invert the layering; see ``.importlinter``.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

from mooring import config, marimo_rt, policy
from mooring.app import notebooks
from mooring.app.apply import ApplyGateHeld, ApplyGuard

# How long the observation waits for the kernel to settle before reporting "could not
# see". Deliberately the introspect module's own budget: the settle rule, the poll
# interval and this ceiling are one calibration and belong together.
OBSERVE_TIMEOUT = 20.0

# Fixed, value-free texts. Constants because each is a thing the MODEL reads and acts
# on, and a sentence assembled at the call site is a sentence that can drift.
_MANUAL_TEXT = (
    "mooring is set to let the analyst apply changes themselves, so this one is "
    "waiting for them. It is NOT running yet."
)
_CANCELLED_TEXT = "The analyst stopped this turn, so nothing was written."
_DISABLED_TEXT = "The copilot is switched off for this notebook, so nothing was written."
_NO_NOTEBOOK_TEXT = "The notebook could not be opened, so nothing was written."


@dataclass(frozen=True)
class ApplyOutcome:
    """What one model-driven write did — read by the tool AND by the chat session.

    ``status`` is the discriminator: ``applied`` / ``held`` / ``conflict`` /
    ``disabled`` / ``cancelled`` / ``error``. ``text`` is the value-free text handed to
    the MODEL (the observation, or the reason nothing happened). ``payload`` is the
    value-free body for the LOCAL UI event — an ``applied`` receipt or, for ``held``,
    the ordinary proposal card.

    The attribute is ``payload``. :func:`mooring.ai.chat._event_payload` reads
    ``.payload`` first and falls back to ``.data``, so naming it the other way would
    still have reached the browser — but only by accident, and a third shape would
    silently ship an empty receipt. This is the name.
    """

    status: str
    text: str
    is_error: bool = False
    payload: dict = field(default_factory=dict)


def make_applier(
    *,
    workspace: Path,
    notebook_rel: str,
    guard: ApplyGuard,
    cfg_fn,
    editor_fn,
    observe_timeout: float = OBSERVE_TIMEOUT,
) -> "NotebookApplier":
    """Build the ``apply_edit(op_dicts, rationale) -> ApplyOutcome`` callback.

    ``guard`` is the hub's ONE :class:`~mooring.app.apply.ApplyGuard`, so a model write
    serialises with a manual Apply, an Undo and a sync rollback on the same lock.
    ``cfg_fn`` returns the CURRENT :class:`mooring.config.Config` (the hub reloads its
    config in place, and a captured one would pin a stale workspace). ``editor_fn``
    returns this workspace's marimo :class:`~mooring.editor.EditorServer`, or ``None``
    when nothing is running — the observation degrades to "could not see", which is a
    different thing from a failure and is reported as such.

    The result is a callable object, not a closure, because the caller also needs
    :meth:`NotebookApplier.bind` (to reach the session's cancel flag, which does not
    exist until the session is built with this applier) and
    :meth:`NotebookApplier.begin_turn`.
    """
    return NotebookApplier(
        workspace=Path(workspace),
        notebook_rel=notebook_rel,
        guard=guard,
        cfg_fn=cfg_fn,
        editor_fn=editor_fn,
        observe_timeout=observe_timeout,
    )


class NotebookApplier:
    """One notebook's in-turn writer, for the life of one chat session."""

    def __init__(
        self,
        *,
        workspace: Path,
        notebook_rel: str,
        guard: ApplyGuard,
        cfg_fn,
        editor_fn,
        observe_timeout: float = OBSERVE_TIMEOUT,
    ) -> None:
        self._workspace = Path(workspace)
        self._notebook_rel = notebook_rel
        self._guard = guard
        self._cfg_fn = cfg_fn
        self._editor_fn = editor_fn
        self._observe_timeout = observe_timeout
        self._session = None
        # The turn this applier is writing under. Minted here so a write is always
        # turn-scoped even if nobody ever calls begin_turn (a session that writes
        # before its first send still groups its own writes into one checkpoint).
        self._turn_id = _new_turn_id()
        self._lock = threading.Lock()

    # -- wiring ---------------------------------------------------------------

    def bind(self, session) -> None:
        """Attach the chat session, for its cancel flag and its traceback sanitiser.

        Separate from construction because of an ordering the layering forces: the
        applier is an argument to the session's constructor, so it exists first. Both
        uses are duck-typed and optional — an unbound applier simply never cancels and
        never runs a report.
        """
        self._session = session

    def begin_turn(self) -> str:
        """Start a new turn: later writes get a FRESH undo checkpoint and a new receipt
        group. Called when the analyst sends (see ``hub/routes/chat.py``), which is the
        only thing that starts a turn."""
        with self._lock:
            self._turn_id = _new_turn_id()
            return self._turn_id

    @property
    def turn_id(self) -> str:
        with self._lock:
            return self._turn_id

    # -- the write ------------------------------------------------------------

    def __call__(self, op_dicts, rationale: str = "") -> ApplyOutcome:
        ops = list(op_dicts or [])
        rationale = str(rationale or "")
        turn_id = self.turn_id
        auto_apply, auto_run_report = _arming(self._workspace)

        if not auto_apply:
            # Manual mode, decided at the moment of THIS write. Reuses the hold path on
            # purpose: the analyst gets the ordinary proposal card and the model is told
            # the same thing a codeguard hold tells it — a human has to act.
            return _held(ops, rationale, turn_id, _MANUAL_TEXT, gate=None)
        if self._cancelled():
            return ApplyOutcome("cancelled", _CANCELLED_TEXT)

        try:
            nb_path = notebooks.ws_file(self._workspace, self._notebook_rel, suffix=".py")
        except (ValueError, FileNotFoundError):
            # Value-free: the path is the analyst's, and it is already known to the
            # model from the system context — but there is nothing useful to add.
            return ApplyOutcome("error", _NO_NOTEBOOK_TEXT, is_error=True)

        from mooring.ai.cellwrite import CellApplyConflict, CellWriteError

        try:
            undo_depth = self._guard.apply_with_undo(
                nb_path, self._workspace, self._notebook_rel, ops, turn_id=turn_id
            )
        except PermissionError:  # "notebook_disabled" — the AI opt-out, re-read at the write
            return ApplyOutcome("disabled", _DISABLED_TEXT, is_error=True)
        except ApplyGateHeld as held:
            # NOTHING was written — no snapshot, no bytes. The gate block travels
            # verbatim so today's hold card renders exactly as it always did.
            from mooring.ai import codeguard

            reasons = "\n".join(f"- {line}" for line in codeguard.describe(held.verdict))
            return _held(
                ops,
                rationale,
                turn_id,
                "mooring is holding this change for the analyst to confirm, because of "
                f"what it does:\n{reasons}",
                gate=held.payload()["gate"],
            )
        except CellApplyConflict as exc:  # a CellWriteError subclass — catch it first
            return ApplyOutcome("conflict", _clean(str(exc)), is_error=True)
        except CellWriteError as exc:
            return ApplyOutcome("error", _clean(str(exc)), is_error=True)

        return self._observe_and_report(
            nb_path, ops, rationale, turn_id, undo_depth, auto_run_report
        )

    # -- what came back -------------------------------------------------------

    def _observe_and_report(
        self, nb_path: Path, ops, rationale: str, turn_id: str, undo_depth: int, auto_run_report
    ) -> ApplyOutcome:
        """The half of the feature that is the point: watch the change run.

        Everything here is best-effort by construction. The bytes are already on disk
        and marimo is already running them, so a failure to OBSERVE must never be
        reported as a failure to apply — it is reported as "could not see", which
        :func:`mooring.ai.introspect.format_observation` states in terms the model is
        told not to act on.
        """
        from mooring.ai import introspect

        new_source = _read_text(nb_path)
        try:
            defs = marimo_rt.cell_defs(new_source)
            touched = _touched_indices(ops, len(defs))
            obs = introspect.observe(
                self._editor(),
                self._notebook_rel,
                _expected_names(defs, touched),
                timeout=self._observe_timeout,
            )
            text = introspect.format_observation(obs)
        except Exception:  # noqa: BLE001
            # The bytes ARE on disk and marimo is running them. Reporting an error here
            # would tell the model its change did not land, which is false and is the one
            # answer that makes it undo working code. Degrade to "could not see" instead.
            defs, obs = [], introspect.Observation(detail="the observation failed")
            text = introspect.format_observation(obs)

        report = ""
        if auto_run_report and obs.observed and obs.missing:
            report = self._auto_run_report(new_source)
        if report:
            text = f"{text}\n\n{report}"

        return ApplyOutcome(
            "applied",
            text,
            payload={
                "summary": _summary(ops, len(defs)),
                "rationale": rationale,
                "undo_depth": undo_depth,
                "turn_id": turn_id,
                "observation": _observation_line(obs),
            },
        )

    def _auto_run_report(self, new_source: str) -> str:
        """Smoke-run the notebook and return the value-safe failure summary, or ``""``.

        Fires only when the observation already said a name the change should have bound
        is NOT bound — i.e. a cell did not complete — so the analyst never pays minutes
        of CPU for a change that worked. On top of that, the three conditions the module
        docstring of :mod:`mooring.app.run_report` names: the knob (checked by the
        caller), a notebook that scans ``clean`` under codeguard, and a turn that has not
        been cancelled. The band is the CONDITION, not a proxy for one — it answers
        exactly "is re-executing this safe?", which is the question being asked.

        Every failure — no session, a busy workspace, a broken environment, the notebook
        disabled mid-run — returns ``""``. The model still gets the observation; it just
        does not get the extra detail.
        """
        from mooring.ai import codeguard

        session = self._session
        if session is None or self._cancelled():
            return ""
        if codeguard.scan_code(new_source).band != codeguard.BAND_CLEAN:
            return ""

        from mooring.app import run_report

        cancel = _CancelBridge(self._cancelled, keepalive=getattr(session, "touch", None))
        try:
            cfg = self._cfg_fn()
            report = run_report.run_and_collect(
                session, cfg, self._notebook_rel, cancel=cancel.event
            )
        except Exception:  # noqa: BLE001 — a report that cannot run must not break the write
            return ""
        finally:
            cancel.stop()
        if report.cancelled or not report.sent:
            return ""
        return report.sent

    # -- small helpers --------------------------------------------------------

    def _editor(self):
        try:
            return self._editor_fn()
        except Exception:  # noqa: BLE001 — no editor is "could not see", never a crash
            return None

    def _cancelled(self) -> bool:
        """Whether the analyst has stopped this turn. Duck-typed and fail-OPEN: an
        applier with no session (or a session without the method) is not cancelled, so
        a missing signal never silently blocks every write."""
        session = self._session
        checker = getattr(session, "cancel_requested", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001
            return False


class _CancelBridge:
    """A ``threading.Event`` that mirrors a ``cancelled()`` predicate, and keeps the chat
    alive while the run it guards is going.

    :func:`mooring.app.notebook_run.run` takes an Event (it waits on it from a watchdog
    thread to kill the process tree), while the session exposes a boolean callable. One
    daemon poller for the life of one run bridges them; :meth:`stop` ends it when the run
    does, so a long chat never accumulates threads.

    ``keepalive`` is the session's ``touch``. A smoke run is minutes long and happens
    INSIDE a tool call, with nothing else moving the session's activity clock — and idle
    chats are reaped at 15 minutes. Being reaped mid-run would close the very session the
    result is for.
    """

    POLL_SECONDS = 0.4

    def __init__(self, cancelled, keepalive=None) -> None:
        self.event = threading.Event()
        self._done = threading.Event()
        self._cancelled = cancelled
        self._keepalive = keepalive if callable(keepalive) else None
        threading.Thread(target=self._poll, name="mooring-run-cancel", daemon=True).start()

    def _poll(self) -> None:
        while not self._done.wait(self.POLL_SECONDS):
            try:
                if self._cancelled():
                    self.event.set()
                    return
                if self._keepalive is not None:
                    self._keepalive()
            except Exception:  # noqa: BLE001 — a broken predicate must not kill the run
                return

    def stop(self) -> None:
        self._done.set()


def _new_turn_id() -> str:
    return secrets.token_urlsafe(6)


def _arming(workspace: Path) -> tuple[bool, bool]:
    """``(auto_apply, auto_run_report)``, read FRESH from disk and policy-folded.

    The same read, in the same place, for the same TOCTOU reason as
    :func:`mooring.app.apply._guard_armed`: the local config file and the synced
    ``mooring.toml`` both change under a running hub — a ``mooring config set``, a
    Settings write, a pull that brings a new ``[policy]`` block — and an answer captured
    when the chat opened would be about a config that no longer exists. A policy pinning
    ``ai.auto_apply = false`` has to bite on the very NEXT write, not the next restart.

    ``policy.tighten_app_config`` rather than the local value alone, because that is the
    point of both knobs being policy-governed: a team can take the model's write away
    for everyone, and local config (env vars included) cannot answer back.

    Fails CLOSED — an unreadable config means manual mode and no automatic run. The
    worst case of failing closed is one Apply click; the worst case of failing open is
    "corrupt the config" becoming a way past both knobs.
    """
    try:
        app_cfg = policy.tighten_app_config(config.load_app_config(), workspace)
        return bool(app_cfg.ai_auto_apply), bool(app_cfg.ai_auto_run_report)
    except Exception:  # noqa: BLE001
        return False, False


def _read_text(path: Path) -> str:
    try:
        return path.read_text("utf-8")
    except OSError:
        return ""


def _index_of(op) -> int | None:
    try:
        return int(op.get("index"))
    except (AttributeError, TypeError, ValueError):
        return None


def _kinds(ops) -> list[tuple[str, dict]]:
    return [(str(op.get("op", "")), op) for op in ops if isinstance(op, dict)]


def _indices(kinds, want: str) -> list[int]:
    """The readable ``index`` of every op of one kind, ascending. An op whose index is
    missing or unreadable is dropped: it cannot be turned into a cell number, and
    inventing one would put the wrong cell on the receipt."""
    found = {_index_of(op) for kind, op in kinds if kind == want}
    return sorted(i for i in found if i is not None)


def _touched_indices(ops, n_cells: int) -> set[int]:
    """Which cells of the NEW notebook this patch wrote, as indices into it.

    Computed rather than guessed, because the observation is only meaningful if it asks
    about the right cells. :func:`mooring.marimo_rt.apply_cell_patch` keeps the original
    order (edits in place, deletes removed) and puts appends at the END, so an edit's new
    index is its old one minus the deletes before it, and the appends are the last cells
    in the file. ``replace_all`` is every cell — it is exclusive, so nothing else can be
    in the same patch.

    Anything out of range is dropped: a patch that has already been applied cannot be
    re-derived wrongly here, but a bad index reaching ``cell_defs`` would ask about a
    cell that is not there.
    """
    kinds = _kinds(ops)
    if any(kind == "replace_all" for kind, _ in kinds):
        return set(range(n_cells))
    deletes = _indices(kinds, "delete")
    n_appends = sum(1 for kind, _ in kinds if kind == "append")
    touched: set[int] = set()
    for kind, op in kinds:
        if kind != "edit":
            continue
        idx = _index_of(op)
        if idx is None:
            continue
        shifted = idx - sum(1 for d in deletes if d < idx)
        if 0 <= shifted < n_cells:
            touched.add(shifted)
    for offset in range(n_appends):
        appended = n_cells - n_appends + offset
        if 0 <= appended < n_cells:
            touched.add(appended)
    return touched


def _expected_names(defs, touched: set[int]) -> tuple[str, ...]:
    """The names the written cells DEFINE, deduped, in document order.

    ``defs`` is :func:`mooring.marimo_rt.cell_defs`' output, which is ``[]`` when marimo
    could not tell — in which case this is empty too and the observation reports the
    session's frames without claiming anything is missing. "Could not tell" must not
    become "it failed" at any step of this chain.
    """
    seen: dict[str, None] = {}
    for index, names in defs:
        if index in touched:
            for name in names:
                seen.setdefault(str(name), None)
    return tuple(seen)


def _summary(ops, n_cells: int) -> dict:
    """The receipt's cell numbers: ``{edited, appended, deleted}``.

    Edits and deletes are reported by the index the change TARGETED — the number the
    model used and the proposal card labels ("cell 3") — because that is the only number
    a deleted cell has. Appends have no target, so they are reported by where they landed.
    """
    kinds = _kinds(ops)
    if any(kind == "replace_all" for kind, _ in kinds):
        return {"edited": list(range(n_cells)), "appended": [], "deleted": []}
    n_appends = sum(1 for kind, _ in kinds if kind == "append")
    appended = [i for i in range(n_cells - n_appends, n_cells) if i >= 0]
    return {
        "edited": _indices(kinds, "edit"),
        "appended": appended,
        "deleted": _indices(kinds, "delete"),
    }


def _diffs(ops) -> list[dict]:
    """The hold card's before/after sections, in the SHAPE ``ai/tools.py`` emits in
    propose mode — same labels, same order — so a held model write and a proposed one
    render as the same card. The ``anchor`` an edit/delete already carries IS the cell's
    current source, so nothing is read off disk to build this.
    """
    out: list[dict] = []
    for kind, op in _kinds(ops):
        anchor = str(op.get("anchor") or "")
        code = str(op.get("code") or "")
        if kind == "edit":
            out.append({"label": f"cell {_index_of(op)}", "before": anchor, "after": code})
        elif kind == "delete":
            out.append({"label": f"cell {_index_of(op)} (deleted)", "before": anchor, "after": ""})
        elif kind == "append":
            out.append({"label": "new cell", "before": "", "after": code})
        elif kind == "replace_all":
            cells = op.get("cells") or []
            after = "\n\n".join(str(c) for c in cells) if isinstance(cells, (list, tuple)) else ""
            out.append({"label": "whole notebook", "before": "", "after": after})
    return out


def _proposal_kind(ops) -> str:
    """The card shape, chosen the way ``ai/tools.py`` chooses it in propose mode.

    Matched deliberately, kind for kind: a lone append renders as an additive block, a
    lone edit as a one-cell diff, a rewrite as a whole-notebook one. A held model write
    is the same event as a proposal, so it must not arrive looking like a different one.
    """
    kinds = [kind for kind, _ in _kinds(ops)]
    if "replace_all" in kinds:
        return "rewrite"
    if kinds == ["edit"]:
        return "edit"
    if kinds == ["append"]:
        return "append"
    return "patch"


def _held(ops, rationale: str, turn_id: str, text: str, *, gate) -> ApplyOutcome:
    """The one hold outcome, for BOTH reasons a write can be held.

    Manual mode and a codeguard hold are the same event from every side that matters:
    nothing was written, and a human decides next. So they share the status, the payload
    and the card — the analyst sees the proposal they would have seen anyway, clicks
    Apply, and (for a gated change only) meets the confirm they would have met anyway,
    because ``/api/ai/chat/apply`` re-scans and re-derives the token server-side.

    ``gate`` is :meth:`mooring.app.apply.ApplyGateHeld.payload`'s block, verbatim, or
    ``None`` in manual mode. It rides along unchanged for any consumer that wants it;
    the card itself is driven by ``kind``/``ops``/``diffs``, exactly as a proposal is.
    """
    kind = _proposal_kind(ops)
    payload = {
        "kind": kind,
        "rationale": rationale,
        "ops": ops,
        "diffs": _diffs(ops),
        "turn_id": turn_id,
    }
    if kind == "append":
        # The additive block reads the code off `code`, not off `ops` — the legacy shape
        # a lone appended cell has always arrived in, kept so the commonest change the
        # copilot makes looks the same held as proposed.
        payload["code"] = str(ops[0].get("code") or "")
    if gate is not None:
        payload["gate"] = gate
    return ApplyOutcome("held", text, payload=payload)


def _clean(text: str) -> str:
    """One line, bounded. Applied to the two messages that come from an EXCEPTION rather
    than from this module's own constants; codeguard/cellwrite never interpolate cell
    content into their messages, so this is tidying, not the value-safety guarantee."""
    return " ".join(str(text).split())[:300]


def _observation_line(obs) -> str:
    """The receipt's one-line observation — what the analyst reads instead of the code.

    Value-free by the same rule as :func:`mooring.ai.introspect.format_observation`:
    names, dtypes, counts and fixed words. Kept separate from that function because they
    answer to different readers — the model gets the full schema block, the analyst gets
    the line that says whether it worked.
    """
    if not getattr(obs, "observed", False):
        # "Could not see" — never "it failed". The two are different facts and the
        # receipt says which one this is, in words, for the same reason the model's
        # copy does.
        blind = "could not see the notebook run"
        detail = str(getattr(obs, "detail", "") or "")
        return f"{blind} — {detail}" if detail else blind
    frame_of = {f.name: f for f in getattr(obs, "frames", ())}
    parts: list[str] = []
    for name in getattr(obs, "present", ()):
        frame = frame_of.get(name)
        if frame is None:
            parts.append(f"{name} bound")
            continue
        rows = f", {frame.n_rows} rows" if frame.n_rows is not None else ""
        cols = len(frame.columns)
        parts.append(f"{name}: {cols} column{'' if cols == 1 else 's'}{rows}")
    missing = tuple(getattr(obs, "missing", ()))
    if missing:
        parts.append("not bound: " + ", ".join(missing))
    if not parts:
        return "ran — nothing new is bound"
    return " · ".join(parts)
