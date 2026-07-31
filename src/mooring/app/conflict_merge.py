"""Cell-level three-way merge for a conflicted marimo notebook.

A conflicted notebook is the scariest moment in the product. The three existing
resolutions (Use remote / Keep both / Push as copy) are all-or-nothing, and
"Keep both … merge by hand" means an analyst with no git diffing two ``.py``
files in Notepad — the one place the "nothing is silently lost" promise
degrades. This module makes the ordinary case ordinary: most conflicts are two
people editing DIFFERENT cells of the same notebook, which needs no human
decision at all.

It is a genuine three-way merge over the same three SHAs the sync engine
classifies with — the manifest base, the file on disk, the remote blob. A cell
changed on only ONE side is taken automatically; only a cell changed on BOTH
sides becomes a choice.

**The governing rule is that a wrong merge is far worse than no merge.** Every
ambiguity resolves to :class:`MergeUnavailable` and the caller falls back to the
three whole-file resolutions, which this module never touches. That rule is why:

* Cells pair against the base with :mod:`mooring.celldiff`'s matcher (a marimo
  ``.py`` persists no per-cell identity — see :mod:`marimo_rt`), but anything it
  leaves unpaired on BOTH sides at once is settled by marimo's own dataflow
  identity — two cells defining the same name ARE one cell — and refused if that
  still does not settle it. Reporting a rewritten cell as "you deleted it" would
  be a lie the analyst then acts on.
* Two people's ADDITIONS are never paired by similarity. Similarity is evidence
  that a cell was edited; it is no evidence that two brand-new cells are the
  same cell (``import polars as pl`` and ``import altair as alt`` pair at 0.77
  and are entirely unrelated). Both are kept, exactly as the docs promise; only
  byte-identical additions collapse into one.
* The merged cell list is checked for duplicate top-level definitions before it
  is written. Two individually valid halves can compose into a notebook marimo
  refuses to run (``MultipleDefinitionError``), and nothing downstream would
  catch it — the file would write, push, and break for the whole team.
* A notebook's frame — its PEP 723 ``# /// script`` header and its
  ``marimo.App(...)`` options — is three-wayed like a cell, so a teammate's
  dependency pin is never silently reverted; and each merged cell is carried
  over WHOLE (its name and ``@app.cell`` options included), so a cell the team
  deliberately disabled does not quietly start running.

Two more properties make it safe to offer:

* **Nothing is lost.** The pre-merge bytes go to the local trash before the
  write; unlike the sync engine's pre-images these are the analyst's UNPUSHED
  work, so a failed deposit ABORTS the merge rather than degrading past it.
* **The client never supplies source.** :func:`apply` recomputes the plan from
  the three SHAs and requires the caller to name all three, so a request carries
  only *which side wins* per cell — never code to write, and never a waiver of
  the staleness check.

The merge writes the workspace file and nothing else — it never pushes.
Afterwards the file is a plain MODIFIED push candidate (the manifest base
advances to the remote sha, exactly as KEEP_BOTH does), so the analyst publishes
the merged result deliberately, like any other edit.

Known limit: cell decisions compare SOURCE, so a cell whose code is identical on
both sides but whose ``@app.cell`` options a teammate changed keeps yours. That
is a display setting on a cell neither of you edited, not lost work.

Orchestration lives here rather than in ``sync.py`` because the sync domain core
may not import ``celldiff``/``marimo_rt`` (see ``.importlinter``) — this is
precisely the "needed by both adapters, sits above the core" shape ``app/``
exists for.
"""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from mooring import celldiff, gitsha
from mooring import manifest as manifest_mod
from mooring import marimo_rt, notebook_template, trash
from mooring.config import Config
from mooring.github import NotFound
from mooring.paths import safe_write_bytes

