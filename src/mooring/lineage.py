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
  asymmetry is the whole honesty story — see :func:`coverage_note`, which every caller
  showing lineage is expected to show alongside it.
* **Value-free.** Paths, notebook paths, and counts. The underlying receipts hold a hash,
  two counts, and column names/types — never a data value — and they live in the
  sync-excluded ``.mooring`` dir: never pushed, never handed to the AI copilot.

Lean-core leaf: imports only :mod:`mooring.inputs` and the standard library, so it carries
no path to marimo / the Copilot SDK / spaCy (locked by ``frozen-core-is-lean``).
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from mooring import inputs


@dataclass(frozen=True)
class Graph:
    """Who reads and who writes each dataset. Every member is value-free.

    Dataset entries are keyed by :func:`_key`, not by the path as written, so two
    spellings of one file cannot become two nodes; ``display`` maps each key back to the
    path as first recorded, which is what a user is shown. ``notebooks`` is every notebook
    that recorded anything — the denominator of :func:`coverage_note`, and the reason an
    empty graph can be described honestly rather than as "no dependencies".
    """

    readers: dict[str, tuple[str, ...]] = field(default_factory=dict)  # dataset -> notebooks
    writers: dict[str, tuple[str, ...]] = field(default_factory=dict)  # dataset -> notebooks
    reads: dict[str, tuple[str, ...]] = field(default_factory=dict)  # notebook -> datasets
    writes: dict[str, tuple[str, ...]] = field(default_factory=dict)  # notebook -> datasets
    display: dict[str, str] = field(default_factory=dict)  # dataset key -> path as recorded
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
    """The dataset path an ``inputs`` receipt entry refers to, or ``""`` if it names none.

    Prefer the entry's ``rel``: the runtime resolved it against the kernel's own working
    directory, which is the only place that could be done correctly — ``"data/sales.csv"``
    means different files from different notebooks. Fall back to the raw ``path`` for
    receipts written before ``rel`` existed; that is a best-effort join, but a stale
    receipt yielding a slightly wrong edge is bounded, and one run re-records it."""
    rel = entry.get("rel")
    if isinstance(rel, str) and rel:
        return _norm(rel)
    raw = entry.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    return _norm(raw)


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
    notebooks: list[str] = []
    for receipt in receipts:
        notebook = receipt.get("notebook")
        if not isinstance(notebook, str) or not notebook:
            continue  # read_receipts guarantees this; belt and braces for a hand-built one
        if notebook not in notebooks:
            notebooks.append(notebook)
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
    from). The two ``seen`` sets are the cycle guard: a workspace where A writes x, B reads
    x and writes y, and A reads y is a perfectly ordinary mistake, and it must terminate,
    not hang."""
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


def counts(graph: Graph, paths) -> dict[str, dict]:
    """``{path: {"readers": n, "writers": n}}`` for the given paths — omitting any with
    neither, so a caller can only ever render a positive claim.

    Keyed by the caller's own spelling of each path (matching is done on the normalised
    key internally), so a hub row can look itself up by the path it already displays."""
    out: dict[str, dict] = {}
    for path in paths:
        key = _key(_norm(path))
        read_by = len(graph.readers.get(key, ()))
        written_by = len(graph.writers.get(key, ()))
        if read_by or written_by:
            out[path] = {"readers": read_by, "writers": written_by}
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
        "that nothing reads a file."
    )
