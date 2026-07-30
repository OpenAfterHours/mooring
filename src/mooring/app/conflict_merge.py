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
sides becomes a choice. Cell pairing reuses :mod:`mooring.celldiff`'s matcher —
the same heuristic the review panel and the reviewer inbox already trust —
because a marimo ``.py`` persists no per-cell identity (see :mod:`marimo_rt`).

Three properties make this safe to offer:

* **Nothing is lost.** The pre-merge bytes are deposited in the local trash
  before the write, so the hub's existing Undo affordance covers a merge
  exactly as it covers a pull's overwrite.
* **The client never supplies source.** :func:`apply` recomputes the plan from
  the three SHAs and refuses (:class:`MergeStale`) if any of them moved, so a
  request carries only *which side wins* per cell — never code to write.
* **It refuses rather than guesses.** A non-notebook, an unreadable side, a
  missing base, or a notebook restructured past confident matching raises
  :class:`MergeUnavailable` and the caller keeps the three whole-file
  resolutions, unchanged.

The merge writes the workspace file and nothing else — it never pushes.
Afterwards the file is a plain MODIFIED push candidate (the manifest base
advances to the remote sha, exactly as KEEP_BOTH does), so the analyst
publishes the merged result deliberately, like any other edit.

Orchestration lives here rather than in ``sync.py`` because the sync domain
core may not import ``celldiff``/``marimo_rt`` (see ``.importlinter``) — this
is precisely the "needed by both adapters, sits above the core" shape ``app/``
exists for.
"""

from __future__ import annotations

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
# reviewer inbox, and this merge means a cell that "is the same cell" in one view
# is the same cell in all of them — and one diff dialect means one renderer.
_match_cells = celldiff._match_cells
_unified = celldiff._unified

# A side that kept fewer than this fraction of the base's cells has been
# restructured wholesale, not edited: pairing what is left would be guesswork
# dressed as a per-cell choice. Below the floor we refuse and the three
# whole-file resolutions stay the honest answer.
MIN_BASE_MATCH_RATIO = 0.5

# The action label the trash deposit carries (see mooring.trash) — the Activity
# page shows it verbatim, so it names what destroyed the pre-image.
TRASH_ACTION = "merge-cells"


class MergeUnavailable(Exception):
    """This conflict cannot be resolved cell by cell; the caller falls back to the
    three whole-file resolutions. ``str(exc)`` is the user-facing reason."""


class MergeStale(Exception):
    """One of the three sides moved between planning and applying the merge, so the
    choices the user made no longer describe the file. ``str(exc)`` is the reason."""


@dataclass(frozen=True)
class MergeCell:
    """One cell's fate in the merged notebook.

    ``origin`` is where the cell comes from: ``"base"`` (a last-synced cell,
    matched on one or both sides), ``"local"`` / ``"remote"`` (added on that side
    only), or ``"both"`` (added on both sides). ``status`` is ``"auto"`` (decided)
    or ``"choice"`` (both sides changed it differently). For an auto cell ``side``
    names who won and ``code`` is the merged source — ``None`` meaning the cell is
    dropped. For a choice, ``local`` / ``remote`` carry the two candidates (``None``
    = that side deleted it) and ``diff`` is a unified diff between them.
    """

    id: str
    origin: str
    status: str
    side: str = ""
    index_base: int | None = None
    code: str | None = None
    local: str | None = None
    remote: str | None = None
    diff: str = ""


@dataclass(frozen=True)
class MergePlan:
    """What a merge would do, in merged-document order, plus the three SHAs it was
    computed from (the staleness key :func:`apply` re-checks)."""

    path: str
    base_sha: str
    local_sha: str
    remote_sha: str
    cells: tuple[MergeCell, ...]
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
    expect: dict[str, str] | None = None,
) -> MergeOutcome:
    """Write the merged notebook, taking ``choices`` (cell id -> ``"local"`` /
    ``"remote"``) for the cells both sides changed.

    The plan is RECOMPUTED here from the three sides and, when ``expect`` carries
    the SHAs the user's plan was rendered against, re-checked against them — so the
    caller sends decisions, never source, and a teammate's push (or the analyst's
    own edit) landing mid-decision is a loud :class:`MergeStale` rather than a
    merge of a file nobody looked at.

    Raises :class:`MergeUnavailable` / :class:`MergeStale`, or ``ValueError`` when a
    conflicted cell has no (valid) choice.
    """
    sides = _read_sides(client, cfg, rel_path)
    current = _build_plan(sides)
    if expect:
        _require_fresh(current, expect)

    codes: list[str] = []
    chosen_local = chosen_remote = 0
    for cell in current.cells:
        if cell.status == "auto":
            code = cell.code
        else:
            pick = choices.get(cell.id, "")
            if pick not in ("local", "remote"):
                raise ValueError(
                    "Every cell both of you changed needs a choice before the merge "
                    "can be written."
                )
            code = cell.local if pick == "local" else cell.remote
            if pick == "local":
                chosen_local += 1
            else:
                chosen_remote += 1
        if code is not None:
            codes.append(code)
    if not codes:
        raise MergeUnavailable("Those choices would empty the notebook — nothing to write.")

    try:
        merged = marimo_rt.apply_cell_patch(
            sides.local_text, [marimo_rt.CellOp(op="replace_all", cells=tuple(codes))]
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
                "dropped": c.status == "auto" and c.code is None,
                "has_local": c.status != "choice" or c.local is not None,
                "has_remote": c.status != "choice" or c.remote is not None,
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


# -- the merge itself -----------------------------------------------------------


def _build_plan(sides: _Sides) -> MergePlan:
    base_codes = _cells(sides.base_text, "your last-synced version")
    local_codes = _cells(sides.local_text, "your copy")
    remote_codes = _cells(sides.remote_text, "the team's version")
    if not base_codes:
        raise MergeUnavailable("Your last-synced version has no cells to merge against.")

    # local index -> base index, and the base indices each side claimed.
    pair_local, used_local = _match_cells(base_codes, local_codes)
    pair_remote, used_remote = _match_cells(base_codes, remote_codes)
    _require_confident(base_codes, used_local, "Your copy")
    _require_confident(base_codes, used_remote, "The team's version")

    base_slots = _base_slots(base_codes, local_codes, remote_codes, pair_local, pair_remote)
    buckets = _addition_slots(local_codes, remote_codes, pair_local, pair_remote)

    cells: list[MergeCell] = list(buckets.get(-1, ()))
    for i in range(len(base_codes)):
        cells.append(base_slots[i])
        cells.extend(buckets.get(i, ()))

    sides_taken = [c.side for c in cells if c.status == "auto"]
    return MergePlan(
        path=sides.rel,
        base_sha=sides.base_sha,
        local_sha=sides.local_sha,
        remote_sha=sides.remote_sha,
        cells=tuple(cells),
        auto_local=sides_taken.count("local"),
        auto_remote=sides_taken.count("remote"),
        auto_both=sides_taken.count("both"),
        unchanged=sides_taken.count("unchanged"),
    )


def _require_confident(base_codes: list[str], used: set[int], label: str) -> None:
    if len(used) < MIN_BASE_MATCH_RATIO * len(base_codes):
        raise MergeUnavailable(
            f"{label} has been restructured too heavily to line its cells up against "
            "the last-synced version — resolve this conflict whole-file instead."
        )


def _base_slots(
    base_codes: list[str],
    local_codes: list[str],
    remote_codes: list[str],
    pair_local: dict[int, int],
    pair_remote: dict[int, int],
) -> dict[int, MergeCell]:
    """One slot per last-synced cell: the three-way decision matrix, per cell.

    A side that has no counterpart for a base cell DELETED it (``None``), so a
    one-sided delete is taken like any other one-sided change, and delete-vs-edit
    becomes a choice between the team's cell and dropping it."""
    to_local = {i: j for j, i in pair_local.items()}
    to_remote = {i: j for j, i in pair_remote.items()}
    slots: dict[int, MergeCell] = {}
    for i, base in enumerate(base_codes):
        local = local_codes[to_local[i]] if i in to_local else None
        remote = remote_codes[to_remote[i]] if i in to_remote else None
        cell_id = f"b{i}"
        if local == base and remote == base:
            slots[i] = MergeCell(cell_id, "base", "auto", "unchanged", i, code=base)
        elif remote == base:
            slots[i] = MergeCell(cell_id, "base", "auto", "local", i, code=local)
        elif local == base:
            slots[i] = MergeCell(cell_id, "base", "auto", "remote", i, code=remote)
        elif local == remote:
            # Both of you made the SAME edit (or both deleted it) — agreement is
            # not a conflict, whatever the SHAs said about the file as a whole.
            slots[i] = MergeCell(cell_id, "base", "auto", "both", i, code=local)
        else:
            slots[i] = MergeCell(
                cell_id,
                "base",
                "choice",
                index_base=i,
                local=local,
                remote=remote,
                diff=_choice_diff(local, remote),
            )
    return slots


