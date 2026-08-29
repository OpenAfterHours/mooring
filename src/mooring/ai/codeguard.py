"""What will this cell DO if it lands? — the static classifier behind the Apply gate.

The copilot's Apply writes a cell into the analyst's open notebook, and marimo (running
with ``watcher_on_save = "autorun"``) immediately EXECUTES it. Undo restores the
notebook's bytes, so it is a COMPLETE remedy for a cell that only computes and NO remedy
at all for one that deleted a file or dropped a table. That asymmetry — not "does this
code look scary?" — is the whole of the band assignment here:

* :data:`BAND_CLEAN` — Undo fully covers it. Apply silently. Most cells.
* :data:`BAND_ASK` — Undo leaves something behind, so ask once with a plain reason.
* :data:`BAND_FLOOR` — the effect is outside the notebook and irreversible. Always ask;
  nothing downgrades it (not config, not a model's own verdict, not an instruction
  inside a synced file).

**Destructive is not the same as side-effecting.** Creating a NEW file destroys nothing
and stays clean; overwriting an existing one is ``ask``. Analysts write files constantly,
so a gate that prompts on ``to_csv`` would be turned off within a day and protect nothing
— false positives are the failure mode that matters here, ahead of misses.

This is the mirror image of :mod:`mooring.pushguard`: same value-free ``Finding`` shape,
same stateless confirm token bound to the exact bytes AND the exact finding set, but
pointed INBOUND at code arriving from the model rather than outbound at bytes leaving for
the team repo. Like the push guard it is defence in depth, never a guarantee — a clean
verdict means "nothing in the enumerated table matched", not "this code is safe".

Why ``ast`` and not a regex, which would be a tenth of the code: a regex fires on
``# os.remove(old)`` and on ``note = "we used to DROP TABLE here"``. A comment is not in
the tree at all, and a string literal has to be reached through a slot we chose to look
at, so both fall out for free rather than needing an exception list. The same reasoning
runs through the two string heuristics below (SQL text, install commands): a string in a
PROSE slot — a docstring, a bare string statement, an argument to ``mo.md`` or ``print``
— is skipped, because none of those slots can execute what the string says.

Two rules exist because a heuristic alone could not be trusted with the floor band, and
both are worth reading before changing anything here:

* **Only SQL in a known SQL slot may reach the floor** (:func:`_cap_loose`). A loose
  literal that merely LOOKS like SQL caps at ``ask`` — ``button_text = "Delete from
  list"`` is ordinary English opening with a SQL verb, and no english-detection filter
  wins that arms race for long. The band ceiling is a rule; the filters above it only
  decide whether to say anything at all.
* **The outbox carve-out is anchored and refuses navigation** (:func:`_path_findings`).
  It is the one rule that turns a write CLEAN, so it is also the one an escape would aim
  at: an unanchored prefix test let ``.mooring/outbox/../mooring.toml`` walk onto the
  policy file with no prompt at all.

**Known limits, stated plainly because this is defence in depth and not a security
boundary.** The matcher reads names, not values, so a call reached through a REBOUND name
is invisible to it: ``rm = os.remove; rm(p)``, ``mod = os; mod.remove(p)``, and
``os.__dict__["remove"](p)`` all classify clean, and so does ``p.rename(q)`` (see the note
on :data:`_ATTR_CALLS`) and ``mo.sql(f"{verb} TABLE t")``, whose verb is interpolated.
Three narrow one-hop resolutions exist — the buffer carve-out, the SQL binding, and
``sys.modules["<literal>"]`` — and none generalises: following arbitrary rebinding would
mean real dataflow analysis, which is a different tool. A cell is also scanned ALONE, so
an ``import`` in another cell is not seen (the module tables compensate by taking
``os.``/``subprocess.`` at face value). The gate raises the cost of an accident, not of an
adversary.

**Top follow-up, deliberately not taken here: let a REFERENCE count, not only a call.**
``functools.partial(os.remove, p)``, ``list(map(os.remove, paths))`` and — the nastiest —
``atexit.register(shutil.rmtree, tmp)`` all classify clean today, because ``os.remove``
appears as a bare attribute REFERENCE that is called elsewhere. Classifying such a
reference through the existing tables needs no dataflow at all, and the precedent is
already here: ``handler = getattr(os, "remove")`` reaches the floor today, because the
literal-``getattr`` path classifies an expression that YIELDS a dangerous callable rather
than only a call of one. It would also narrow the rebinding family above — the reference
SITE would be flagged, so ``rm = os.remove`` fires even though ``rm(p)`` alone never
could — without closing it, since ``os.__dict__["remove"](p)`` stays out of reach. It is
held back because it is a semantic rule change ("a reference counts"), not a table entry,
and it would land after the false-positive rate was measured against the current rules;
this feature's own design argues for measuring before tightening. Whoever picks it up
should re-measure a realistic corpus first, not just re-run the tests.

Privacy: a :class:`Finding` is ``(line, kind, label, band)`` where ``label`` is a FIXED
string looked up from :data:`KINDS`. Nothing read out of the analyst's code — no path, no
variable name, no matched substring — is ever interpolated into a finding, so the gate's
output (which reaches the wire, the UI, and the logs) carries no data. Literal paths ARE
read, to decide a band; they are never carried. Pinned by
``tests/test_codeguard.py::test_findings_are_value_free``.

Pure stdlib by contract (``ast``/``re``/``hashlib``/``dataclasses``): no marimo, no HTTP,
no adapter. The gate has to be able to run anywhere the Apply path runs.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass

BAND_CLEAN = "clean"
BAND_ASK = "ask"
BAND_FLOOR = "floor"

# Worst band wins across a cell's findings, and across every op in one Apply.
_BAND_RANK = {BAND_CLEAN: 0, BAND_ASK: 1, BAND_FLOOR: 2}

# kind -> (analyst-facing label, band). ONE table, so a label is a constant chosen here
# and never assembled from what was matched — that is what keeps findings value-free.
# The labels are written for someone who does not read Python: "Deletes files or folders",
# not "call to shutil.rmtree". They are the product; the kinds are the stable slugs the
# wire and the tests use.
KINDS: dict[str, tuple[str, str]] = {
    # -- floor: the effect outlives the notebook, so Undo is not a remedy -----------
    "deletes_files": ("Deletes files or folders", BAND_FLOOR),
    "destroys_rows": ("Deletes a database table, or every row in one", BAND_FLOOR),
    "runs_program": ("Runs another program on your computer", BAND_FLOOR),
    "dynamic_code": ("Runs code that is built while it runs", BAND_FLOOR),
    "edits_mooring_config": ("Changes mooring's own settings files", BAND_FLOOR),
    # -- ask: Undo leaves something behind ------------------------------------------
    "overwrites_file": ("Writes a file, replacing anything already there", BAND_ASK),
    "changes_database": ("Changes data in a database", BAND_ASK),
    "sends_data": ("Sends data to another computer", BAND_ASK),
    "installs_package": ("Installs software", BAND_ASK),
    "replaces_notebook": ("Replaces every cell in this notebook", BAND_ASK),
    "unparseable": ("This code could not be read, so it was not checked", BAND_ASK),
}


@dataclass(frozen=True)
class Finding:
    line: int  # 1-based line within the cell; 1 for a whole-cell finding
    kind: str  # stable value-free slug — the key into KINDS
    label: str  # analyst-facing plain English, fixed per kind
    band: str  # BAND_ASK or BAND_FLOOR (a clean cell yields no findings)


@dataclass(frozen=True)
class Verdict:
    band: str  # worst band across the findings; BAND_CLEAN when there are none
    findings: tuple[Finding, ...]  # sorted by (line, kind)


# ---------------------------------------------------------------------------
# The detector tables
# ---------------------------------------------------------------------------
# Three shapes of call are recognised, because three shapes are how these APIs are
# actually written in a notebook:
#
#   1. a call on a MODULE          os.remove(p), subprocess.run(...), requests.post(...)
#   2. a call on an unknown object .unlink(), .to_csv(...), df.to_sql(...)
#   3. a BARE builtin              eval(...), exec(...), open(p, "w")
#
# Shape 1 resolves the receiver through the cell's own imports and, failing that, takes
# the written name at face value: a cell is scanned ALONE, so `import os` very often sits
# in a different cell and `os.remove` must still be recognised. That face-value fallback
# is why the module keys are deliberately unambiguous stdlib names — no analyst calls a
# DataFrame `subprocess`.
#
# Shape 2 is the one that needs discipline, since the receiver is unknowable: only method
# names with essentially no collisions are listed. `.put`, `.patch` and `.delete` are
# NOT here even though the contract's `sends_data` row names them, because numpy owns
# `np.put`/`np.delete` and `unittest.mock` owns `patch` — with the receiver unresolved
# those would fire on ordinary analysis. `requests.put(...)`/`httpx.delete(...)` are still
# caught, through shape 1 where the receiver IS known. Missing `session.delete(url)` is
# the deliberate trade; a false prompt on `np.delete(arr, 0)` would be worse.

_MODULE_ANY_CALL = {
    # Every attribute of these modules does the thing the band is about, so listing
    # individual functions would only be a way to miss one (subprocess.run,
    # .Popen, .check_output, .call, .check_call, ...).
    "subprocess": "runs_program",
    "smtplib": "sends_data",
}

_MODULE_ATTR_CALLS: dict[tuple[str, str], str] = {
    ("os", "remove"): "deletes_files",
    ("os", "unlink"): "deletes_files",
    ("os", "rmdir"): "deletes_files",
    ("os", "removedirs"): "deletes_files",
    ("shutil", "rmtree"): "deletes_files",
    # These destroy just as irreversibly as a delete: replace/truncate wipe an existing
    # file's contents, and rename/move take the destination out AND the source with it.
    ("os", "replace"): "deletes_files",
    ("os", "truncate"): "deletes_files",
    ("os", "rename"): "deletes_files",
    ("os", "renames"): "deletes_files",
    ("shutil", "move"): "deletes_files",
    # Copying overwrites the destination but destroys nothing that was not already
    # duplicated elsewhere, so it asks rather than sitting at the floor.
    ("shutil", "copy"): "overwrites_file",
    ("shutil", "copy2"): "overwrites_file",
    ("shutil", "copyfile"): "overwrites_file",
    ("os", "system"): "runs_program",
    ("os", "popen"): "runs_program",
    # Windows is mooring's primary platform and os.startfile is its os.system.
    ("os", "startfile"): "runs_program",
    ("pickle", "load"): "dynamic_code",
    ("pickle", "loads"): "dynamic_code",
    ("marshal", "load"): "dynamic_code",
    ("marshal", "loads"): "dynamic_code",
    # The modern spelling of `__import__`: importing a module named at run time is the
    # same "call anything" move, so it sits at the same band. Deliberately these two
    # NAMES and not the importlib package — `importlib.metadata.version(...)` and
    # `importlib.resources.files(...)` are ordinary and must stay clean.
    ("importlib", "import_module"): "dynamic_code",
    ("importlib", "__import__"): "dynamic_code",
    ("requests", "post"): "sends_data",
    ("requests", "put"): "sends_data",
    ("requests", "patch"): "sends_data",
    ("requests", "delete"): "sends_data",
    ("requests", "request"): "sends_data",
    ("httpx", "post"): "sends_data",
    ("httpx", "put"): "sends_data",
    ("httpx", "patch"): "sends_data",
    ("httpx", "delete"): "sends_data",
    ("httpx", "request"): "sends_data",
    ("micropip", "install"): "installs_package",
    ("pip", "main"): "installs_package",
    # The builtins module spells every bare call below a second way. _scan_call rewrites
    # `builtins.exec(...)` to the bare form, so these entries are what classifies the
    # THIRD spelling, `getattr(builtins, "exec")`, through _module_kind.
    ("builtins", "eval"): "dynamic_code",
    ("builtins", "exec"): "dynamic_code",
    ("builtins", "compile"): "dynamic_code",
    ("builtins", "__import__"): "dynamic_code",
}

# (module, attr-prefix) -> kind. os.execv/execl/execve/... and os.spawnv/spawnl/...
# are a family, not a list.
_MODULE_ATTR_PREFIXES = (
    ("os", "exec", "runs_program"),
    ("os", "spawn", "runs_program"),
)

_ATTR_CALLS: dict[str, str] = {
    "unlink": "deletes_files",  # Path.unlink
    "rmdir": "deletes_files",  # Path.rmdir
    "rmtree": "deletes_files",  # shutil.rmtree through any alias
    "sendmail": "sends_data",
    "send_message": "sends_data",
    "put_object": "sends_data",  # boto3
    "upload_file": "sends_data",  # boto3
    "upload_fileobj": "sends_data",  # boto3
    "post": "sends_data",  # session.post(...) — see the note above on the verb set
    "request": "sends_data",  # session.request("POST", url, …); collides with nothing
    "to_sql": "changes_database",  # pandas: writes rows whatever if_exists says
    "write_database": "changes_database",  # polars
}

# `Path.rename`, `Path.replace` and `Path.truncate` are DELIBERATELY absent from
# _ATTR_CALLS despite belonging to the same family as os.rename above. The receiver of a
# bare `.rename(...)` / `.replace(...)` cannot be resolved, and both names are everyday
# pandas — `df.rename(columns=…)` and `df.replace(0, None)` appear in a large share of
# real notebooks. Listing them would put an UN-DOWNGRADABLE floor prompt on ordinary
# dataframe work, which is the one failure this gate cannot survive. The module-qualified
# spellings above are caught; `p.rename(q)` is a known, accepted miss.

# Bare builtin-ish names. Each is skipped when the cell's own imports show the name was
# imported from somewhere (`from re import compile`), so a shadowed builtin never fires.
_BARE_CALLS: dict[str, str] = {
    "eval": "dynamic_code",
    "exec": "dynamic_code",
    "compile": "dynamic_code",
    "__import__": "dynamic_code",
    "Popen": "runs_program",
}

# File writes where the destination is an ARGUMENT. The pandas `to_*` family is the
# contract's; the polars `write_*` family is added because this repo (and the notebooks
# it ships to) is polars-first, and plotly's `write_html`/`write_image` because Deliver
# has made writing a report the everyday shape of "produce an artifact".
_ARG_PATH_WRITES = frozenset(
    {
        "to_csv", "to_excel", "to_parquet", "to_feather", "to_json", "to_pickle",
        "write_csv", "write_parquet", "write_json", "write_ndjson", "write_ipc",
        "write_excel", "write_avro", "write_delta",
        "write_html", "write_image",
        "savefig",
    }
)

# File writes where the destination is the RECEIVER: Path(p).write_text(data).
_SELF_PATH_WRITES = frozenset({"write_text", "write_bytes"})

# Writes whose destination is in NO fixed position: `np.save(path, arr)` puts it first,
# `torch.save(model, path)` and `joblib.dump(model, path)` put it second, and
# `wb.save(path)` (openpyxl) / `chart.save(path)` (altair) take it alone. Rather than
# guess an order per library, the first string-LITERAL argument is taken as the
# destination and anything else falls through to the ordinary computed-path `ask`.
_ANY_ARG_PATH_WRITES = frozenset({"save", "savez", "savez_compressed", "dump"})

# `dump` alone needs a second argument to be a write: `yaml.dump(data)` and
# `json.dumps`-alikes with one argument return a string and touch no file.
_NEEDS_DESTINATION = frozenset({"dump"})

# Keyword names the write family uses for its destination, across pandas, polars,
# matplotlib and plotly.
_PATH_KWARGS = frozenset(
    {"path", "path_or_buf", "buf", "excel_writer", "file", "fname", "filename",
     "target", "workbook", "destination"}
)

# Modules whose `open` is the builtin's shape (path first); anything else carrying
# `.open` is treated as a path object, whose `open` takes the MODE first.
_PATH_FIRST_OPEN_MODULES = frozenset({"io", "gzip", "bz2", "lzma", "codecs", "fsspec"})

# `buf = io.StringIO(); df.write_csv(buf)` is an everyday way to render a frame to TEXT
# — it touches no file at all. The destination is a bare name, so the normal
# "computed path, therefore ask" rule would prompt on it; recognising the one-line
# binding that produced it is the cheapest way not to. Deliberately the ONLY dataflow
# in this module: a direct `name = StringIO()` assignment in the same cell, nothing more.
_BUFFER_FACTORIES = frozenset({"StringIO", "BytesIO"})

# Calls whose string arguments are PROSE, not instructions: nothing here can execute the
# SQL or the shell command a string contains, so scanning them would only invent false
# positives out of documentation ("run `pip install polars` first").
_PROSE_ARG_CALLS = frozenset(
    {"md", "markdown", "print", "callout", "plain_text", "tooltip", "accordion",
     "info", "warning", "error", "debug", "exception"}
)

# Keyword arguments that are, by their name, text shown to a human. A marimo widget's
# ``label="Delete from list"`` is a caption on a button, not a statement — and no call
# site can execute it, so reading it would only manufacture false positives. This does
# NOT hide real SQL: a query reaching a cursor is found through its call site, which
# does not consult this list.
_PROSE_KWARGS = frozenset(
    {"label", "title", "description", "help", "placeholder", "caption", "subtitle",
     "header", "message", "hint", "name"}
)

# Call names whose string argument IS handed to a database. A first-keyword scan of the
# string is enough here; the structural second opinion below is only needed for loose
# string literals, whose call site tells us nothing.
_SQL_CALL_ATTRS = frozenset(
    {"sql", "execute", "executemany", "executescript", "read_sql", "read_sql_query",
     "read_sql_table", "read_database", "exec_driver_sql"}
)

# Single-argument wrappers a query is routinely handed through on its way to a slot:
# SQLAlchemy's `text(...)`, `textwrap.dedent(...)` around a triple-quoted query, a bare
# `str(...)`. Unwrapping them is what keeps the slot's authority — and with it the floor
# band — attached to the literal inside.
_SQL_PASSTHROUGH = frozenset({"text", "dedent", "str"})

# The install-command shapes worth catching in any string (or any list of strings) that
# ends up handed to a runner — ANCHORED to the start of a line or to a shell separator,
# so the string has to be shaped like a command rather than merely mention one. The model
# writes setup notes constantly ("Run pip install polars first"), and an unanchored
# search turns every one of them into a prompt. An optional interpreter prefix keeps the
# real spellings: `python -m pip install …`, and the `["-m", "pip", "install", …]` list
# form once its string parts are joined.
_INSTALL_COMMAND = (
    r"(?:pip3?|micropip)\s+install\b"
    r"|uv\s+(?:add|pip\s+install)\b"
    r"|conda\s+install\b"
    r"|poetry\s+add\b"
)
_INSTALL_RE = re.compile(
    rf"(?:^|[;&|]\s*)\s*(?:[!%]\s*)?(?:\S*python\S*\s+)?(?:-m\s+)?(?:{_INSTALL_COMMAND})",
    re.IGNORECASE | re.MULTILINE,
)

# Everything in a SQL string that can carry a VALUE rather than name a thing: quoted
# strings and identifiers, and both comment forms. Stripping these first is what makes
# the rest of the SQL handling cheap and safe — a `;` or a `WHERE` inside a literal can
# no longer steer the classification, and neither can prose in a comment.
_SQL_STRIP_RE = re.compile(
    r"""'(?:''|[^'])*'      # single-quoted string
      | \$\$.*?\$\$         # dollar-quoted block
      | "(?:""|[^"])*"      # double-quoted identifier
      | `[^`]*`             # backtick-quoted identifier
      | --[^\n]*            # line comment
      | /\*.*?\*/           # block comment
    """,
    re.VERBOSE | re.DOTALL,
)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_SQL_READ_FIRST = frozenset({"select", "show", "describe", "desc", "explain", "pragma"})
_SQL_DESTROY_FIRST = frozenset({"drop", "truncate"})
_SQL_CHANGE_FIRST = frozenset({"insert", "update", "alter", "merge", "grant", "replace",
                               "upsert"})
# DuckDB's own verbs. This repo shipped "copilot Speak SQL" on DuckDB, so these are
# idiomatic model-authored code, and `COPY … TO 'out.parquet'` in particular is THE
# DuckDB file export — a write that reaches the disk without passing any of the Python
# write detectors.
_SQL_DUCKDB_FIRST = frozenset({"copy", "install", "force", "attach"})
_SQL_FIRST_KEYWORDS = (
    _SQL_READ_FIRST
    | _SQL_DESTROY_FIRST
    | _SQL_CHANGE_FIRST
    | _SQL_DUCKDB_FIRST
    | {"delete", "create", "with"}
)
# Severity order, used when a WITH statement wraps a modifying statement.
_SQL_SEVERITY = ("drop", "truncate", "delete", "insert", "update", "alter", "merge", "grant")

# A loose string literal only counts as SQL when it ALSO carries a structural token.
# "UPDATE complete" and "Insert your name here" are English; "UPDATE t SET x = 1" is not.
_SQL_STRUCTURE = frozenset(
    {"from", "into", "table", "set", "values", "where", "view", "schema", "database",
     "index", "column", "join", "using"}
)

# …and the structural token has to be in the RIGHT PLACE. A structural token anywhere is
# not enough: "DROP is how you remove a table" opens with a keyword and mentions a table,
# and is a sentence. Requiring the word that FOLLOWS the verb to be one SQL could
# actually put there is what separates a statement from prose about one. Verbs absent
# from this map are checked by _loose_shape_ok's own rules.
_SQL_SECOND_WORD = {
    "drop": frozenset({"table", "view", "schema", "database", "index", "function",
                       "procedure", "sequence", "type", "trigger", "column", "constraint",
                       "materialized", "temporary", "temp", "if"}),
    "truncate": frozenset({"table"}),
    "delete": frozenset({"from"}),
    "insert": frozenset({"into", "or", "overwrite", "ignore"}),
    "replace": frozenset({"into"}),
    "upsert": frozenset({"into"}),
    "merge": frozenset({"into"}),
    "alter": frozenset({"table", "view", "schema", "database", "index", "column",
                        "sequence", "type"}),
}

# Words no SQL statement puts in its first three, and English puts there constantly
# ("Update THE set of columns", "Insert INTO THE report a summary"). The last cheap
# prose filter on a loose literal.
_ENGLISH_HINTS = frozenset(
    {"the", "a", "an", "your", "my", "our", "their", "this", "that", "these", "those",
     "please", "each", "every"}
)

# The one path prefix that makes a write create-not-overwrite: `.mooring/outbox` is
# Deliver's local, sync-excluded drop box, so a literal write under it lands beside the
# other artifacts rather than over the analyst's work. A COMPUTED path stays `ask` even
# when it would have resolved here — the gate only trusts what it can read statically.
#
# The carve-out is the ONE rule here that turns a write clean, which makes it the one
# rule an escape has to be impossible through. It is therefore anchored at the first two
# segments of a RELATIVE path, applied only after `.`/`..` are collapsed, and refused
# outright for a literal that mentioned `..` at all — an unanchored prefix test would let
# `.mooring/outbox/../mooring.toml` walk out of the box and land on the policy file
# without a prompt, and `policy.py` treats that file as attacker-controlled. An ABSOLUTE
# path never qualifies either: the gate has no workspace root, so it cannot know whose
# `.mooring/outbox` an absolute path names.
_OUTBOX_SEGMENTS = (".mooring", "outbox")

# Names that identify mooring's own configuration. A write to one of these can change
# what the app enforces — including the policy block — so it sits at the floor, and the
# floor is checked BEFORE the carve-out above.
_CONFIG_NAMES = frozenset({".marimo.toml", "mooring.toml"})
_CONFIG_DIR = ".mooring"

# Module names taken at face value when deciding whether a getattr target is module-like.
_KNOWN_MODULES = frozenset(
    {"os", "sys", "shutil", "subprocess", "builtins", "importlib", "pickle", "marshal",
     "smtplib", "requests", "httpx", "socket", "ctypes"}
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_code(code: str) -> Verdict:
    """Classify ONE cell's source.

    Never raises for bad input: a cell that does not parse is itself a finding
    (``unparseable``, band ``ask``) rather than an exception, because "we could not check
    this" is a thing the analyst should be told, not a reason to fail the Apply open.
    """
    return _verdict(_scan_cell(code))


def scan_ops(op_dicts) -> Verdict:
    """Classify the wire op-dicts the Apply path carries — the union of every op's code.

    Only the code that is ARRIVING is scanned. An ``edit``'s ``anchor`` is the cell's
    existing source (carried for conflict detection) and a ``delete`` introduces no code
    at all, so neither is scanned: prompting because the cell being REMOVED contained
    ``os.remove`` would be exactly backwards.

    ``replace_all`` always contributes ``replaces_notebook`` on top of whatever its cells
    contain — rewriting every cell is a big enough move to name even when each new cell
    is individually clean.

    An op of an unknown shape is ignored: :func:`mooring.ai.cellwrite.apply_wire_patch`
    rejects it before anything is written, so there is nothing here for the gate to hold.
    A MALFORMED one is different — ``{"op": "replace_all", "cells": 7}`` would raise, and
    the gate runs BEFORE cellwrite gets to reject it, which would make a bad request a
    500 instead of a held Apply. So like :func:`scan_code` this never raises: anything it
    cannot read becomes ``unparseable`` at band ``ask``.

    ``code`` is stringified exactly the way ``cellwrite._ops_from_wire`` does it, so the
    gate reads what will actually be written rather than a tidied version of it.
    """
    if op_dicts is not None and not isinstance(op_dicts, (list, tuple)):
        return _verdict([_finding(1, "unparseable")])
    findings: list[Finding] = []
    try:
        for op in op_dicts or []:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op", ""))
            if kind == "replace_all":
                # Do NOT "simplify" this to clean. A whole-notebook rewrite IS fully
                # undoable, so a literal reading of the band rule ("ask only where Undo
                # stops being a remedy") says clean. The rule is incomplete: a remedy
                # only counts if the analyst NOTICES they need it. One appended cell is
                # visible and local; a replacement of every cell destroys their own work
                # at a scale where the diff is large and the loss is easy to miss until
                # after a save or a push. The remedy exists, the detection doesn't. The
                # fatigue cost of asking is near zero because the operation is rare.
                findings.append(_finding(1, "replaces_notebook"))
                cells = op.get("cells") or []
                if not isinstance(cells, (list, tuple)):
                    findings.append(_finding(1, "unparseable"))
                    continue
                for cell in cells:
                    findings += _scan_cell(str(cell))
            elif kind in ("append", "edit"):
                findings += _scan_cell(str(op.get("code", "")))
    except Exception:  # noqa: BLE001 — fail CLOSED, exactly as _scan_cell does
        findings.append(_finding(1, "unparseable"))
    return _verdict(findings)


def token(notebook_rel: str, op_dicts, verdict: Verdict, *, notebook_bytes: bytes) -> str:
    """A stateless confirm token binding the notebook's PATH and CURRENT BYTES + the
    exact ops + the exact finding set, mirroring :func:`mooring.pushguard.file_token`
    (which hashes its data for the same reason).

    A confirmation stops matching the moment ANY of those change: a re-proposed cell, an
    edited anchor, a newly-detected finding, or the notebook drifting underneath. The
    bytes are what close the last of those — an ``edit`` is protected by its anchor
    (:mod:`mooring.marimo_rt` re-checks it), but an ``append`` carries no anchor, so
    without the bytes a held confirm would stay valid across an unrelated change to the
    file it is about to land in. The server re-scans and re-derives the token, so a
    client cannot supply one for ops or a notebook state it did not send.
    """
    h = hashlib.sha256()
    h.update((notebook_rel or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(hashlib.sha256(notebook_bytes or b"").digest())
    h.update(b"\x00")
    h.update(_canonical(op_dicts).encode("utf-8"))
    h.update(b"\x00")
    h.update(verdict.band.encode("utf-8"))
    for f in verdict.findings:
        h.update(f"\x00{f.line}:{f.kind}".encode())
    return h.hexdigest()[:16]


def describe(verdict: Verdict) -> list[str]:
    """Value-free one-liners ("line 4: Deletes files or folders") for a result line."""
    return [f"line {f.line}: {f.label}" for f in verdict.findings]


# ---------------------------------------------------------------------------
# Verdict assembly
# ---------------------------------------------------------------------------


def _finding(line: int, kind: str) -> Finding:
    label, band = KINDS[kind]
    return Finding(line=max(1, int(line or 1)), kind=kind, label=label, band=band)


def _verdict(findings: list[Finding]) -> Verdict:
    """Dedupe by ``(line, kind)``, sort, and take the worst band.

    Deduping matters more than it looks: several detectors legitimately fire on the same
    call (``requests.post`` matches both a module rule and an attribute rule), and two ops
    can carry the same line. The analyst should see one reason per reason.
    """
    unique: dict[tuple[int, str], Finding] = {}
    for f in findings:
        unique.setdefault((f.line, f.kind), f)
    ordered = tuple(unique[key] for key in sorted(unique))
    band = BAND_CLEAN
    for f in ordered:
        if _BAND_RANK[f.band] > _BAND_RANK[band]:
            band = f.band
    return Verdict(band=band, findings=ordered)


def _canonical(value) -> str:
    """A deterministic rendering of the wire ops, so the token does not depend on JSON key
    order or on which client serialised the body. Every key is included, not only the ones
    the scanner reads — the token binds what will be APPLIED."""
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: str(kv[0]))
        return "{" + ",".join(f"{_canonical(k)}:{_canonical(v)}" for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    return repr(value)


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _scan_cell(code: str) -> list[Finding]:
    """Findings for one cell — never raises.

    The whole scan is guarded, not just the parse. The helpers that read literal paths
    and string expressions recurse, so a pathological cell can raise ``RecursionError``
    (or anything else a malformed tree provokes) AFTER the parse succeeded, and an
    exception escaping here would surface as a 500 on the Apply route instead of a
    prompt. Failing closed to ``unparseable`` is strictly better: the analyst is asked
    about a cell the gate could not read, which is exactly what that finding says."""
    try:
        tree = ast.parse(code or "")
    except (SyntaxError, ValueError, RecursionError):
        # Whole-cell finding, so line 1: the cell as a whole is what went unchecked.
        return [_finding(1, "unparseable")]
    try:
        aliases, funcs = _imports(tree)
        prose = _prose_nodes(tree)
        buffers = _buffer_binds(tree)
        bindings = _sql_bindings(tree)
        sql_slots = _sql_slot_literals(tree, bindings)
        findings: list[Finding] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                findings += _scan_call(node, aliases, funcs, buffers, bindings)
        findings += _scan_strings(tree, prose, sql_slots)
        return findings
    except Exception:  # noqa: BLE001 — fail CLOSED; a guard that crashes is worse
        return [_finding(1, "unparseable")]


def _imports(tree) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """``(aliases, funcs)`` for the cell: local name -> module (``pd`` -> ``pandas``), and
    directly-imported callable -> ``(module, attr)`` so ``from os import remove`` still
    resolves. Imports live inside marimo cell bodies, so this walks the whole tree."""
    aliases: dict[str, str] = {}
    funcs: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                aliases[n.asname or n.name.split(".")[0]] = n.name
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            for n in node.names:
                if n.name != "*":
                    funcs[n.asname or n.name] = (module, n.name)
    return aliases, funcs


def _prose_nodes(tree) -> set[int]:
    """``id()``\\ s of every node inside a prose slot — a bare string statement (a
    docstring, or a stray string that executes nothing) and the arguments of the
    display/log calls in :data:`_PROSE_ARG_CALLS`.

    The whole subtree is marked, not just the top node, so ``mo.md("… " + name)`` does not
    leak its parts back into the string heuristics."""
    skip: set[int] = set()

    def bury(node) -> None:
        for sub in ast.walk(node):
            skip.add(id(sub))

    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant, ast.JoinedStr)):
            bury(node.value)
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            for kw in node.keywords:
                if name in _PROSE_ARG_CALLS or kw.arg in _PROSE_KWARGS:
                    bury(kw.value)
            if name in _PROSE_ARG_CALLS:
                for arg in node.args:
                    bury(arg)
    return skip


def _buffer_binds(tree) -> dict[str, list[tuple[int, bool]]]:
    """``name -> [(line, is_buffer)]`` for every binding of a local name in this cell.

    Writing a frame into an in-memory buffer renders it to text; it is not a file write
    at all, and the destination being a bare name would otherwise make it an
    ``overwrites_file`` by the computed-path rule.

    Every binding is recorded, not only the buffer ones, and each carries its line — so
    the carve-out is positional rather than a name that is "a buffer somewhere in the
    cell". A set of names would clear ``buf = io.StringIO()`` … ``buf = "sales.csv"`` …
    ``df.write_csv(buf)``, which writes a real file. See :data:`_BUFFER_FACTORIES` for
    why this is the only dataflow the module does at all."""
    binds: dict[str, list[tuple[int, bool]]] = {}

    def record(target, value, line: int) -> None:
        if isinstance(target, ast.Name):
            is_buffer = isinstance(value, ast.Call) and _call_name(value.func) in _BUFFER_FACTORIES
            binds.setdefault(target.id, []).append((line, is_buffer))
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                record(element, None, line)

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                record(target, node.value, line)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            record(node.target, node.value, line)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            record(node.target, None, line)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            record(node.optional_vars, node.context_expr, getattr(node.context_expr, "lineno", 0))
    return binds


def _is_buffer(name: str, line: int, binds) -> bool:
    """Was ``name`` holding an in-memory buffer at ``line``? The LAST binding at or above
    that line decides, so a rebinding to anything else takes the carve-out away."""
    prior = [bind for bind in (binds or {}).get(name, ()) if bind[0] <= line]
    if not prior:
        return False
    last = max(bind_line for bind_line, _ in prior)
    # Two bindings on one line (`buf = StringIO(); buf = path`) are ambiguous: fail closed.
    return all(is_buffer for bind_line, is_buffer in prior if bind_line == last)


def _call_name(func) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _root_module(func, aliases: dict[str, str]) -> str | None:
    """The module an attribute call's receiver names, or ``None``.

    ``pd.read_csv`` -> ``pandas`` when the cell imported it, and ``os.remove`` -> ``os``
    even when it did not: a cell is scanned on its own and its ``import os`` is very often
    in a different cell, so an unresolved root is taken at face value. That is safe only
    because the module keys in the tables are unambiguous stdlib names."""
    if not isinstance(func, ast.Attribute):
        return None
    node = func.value
    through_sys_modules = _sys_modules_key(node, aliases)
    if through_sys_modules is not None:
        return through_sys_modules
    while isinstance(node, ast.Attribute):
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return aliases.get(node.id, node.id).split(".")[0]


def _sys_modules_key(node, aliases: dict[str, str]) -> str | None:
    """The module ``sys.modules["<literal>"]`` names, or ``None``.

    The next spelling after ``getattr(os, "remove")``, and closed for the same reason:
    the key is a LITERAL, so this reads a module name that is written down rather than
    computed. ``sys.modules[name]`` with a variable key resolves to nothing and stays
    clean — that one needs dataflow."""
    if not (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)):
        return None
    if node.value.attr != "modules" or not isinstance(node.value.value, ast.Name):
        return None
    if aliases.get(node.value.value.id, node.value.value.id).split(".")[0] != "sys":
        return None
    key = node.slice
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value.split(".")[0]
    return None


def _scan_call(node: ast.Call, aliases, funcs, buffers=None, bindings=None) -> list[Finding]:
    line = getattr(node, "lineno", 1)
    func = node.func
    name = func.id if isinstance(func, ast.Name) else None
    attr = func.attr if isinstance(func, ast.Attribute) else None
    module = _root_module(func, aliases)
    if name is not None and name in funcs:
        # `from subprocess import run` — the bare call is really a module call.
        module, attr = funcs[name]
        name = None
    if module == "builtins" and attr is not None:
        # `builtins.exec(src)` is `exec(src)` with an extra hop, and the hop must not be
        # a way around the floor.
        name, module = attr, None

    out: list[Finding] = []
    kind = _module_kind(module, attr)
    if kind:
        out.append(_finding(line, kind))
    if attr is not None and attr in _ATTR_CALLS:
        out.append(_finding(line, _ATTR_CALLS[attr]))
    if name is not None and name in _BARE_CALLS and name not in funcs:
        out.append(_finding(line, _BARE_CALLS[name]))

    out += _scan_write(node, attr, name, module, line, buffers)
    out += _scan_sql_call(node, attr, line, bindings)
    out += _scan_egress_call(node, attr or name, line)
    out += _scan_getattr(node, name, funcs, aliases, line)
    return out


def _module_kind(module, attr) -> str | None:
    """The kind a ``<module>.<attr>`` call carries, or ``None``. Shared by the direct
    call path and :func:`_scan_getattr`, so a name reached through ``getattr`` is
    classified by exactly the same table as one written out."""
    if module is None or attr is None:
        return None
    kind = _MODULE_ANY_CALL.get(module) or _MODULE_ATTR_CALLS.get((module, attr))
    if kind:
        return kind
    for mod, prefix, prefix_kind in _MODULE_ATTR_PREFIXES:
        if module == mod and attr.startswith(prefix):
            return prefix_kind
    return None


def _scan_write(node, attr, name, module, line, buffers) -> list[Finding]:
    """File-write detection, including the new-file carve-out and the config floor."""
    if attr in _ANY_ARG_PATH_WRITES:
        if attr in _NEEDS_DESTINATION and len(node.args) < 2:
            return []  # `yaml.dump(data)` renders to a string; it writes nothing
        if not node.args and _write_destination(node) is _MISSING:
            return []
        literal = next(
            (a for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)),
            _MISSING,
        )
        return _path_findings(literal, line, buffers)
    if attr == "copytree":
        # A copytree onto a fresh directory creates; only dirs_exist_ok lets it land on
        # top of files that are already there.
        exists_ok = _kwarg(node, ("dirs_exist_ok",))
        if exists_ok is _MISSING or (
            isinstance(exists_ok, ast.Constant) and not exists_ok.value
        ):
            return []
        return _path_findings(node.args[1] if len(node.args) > 1 else _MISSING, line, buffers)
    if attr in _ARG_PATH_WRITES:
        path = _write_destination(node)
        if path is _MISSING:
            return []  # df.to_csv() with no destination returns a string; it writes nothing
        return _path_findings(path, line, buffers)
    if attr in _SELF_PATH_WRITES:
        # Path(p).write_text(data) — the destination is the receiver, not an argument.
        return _path_findings(node.func.value, line, buffers)
    if name == "open" or attr == "open":
        return _scan_open(node, name, module, line, buffers)
    return []


_MISSING = object()


def _write_destination(node):
    """The destination expression of an ``_ARG_PATH_WRITES`` call, or :data:`_MISSING`."""
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg in _PATH_KWARGS:
            return kw.value
    return _MISSING


def _scan_open(node, name, module, line, buffers) -> list[Finding]:
    """``open`` in either of its two argument orders.

    The builtin (and ``io``/``gzip``/... ``open``) takes the path first and the mode
    second; ``Path.open`` takes the MODE first and carries its path on the receiver.
    Only a receiver that RESOLVES tells us which we are looking at.

    An unresolved receiver is therefore not guessed. Assuming pathlib's order silently
    misses every write through an object this module has never heard of — an s3fs or
    smart_open handle, a ``ZipFile``, a team helper — because ``fs.open("report.csv",
    "w")`` reads its path as a mode, finds no ``w`` in it, and passes. So the unknown
    case looks for a write mode in EVERY position and fails toward ``ask``: whichever
    argument is the mode, the other one is the destination.
    """
    args = node.args
    mode_kw = _kwarg(node, ("mode",))
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else _MISSING

    if name == "open" or module in _PATH_FIRST_OPEN_MODULES:
        path = args[0] if args else _kwarg(node, ("file", "filename"))
        mode = args[1] if len(args) > 1 else mode_kw
        if mode is _MISSING or _literal_mode(mode) == "read":
            return []  # no mode, or an explicit read mode
        # A computed mode is rare and unreadable, so it counts as a write: the cost of
        # being wrong is one prompt, and the cost the other way is a silent overwrite.
        return _path_findings(path, line, buffers)

    if mode_kw is not _MISSING:  # `handle.open(p, mode="w")` — unambiguous either way
        if _literal_mode(mode_kw) == "read":
            return []
        return _path_findings(args[0] if args else receiver, line, buffers)
    for index, arg in enumerate(args[:2]):
        if _literal_mode(arg) == "write":
            # index 0 is pathlib's order (the path is the receiver); index 1 is the
            # builtin's (the path is the first argument).
            return _path_findings(receiver if index == 0 else args[0], line, buffers)
    if any(_literal_mode(arg) == "read" for arg in args[:2]):
        return []
    if args and not all(isinstance(a, ast.Constant) for a in args[:2]):
        # A computed argument sits where a mode could be: it cannot be ruled out.
        return _path_findings(args[0] if len(args) > 1 else receiver, line, buffers)
    return []  # no args at all, or only literals that are plainly not modes


def _literal_mode(node) -> str | None:
    """``"write"``/``"read"`` when ``node`` is a literal that could be an ``open`` mode,
    else ``None`` (a computed expression, or a literal no ``open`` would accept).

    Mode strings are short and drawn from one tiny alphabet, which is what lets a file
    NAME in the same position be told apart from a mode."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return None
    text = node.value
    if not text or len(text) > 3 or not set(text) <= set("rwaxbt+"):
        return None
    return "write" if set(text) & set("wax+") else "read"


def _kwarg(node, names):
    for kw in node.keywords:
        if kw.arg in names:
            return kw.value
    return _MISSING


def _path_findings(path_node, line, buffers=None) -> list[Finding]:
    """The finding (if any) for a write to ``path_node``.

    NORMALISE, then let precedence stand. The outbox carve-out is checked before the
    ``.mooring/`` DIRECTORY floor and that order is correct: ``.mooring/outbox/report.csv``
    contains a ``.mooring/`` segment, so dir-floor-first would put an unskippable prompt
    on every Deliver artifact — the most common write in this product. The escape was
    never the precedence, it was matching an un-normalised string:
    ``.mooring/outbox/../mooring.toml`` collapses to ``.mooring/mooring.toml``, which is
    simply not under the outbox any more and falls through to the floor on its own.

    The config NAMES, by contrast, are checked ahead of the carve-out. That costs nothing
    — no delivered artifact is called ``mooring.toml`` or ``.marimo.toml`` — and it means
    a literal ending in one of those names can never come out clean by any route.

    A path that cannot be read statically is an ordinary ``overwrites_file``: the
    carve-out is only ever granted to a literal we could check.
    """
    if isinstance(path_node, ast.Name) and _is_buffer(path_node.id, line, buffers):
        return []  # an in-memory buffer, not a file
    literal = None if path_node is _MISSING else _literal_path(path_node)
    if literal is None:
        return [_finding(line, "overwrites_file")]
    segments, absolute = _normalise_path(literal)
    if not segments:
        return [_finding(line, "overwrites_file")]
    # The two mooring-config checks sit on OPPOSITE SIDES of the carve-out, deliberately.
    # Do not merge them back into one condition — the asymmetry is the whole point:
    #
    #   NAME check, BEFORE  — nothing a Deliver run writes is called `mooring.toml` or
    #     `.marimo.toml`, so putting it first costs zero false prompts, and it means a
    #     literal ending in one of those names cannot come out clean by ANY route.
    #   DIR check, AFTER    — `.mooring/outbox/report.csv` contains a `.mooring/` segment,
    #     so running this one first would put an un-downgradable floor prompt on every
    #     Deliver artifact, the most common write in this product.
    #
    # Merging them either way breaks one of those. See _normalise_path: the escape this
    # ordering used to have was never the precedence, it was the un-normalised match.
    if segments[-1] in _CONFIG_NAMES:
        return [_finding(line, "edits_mooring_config")]
    if not absolute and tuple(segments[:2]) == _OUTBOX_SEGMENTS and len(segments) > 2:
        return []  # a new file in the drop box
    if _CONFIG_DIR in segments:
        return [_finding(line, "edits_mooring_config")]
    return [_finding(line, "overwrites_file")]


def _normalise_path(literal: str) -> tuple[list[str], bool]:
    """``(segments, absolute)`` for a literal path, collapsed lexically.

    Both separators are folded — Windows is the primary platform, so
    ``.mooring\\outbox\\..\\mooring.toml`` has to normalise exactly like its POSIX
    spelling — ``.`` segments are dropped, and ``..`` is collapsed against what precedes
    it. A ``..`` that escapes above the root SURVIVES as a segment, which is what stops
    such a path from ever matching the outbox anchor. ``absolute`` covers ``/x``,
    ``C:/x`` and UNC ``//host``: an absolute literal names nobody's workspace in
    particular, and the gate has no workspace root to check it against, so it never
    qualifies for the carve-out either.
    """
    text = literal.replace("\\", "/").lower()
    absolute = text.startswith("/") or bool(re.match(r"^[a-z]:(/|$)", text))
    segments: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part != "..":
            segments.append(part)
        elif segments and segments[-1] != "..":
            segments.pop()
        elif not absolute:
            segments.append(part)  # a leading ".." that cannot be resolved away
    return segments, absolute


def _literal_path(node) -> str | None:
    """The path a node names when it is statically readable, else ``None``.

    Handles the three ways an analyst writes a constant path — a plain string,
    ``Path("a", "b")`` / ``str(...)`` wrappers, ``os.path.join("a", "b")``, and the
    ``Path("a") / "b"`` operator. An f-string or a variable returns ``None``, which keeps
    the write at ``ask``: the carve-out is only for paths the gate can actually read.

    The literal is used to CHOOSE a band and is never carried into a finding."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name in ("Path", "PurePath", "PosixPath", "WindowsPath", "join") and node.args:
            parts = [_literal_path(a) for a in node.args]
            if all(p is not None for p in parts):
                return "/".join(p.rstrip("/\\") for p in parts if p is not None)
        if name == "str" and len(node.args) == 1:
            return _literal_path(node.args[0])
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = _literal_path(node.left), _literal_path(node.right)
        if left is not None and right is not None:
            return f"{left.rstrip('/')}/{right}"
    return None


def _scan_sql_call(node, attr, line, bindings=None) -> list[Finding]:
    """SQL handed straight to a database — the call site says it is SQL, so no structural
    second opinion is needed on the text, and no band ceiling applies.

    Findings are reported at the line of the RESOLVED text rather than the call, so a
    query written on one line and executed on the next produces ONE finding pointing at
    the query, not two saying the same thing."""
    if attr not in _SQL_CALL_ATTRS:
        return []
    findings: list[Finding] = []
    for target in _sql_arguments(node, bindings or {}):
        text = _string_value(target)
        if text:
            at = getattr(target, "lineno", line)
            findings += [_finding(at, kind) for kind in _sql_kinds(text)]
    return findings


def _scan_egress_call(node, called: str | None, line) -> list[Finding]:
    """``urlopen`` is the one egress call whose band depends on its arguments: fetching a
    URL is a read, but handing it a body is a send. ``called`` is the attribute name or
    the bare name, since ``from urllib.request import urlopen`` is how it is usually
    written."""
    if called == "urlopen" and (len(node.args) >= 2 or _kwarg(node, ("data",)) is not _MISSING):
        return [_finding(line, "sends_data")]
    return []


def _scan_getattr(node, name, funcs, aliases, line) -> list[Finding]:
    """``getattr(os, action)()`` — a module attribute chosen at run time is a way to call
    anything, so it belongs with ``eval``. Narrow on purpose: ``getattr(df, column)`` is
    everyday analysis, so only a module-like first argument counts.

    A LITERAL second argument is not dynamic — it is just a long spelling of an attribute
    access — but that is precisely why it cannot simply be waved through: ``getattr(os,
    "remove")(p)`` deletes a file. So it is classified as the attribute access it spells,
    through the same table :func:`_scan_call` uses. ``getattr(os, "getcwd")`` is in no
    table and stays clean, exactly as writing ``os.getcwd`` would."""
    if name != "getattr" or "getattr" in funcs or len(node.args) < 2:
        return []
    target, wanted = node.args[0], node.args[1]
    root = target.id if isinstance(target, ast.Name) else None
    if root is None:
        return []
    if not (root in aliases or aliases.get(root, root).split(".")[0] in _KNOWN_MODULES):
        return []
    module = aliases.get(root, root).split(".")[0]
    if isinstance(wanted, ast.Constant) and isinstance(wanted.value, str):
        kind = _module_kind(module, wanted.value)
        return [_finding(line, kind)] if kind else []
    return [_finding(line, "dynamic_code")]


# ---------------------------------------------------------------------------
# String heuristics: SQL text and install commands
# ---------------------------------------------------------------------------


def _sql_bindings(tree) -> dict[str, list]:
    """``name -> [string expressions it is bound to]`` within this cell.

    The ONE hop that matters: a notebook almost always writes its query to a name and
    hands it over on the next line (``query = \"\"\"DELETE FROM sales\"\"\"`` …
    ``con.execute(query)``). Without this the loose-literal ceiling would take that
    query down to ``ask`` — trading a false-positive class for a coverage hole. Every
    binding of a name is kept, so a name reassigned twice is resolved through both
    (more findings, never fewer). Same shape as the buffer carve-out's dataflow: one
    hop, same cell, nothing more."""
    binds: dict[str, list] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = _unwrap_sql(node.value)
        if _string_value(value) is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                binds.setdefault(target.id, []).append(value)
    return binds


def _unwrap_sql(node, depth: int = 0):
    """``node`` with any :data:`_SQL_PASSTHROUGH` wrappers peeled off (bounded)."""
    while (
        depth < 4
        and isinstance(node, ast.Call)
        and _call_name(node.func) in _SQL_PASSTHROUGH
        and len(node.args) == 1
    ):
        node, depth = node.args[0], depth + 1
    return node


def _sql_arguments(call, bindings) -> list:
    """Every expression a SQL-slot call could be receiving its query text in.

    ALL positional arguments, not just the first: ``cur.execute(conn, "DROP TABLE t")``
    puts the SQL second in some drivers, and the parameter slot beside a real query holds
    a tuple or a connection, not SQL-shaped text. Each is unwrapped and then resolved one
    hop through ``bindings``."""
    out = []
    for arg in list(call.args) + [k.value for k in call.keywords if k.arg in ("query", "sql")]:
        target = _unwrap_sql(arg)
        if isinstance(target, ast.Name):
            out.extend(bindings.get(target.id, ()))
        elif _string_value(target) is not None:
            # Only a STRING expression is vouched for. A parameter list beside a real
            # query (`execute("SELECT 1", [value])`) is data, so a value inside it that
            # happens to read like SQL must not inherit the call site's floor band.
            out.append(target)
    return out


def _sql_slot_literals(tree, bindings) -> set[int]:
    """``id()``\\ s of every node a KNOWN SQL slot will receive.

    Marking them keeps the string pass and the call-site pass in agreement — otherwise
    the string pass would add a second, CAPPED finding beside the call site's real one.
    Whole subtrees are marked, so an f-string's own segments (which the walk visits
    separately) are covered by the same vouching."""
    slots: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) in _SQL_CALL_ATTRS:
            for target in _sql_arguments(node, bindings):
                slots.update(id(sub) for sub in ast.walk(target))
    return slots


def _scan_strings(tree, prose: set[int], sql_slots: set[int] | None = None) -> list[Finding]:
    """The two heuristics that read string CONTENT, skipping every prose slot.

    A LOOSE literal — one no call site vouches for — is held to two extra conditions and
    then capped at ``ask`` (:func:`_cap_loose`). A list of strings is joined before the
    install scan only, so ``[sys.executable, "-m", "pip", "install", "polars"]`` reads as
    the command it becomes."""
    sql_slots = sql_slots or set()
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if id(node) in prose:
            continue
        line = getattr(node, "lineno", 1)
        if isinstance(node, (ast.List, ast.Tuple)):
            parts = [
                e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            joined = " ".join(parts)
            if joined and _INSTALL_RE.search(joined):
                findings.append(_finding(line, "installs_package"))
            continue
        text = _string_value(node)
        if not text:
            continue
        if _INSTALL_RE.search(text):
            findings.append(_finding(line, "installs_package"))
        if id(node) in sql_slots:
            findings += [_finding(line, kind) for kind in _sql_kinds(text)]
        else:
            findings += [
                _finding(line, _cap_loose(kind))
                for kind in _sql_kinds(text, require_structure=True)
            ]
    return findings


def _cap_loose(kind: str) -> str:
    """The band ceiling for SQL nobody handed to a database.

    Only a string in a KNOWN SQL SLOT may reach the floor. A loose literal that merely
    LOOKS like SQL caps at ``ask``, because the alternative is an arms race no filter
    wins: ``button_text = "Delete from list"`` is ordinary English that opens with a SQL
    verb, and it must never reach the band that cannot be downgraded. The ceiling is a
    rule rather than another heuristic, and it costs one band in the rare case of real
    DDL written to a name this cell never uses."""
    return "changes_database" if kind == "destroys_rows" else kind


def _string_value(node) -> str | None:
    """The statically-readable text of a string expression.

    An f-string contributes only its LITERAL segments (joined by a space so nothing fuses
    across the hole): ``f"DELETE FROM {table}"`` still reads as a DELETE, while the
    interpolated value — which is where data would be — is simply absent."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return " ".join(parts) if parts else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_value(node.left), _string_value(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.Call) and _call_name(node.func) == "join" and len(node.args) == 1:
        # `" ".join(["DROP", "TABLE", "t"])` — constant folding, not dataflow: the
        # separator and every element have to be literals, so the result is as readable
        # as the string it spells and nothing computed can enter it.
        separator = _string_value(node.func.value) if isinstance(node.func, ast.Attribute) else None
        elements = node.args[0]
        if separator is not None and isinstance(elements, (ast.List, ast.Tuple)):
            parts = [_string_value(e) for e in elements.elts]
            if parts and all(p is not None for p in parts):
                return separator.join(parts)
    return None


def _sql_kinds(text: str, *, require_structure: bool = False) -> list[str]:
    """Kinds for every statement in ``text``. Cheap and conservative by design: strip the
    quoted spans and comments, split on ``;``, look at the FIRST keyword of each
    statement. Deliberately NOT a SQL parser — a parser would have to be right about
    every dialect to be useful, while a first-keyword rule is right about the handful of
    verbs the bands actually turn on."""
    kinds: list[str] = []
    stripped = _SQL_STRIP_RE.sub(" ", text)
    for statement in (_truncate_at_stray_quote(s) for s in stripped.split(";")):
        kind = _statement_kind(statement, require_structure=require_structure)
        if kind:
            kinds.append(kind)
    return kinds


def _truncate_at_stray_quote(statement: str) -> str:
    """One statement, cut at the first quote that survived the strip — the fail-closed
    shape of :func:`mooring.ai.notebookindex.ast_walk._strip_sql_text`.

    An unbalanced quote is the interesting case: the strip regex needs a closing quote,
    so it does not match, and everything after the stray quote — including whatever prose
    followed it — would otherwise still be scanned for keywords. That is how
    ``"Don't DROP TABLE without a backup"`` gets read as DDL.

    It is applied PER STATEMENT rather than to the whole string, which is the one place
    this module must not copy ast_walk exactly: there, discarding text can only lose a
    table name, while here it could discard a whole `DROP` that follows the stray quote.
    Cutting inside the statement that carries the quote keeps the prose guard without
    ever letting one malformed statement hide the next."""
    cut = min((i for i in (statement.find("'"), statement.find('"'), statement.find("`"))
               if i >= 0), default=-1)
    return statement if cut < 0 else statement[:cut]


