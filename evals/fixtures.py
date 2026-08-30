"""The synthetic worlds a case runs in: marimo notebooks and dataset schemas.

Everything here is FABRICATED. A case is sent to a real model, so a fixture that
carried real column names or real content would be an egress of exactly the kind
the copilot exists to prevent. Datasets are written as a header row plus ONE
made-up sample row — enough for :func:`mooring.schema.extract_schema` to report
names + dtypes, which is all the model ever sees of a dataset anyway.

Notebooks are COMPOSED, never hand-written: each fixture is a list of cell BODIES
run through :func:`mooring.marimo_rt.apply_cell_patch`, so marimo's own codegen
writes the ``@app.cell`` wrappers and the return tuples. A hand-written wrapper
that drifted from what marimo emits would make every case measure the fixture
rather than the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mooring import marimo_rt

# A marimo app with no cells — the seed every fixture notebook is appended onto.
_EMPTY_APP = (
    "import marimo\n\n"
    '__generated_with = "0.23.9"\n'
    "app = marimo.App()\n\n\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)


@dataclass(frozen=True)
class DatasetFixture:
    """A dataset the model sees the SCHEMA of. ``sample`` is one fabricated row,
    written only so the CSV reader can infer the dtypes the case wants."""

    rel: str
    columns: tuple[str, ...]
    sample: tuple[str, ...]


@dataclass(frozen=True)
class NotebookFixture:
    """A marimo notebook, as the cell BODIES it is composed from."""

    rel: str
    cells: tuple[str, ...]
    datasets: tuple[DatasetFixture, ...] = field(default_factory=tuple)

    def source(self) -> str:
        src = _EMPTY_APP
        for body in self.cells:
            src = marimo_rt.apply_cell_patch(src, [marimo_rt.CellOp(op="append", code=body)])
        return src


def _csv(rel: str, columns: tuple[str, ...], sample: tuple[str, ...]) -> DatasetFixture:
    return DatasetFixture(rel=rel, columns=columns, sample=sample)


# -- the datasets -------------------------------------------------------------

SALES = _csv(
    "data/sales.csv",
    ("region", "product", "amount", "order_date"),
    ("north", "widget", "125", "2026-01-04"),
)
# Deliberately capitalised: a model that lowercases column names silently breaks
# the cell, and no amount of "be careful" in a prompt fixes it — only reading the
# schema does. The one case that uses this isolates exactly that.
LEDGER = _csv(
    "data/ledger.csv",
    ("Account", "Amount", "PostedOn"),
    ("4000", "99.5", "2026-02-01"),
)
REGIONS = _csv(
    "data/regions.csv",
    ("region", "manager", "target"),
    ("north", "m-1", "5000"),
)


# -- the notebooks ------------------------------------------------------------

_IMPORTS = "import marimo as mo\nimport polars as pl"


def _nb(rel: str, cells: list[str], datasets: tuple[DatasetFixture, ...] = ()) -> NotebookFixture:
    return NotebookFixture(rel=rel, cells=tuple(cells), datasets=datasets)


NOTEBOOKS: dict[str, NotebookFixture] = {
    # The workhorse: imports, a load, a peek. Three cells, indices 0/1/2.
    "sales": _nb(
        "notebooks/sales.py",
        [
            _IMPORTS,
            'df = pl.read_csv("data/sales.csv")\ndf',
            "summary = df.describe()\nsummary",
        ],
        (SALES,),
    ),
    # Carries a named constant in a cell of its own (index 2) — the target for
    # "change the threshold", which a weak model answers by REDEFINING it.
    "threshold": _nb(
        "notebooks/threshold.py",
        [
            _IMPORTS,
            'df = pl.read_csv("data/sales.csv")',
            "THRESHOLD = 100",
            'big = df.filter(pl.col("amount") > THRESHOLD)\nbig',
        ],
        (SALES,),
    ),
    # Has a typo in a column name in cell 2, and dead code in cell 3 — the two
    # "fix/remove cell N" targets.
    "typo": _nb(
        "notebooks/typo.py",
        [
            _IMPORTS,
            'df = pl.read_csv("data/sales.csv")',
            'totals = df.group_by("region").agg(pl.col("amont").sum())\ntotals',
            "unused_scratch = 1 + 1",
        ],
        (SALES,),
    ),
    # A two-step pipeline whose middle frame has a bad name — the rename target.
    "rename": _nb(
        "notebooks/rename.py",
        [
            _IMPORTS,
            'df = pl.read_csv("data/sales.csv")',
            'df2 = df.filter(pl.col("region") == "north")',
            "df2.height",
        ],
        (SALES,),
    ),
    # Two datasets, only one of them loaded: the model must reach for
    # mooring_get_schema to learn the second one's columns before joining.
    "join": _nb(
        "notebooks/join.py",
        [
            _IMPORTS,
            'sales_df = pl.read_csv("data/sales.csv")',
        ],
        (SALES, REGIONS),
    ),
    # Capitalised columns (see LEDGER).
    "ledger": _nb(
        "notebooks/ledger.py",
        [
            _IMPORTS,
            'ledger = pl.read_csv("data/ledger.csv")',
        ],
        (LEDGER,),
    ),
    # NO `import marimo as mo`: a SQL cell needs it, so the model must add it.
    "no_marimo": _nb(
        "notebooks/no_marimo.py",
        [
            "import polars as pl",
            'df = pl.read_csv("data/sales.csv")',
        ],
        (SALES,),
    ),
    # A stack of near-identical steps. Asked to change "the cell that defines step2",
    # a model that GUESSES the index is off by one as often as not — and its `expect`
    # then names a cell it never meant, so the change is refused. A model that reads
    # first gets it right. That is the whole tool bucket in one fixture.
    "stack": _nb(
        "notebooks/stack.py",
        [
            _IMPORTS,
            'df = pl.read_csv("data/sales.csv")',
            "step1 = df.head(10)",
            "step2 = step1.tail(5)",
            "step3 = step2.head(2)\nstep3",
        ],
        (SALES,),
    ),
    # ALREADY broken: two cells define `totals`. The gate must report this as
    # pre-existing rather than blaming the model for it (see `_already_broken`),
    # so a proposal on this notebook still has to get through.
    "already_broken": _nb(
        "notebooks/already_broken.py",
        [
            _IMPORTS,
            'df = pl.read_csv("data/sales.csv")',
            'totals = df.group_by("region").len()',
            'totals = df.group_by("product").len()',
        ],
        (SALES,),
    ),
}


def first_line(notebook: str, index: int) -> str:
    """The first non-blank line of a fixture's cell ``index`` — what a model that
    actually READ the notebook would send as its ``expect``.

    Exists so a golden answer derives its claim from the fixture instead of repeating
    it as a literal: edit a fixture cell and every golden that targets it stays
    correct, where a hardcoded string would quietly start failing a case for a reason
    that has nothing to do with the model. A test that wants to script a model
    getting ``expect`` WRONG passes its own literal, which is the point.
    """
    cells = NOTEBOOKS[notebook].cells
    lines = [line for line in cells[index].splitlines() if line.strip()]
    return lines[0] if lines else ""


def cell_count(notebook: str) -> int:
    """How many cells a fixture has — a rewrite's ``expect_cells`` claim."""
    return len(NOTEBOOKS[notebook].cells)