# celldiff's cell matcher and unified-diff shaper are used directly (rather than
# re-implemented) on purpose: one matching heuristic across the review panel, the
# reviewer inbox, and this merge means a cell that "is the same cell" in one view is
# the same cell in all of them — and one diff dialect means one renderer.
# NOTE: here they decide DATA outcomes, not just what a panel shows. A pairing
# celldiff gets wrong mislabels a cell in the review panel; the same pairing here
# would write the wrong cell to disk. Everything this module builds on top of them —
# the name-overlap fallback, the both-sides-unpaired refusal, the duplicate
# definition check — exists to keep a display heuristic from deciding an analyst's
# work. Weigh that before changing either function.
_match_cells = celldiff._match_cells
_unified = celldiff._unified

# A side must keep MORE than this fraction of the base's cells; below or AT the
# boundary it has been restructured wholesale rather than edited, and pairing what
# is left would be guesswork dressed as a per-cell choice. Deliberately fail-closed
# at exactly half — a boundary that decides whether an analyst's cells are merged
# or guessed at belongs on the safe side.
MIN_BASE_MATCH_RATIO = 0.5

# The action label the trash deposit carries (see mooring.trash) — the Activity page
# shows it verbatim, so it names what destroyed the pre-image.
TRASH_ACTION = "merge-cells"

_STALE_REASONS = {
    "remote_sha": (
        "The team pushed a new version while you were choosing — reopen the merge to "
        "see their latest cells."
    ),
    "local_sha": (
        "Your copy changed while you were choosing — reopen the merge so the choices "
        "match the file on disk."
    ),
    "base_sha": "The last-synced version changed — reopen the merge.",
}


class MergeUnavailable(Exception):
    """This conflict cannot be resolved cell by cell; the caller falls back to the
    three whole-file resolutions. ``str(exc)`` is the user-facing reason."""


class MergeStale(Exception):
    """One of the three sides moved between planning and applying the merge, so the
    choices the user made no longer describe the file. ``str(exc)`` is the reason."""


@dataclass(frozen=True)
class _CellRef:
    """A cell in one of the two candidate notebooks: which side, where in it, and its
    source. The ``(side, index)`` pair is what the writer emits, so the cell arrives
    carrying its own name and ``@app.cell`` options; ``code`` is for comparing,
    diffing, and the duplicate-definition check."""

    side: str  # "local" | "remote"
    index: int
    code: str


@dataclass(frozen=True)
class MergeCell:
    """One cell's fate in the merged notebook.

    ``origin`` is where the cell comes from: ``"base"`` (a last-synced cell, matched
    on one or both sides), ``"local"`` / ``"remote"`` (added on that side only), or
    ``"both"`` (the byte-identical cell added on both). ``status`` is ``"auto"``
    (decided) or ``"choice"`` (both sides changed it differently — only ever a base
    cell). For an auto cell ``side`` names who won and ``take`` is the cell to emit
    (``None`` = the cell is dropped); for a choice, ``local_ref`` / ``remote_ref``
    are the two candidates (``None`` = that side deleted it) and ``diff`` is a
    unified diff between them.
    """

    id: str
    origin: str
    status: str
    side: str = ""
    index_base: int | None = None
    take: _CellRef | None = None
    local_ref: _CellRef | None = None
    remote_ref: _CellRef | None = None
    diff: str = ""

    @property
    def code(self) -> str | None:
        return self.take.code if self.take is not None else None

    @property
    def local(self) -> str | None:
        return self.local_ref.code if self.local_ref is not None else None

    @property
    def remote(self) -> str | None:
        return self.remote_ref.code if self.remote_ref is not None else None


@dataclass(frozen=True)
class MergePlan:
    """What a merge would do, in merged-document order, plus the three SHAs it was
    computed from (the staleness key :func:`apply` re-checks)."""

    path: str
    base_sha: str
    local_sha: str
    remote_sha: str
    cells: tuple[MergeCell, ...]
    frame_side: str = "local"  # whose header + marimo.App(...) options survive
    auto_local: int = 0
    auto_remote: int = 0
    auto_both: int = 0
    unchanged: int = 0

    @property
    def conflicts(self) -> tuple[MergeCell, ...]:
        return tuple(c for c in self.cells if c.status == "choice")

    @property
    def auto_merged(self) -> int:
        """Cells a side actually changed and the merge took without asking — the
        number that carries most of this feature's value."""
        return self.auto_local + self.auto_remote + self.auto_both