def _statement_kind(statement: str, *, require_structure: bool) -> str | None:
    words = [w.lower() for w in _WORD_RE.findall(statement)]
    if not words or words[0] not in _SQL_FIRST_KEYWORDS:
        return None
    first = words[0]
    if require_structure and not (set(words) & _SQL_STRUCTURE and _loose_shape_ok(first, words)):
        return None
    if first == "with":
        # A CTE is read-only until it wraps something that writes. Take the most severe
        # verb mentioned; `WITH … SELECT` (the overwhelmingly common shape) has none.
        for candidate in _SQL_SEVERITY:
            if candidate in words:
                first = candidate
                break
        else:
            return None
    return _verb_kind(first, words)


def _loose_shape_ok(first: str, words: list[str]) -> bool:
    """Does a LOOSE string literal really open with a SQL statement?

    Applied only to strings whose call site tells us nothing (anything handed to
    ``.execute`` / ``mo.sql`` is SQL by construction and skips this). English sentences
    start with these verbs all the time — "DROP is how you remove a table", "UPDATE
    complete", "Insert your name here" — so the word after the verb has to be one SQL
    could actually put there. The cost is that an exotic loose literal (``TRUNCATE
    staging`` without ``TABLE``) is missed unless it reaches a cursor in the same cell;
    the benefit is that ordinary prose never prompts."""
    if set(words[:3]) & _ENGLISH_HINTS:
        return False
    expected = _SQL_SECOND_WORD.get(first)
    if expected is not None and not (len(words) > 1 and words[1] in expected):
        return False
    if first == "insert":
        # INSERT INTO t VALUES … / … SELECT … — the tail an English "insert into" lacks.
        return bool({"values", "select", "from"} & set(words))
    if first == "merge":
        return "using" in words
    if first == "update":
        return "set" in words
    if first == "grant":
        return "on" in words
    return True  # read-only verbs and CREATE, which carry their own rules below


def _verb_kind(first: str, words: list[str]) -> str | None:
    """The band-bearing kind for one statement's leading verb.

    ``DELETE`` splits on whether a ``WHERE`` token survives the strip: without one it
    empties the table, which is the same irreversible loss as ``TRUNCATE``. ``CREATE``
    without ``OR REPLACE`` creates something new and destroys nothing, so it stays clean —
    the same create-is-not-destroy rule the new-file carve-out applies to files."""
    if first in _SQL_DESTROY_FIRST:
        return "destroys_rows"
    if first == "delete":
        return "changes_database" if "where" in words else "destroys_rows"
    if first == "create":
        return "changes_database" if ("or" in words and "replace" in words) else None
    if first == "copy":
        # `COPY (SELECT …) TO 'out.parquet'` writes a FILE; `COPY t FROM 'x.csv'` loads
        # rows into the database. Same verb, two different bands.
        if "to" in words:
            return "overwrites_file"
        return "changes_database" if "from" in words else None
    if first in ("install", "force"):
        return "installs_package" if first == "install" or "install" in words else None
    if first == "attach":
        return "changes_database"
    if first in _SQL_CHANGE_FIRST:
        return "changes_database"
    return None
