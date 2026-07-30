"""The value-free lineage graph: who reads a file, who writes it, what breaks downstream.

Nothing new is collected here. :mod:`mooring.inputs` already has notebooks record what
they read (``mi.fingerprint``) and write (``mi.output``); a dataset path that appears as
one notebook's output and another's input IS an edge, so the graph falls out of receipts
that exist for a different reason. It answers the question this audience actually has
and no notebook platform answers for them: *if I change this file, what breaks?*

Three properties are deliberate, and each is a constraint on what may be added here:

* **Explicit, never inferred.** A dependency is recorded because an analyst wrote a call
  saying so — not by parsing source for ``read_csv`` and not by watching the filesystem.
  Inference would make the graph bigger and make every edge a guess; a claim that "3
  notebooks read this" is only worth showing if it is a fact.
* **A FLOOR, never a census.** The graph knows only notebooks that call ``mooring_inputs``.
  So every surface may make POSITIVE claims ("3 notebooks read this") and none may make a
  negative one: "no recorded readers" must never be rendered as "safe to change". That
  asymmetry is the whole honesty story. It is enforced STRUCTURALLY, not by wording:
  :func:`counts` omits a zero rather than reporting it, so there is no zero for a caller
  to render as an all-clear, and :func:`coverage_note` — which every caller showing
  lineage is expected to show alongside it — says what the graph cannot see.
* **Dated, because a receipt outlives the code that wrote it.** An entry is only removed
  by ``mi.reset()``, so a notebook that stopped reading a file keeps asserting it did
  until it next runs a reset. The graph therefore carries WHEN each notebook last
  confirmed its edges (:attr:`Graph.confirmed`) and every surface shows it, because a
  claim that gates a bulk action must not be able to be six months old invisibly.
* **Value-free.** Paths, notebook paths, counts, and timestamps. The underlying receipts
  hold a hash, two counts, and column names/types — never a data value — and they live in
  the sync-excluded ``.mooring`` dir: never pushed, never handed to the AI copilot.

Lean-core leaf: imports only :mod:`mooring.inputs` and the standard library, so it carries
no path to marimo / the Copilot SDK / spaCy (locked by ``frozen-core-is-lean``).
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mooring import inputs

# How long a recorded edge stands before surfaces start marking it "as of <date>" rather
# than stating it flat. Not a config knob and not an expiry: an aged edge is still shown
# (dropping it would be exactly the silent false all-clear this module exists to avoid),
# it is just no longer presented as current. A month is roughly "older than the reporting
# cycle that would have re-run the notebook".
STALE_AFTER_DAYS = 30


@dataclass(frozen=True)
class Graph:
    """Who reads and who writes each dataset. Every member is value-free.

    Dataset entries are keyed by :func:`_key`, not by the path as written, so two
    spellings of one file cannot become two nodes; ``display`` maps each key back to the
    path as first recorded, which is what a user is shown. ``notebooks`` is every notebook
    that recorded anything — the denominator of :func:`coverage_note`, and the reason an
    empty graph can be described honestly rather than as "no dependencies".

    ``confirmed`` dates each notebook's edges (its receipt's ``updated``). Every edge a
    notebook contributes is exactly as old as its last run, so this is the age of the
    CLAIM, and surfaces show it: see :func:`as_of`.
    """

    readers: dict[str, tuple[str, ...]] = field(default_factory=dict)  # dataset -> notebooks
    writers: dict[str, tuple[str, ...]] = field(default_factory=dict)  # dataset -> notebooks
    reads: dict[str, tuple[str, ...]] = field(default_factory=dict)  # notebook -> datasets
    writes: dict[str, tuple[str, ...]] = field(default_factory=dict)  # notebook -> datasets
    display: dict[str, str] = field(default_factory=dict)  # dataset key -> path as recorded
    confirmed: dict[str, str] = field(default_factory=dict)  # notebook -> receipt timestamp
    notebooks: tuple[str, ...] = ()


@dataclass(frozen=True)
class Impact:
    """A closure over the graph: the notebooks and datasets reachable from one file.

    Both tuples are sorted and exclude the file asked about, so ``if not impact.notebooks``
    reads as "nothing recorded downstream of this" — which is not the same as "nothing
    depends on it" (see :func:`coverage_note`)."""

    notebooks: tuple[str, ...] = ()
    datasets: tuple[str, ...] = ()


def _norm(path: str) -> str:
    """A recorded path in canonical POSIX form (``.``/``..`` segments collapsed)."""
    return posixpath.normpath(str(path).replace("\\", "/"))


def _key(path: str) -> str:
    """The node identity of a path.

    Case-folded on Windows ONLY, where ``Data/x.csv`` and ``data/x.csv`` genuinely are the
    same file and treating them as two nodes would silently lose an edge. On POSIX they
    are two different files, and folding would FABRICATE an edge — the one failure mode
    worse than missing one."""
    return path.casefold() if os.name == "nt" else path