@dataclass(frozen=True)
class MergeOutcome:
    """The result of a completed merge, shaped like a :class:`sync.SyncResult` slice
    so the adapters' existing log/undo rendering serves it unchanged."""

    path: str
    auto_merged: int
    chosen_local: int
    chosen_remote: int
    lines: tuple[str, ...]
    trashed: tuple[tuple[str, str], ...] = ()

    def summary(self) -> str:
        chosen = self.chosen_local + self.chosen_remote
        text = f"merged {self.path}: {self.auto_merged} cell(s) merged automatically"
        if chosen:
            text += f", {chosen} resolved by you"
        return text + " — review it, then push"


@dataclass(frozen=True)
class _Sides:
    """The three versions of one file, read once and shared by plan and apply."""

    rel: str
    base_sha: str
    local_sha: str
    remote_sha: str
    base_text: str
    local_text: str
    remote_text: str


def plan(client, cfg: Config, rel_path: str) -> MergePlan:
    """What a cell-level merge of ``rel_path`` would do. Read-only: fetches the base
    and remote blobs and reads the local file, writing nothing.

    Raises :class:`MergeUnavailable` when the conflict is not mergeable per cell.
    """
    return _build_plan(_read_sides(client, cfg, rel_path))


def apply(
    client,
    cfg: Config,
    rel_path: str,
    choices: dict[str, str],
    *,
    expect: dict[str, str],
) -> MergeOutcome:
    """Write the merged notebook, taking ``choices`` (cell id -> ``"local"`` /
    ``"remote"``) for the cells both sides changed.

    The plan is RECOMPUTED here from the three sides and re-checked against
    ``expect`` — the base/local/remote SHAs the user's plan was rendered against,
    all three REQUIRED. So the caller sends decisions, never source; a teammate's
    push (or the analyst's own edit) landing mid-decision is a loud
    :class:`MergeStale`; and a caller that omits a sha gets a ``ValueError`` rather
    than a merge against a file nobody looked at.

    Raises :class:`MergeUnavailable` / :class:`MergeStale`, or ``ValueError`` when a
    conflicted cell has no (valid) choice or ``expect`` is incomplete.
    """
    sides = _read_sides(client, cfg, rel_path)
    current = _build_plan(sides)
    _require_fresh(current, expect)

    picks: list[_CellRef] = []
    chosen_local = chosen_remote = 0
    for cell in current.cells:
        if cell.status == "auto":
            ref = cell.take
        else:
            pick = choices.get(cell.id, "")
            if pick not in ("local", "remote"):
                raise ValueError(
                    "Every cell both of you changed needs a choice before the merge "
                    "can be written."
                )
            ref = cell.local_ref if pick == "local" else cell.remote_ref
            if pick == "local":
                chosen_local += 1
            else:
                chosen_remote += 1
        if ref is not None:
            picks.append(ref)
    if not picks:
        raise MergeUnavailable("Those choices would empty the notebook — nothing to write.")
    _require_no_duplicate_definitions(picks)

    text_of = {"local": sides.local_text, "remote": sides.remote_text}
    try:
        merged = marimo_rt.compose_notebook(
            text_of[current.frame_side], [(text_of[r.side], r.index) for r in picks]
        )
    except (ValueError, marimo_rt.MarimoTooOld, marimo_rt.MarimoTransportError) as exc:
        raise MergeUnavailable(f"The merged notebook would not be valid: {exc}") from exc

    workspace = cfg.workspace()
    target = workspace / sides.rel
    # LF only: a notebook's push bytes are LF-normalized (gitsha), so writing CRLF
    # here would make the merged file differ from what a push uploads.
    data = merged.encode("utf-8")
    trashed = _bank(workspace, sides.rel, target, data, cfg.trash_max_file_mb)
    safe_write_bytes(target, data)

    # The merged file CONTAINS the team's version, so the conflict is over: advance
    # the base to the remote sha and the three-way engine reclassifies the file as a
    # plain MODIFIED push candidate (exactly what KEEP_BOTH does). Loaded fresh
    # rather than reused from the plan so a concurrent sync's manifest edits survive.
    mft = manifest_mod.load(workspace)
    mft.files[sides.rel] = sides.remote_sha
    mft.branch = cfg.branch
    manifest_mod.save(workspace, mft)

    lines = [
        f"merged   {sides.rel} ({current.auto_merged} cell(s) merged automatically, "
        f"{current.unchanged} unchanged)"
    ]
    if chosen_local or chosen_remote:
        lines.append(
            f"chose    {chosen_local} of your cell(s) and {chosen_remote} of the team's"
        )
    if current.frame_side == "remote":
        lines.append(f"kept     the team's notebook header for {sides.rel}")
    lines.append(f"local    {sides.rel} is now yours to push (nothing was published)")
    return MergeOutcome(
        path=sides.rel,
        auto_merged=current.auto_merged,
        chosen_local=chosen_local,
        chosen_remote=chosen_remote,
        lines=tuple(lines),
        trashed=trashed,
    )


