"""The push guard: scan bytes leaving for the team repo, before they leave.

All of mooring's privacy machinery watches the AI channel; this module points
the SAME high-precision detectors (:mod:`mooring.ai.secrets`,
:mod:`mooring.ai.pii` — stdlib-only, no copilot extra needed) at the more
damaging channel: the push itself. Because analysts have no git, mooring is the
only write path into the shared repo, so a gate at the push seam covers every
push the team makes.

The detectors answer "do these bytes EXPOSE something?". An outgoing notebook
raises a second question that no exposure scan asks — "what will these bytes
DO when a teammate opens them?" — and :mod:`mooring.ai.codeguard` (the Apply
gate's classifier) already answers it. Pointing it at the push seam is the same
move again: a destructive cell arriving in the SHARED repo is arguably worse
than a leaked secret, because every teammate pulls it and marimo runs it with
their credentials. Two scopes keep that reuse honest, both measured rather than
assumed: only ``floor``-band findings gate a push, and only marimo NOTEBOOKS are
classified at all (:func:`code_findings` and :data:`_IS_NOTEBOOK` carry the
numbers). The classifier was calibrated for one notebook cell, and it is used
here on exactly that.

Both scopes are tighter than the Apply gate's, and the reason is a difference in
FATIGUE PROFILE that is easy to miss because the two share a classifier. The
Apply gate fires on a NEW cell: a one-time question, answered once, gone. A push
finding is a STANDING PROPERTY of a file. A notebook with one legitimate
``os.remove(tmp)`` in a cleanup cell is withheld on EVERY push, forever, until
someone retires it — a recurring tax, not a question. That is precisely the
mechanism by which a shared dialog gets clicked through, and this dialog also
carries the secret and PII findings, so the cost of a standing false positive
here is paid by detectors that have nothing to do with the classifier. Hence:
narrow scope, floor only, and a release valve that has to actually work.

``mooring: push-ok`` is that valve, and its usability is a feature requirement,
not a nicety. It is line-scoped, and the line a finding reports is the line the
call STARTS on — so a call spanning several lines is retired by a comment after
its opening parenthesis, which is where a reader would put it::

    subprocess.run(  # mooring: push-ok
        ["pack", "--all"],
        check=True,
    )

The valve has one known hole: SQL in a multi-line triple-quoted string reports
the line the STRING opens on, and the only text there is inside the literal, so
the marker would land in the query itself. Rewrite such a statement to a
single-line string (or bind it one hop above and mark the binding, which the
classifier follows) — do not paste the marker into SQL. Pinned by
``tests/test_pushguard.py::test_push_ok_pragma_reaches_a_multi_line_call`` and
its ``…_cannot_reach_a_triple_quoted_sql_statement`` sibling.

This module is the ORCHESTRATOR — candidate policy (text extensions, size
cap), the ``mooring: push-ok`` line pragma, the conservative raw-data
heuristic, and the per-file confirm token — while the detectors stay where
they live. It is deliberately a *second consumer* of the scanners, not a
change to the AI channel: ``ai/egress.py`` and its pinned tests are untouched,
and ``codeguard`` is read-only here (the Apply gate keeps its own bands).

Enforcement rides :func:`mooring.sync.push`'s injected ``guard_fn`` (the
``snapshot_fn`` idiom), so the L2 sync core never imports the scanners; the
adapters build the guard with :func:`make_guard` and surface withheld files
with a warn-and-confirm flow. Like the detectors themselves this is
**defence in depth, never a guarantee** — a clean scan does not mean a file
is value-free (see docs/admins/ai-privacy.md).

THREE guards now ride that one seam, with deliberately DIFFERENT override
rules — none of them may be folded into another:

* **content** (this module, :func:`make_guard`) — acknowledgeable in warn mode,
  refused under ``[guard] push = "block"``; runs on push AND propose.
* **dependency change** (:func:`make_lock_guard`) — ALWAYS acknowledgeable,
  block mode included; runs on push AND propose.
* **policy propose-only** (:func:`mooring.policy.make_propose_gate`) — NEVER
  acknowledgeable, no token exists; runs on DIRECT push only, because Propose
  is the road it is pointing at.

:func:`mooring.policy.compose_guards` runs them behind the single ``guard_fn``
sync expects, each keeping its own ``collected`` map and so its own tokens —
which is exactly what lets one seam carry three override policies. The deps
gate is separate rather than another detector inside :func:`make_guard`
because a team's ``[guard] push = "block"`` is a policy about sensitive
CONTENT and must not silently become a wall around lock files.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from mooring import notebook_template, sweep
from mooring.ai import codeguard, pii, secrets

# A reviewed false positive can be retired by putting this marker on the line —
# the push-scope sibling of ai/pii.py's "mooring: pii-ok" (which keeps working
# here too, but only for PII findings; this one silences the push guard for
# every detector on that line, without changing what the AI channel scans).
PUSH_OK_MARKER = "mooring: push-ok"

# Only text-like files are scanned; anything else passes through untouched
# (a regex scan of binary bytes is noise). ".platform" is PBIP's required
# dot-named metadata file (see sync.KEEP_DOT_NAMES).
TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".csv", ".tsv",
     ".sql", ".ini", ".cfg", ".tmdl", ".bim", ".pbip", ".pbir"}
)
TEXT_NAMES = frozenset({".platform"})

# Detector scan cap: beyond this the regex pass is skipped (the raw-data
# heuristic below still runs — a huge file is exactly what it exists to flag).
_MAX_SCAN_BYTES = 4 * 1024 * 1024

# The raw-data heuristic: a tabular file with at least this many
# delimiter-consistent rows looks like a data export, not an analysis asset.
_ROW_THRESHOLD = 1000
_TABULAR_SUFFIXES = frozenset({".csv", ".tsv"})

# Only Python carries code the classifier can read. (".sql" is text-like and often
# destructive, but a .sql file is a document until something runs it — codeguard
# classifies a Python cell, and guessing at a bare script's dialect is a different job.)
_CODE_SUFFIX = ".py"

# …and only a NOTEBOOK, not every .py. codeguard was specified and tuned against one
# marimo cell; a plain helper module is a different shape, and MEASURED against real
# module code the difference is not subtle:
#
#   marimo notebook sources (70 harvested from this repo)   0.0% would be withheld
#   src/mooring/**  (127 systems modules)                  27.6%
#   tests/**        (113)                                  18.6%
#   scripts/**      (7)                                    71.4%
#
# Those module hits are not misfires — ~97% are true `deletes_files` / `runs_program` by
# the detector table. They are just what module code IS: 47% of them are `Path.unlink()`
# and 18% are `os.replace(tmp, path)`, which is the CORRECT atomic-save idiom. Withholding
# a quarter of every helper module for writing files safely would make this dialog — the
# one that also carries the secret and PII findings — noise that gets clicked through.
# The classifier was RIGHT and the scope was wrong: it is calibrated for one marimo cell,
# so it is applied to exactly that. That also lands it where the copilot's Apply writes,
# so the Apply gate and this guard cover the same artifact end to end.
#
# THE GAP THIS LEAVES, so nobody has to rediscover it: a hand-written helper module
# pushes unscanned — including a synced `codelib/`, which is a first-class synced thing
# (see ai/codelib/), imported and run by every teammate. Shared helper code gets NO
# destructive-code check. That is a real hole, stated plainly in docs/admins/
# configuration.md, and still a far better trade than a guard nobody reads.
#
# Do not widen this to every `.py` without re-running the measurement; the two tests
# named below carry the numbers so a future "why is this notebook-only?" cannot quietly
# revert it. See tests/test_pushguard.py::test_a_plain_helper_module_is_never_classified
# and ::test_the_atomic_save_idiom_is_the_reason_modules_are_out_of_scope.
_IS_NOTEBOOK = notebook_template.is_marimo_app

# WHICH codeguard bands gate a push. FLOOR ONLY, and the threshold is the whole design:
# `ask` says "Undo leaves something behind" — a statement about the ANALYST's local
# notebook at Apply time, not about what a teammate inherits. `overwrites_file` fires on
# `df.to_csv("out.csv")`, which is what an analysis notebook IS; `changes_database` and
# `sends_data` fire on the warehouse write and the API call that half of them do for a
# living. Gating a push on those would withhold most notebooks on most pushes, and a
# guard that fires on ordinary work is turned off (or clicked through) within a day —
# taking the secret and PII findings with it, since they share this dialog. `floor` is
# the band that survives that test: an irreversible effect on someone else's machine is
# rare, and it is exactly what a second pair of eyes is for.
_PUSH_BLOCKING_BANDS = frozenset({codeguard.BAND_FLOOR})


@dataclass(frozen=True)
class Finding:
    line: int  # 1-based line the finding sits on (1 for whole-file findings)
    kind: str  # value-free human label — never the matched substring


def _is_text(rel_path: str) -> bool:
    p = Path(rel_path)
    return p.suffix.lower() in TEXT_SUFFIXES or p.name in TEXT_NAMES


def _looks_like_data_export(text: str, suffix: str) -> int:
    """The row count when ``text`` looks like a bulk tabular export, else 0.

    Deliberately conservative (a false positive here is corrosive): fires only
    for .csv/.tsv, only past a row threshold, and only when the first rows are
    delimiter-consistent (same field count, at least three fields) — a prose
    .csv or a small lookup table never trips it.
    """
    if suffix not in _TABULAR_SUFFIXES:
        return 0
    sep = "\t" if suffix == ".tsv" else ","
    lines = text.splitlines()
    rows = len(lines)
    if rows < _ROW_THRESHOLD:
        return 0
    sample = [ln for ln in lines[:50] if ln.strip()]
    if len(sample) < 10:
        return 0
    fields = sample[0].count(sep) + 1
    if fields < 3:
        return 0
    if any(ln.count(sep) + 1 != fields for ln in sample):
        return 0
    return rows


def _retired(lines: list[str], line: int) -> bool:
    """Does ``line`` (1-based) carry the ``mooring: push-ok`` pragma?"""
    return 1 <= line <= len(lines) and PUSH_OK_MARKER in lines[line - 1]


def code_findings(rel_path: str, data: bytes | None) -> list[codeguard.Finding]:
    """Every non-clean :mod:`mooring.ai.codeguard` finding for an outgoing NOTEBOOK.

    The classifier's OWN findings — ``codeguard.Finding``, so each keeps its ``band``,
    ``kind`` slug and analyst-facing ``label``. :func:`scan_text` narrows this to the
    push-blocking bands (:data:`_PUSH_BLOCKING_BANDS`) and flattens it onto the local
    ``(line, kind)`` shape; the reviewer inbox (:mod:`mooring.app.reviews`) wants the
    bands intact, which is why this returns them and lives here rather than inside
    ``scan_text``. Exposed for both, so the ``mooring: push-ok`` pragma is honoured in
    ONE place and the same verdict shows on both sides of a proposal.

    Candidates are a ``.py`` that :func:`mooring.notebook_template.is_marimo_app` calls a
    notebook — see :data:`_IS_NOTEBOOK` for the measurement behind that scope, which is
    the load-bearing decision in this whole detector. The same predicate already decides
    what ``app/sweep_run`` and ``app/notebooks`` treat as a notebook, so "notebook" means
    one thing across the product.

    Returns nothing for a deletion (``data is None`` — a content guard publishes nothing
    when it removes a file), a non-Python path, a plain module, or a file past the scan
    cap: parsing multiple megabytes of "Python" to classify it is not a cost worth paying
    at push time, and :func:`scan_text` already flags an over-cap text file on its own.

    Value-free by construction: ``codeguard`` never interpolates matched code into a
    finding, and nothing is added here.
    """
    if data is None or Path(rel_path).suffix.lower() != _CODE_SUFFIX:
        return []
    if len(data) > _MAX_SCAN_BYTES:
        return []
    text = data.decode("utf-8", "replace")
    if not _IS_NOTEBOOK(text):
        return []
    lines = text.splitlines()
    # A notebook that does not parse is `unparseable`/ask, so it drops out at the band
    # filter rather than gating a push — "we could not read this" is worth telling a
    # reviewer, and never worth withholding someone's work over.
    return [f for f in codeguard.scan_code(text).findings if not _retired(lines, f.line)]


def scan_text(rel_path: str, data: bytes | None) -> list[Finding]:
    """Value-free findings for one outgoing file (empty when clean or binary).

    Runs the secret + structured-PII detectors over text-like files, adds the
    destructive-code classifier's ``floor``-band findings for a marimo notebook
    (:func:`code_findings`), drops any finding whose line carries the
    ``mooring: push-ok`` pragma, and adds the raw-data heuristic for tabular
    files. Read-only: never modifies ``data``.

    The three detectors merge into ONE list on purpose: they all answer "should
    these exact bytes go to the team?", they are all retired by the same pragma,
    and they all bind into the same per-file :func:`file_token`, so a newly
    appearing code finding invalidates an old content acknowledgement exactly
    like a newly appearing secret does.

    ``data is None`` marks a DELETION (sync now offers every candidate to the
    guard, not only the ones that upload bytes). This guard is a CONTENT guard,
    so a deletion is clean by construction — it publishes nothing. The path
    rules that DO care about a deletion live in :mod:`mooring.policy`.
    """
    if data is None or not _is_text(rel_path):
        return []
    text = data.decode("utf-8", "replace")
    findings: list[Finding] = []
    if len(data) <= _MAX_SCAN_BYTES:
        merged = [(f.line, f.kind) for f in secrets.scan(text)]
        merged += [(f.line, f.kind) for f in pii.scan(text)]
        # The classifier's LABEL is the kind here ("Deletes files or folders"), so a
        # code finding reads in the dialog exactly like a secret one ("line 12: …").
        # The slug stays internal; nothing outside codeguard needs it to gate a push.
        merged += [
            (f.line, f.label)
            for f in code_findings(rel_path, data)
            if f.band in _PUSH_BLOCKING_BANDS
        ]
        lines = text.splitlines()
        for line, kind in sorted(set(merged)):
            if _retired(lines, line):
                continue  # a reviewed false positive, retired in the diff
            findings.append(Finding(line=line, kind=kind))
    else:
        # No silent caps: a text file too big to scan is flagged instead of
        # waved through — a multi-MB "text" file heading for the shared repo is
        # exactly the data-dump shape the guard exists to question.
        mb = len(data) // (1024 * 1024)
        findings.append(
            Finding(line=1, kind=f"large text file (~{mb} MB) — too big to scan; review it")
        )
    rows = _looks_like_data_export(text, Path(rel_path).suffix.lower())
    if rows:
        findings.append(Finding(line=1, kind=f"bulk data export (~{rows} rows)"))
    return findings


def file_token(
    rel_path: str, data: bytes | None, findings: list[Finding], *, extra: str = ""
) -> str:
    """A stateless per-file confirm token binding the exact findings set to the
    exact bytes: a confirmed token stops matching the moment the file changes or
    a new finding appears, so an old confirm can never cover new exposure.

    ``extra`` mixes in state the findings' WORDING does not fully determine. Content
    findings need none — the (line, kind) list is the finding. The dependency gate does:
    it collapses a whole sweep to a count, and two different results can word themselves
    the same, so it binds to a digest of the result instead (see sweep.LockGate)."""
    h = hashlib.sha256()
    h.update(rel_path.encode("utf-8"))
    h.update(hashlib.sha256(data if data is not None else b"").digest())
    for f in sorted(findings, key=lambda x: (x.line, x.kind)):
        h.update(f"{f.line}:{f.kind}".encode())
    if extra:
        h.update(b"\x00")
        h.update(extra.encode("utf-8"))
    return h.hexdigest()[:16]


def describe(findings: list[Finding]) -> list[str]:
    """Human, value-free one-liners ("line 12: GitHub token") for result lines."""
    return [f"line {f.line}: {f.kind}" for f in findings]


def make_guard(allowed_tokens: frozenset[str] | set[str] = frozenset()):
    """Build a ``guard_fn`` for :func:`mooring.sync.push` / ``propose``.

    The returned ``guard_fn(rel_path, data)`` scans the exact upload bytes and
    returns value-free description strings — sync withholds the file when the
    list is non-empty. A file whose :func:`file_token` is in ``allowed_tokens``
    was explicitly acknowledged (warn mode's "Push anyway") and passes.

    Also returns ``collected``: ``rel_path -> {"findings", "token"}`` for every
    withheld file, from which the adapters build the confirm payload.
    """
    collected: dict[str, dict] = {}

    def guard_fn(rel_path: str, data: bytes | None) -> list[str]:
        findings = scan_text(rel_path, data)
        if not findings:
            return []
        token = file_token(rel_path, data, findings)
        if token in allowed_tokens:
            return []
        collected[rel_path] = {"findings": findings, "token": token}
        return describe(findings)

    return guard_fn, collected


def lock_findings(
    workspace, rel_path: str, data: bytes | None, notebooks=None
) -> list[Finding]:
    """The dependency-change gate's findings for one outgoing file, as ``Finding``\\ s —
    the scan half of :func:`make_lock_guard`, exposed so an adapter's
    ``--acknowledge-findings`` path can still SHOW what it is letting through."""
    return [
        Finding(line=line, kind=kind)
        for line, kind in sweep.dependency_findings(workspace, rel_path, data, notebooks)
    ]


def make_lock_guard(
    workspace,
    allowed_tokens: frozenset[str] | set[str] = frozenset(),
    notebooks_fn=None,
):
    """Build a ``guard_fn`` for the DEPENDENCY-CHANGE gate — the second thing worth
    stopping at the push seam.

    The content scanners above ask "do these bytes look sensitive?". This asks a question
    about the same bytes that no scan can answer: ``uv.lock`` is the one file whose push
    changes what *every* teammate's notebooks run against, and nothing else checks that the
    repo still runs afterwards. The verdict comes from the stored verify sweep
    (:func:`mooring.sweep.dependency_findings`), which only counts when it was taken
    against these exact lock bytes.

    Same shape as :func:`make_guard` — a ``(guard_fn, collected)`` pair — but the token
    binds the bytes to a DIGEST OF THE RESULT rather than to the finding's wording, so an
    acknowledgement expires on any change to what the sweep says, not merely on a change
    to how it reads. Deliberately a SEPARATE guard rather than an extra detector inside
    ``make_guard``: the team's ``[guard] push = "block"`` policy is about sensitive
    CONTENT, and must not silently become "you may never push a lock file that breaks
    something" — this gate warns and is always acknowledgeable. Compose the three
    guards on this seam with :func:`mooring.policy.compose_guards`.

    ``notebooks_fn`` is an optional zero-argument callable returning the workspace's
    current notebook list. It is called ONLY when a ``uv.lock`` is actually being pushed
    (enumerating costs a workspace walk), and lets the gate notice a notebook added since
    the sweep instead of falling silent over it.

    Like the policy gate — and unlike the content guard — this fires for a DELETION
    (``data is None``) too. Removing the team's ``uv.lock`` is the most drastic
    environment change there is: everyone stops running against a pinned resolution. The
    NO_LOCK sentinel makes that fall out rather than needing a special case — a sweep
    taken WITH a lock cannot match ``fingerprint(None)``, so the deletion is questioned
    exactly like a rewrite.
    """
    collected: dict[str, dict] = {}

    def guard_fn(rel_path: str, data: bytes | None) -> list[str]:
        if not sweep.is_lock(rel_path):
            return []  # cheap gate first: never walk the workspace for an ordinary file
        notebooks = notebooks_fn() if notebooks_fn is not None else None
        result = sweep.gate(workspace, rel_path, data, notebooks)
        if not result:
            return []
        findings = [Finding(line=line, kind=kind) for line, kind in result.findings]
        token = file_token(rel_path, data, findings, extra=result.digest)
        if token in allowed_tokens:
            return []
        collected[rel_path] = {"findings": findings, "token": token}
        # The kind alone: unlike a content finding there is no line to point at, so
        # "line 1: …" would be noise in the withheld line.
        return [f.kind for f in findings]

    return guard_fn, collected
