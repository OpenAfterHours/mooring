"""Statically extract a value-free catalog entry from a marimo notebook via ``ast``.

The load-bearing privacy property, exactly as in :mod:`mooring.ai.codelib.ast_walk`:
this NEVER imports, executes, compiles-to-eval, or ``ast.literal_eval``s the notebook —
it only ``ast.parse``s text. And it never opens a ``.mooring/`` receipt: the fingerprints
and checks it reports are the ones the SOURCE declares, not the ones a run produced, so
the catalog can carry no run artifact (a receipt is a local artifact of executing against
real data, and routing one to the model would be a new egress channel).

A marimo notebook keeps its real content INSIDE cell function bodies, so unlike the
code-library walk this one does descend into them — but only to *count* allowlisted
facts. The extractor still has no slot for a cell body, an expression, an arbitrary
literal, or an output, so descending cannot widen what leaves. A literal is lifted only
from a named argument of a known call (``mooring_inputs.fingerprint``,
``mooring_checks.*``, ``mo.md``, ``mo.sql``); a computed argument — an f-string, a
variable, a concatenation — has no slot at all and is dropped, which is what keeps a
runtime-built value out of the catalog.
"""

from __future__ import annotations

import ast
import re
from collections import Counter

from mooring import notebook_template
from mooring.ai.notebookindex import prosescan
from mooring.ai.notebookindex.model import SUMMARY_CAP, Check, Dataset, ExtractReport, Notebook

_INPUTS_MODULE = "mooring_inputs"
_CHECKS_MODULE = "mooring_checks"
_MARIMO_MODULE = "marimo"

# The value-free tie-out API (mooring._checks_runtime). `reset` is bookkeeping, not a
# check, so it is deliberately absent.
_CHECK_FUNCS = frozenset(
    {"reconciles", "unique_key", "no_fanout", "row_delta", "not_null", "expect"}
)

# Caps: a catalog entry is a summary, not a transcript. A generated notebook with
# hundreds of calls must not turn one tool result into a wall of text.
_MAX_IMPORTS = 40
_MAX_DATASETS = 40
_MAX_CHECKS = 40
_MAX_SQL_TABLES = 12

# The one reduction applied to an mo.sql literal: the identifier-shaped token after
# FROM/JOIN. It cannot match a quoted string (an identifier can't start with a quote),
# so a value inlined into a WHERE clause has no way to be captured as a "table".
_SQL_TABLE_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_.$]*)", re.IGNORECASE)
# Words that legally follow FROM/JOIN without naming a table.
_SQL_NOISE = frozenset({"lateral", "unnest", "values", "only", "select"})

# A leading markdown ATX heading — already harvested as the title, so the summary keeps
# the prose BENEATH it rather than restating it.
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")


def extract_notebook(source: str, rel: str) -> tuple[Notebook, ExtractReport]:
    """Parse ``source`` into a value-free :class:`Notebook` + a drift :class:`ExtractReport`.

    Never raises for bad input: a ``SyntaxError`` (or any parse error) degrades to an
    empty entry and a report whose ``error`` is the exception TYPE + line ONLY — never
    ``str(exc)``, whose message embeds the offending source line. The caller decides what
    to do with a file whose ``is_notebook`` is False (a plain helper module belongs to the
    code library, not the catalog).
    """
    dropped: Counter = Counter()
    is_notebook = notebook_template.is_marimo_app(source)
    try:
        tree = ast.parse(source, type_comments=False)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return (
            Notebook(path=rel),
            ExtractReport(
                path=rel,
                is_notebook=is_notebook,
                error=f"{type(exc).__name__}@{getattr(exc, 'lineno', 0) or 0}",
            ),
        )

    aliases, funcs, imports = _imports(tree)
    datasets: list[Dataset] = []
    checks: list[Check] = []
    sql_tables: list[str] = []
    markdown: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _resolve(node.func, aliases, funcs)
        if callee is None:
            continue
        module, attr = callee
        if module == _INPUTS_MODULE and attr == "fingerprint":
            datasets.append(_dataset(node, dropped))
        elif module == _CHECKS_MODULE and attr in _CHECK_FUNCS:
            checks.append(Check(kind=attr, name=_kwarg_str(node, "name", dropped) or ""))
        elif module == _MARIMO_MODULE and attr == "md":
            text = _first_str(node, dropped)
            if text:
                markdown.append((getattr(node, "lineno", 0) or 0, text))
        elif module == _MARIMO_MODULE and attr == "sql":
            sql_tables += _sql_tables(_first_str(node, dropped) or "")

    title = notebook_template.notebook_title(source)
    summary, withheld = _summary(markdown, title)
    if withheld:
        dropped["summary"] += 1
    n_cells = sum(1 for stmt in tree.body if _is_cell(stmt))
    dropped["cell_body"] += n_cells  # walked for facts; NEVER read into any slot

    notebook = Notebook(
        path=rel,
        title=title,
        summary=summary,
        imports=tuple(dict.fromkeys(imports))[:_MAX_IMPORTS],
        datasets=tuple(dict.fromkeys(datasets))[:_MAX_DATASETS],
        checks=tuple(dict.fromkeys(checks))[:_MAX_CHECKS],
        sql_tables=tuple(dict.fromkeys(sql_tables))[:_MAX_SQL_TABLES],
        n_cells=n_cells,
    )
    report = ExtractReport(
        path=rel,
        is_notebook=is_notebook,
        n_datasets=len(notebook.datasets),
        n_checks=len(notebook.checks),
        dropped_nodes=tuple(sorted(dropped.items())),
    )
    return notebook, report