def _node(entry: dict) -> str:
    """The dataset a receipt entry names, as a workspace-relative key — or ``""`` to drop it.

    ONLY the entry's ``rel``, which the runtime resolved against the kernel's real working
    directory (see :func:`mooring._inputs_runtime._workspace_rel`). There is deliberately
    no fallback to the raw ``path`` as written: that string is unresolved, and joining it
    into the workspace-relative namespace would key ``"sales.csv"`` to a repo-root file
    that may not be the one the notebook meant — a FABRICATED edge, which :func:`_key`
    already calls the one failure mode worse than a missing one.

    Two things are dropped by this rule, both deliberately and both bounded:

    * entries from receipts written before ``rel`` existed — re-recorded, with a ``rel``,
      the next time that notebook runs, so the gap is one run and not permanent;
    * files that resolve OUTSIDE the workspace (``rel`` is ``""`` for those), which have no
      hub row to badge anyway. Two notebooks sharing a network-drive extract are therefore
      not linked — a known floor, consistent with everything else here under-reporting.
    """
    rel = entry.get("rel")
    return _norm(rel) if isinstance(rel, str) and rel else ""


def _append(mapping: dict[str, list[str]], key: str, value: str) -> None:
    bucket = mapping.setdefault(key, [])
    if value not in bucket:
        bucket.append(value)


