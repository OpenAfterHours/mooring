"""A value-free, repo-wide catalog of the workspace's marimo notebooks.

The question a growing team actually asks — *"has someone already built this?"*, *"which
notebook produces the month-end number?"* — had no answer in the product: the copilot
could read exactly ONE notebook (the open one) and the hub's filter box matched only a
filename. This package answers it from AUTHORED SOURCE alone, and the same entries feed
both consumers: the copilot's catalog tools (:mod:`mooring.ai.tools`) and the hub's
client-side search box.

The notebook-source analogue of :mod:`mooring.ai.codelib`: each ``.py`` is parsed with
``ast`` (**never imported, executed, or run**) and reduced to its title, the collapsed
text of its first markdown cell, what it imports, the inputs it fingerprints and the
checks it asserts *as written in the source*, and the tables its SQL selects from.

The frozen dataclasses in :mod:`.model` ARE the allowlist. Two rules keep this the same
privacy tier as the notebook source the copilot already sees, and no wider:

* **A receipt is never opened.** ``.mooring/inputs`` and ``.mooring/checks`` hold what a
  run against REAL data observed; the catalog reports only what the source *declares*, so
  no run artifact can ride this channel.
* **A literal is lifted only from a named slot of a known call.** A computed string (an
  f-string, a variable) has no slot at all — which is where a data value would appear.

Layout mirrors ``codelib``: :mod:`.model` is the model + renderers, :mod:`.ast_walk` the
allowlist walk, :mod:`.prosescan` the summary scanner, :mod:`.loader` the file discovery
+ orchestration.
"""

from __future__ import annotations

from mooring.ai.notebookindex.ast_walk import extract_notebook
from mooring.ai.notebookindex.loader import DEFAULT_MAX_FILE_BYTES, load_catalog
from mooring.ai.notebookindex.model import (
    SUMMARY_CAP,
    Catalog,
    Check,
    Dataset,
    ExtractReport,
    Notebook,
    render_lines,
    render_listing,
    render_notebook,
    render_notebooks,
)

__all__ = [
    "Catalog",
    "Check",
    "Dataset",
    "ExtractReport",
    "Notebook",
    "extract_notebook",
    "load_catalog",
    "render_lines",
    "render_listing",
    "render_notebook",
    "render_notebooks",
    "SUMMARY_CAP",
    "DEFAULT_MAX_FILE_BYTES",
]