def plan_payload(merge_plan: MergePlan) -> dict:
    """The plan as JSON for an adapter.

    Deliberately carries no cell SOURCE — only labels, counts, and the unified diff
    of a contested cell. The browser's job is to pick a side, and :func:`apply`
    re-derives the code itself, so shipping source would be payload no one needs.
    """
    return {
        "path": merge_plan.path,
        "base_sha": merge_plan.base_sha,
        "local_sha": merge_plan.local_sha,
        "remote_sha": merge_plan.remote_sha,
        "frame_from": merge_plan.frame_side,
        "auto_local": merge_plan.auto_local,
        "auto_remote": merge_plan.auto_remote,
        "auto_both": merge_plan.auto_both,
        "auto_merged": merge_plan.auto_merged,
        "unchanged": merge_plan.unchanged,
        "cells": [
            {
                "id": c.id,
                "origin": c.origin,
                "status": c.status,
                "side": c.side,
                "index_base": c.index_base,
                "dropped": c.status == "auto" and c.take is None,
                "has_local": c.status != "choice" or c.local_ref is not None,
                "has_remote": c.status != "choice" or c.remote_ref is not None,
                "diff": c.diff,
            }
            for c in merge_plan.cells
        ],
    }


# -- reading the three sides ----------------------------------------------------


def _read_sides(client, cfg: Config, rel_path: str) -> _Sides:
    rel = str(rel_path).replace("\\", "/").strip("/")
    if not rel.endswith(".py"):
        raise MergeUnavailable("Only marimo notebooks can be merged cell by cell.")
    workspace = cfg.workspace()
    target = workspace / rel
    if not target.is_file():
        raise MergeUnavailable(
            "There is no local copy to merge — resolve this one with Use remote."
        )
    base_sha = manifest_mod.load(workspace).files.get(rel) or ""
    if not base_sha:
        # classify() calls this a conflict because both sides CREATED the file
        # independently. With no common ancestor every cell would be a choice,
        # which is a rename decision, not a merge.
        raise MergeUnavailable(
            "You and the team created this file separately, so there is no shared "
            "version to merge against."
        )
    head = client.get_branch_head(cfg.branch)
    try:
        remote_sha, remote_bytes = client.get_file_at(rel, head)
    except NotFound:
        raise MergeUnavailable(
            "The team deleted this file, so there are no cells to merge with."
        ) from None
    try:
        base_bytes = client.get_blob(base_sha)
    except NotFound:
        # The base blob was garbage-collected (a force-push, a squashed history) —
        # the same degradation /api/diff takes, but here it is fatal: a two-way
        # merge would ask about every cell.
        raise MergeUnavailable(
            "GitHub no longer has your last-synced version, so the three-way merge "
            "has no starting point."
        ) from None
    local_bytes = gitsha.read_for_push(target, rel)
    return _Sides(
        rel=rel,
        base_sha=base_sha,
        local_sha=gitsha.blob_sha(local_bytes),
        remote_sha=remote_sha,
        base_text=_text(base_bytes, "your last-synced version"),
        local_text=_text(local_bytes, "your copy"),
        remote_text=_text(remote_bytes, "the team's version"),
    )