def _addition_slots(
    local_codes: list[str],
    remote_codes: list[str],
    pair_local: dict[int, int],
    pair_remote: dict[int, int],
) -> dict[int, list[MergeCell]]:
    """Cells neither side inherited from the base, bucketed by where they belong.

    Placement is anchored: an addition lands after the last base-derived cell that
    preceded it in its OWN document (``-1`` = before every base cell), which keeps a
    new cell beside the code it was written next to. Two additions that pair with
    each other are one cell — identical means agreement, different means a choice —
    so a merge never lands two cells defining the same name, which in marimo's
    dataflow is a hard error rather than a cosmetic duplicate.
    """
    local_adds = [j for j in range(len(local_codes)) if j not in pair_local]
    remote_adds = [k for k in range(len(remote_codes)) if k not in pair_remote]
    local_anchor = _anchors(len(local_codes), pair_local)
    remote_anchor = _anchors(len(remote_codes), pair_remote)

    # _match_cells(a, b) pairs b's entries onto a's, so this reads remote-add -> local-add.
    paired_remote, _ = _match_cells(
        [local_codes[j] for j in local_adds], [remote_codes[k] for k in remote_adds]
    )
    partner = {local_pos: remote_pos for remote_pos, local_pos in paired_remote.items()}

    buckets: dict[int, list[MergeCell]] = {}
    for local_pos, j in enumerate(local_adds):
        local = local_codes[j]
        remote_pos = partner.get(local_pos)
        if remote_pos is None:
            slot = MergeCell(f"l{j}", "local", "auto", "local", code=local)
        else:
            k = remote_adds[remote_pos]
            remote = remote_codes[k]
            cell_id = f"l{j}r{k}"
            slot = (
                MergeCell(cell_id, "both", "auto", "both", code=local)
                if local == remote
                else MergeCell(
                    cell_id, "both", "choice", local=local, remote=remote,
                    diff=_choice_diff(local, remote),
                )
            )
        buckets.setdefault(local_anchor[j], []).append(slot)
    for remote_pos, k in enumerate(remote_adds):
        if remote_pos in paired_remote:
            continue  # already emitted beside its local partner
        buckets.setdefault(remote_anchor[k], []).append(
            MergeCell(f"r{k}", "remote", "auto", "remote", code=remote_codes[k])
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
    if str(expect.get("remote_sha") or "") not in ("", current.remote_sha):
        raise MergeStale(
            "The team pushed a new version while you were choosing — reopen the merge "
            "to see their latest cells."
        )
    if str(expect.get("local_sha") or "") not in ("", current.local_sha):
        raise MergeStale(
            "Your copy changed while you were choosing — reopen the merge so the "
            "choices match the file on disk."
        )
    if str(expect.get("base_sha") or "") not in ("", current.base_sha):
        raise MergeStale("The last-synced version changed — reopen the merge.")


def _bank(
    workspace: Path, rel: str, target: Path, data: bytes, cap_mb: int
) -> tuple[tuple[str, str], ...]:
    """Deposit the pre-merge bytes in the local trash so the hub's Undo toast covers
    a merge like any other overwrite. Best-effort by design (the sync engine's
    ``_bank_pre_image`` posture): losing the safety net must not lose the merge."""
    try:
        token = trash.deposit(
            workspace,
            rel,
            target.read_bytes(),
            TRASH_ACTION,
            after_sha=gitsha.blob_sha(gitsha.normalize(data)),
            max_file_mb=cap_mb,
        )
    except OSError:
        return ()
    return ((rel, token),) if token else ()