def _freeze(mapping: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    """Immutable and SORTED — the order receipts happen to be read in is an accident of
    the slug filenames, and a list a user is shown must not reshuffle between runs."""
    return {key: tuple(sorted(value)) for key, value in mapping.items()}


def from_receipts(receipts: list[dict]) -> Graph:
    """Build the graph from receipts already read (see :func:`mooring.inputs.read_receipts`).

    Split from :func:`build` so a caller that also wants the per-notebook badge counts —
    the hub renders both on every ``/api/state`` poll — walks the receipt directory once
    instead of twice."""
    readers: dict[str, list[str]] = {}
    writers: dict[str, list[str]] = {}
    reads: dict[str, list[str]] = {}
    writes: dict[str, list[str]] = {}
    display: dict[str, str] = {}
    confirmed: dict[str, str] = {}
    notebooks: list[str] = []
    for receipt in receipts:
        notebook = receipt.get("notebook")
        if not isinstance(notebook, str) or not notebook:
            continue  # read_receipts guarantees this; belt and braces for a hand-built one
        if notebook == inputs.UNKNOWN_NOTEBOOK:
            # A receipt whose notebook could not be identified. Every failed detection
            # shares this ONE bucket and overwrites the last, so it is neither a notebook
            # to name as a reader nor a countable unit of coverage — it is an unknown.
            continue
        if notebook not in notebooks:
            notebooks.append(notebook)
        confirmed[notebook] = receipt.get("updated", "")
        for section, by_dataset, by_notebook in (
            (inputs.INPUTS_KEY, readers, reads),
            (inputs.OUTPUTS_KEY, writers, writes),
        ):
            for entry in receipt.get(section, ()):
                node = _node(entry)
                if not node:
                    continue  # a name-only fingerprint pins content but joins nothing
                key = _key(node)
                display.setdefault(key, node)
                _append(by_dataset, key, notebook)
                _append(by_notebook, notebook, key)
    return Graph(
        readers=_freeze(readers),
        writers=_freeze(writers),
        reads=_freeze(reads),
        writes=_freeze(writes),
        display=display,
        confirmed=confirmed,
        notebooks=tuple(sorted(notebooks)),
    )


def build(workspace: Path | str) -> Graph:
    """The lineage graph for one workspace. Best-effort throughout: a corrupt receipt, or
    one whose notebook has been deleted, is dropped by the reader rather than raising."""
    return from_receipts(inputs.read_receipts(workspace))


def readers(graph: Graph, path: str) -> tuple[str, ...]:
    """Notebooks recorded as READING ``path`` (spelled however the caller has it)."""
    return graph.readers.get(_key(_norm(path)), ())


def writers(graph: Graph, path: str) -> tuple[str, ...]:
    """Notebooks recorded as WRITING ``path``."""
    return graph.writers.get(_key(_norm(path)), ())


def _closure(graph: Graph, path: str, *, forward: bool) -> Impact:
    """Breadth-first walk from a dataset, alternating dataset -> notebook -> dataset.

    ``forward`` follows readers and then what they write (what a change to ``path`` could
    reach); reversed, it follows writers and then what they read (what ``path`` is built
    from).

    ``seen_datasets`` is the termination guard, and it alone: a dataset is expanded once,
    so a workspace where A writes x, B reads x and writes y, and A reads y — a perfectly
    ordinary mistake — finishes instead of hanging. ``seen_notebooks`` is not needed for
    that; it stops a notebook reached from several datasets being re-expanded, and doubles
    as the result accumulator."""
    start = _key(_norm(path))
    hop_out = graph.readers if forward else graph.writers
    hop_on = graph.writes if forward else graph.reads
    seen_datasets = {start}
    seen_notebooks: set[str] = set()
    reached_datasets: list[str] = []
    frontier = [start]
    while frontier:
        following: list[str] = []
        for dataset in frontier:
            for notebook in hop_out.get(dataset, ()):
                if notebook in seen_notebooks:
                    continue
                seen_notebooks.add(notebook)
                for onward in hop_on.get(notebook, ()):
                    if onward in seen_datasets:
                        continue
                    seen_datasets.add(onward)
                    reached_datasets.append(onward)
                    following.append(onward)
        frontier = following
    return Impact(
        notebooks=tuple(sorted(seen_notebooks)),
        datasets=tuple(sorted(graph.display.get(k, k) for k in reached_datasets)),
    )


def downstream(graph: Graph, path: str) -> Impact:
    """Everything recorded as depending on ``path``, transitively — the "what breaks if I
    change this?" answer. A floor, not a census (see :func:`coverage_note`)."""
    return _closure(graph, path, forward=True)


def upstream(graph: Graph, path: str) -> Impact:
    """Everything ``path`` is recorded as being built from, transitively."""
    return _closure(graph, path, forward=False)


def as_of(graph: Graph, notebooks) -> str:
    """When the WEAKEST of these notebooks' claims was last confirmed (the oldest receipt).

    The oldest, not the newest: a count is only as current as its stalest member, and
    quoting the freshest date would let one notebook that ran this morning vouch for four
    others that have not run since March. ``""`` when nothing is dated."""
    stamps = sorted(s for s in (graph.confirmed.get(nb, "") for nb in notebooks) if s)
    return stamps[0] if stamps else ""


def is_stale(stamp: str, now: datetime | None = None) -> bool:
    """Whether a confirmation timestamp is older than :data:`STALE_AFTER_DAYS`.

    An unparseable or missing stamp counts as stale: an undateable claim is exactly the
    one a user should be told to check, so this fails toward showing the caveat."""
    if not stamp:
        return True
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment < (now or datetime.now(timezone.utc)) - timedelta(days=STALE_AFTER_DAYS)


def counts(graph: Graph, paths, now: datetime | None = None) -> dict[str, dict]:
    """``{path: {...}}`` for the given paths — the hub row payload.

    POSITIVE CLAIMS ONLY, and that is structural rather than a matter of phrasing:
    ``readers`` and ``writers`` are present only when NON-ZERO, and a path with neither
    gets no entry at all. So there is no zero anywhere for a caller to render — a row
    cannot say "0 notebooks read this", because it is never told that. Lineage sees only
    the notebooks that opted in, and "nobody recorded reading this" is not a fact about
    the file; it is a fact about the records. (The derived-output row is exactly where
    this matters: a generated extract is the file most likely to be consumed by a
    dashboard or a colleague's spreadsheet that mooring will never hear about.)

    Also carries ``as_of`` (when the weakest contributing claim was confirmed) and
    ``stale`` (that date is older than :data:`STALE_AFTER_DAYS`), so a caller can date an
    assertion rather than state it flat. Keyed by the caller's own spelling of each path —
    matching happens on the normalised key internally — so a hub row looks itself up by
    the path it already displays."""
    out: dict[str, dict] = {}
    for path in paths:
        key = _key(_norm(path))
        read_by = graph.readers.get(key, ())
        written_by = graph.writers.get(key, ())
        if not read_by and not written_by:
            continue
        stamp = as_of(graph, (*read_by, *written_by))
        entry: dict = {}
        if read_by:
            entry["readers"] = len(read_by)
        if written_by:
            entry["writers"] = len(written_by)
        if stamp:
            entry["as_of"] = stamp
        entry["stale"] = is_stale(stamp, now)
        out[path] = entry
    return out


def coverage_note(graph: Graph) -> str:
    """The caveat every lineage surface must carry.

    Lineage is derived from notebooks that opted in, so silence means "not recorded", never
    "not there". This sentence is written once, here, so no surface can quietly drop it and
    let an absent warning read as an all-clear."""
    count = len(graph.notebooks)
    if not count:
        return (
            "No lineage recorded yet — it is derived only from notebooks that call "
            "mooring_inputs (mi.fingerprint for what they read, mi.output for what they "
            "write), and none here do. That means mooring knows of no dependencies, which "
            "is not the same as there being none."
        )
    return (
        f"Lineage is derived from the {count} notebook(s) here that record their inputs and "
        "outputs with mooring_inputs. Notebooks that do not — and anything outside mooring — "
        "are invisible to it, so treat this as a floor: no recorded reader is NOT evidence "
        "that nothing reads a file. Each entry is dated with when that notebook last "
        f"recorded it; anything older than {STALE_AFTER_DAYS} days is marked, because a "
        "receipt keeps asserting a dependency until the notebook next runs mi.reset()."
    )