def _text(data: bytes, label: str) -> str:
    try:
        return data.replace(b"\r\n", b"\n").decode("utf-8")
    except UnicodeDecodeError:
        raise MergeUnavailable(f"{label} is not readable as UTF-8 text.") from None


def _cells(text: str, label: str) -> list[str]:
    """The cell sources of one side, or :class:`MergeUnavailable`.

    Uses the LOUD reader: marimo's converter silently swallows what it cannot parse
    into the file header and returns zero cells, which here would read as "they
    deleted everything" and merge away a whole notebook."""
    if not notebook_template.is_marimo_app(text):
        raise MergeUnavailable(f"{label} is not a marimo notebook.")
    try:
        return [code for _, code in marimo_rt.read_cells_checked(text)]
    except (ValueError, marimo_rt.MarimoTooOld, marimo_rt.MarimoTransportError):
        raise MergeUnavailable(f"marimo could not read the cells of {label}.") from None


# -- what a cell DEFINES: the identity marimo itself enforces --------------------


def _defined_names(code: str) -> frozenset[str]:
    """The names a cell binds at its top level — marimo's own notion of what a cell
    *is*, since marimo refuses to run a notebook where two cells define the same name
    (``MultipleDefinitionError``).

    Two uses, both about refusing to guess: it settles whether an unpaired base cell
    and an unpaired side cell are one cell rewritten (they define a name in common)
    or genuinely a delete plus an add, and it is how the merged cell list is checked
    for a collision before anything is written.

    Only TOP-LEVEL bindings count (a name bound inside a ``def``/``class`` body
    belongs to that scope, not the cell), and names starting with ``_`` are skipped —
    marimo scopes those to their own cell, so two cells may both define ``_tmp``.
    Unparseable code claims nothing: erring broad only ever costs a refusal, while a
    missed name would let a broken notebook through.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return frozenset()
    names: set[str] = set()
    for node in tree.body:
        _collect_bindings(node, names)
    return frozenset(name for name in names if not name.startswith("_"))


def _collect_bindings(node: ast.AST, names: set[str]) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
        return  # its body binds in its OWN scope, never the cell's
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[0])
        return
    if isinstance(node, ast.Assign):
        for target in node.targets:
            _collect_targets(target, names)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        _collect_targets(node.target, names)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        _collect_targets(node.target, names)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                _collect_targets(item.optional_vars, names)
    elif isinstance(node, ast.NamedExpr):  # a walrus binds in the enclosing scope
        _collect_targets(node.target, names)
    for child in ast.iter_child_nodes(node):
        _collect_bindings(child, names)


def _collect_targets(node: ast.AST, names: set[str]) -> None:
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            _collect_targets(element, names)
    elif isinstance(node, ast.Starred):
        _collect_targets(node.value, names)
    # An Attribute/Subscript target (obj.x = …) mutates; it binds no new name.


def _require_no_duplicate_definitions(picks: list[_CellRef]) -> None:
    """Refuse a merge that would produce a notebook marimo will not run.

    A merge can compose a ``MultipleDefinitionError`` out of two individually valid
    halves — you rename a cell's variable while a teammate adds a cell using that
    name — and nothing downstream catches it: the file writes, then pushes, then
    breaks for everyone who pulls it. So the composed cell list is checked HERE,
    before the write, and a collision refuses instead of publishing."""
    seen: dict[str, int] = {}
    for position, ref in enumerate(picks):
        for name in _defined_names(ref.code):
            if seen.setdefault(name, position) != position:
                raise MergeUnavailable(
                    f"Merging these cells would define {name!r} in two cells, which "
                    "marimo refuses to run. Resolve this conflict whole-file, or "
                    "rename one of them first and try again."
                )


# -- the merge itself -----------------------------------------------------------


def _build_plan(sides: _Sides) -> MergePlan:
    base_codes = _cells(sides.base_text, "your last-synced version")
    local_codes = _cells(sides.local_text, "your copy")
    remote_codes = _cells(sides.remote_text, "the team's version")
    if not base_codes:
        raise MergeUnavailable("Your last-synced version has no cells to merge against.")

    frame_side = _frame_side(sides)
    pair_local = _match_side(base_codes, local_codes, "Your copy")
    pair_remote = _match_side(base_codes, remote_codes, "The team's version")

    base_slots = _base_slots(base_codes, local_codes, remote_codes, pair_local, pair_remote)
    buckets = _addition_slots(local_codes, remote_codes, pair_local, pair_remote)

    cells: list[MergeCell] = list(buckets.get(-1, ()))
    for i in range(len(base_codes)):
        cells.append(base_slots[i])
        cells.extend(buckets.get(i, ()))

    taken = [c.side for c in cells if c.status == "auto"]
    return MergePlan(
        path=sides.rel,
        base_sha=sides.base_sha,
        local_sha=sides.local_sha,
        remote_sha=sides.remote_sha,
        cells=tuple(cells),
        frame_side=frame_side,
        auto_local=taken.count("local"),
        auto_remote=taken.count("remote"),
        auto_both=taken.count("both"),
        unchanged=taken.count("unchanged"),
    )


def _frame_side(sides: _Sides) -> str:
    """Whose notebook FRAME the merge keeps — the PEP 723 ``# /// script`` header and
    the ``marimo.App(...)`` options (see ``marimo_rt.read_notebook_frame``).

    Three-wayed exactly like a cell, because a notebook rebuilt from cells alone
    silently drops whichever side's frame it did not start from: a teammate's
    dependency pin would be deleted by an operation labelled "merge", and the next
    push would publish that revert. There is no per-cell way to present a frame both
    sides changed, so that case refuses rather than picking for them."""
    try:
        base, local, remote = (
            marimo_rt.read_notebook_frame(text)
            for text in (sides.base_text, sides.local_text, sides.remote_text)
        )
    except (ValueError, marimo_rt.MarimoTooOld, marimo_rt.MarimoTransportError):
        raise MergeUnavailable("marimo could not read this notebook's header.") from None
    if local == remote or remote == base:
        return "local"
    if local == base:
        return "remote"
    raise MergeUnavailable(
        "You and the team both changed this notebook's header — its script "
        "dependencies or its marimo.App settings — which is not something a "
        "cell-by-cell merge can split. Resolve this conflict whole-file instead."
    )


def _match_side(base_codes: list[str], side_codes: list[str], label: str) -> dict[int, int]:
    """Pair one side's cells onto the base (side index -> base index), refusing
    rather than guessing when the pairing is not trustworthy.

    Three gates, in order. The side must still hold MORE than half the base's cells
    (below that it was restructured, not edited). Anything left unpaired on both
    sides at once gets a second pass on marimo's dataflow identity — a base cell and
    a side cell that define a name in common ARE one cell, rewritten past the
    similarity threshold. Whatever is STILL unpaired on both sides refuses. That last
    gate is the important one: without it a heavy rewrite reads as a deletion plus an
    addition, so the panel tells an analyst they deleted a cell they did not, and
    then offers a "choice" whose other answer duplicates it."""
    pair, used = _match_cells(base_codes, side_codes)
    if len(used) <= MIN_BASE_MATCH_RATIO * len(base_codes):
        raise MergeUnavailable(
            f"{label} has been restructured too heavily to line its cells up against "
            "the last-synced version — resolve this conflict whole-file instead."
        )
    leftover_base = [i for i in range(len(base_codes)) if i not in used]
    leftover_side = [j for j in range(len(side_codes)) if j not in pair]
    if not (leftover_base and leftover_side):
        return pair  # unpaired on at most one side: plainly additions, or deletions
    base_names = {i: _defined_names(base_codes[i]) for i in leftover_base}
    for j in list(leftover_side):
        names = _defined_names(side_codes[j])
        rewritten = next((i for i in leftover_base if names & base_names[i]), None)
        if rewritten is None:
            continue
        pair[j] = rewritten
        used.add(rewritten)
        leftover_base.remove(rewritten)
        leftover_side.remove(j)
    if leftover_base and leftover_side:
        raise MergeUnavailable(
            f"{label} has cells mooring cannot line up against the last-synced "
            "version — it cannot tell a rewritten cell from a deleted one plus a new "
            "one, and it will not guess. Resolve this conflict whole-file instead."
        )
    return pair


def _base_slots(
    base_codes: list[str],
    local_codes: list[str],
    remote_codes: list[str],
    pair_local: dict[int, int],
    pair_remote: dict[int, int],
) -> dict[int, MergeCell]:
    """One slot per last-synced cell: the three-way decision matrix, per cell.

    A side with no counterpart for a base cell DELETED it (``None``) — trustworthy
    only because :func:`_match_side` has already refused every case where "deleted"
    might really mean "rewritten". So a one-sided delete is taken like any other
    one-sided change, and delete-versus-edit is a choice between the team's cell and
    dropping it."""
    to_local = {i: j for j, i in pair_local.items()}
    to_remote = {i: j for j, i in pair_remote.items()}
    slots: dict[int, MergeCell] = {}
    for i, base in enumerate(base_codes):
        lj, rk = to_local.get(i), to_remote.get(i)
        local_ref = _CellRef("local", lj, local_codes[lj]) if lj is not None else None
        remote_ref = _CellRef("remote", rk, remote_codes[rk]) if rk is not None else None
        local = local_ref.code if local_ref is not None else None
        remote = remote_ref.code if remote_ref is not None else None
        cell_id = f"b{i}"
        if local == base and remote == base:
            slots[i] = MergeCell(cell_id, "base", "auto", "unchanged", i, take=local_ref)
        elif remote == base:
            slots[i] = MergeCell(cell_id, "base", "auto", "local", i, take=local_ref)
        elif local == base:
            slots[i] = MergeCell(cell_id, "base", "auto", "remote", i, take=remote_ref)
        elif local == remote:
            # Both of you made the SAME edit (or both deleted it) — agreement is not
            # a conflict, whatever the SHAs said about the file as a whole.
            slots[i] = MergeCell(cell_id, "base", "auto", "both", i, take=local_ref)
        else:
            slots[i] = MergeCell(
                cell_id,
                "base",
                "choice",
                index_base=i,
                local_ref=local_ref,
                remote_ref=remote_ref,
                diff=_choice_diff(local, remote),
            )
    return slots


def _addition_slots(
    local_codes: list[str],
    remote_codes: list[str],
    pair_local: dict[int, int],
    pair_remote: dict[int, int],
) -> dict[int, list[MergeCell]]:
    """Cells neither side inherited from the base — every one of them KEPT.

    Placement is anchored: an addition lands after the last base-derived cell that
    preceded it in its OWN document (``-1`` = before every base cell), which keeps a
    new cell beside the code it was written next to.

    Two additions collapse into one slot ONLY when their source is byte-identical.
    They are deliberately NOT paired by similarity: similarity means "this cell was
    edited" and says nothing about whether two people's brand-new cells are the same
    cell — ``import polars as pl`` and ``import altair as alt`` pair at 0.77, and
    collapsing them offers a "choice" whose either answer deletes a teammate's cell
    outright. Keeping both is what a merge tool is expected to do;
    :func:`_require_no_duplicate_definitions` is what refuses if they truly collide.
    """
    local_adds = [j for j in range(len(local_codes)) if j not in pair_local]
    remote_adds = [k for k in range(len(remote_codes)) if k not in pair_remote]
    local_anchor = _anchors(len(local_codes), pair_local)
    remote_anchor = _anchors(len(remote_codes), pair_remote)

    by_code: dict[str, deque[int]] = defaultdict(deque)
    for k in remote_adds:
        by_code[remote_codes[k]].append(k)
    identical: dict[int, int] = {}  # local add index -> the remote add it duplicates
    for j in local_adds:
        bucket = by_code.get(local_codes[j])
        if bucket:
            identical[j] = bucket.popleft()

    buckets: dict[int, list[MergeCell]] = {}
    for j in local_adds:
        origin, side = ("both", "both") if j in identical else ("local", "local")
        buckets.setdefault(local_anchor[j], []).append(
            MergeCell(f"l{j}", origin, "auto", side, take=_CellRef("local", j, local_codes[j]))
        )
    duplicated = set(identical.values())
    for k in remote_adds:
        if k in duplicated:
            continue  # emitted once already, beside its byte-identical local twin
        buckets.setdefault(remote_anchor[k], []).append(
            MergeCell(
                f"r{k}", "remote", "auto", "remote", take=_CellRef("remote", k, remote_codes[k])
            )
        )
    return buckets


def _anchors(count: int, pair: dict[int, int]) -> list[int]:
    """For each cell of one side, the base index of the nearest base-derived cell at
    or before it (``-1`` when none) — an addition's place in the merged order."""
    out: list[int] = []
    last = -1
    for j in range(count):
        if j in pair:
            last = pair[j]
        out.append(last)
    return out