def materialise(fixture: NotebookFixture, root: Path) -> tuple[Path, str, str, tuple[str, ...]]:
    """Write ``fixture`` into a fresh workspace under ``root``.

    Returns ``(workspace, notebook_rel, dataset_rel, folders)`` — the four things
    :meth:`mooring.app.chat_service.ChatService.build_context` needs.
    ``dataset_rel`` is the FIRST dataset (the one an analyst would have selected in
    the hub); any others are reachable only through ``mooring_get_schema``, which
    is the point of the join case.
    """
    workspace = root / "ws"
    for ds in fixture.datasets:
        target = workspace / ds.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = [",".join(ds.columns), ",".join(ds.sample)]
        target.write_text("\n".join(rows) + "\n", "utf-8", newline="\n")
    nb = workspace / fixture.rel
    nb.parent.mkdir(parents=True, exist_ok=True)
    # BOM-less, LF: marimo rejects a notebook that starts with a BOM.
    nb.write_text(fixture.source(), "utf-8", newline="\n")
    names = {Path(fixture.rel).parts[0]}
    names |= {Path(ds.rel).parts[0] for ds in fixture.datasets}
    dataset_rel = fixture.datasets[0].rel if fixture.datasets else ""
    return workspace, fixture.rel, dataset_rel, tuple(sorted(names))