# -- imports: the alias map that makes a call resolvable ---------------------


def _imports(tree) -> tuple[dict[str, str], dict[str, tuple[str, str]], list[str]]:
    """``(aliases, funcs, imports)`` for the whole tree — module imports live INSIDE
    marimo cells, so this walks everything rather than only the top level.

    ``aliases`` maps a local name to the module it stands for (``mo`` -> ``marimo``);
    ``funcs`` maps a directly-imported callable to its ``(module, attr)`` so
    ``from mooring_checks import unique_key`` still resolves; ``imports`` is the flat
    list of dotted names, kept as the notebook's "what it builds on" signal.
    """
    aliases: dict[str, str] = {}
    funcs: dict[str, tuple[str, str]] = {}
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imports.append(n.name)
                aliases[n.asname or n.name.split(".")[0]] = n.name
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            for n in node.names:
                if n.name == "*":
                    continue
                imports.append(f"{mod}.{n.name}" if mod else n.name)
                if mod:
                    funcs[n.asname or n.name] = (mod, n.name)
    return aliases, funcs, imports


def _resolve(func, aliases: dict[str, str], funcs: dict[str, tuple[str, str]]):
    """The ``(module, attr)`` a call's callee names, or ``None`` when it is anything
    else. Only a ``<alias>.<attr>`` or a bare imported name resolves — a call through a
    variable, a subscript, or a chain mooring did not see imported is simply unknown,
    so no fact is extracted from it (fail-closed: unknown means nothing is captured)."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        module = aliases.get(func.value.id)
        return (module, func.attr) if module else None
    if isinstance(func, ast.Name):
        return funcs.get(func.id)
    return None


# -- literal lifting: only from a named slot of a known call -----------------


def _str_const(node) -> str | None:
    """A PLAIN string literal, or ``None``. An f-string (``ast.JoinedStr``) or any
    computed expression returns ``None`` on purpose — a runtime-built string is exactly
    where a data value would appear, and it has no slot in the model."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip()
    return None


def _first_str(call, dropped: Counter) -> str | None:
    if not call.args:
        return None
    text = _str_const(call.args[0])
    if text is None:
        dropped["dynamic_string"] += 1
    return text


def _kwarg_str(call, name: str, dropped: Counter) -> str | None:
    for kw in call.keywords:
        if kw.arg == name:
            text = _str_const(kw.value)
            if text is None:
                dropped["dynamic_string"] += 1
            return text
    return None


def _dataset(call, dropped: Counter) -> Dataset:
    """``mi.fingerprint(df, "sales", path="data/sales.csv")`` -> ``Dataset``. The label is
    the second positional argument or ``name=``; the path is ``path=`` (see
    :func:`mooring.inputs.copilot_guide` for the API). Anything non-literal drops."""
    name = _kwarg_str(call, "name", dropped)
    if name is None and len(call.args) > 1:
        name = _str_const(call.args[1])
    return Dataset(name=name or "", path=_kwarg_str(call, "path", dropped) or "")


def _sql_tables(sql: str) -> list[str]:
    return [
        t for t in _SQL_TABLE_RE.findall(sql) if t.lower() not in _SQL_NOISE
    ][:_MAX_SQL_TABLES]


# -- the one free-text slot --------------------------------------------------


def _summary(markdown: list[tuple[int, str]], title: str) -> tuple[str, bool]:
    """``(summary, withheld)`` from the FIRST markdown cell in source order.

    ``ast.walk`` is breadth-first, so the cells arrive unordered; the line number picks
    the one the analyst actually wrote first. Its leading heading is dropped (that is
    already the title), the rest is collapsed to a single capped line — a compact tool
    result and a good search haystack — and withheld entirely on a scan hit. A cell that
    is ONLY a heading therefore yields no summary rather than restating the title.
    """
    if not markdown:
        return "", False
    text = min(markdown, key=lambda m: m[0])[1]
    lines = text.splitlines()
    if lines and _HEADING_RE.match(lines[0]):
        lines = lines[1:]
    flat = " ".join(" ".join(lines).split())
    if not flat or flat.casefold() == (title or "").casefold():
        return "", False
    if len(flat) > SUMMARY_CAP:
        flat = flat[:SUMMARY_CAP].rstrip() + " ...[trimmed]"
    if prosescan.scan_summary(flat):
        return "", True
    return flat, False


def _is_cell(stmt) -> bool:
    """A marimo cell: a top-level function decorated ``@app.cell`` (mooring's template
    and marimo's own codegen both emit that), or the anonymous ``def _()`` marimo names
    a cell when it is written by hand."""
    if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in stmt.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "cell":
            return True
    return stmt.name == "_"