def _choice_diff(local: str | None, remote: str | None) -> str:
    """The contested cell's two versions, in the same unified shape the review panel
    renders — a deleted side is an empty document, so the diff reads as a removal."""
    return _unified(local or "", remote or "", "your copy", "the team's version")


def _require_fresh(current: MergePlan, expect: dict[str, str]) -> None:
    """Refuse unless the caller's plan was rendered against exactly these three
    versions.

    Fail-CLOSED on a missing sha: a blank is a malformed request, not a waiver. Cell
    ids are positional (``b3`` is "the fourth base cell"), so a plan computed against
    different bytes silently re-binds every choice the user made — which is how a
    merge writes code nobody ever saw."""
    actual = {
        "base_sha": current.base_sha,
        "local_sha": current.local_sha,
        "remote_sha": current.remote_sha,
    }
    for key in ("remote_sha", "local_sha", "base_sha"):
        claimed = str((expect or {}).get(key) or "")
        if not claimed:
            raise ValueError(
                f"This merge request is missing {key}, so mooring cannot tell whether "
                "you are still looking at the current versions — reopen the merge."
            )
        if claimed != actual[key]:
            raise MergeStale(_STALE_REASONS[key])


def _bank(
    workspace: Path, rel: str, target: Path, data: bytes, cap_mb: int
) -> tuple[tuple[str, str], ...]:
    """Deposit the pre-merge bytes in the local trash — and REFUSE the merge if that
    fails.

    Unlike the sync engine's ``_bank_pre_image``, which can afford to be best-effort
    because its pre-images are also on GitHub, the bytes a merge is about to
    overwrite are the analyst's UNPUSHED work: this deposit is the entire recovery
    story, and the hub's Undo toast is built from its token. No safety net, no
    write."""
    try:
        token = trash.deposit(
            workspace,
            rel,
            target.read_bytes(),
            TRASH_ACTION,
            after_sha=gitsha.blob_sha(gitsha.normalize(data)),
            max_file_mb=cap_mb,
        )
    except OSError as exc:
        # Type only, never str(exc): this message reaches the user and the local
        # ledger, and mooring's error surfaces do not echo filesystem paths back.
        raise MergeUnavailable(
            "Could not save a recoverable copy of your notebook "
            f"({type(exc).__name__}), so the merge was not written. Check the "
            ".mooring folder in your workspace and try again."
        ) from exc
    if not token:
        raise MergeUnavailable(
            "This notebook is too large to save a recoverable copy of, so the merge "
            "was not written."
        )
    return ((rel, token),)
