"""The value-minimised notebook model — the allowlist the AST extractor must produce.

The frozen dataclasses here ARE the privacy allowlist, exactly as
:mod:`mooring.ai.codelib.model` is for helper modules. Only these fields may ever
reach the model: a notebook's PATH, its TITLE, the collapsed text of its first
markdown cell, the dotted NAMES of what it imports, the ``name``/``path`` STRING
LITERALS it hands ``mooring_inputs.fingerprint``, the ``mooring_checks`` function it
calls plus that check's literal ``name=``, and the identifier-shaped table names its
``mo.sql`` queries select FROM. A cell BODY, an expression, an arbitrary literal, and
a cell OUTPUT have **no slot** — a mis-detection degrades to a missing field, never a
leak. Nothing here is ever read from a ``.mooring/`` receipt: the catalog describes
what the source SAYS a run reads, never what a run actually saw.

This is a STRUCTURAL guarantee for everything except the one free-text slot,
``summary`` — prose the analyst wrote in a markdown cell, best-effort minimised
(scanned at extraction; see :mod:`mooring.ai.notebookindex.prosescan`) and
human-reviewed, the same weaker tier as a code-library docstring. Value-blindness
here does NOT lean on the egress scrubber, which is only a checksum-PII floor.
"""

from __future__ import annotations

from dataclasses import dataclass

SUMMARY_CAP = 400  # max chars kept from a first markdown cell (the one free-text slot)


@dataclass(frozen=True)
class Dataset:
    """An input the notebook FINGERPRINTS, as written in its source.

    Both fields are string literals lifted from a known argument position of a
    ``mooring_inputs.fingerprint`` call — an authored label and a workspace-relative
    file path, never a value read out of the data. A non-literal argument (an
    f-string, a variable) has no slot and is dropped.
    """

    name: str = ""
    path: str = ""


@dataclass(frozen=True)
class Check:
    """A tie-out the notebook ASSERTS, as written in its source: which
    ``mooring_checks`` function was called and the literal ``name=`` it was given.
    Never the check's outcome — that lives in a receipt the catalog never opens."""

    kind: str
    name: str = ""


@dataclass(frozen=True)
class Notebook:
    path: str  # workspace-relative POSIX path
    title: str = ""  # the first markdown H1 (mooring.notebook_template.notebook_title)
    summary: str = ""  # first markdown cell, collapsed + capped + scanned (the free-text slot)
    imports: tuple[str, ...] = ()  # dotted module/name strings, as imported
    helpers: tuple[str, ...] = ()  # the subset of `imports` that resolve to a .py in the workspace
    datasets: tuple[Dataset, ...] = ()
    checks: tuple[Check, ...] = ()
    sql_tables: tuple[str, ...] = ()  # identifier-shaped names after FROM/JOIN in an mo.sql literal
    n_cells: int = 0

    def terms(self) -> tuple[str, ...]:
        """The notebook's value-free search terms, deduped in a stable order.

        The ONE place a notebook is flattened for matching, shared by
        :meth:`Catalog.search` (the copilot's tool) and the hub's client-side filter
        (the hub row carries this list). Deriving both from the same allowlist is what
        keeps the local search box and the model's view of a notebook identical.
        """
        out: list[str] = [self.path, self.title, self.summary]
        out += list(self.imports)
        for ds in self.datasets:
            out += [ds.name, ds.path]
        for check in self.checks:
            out += [check.kind, check.name]
        out += list(self.sql_tables)
        return tuple(dict.fromkeys(t for t in out if t))


@dataclass(frozen=True)
class ExtractReport:
    """What a single ``.py`` yielded — surfaced so extraction is never silently wrong.

    ``error`` stores ONLY the exception TYPE name + line (e.g. ``"SyntaxError@42"``),
    NEVER ``str(exc)``: a SyntaxError's message embeds the offending source line, which
    is value-bearing. No renderer that can reach the model ever emits an error string.
    """

    path: str
    error: str = ""
    is_notebook: bool = False  # a marimo notebook (a plain module is not catalogued)
    n_datasets: int = 0
    n_checks: int = 0
    dropped_nodes: tuple[tuple[str, int], ...] = ()  # (kind, count) — value-free drift report


@dataclass
class Catalog:
    notebooks: tuple[Notebook, ...] = ()
    reports: tuple[ExtractReport, ...] = ()

    def is_empty(self) -> bool:
        return not self.notebooks

    def get(self, name: str):
        """The notebook matching ``name`` — its workspace-relative path, its file stem,
        or its title — case-insensitively, over the PRE-PARSED in-memory objects only.
        ``name`` is never handed to the filesystem, so a path-like argument that names
        no catalogued notebook simply finds nothing. ``None`` when nothing matches."""
        key = _norm(name)
        if not key:
            return None
        for nb in self.notebooks:
            if key in (nb.path.lower(), _stem(nb.path).lower(), nb.title.lower()):
                return nb
        # A partial path ("recon.py" for "reports/recon.py") is what an analyst and a
        # model both actually type; accept it once the exact keys have had their say.
        for nb in self.notebooks:
            if nb.path.lower().endswith("/" + key):
                return nb
        return None

    def list_notebooks(self) -> list[Notebook]:
        return list(self.notebooks)

    def search(self, query: str, limit: int = 8) -> list[Notebook]:
        """Notebooks matching every space-separated term in ``query``, best first.

        Terms are ANDed (so "month end recon" narrows rather than widens) and matched
        case-insensitively against :meth:`Notebook.terms`. The score prefers a hit in the
        path/title over one buried in the prose summary, so "the notebook actually called
        recon" outranks "a notebook that mentions recon".
        """
        terms = _norm(query).split()
        if not terms:
            return []
        scored: list[tuple[int, str, Notebook]] = []
        for nb in self.notebooks:
            haystack = [t.lower() for t in nb.terms()]
            strong = f"{nb.path} {nb.title}".lower()
            if not all(any(term in t for t in haystack) for term in terms):
                continue
            score = sum(2 if term in strong else 1 for term in terms)
            scored.append((score, nb.path, nb))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [nb for _, _, nb in scored[:limit]]


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).strip().lower()


def _stem(path: str) -> str:
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".py") else base


# -- rendering (what the tools serialise — names, paths, and the scanned summary) --


def render_notebook(nb: Notebook) -> str:
    """One notebook as compact catalog text. Every line is an allowlisted field; there
    is deliberately no cell-source line (the model reads the CURRENT notebook's source
    through ``mooring_read_notebook_source``, and no tool serves another one's)."""
    lines = [f"Notebook `{nb.path}`" + (f" — {nb.title}" if nb.title else "")]
    if nb.summary:
        lines.append(f"  about: {nb.summary}")
    if nb.n_cells:
        lines.append(f"  cells: {nb.n_cells}")
    if nb.datasets:
        rendered = ", ".join(
            f"{d.name or '?'}" + (f" ({d.path})" if d.path else "") for d in nb.datasets
        )
        lines.append(f"  fingerprints inputs: {rendered}")
    if nb.checks:
        lines.append(
            "  asserts checks: "
            + ", ".join(f"{c.kind}" + (f" [{c.name}]" if c.name else "") for c in nb.checks)
        )
    if nb.sql_tables:
        lines.append(f"  sql tables: {', '.join(nb.sql_tables)}")
    if nb.helpers:
        lines.append(f"  uses team helpers: {', '.join(nb.helpers)}")
    if nb.imports:
        lines.append(f"  imports: {', '.join(nb.imports[:20])}")
    return "\n".join(lines)


def render_notebooks(notebooks) -> str:
    return "\n\n".join(render_notebook(nb) for nb in notebooks)


def render_lines(notebooks) -> str:
    """One line per notebook — path, title, and a value-free count of what it pins — in
    the order given, so a search keeps its ranking."""
    out: list[str] = []
    for nb in notebooks:
        bits = []
        if nb.datasets:
            bits.append(f"{len(nb.datasets)} input(s)")
        if nb.checks:
            bits.append(f"{len(nb.checks)} check(s)")
        suffix = f"  [{', '.join(bits)}]" if bits else ""
        out.append(f"{nb.path}" + (f" — {nb.title}" if nb.title else "") + suffix)
    return "\n".join(out)


def render_listing(catalog: Catalog) -> str:
    """The ``mooring_list_notebooks`` body — :func:`render_lines` over the whole catalog,
    path-sorted so a big repo reads as a directory rather than an arbitrary order."""
    return render_lines(sorted(catalog.notebooks, key=lambda n: n.path))
