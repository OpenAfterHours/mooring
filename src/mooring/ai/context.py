"""Discover the team-authored context mooring feeds the copilot, value-minimised.

ONE place reads the opt-in ``context/`` folder and turns it into what the chat's
single choke point (:func:`mooring.ai.chat.build_system_context`) injects:

* ``context/instructions.md`` — free-text guidance, sent verbatim (the
  ``copilot-instructions.md`` equivalent). It is the residual leak vector: a
  human can type anything here, so it is opt-in, capped, frontmatter-stripped,
  and secret-scanned — and **withheld entirely if a high-confidence secret is
  found**, rather than sent.
* the per-domain data dictionary (:mod:`mooring.ai.datadictionary`), already
  reduced to the five-slot allowlist; here we additionally scan each description
  (the one free-text slot) and **drop** any that trips the scanner.

The scanner is best-effort defence in depth, never the guarantee — see
:mod:`mooring.ai.secrets`. With the feature disabled, :func:`discover_context`
returns an empty context and nothing changes versus today.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from mooring.ai import pii, secrets
from mooring.ai.datadictionary import DictionaryIndex, Table, load_index, merge_indexes

_INSTRUCTIONS = "instructions.md"
_DEFAULT_MAX_KB = 256
# Aggregate cap on the COMBINED instructions across all folders (a prompt-size guard on
# top of the per-file cap). Each folder's text is already secret-scanned+withheld before
# the join, so trimming here only ever drops clean, already-vetted text.
_MAX_TOTAL_INSTR_KB = 512


@dataclass(frozen=True)
class Finding:
    source: str  # file path or qualified table name
    where: str  # "line 12" or "description"
    kind: str


@dataclass
class RepoContext:
    instructions: str = ""
    index: DictionaryIndex = field(default_factory=DictionaryIndex)
    loaded_files: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()

    @classmethod
    def empty(cls) -> "RepoContext":
        return cls()

    def is_empty(self) -> bool:
        return not self.instructions and self.index.is_empty()


def discover_context(
    workspace: Path,
    *,
    context_dir: str = "context",
    enabled: bool = False,
    max_kb: int = _DEFAULT_MAX_KB,
) -> RepoContext:
    """Read ``<workspace>/<context_dir>`` into a :class:`RepoContext`.

    Returns an empty context (identical-to-today behaviour) when ``enabled`` is
    False — the opt-in gate. Never raises for a bad/oversized file; it is simply
    omitted, with the reason recorded where useful.
    """
    if not enabled:
        return RepoContext.empty()

    workspace = Path(workspace)
    findings: list[Finding] = []
    loaded: list[str] = []

    instructions = _read_instructions(workspace, context_dir, max_kb)
    if instructions:
        rel = f"{context_dir}/{_INSTRUCTIONS}"
        # Scan once, then partition into HIGH-confidence (a secret or a checksum-
        # validated card/IBAN/NHS) and shape-only (email/NINO).
        pii_hits = pii.scan(instructions)
        hard = secrets.scan(instructions) + [h for h in pii_hits if h.kind in pii.CHECKSUM_KINDS]
        soft = [h for h in pii_hits if h.kind not in pii.CHECKSUM_KINDS]
        if hard:
            # Withhold the WHOLE file, but record EVERY finding (hard and soft) so the
            # value-free report never understates what the withheld file contained.
            findings += [Finding(rel, f"line {h.line}", h.kind) for h in hard + soft]
            instructions = ""  # withheld
        else:
            if soft:
                # A shape-only email/NINO drops just its own line, so a team contact
                # address never silently deletes the whole context.
                findings += [Finding(rel, f"line {h.line}", h.kind) for h in soft]
                instructions = _drop_lines(instructions, {h.line for h in soft})
            if instructions:  # only "loaded" if some content actually survives to send
                loaded.append(rel)

    index = load_index(workspace, context_dir)
    index, dict_findings = _scrub_index(index)
    findings += dict_findings
    loaded += [r.path for r in index.reports if not r.error]

    return RepoContext(
        instructions=instructions,
        index=index,
        loaded_files=tuple(loaded),
        findings=tuple(findings),
    )


def discover_contexts(
    workspace: Path,
    dirs,
    *,
    enabled: bool = False,
    max_kb: int = _DEFAULT_MAX_KB,
) -> RepoContext:
    """Read SEVERAL context folders into one merged :class:`RepoContext`.

    Each folder is read by the single-dir :func:`discover_context` primitive, so
    every privacy-critical guard — the per-file whole-file hard-withhold, the soft
    per-line PII drop, the per-file ``max_kb`` cap, and the path-escape confinement
    — runs INDEPENDENTLY per folder (one poisoned folder can neither blank a clean
    sibling nor escape its own withhold). The surviving parts are then merged:
    instructions concatenated in a stable sorted-folder order (each behind a
    value-free ``<!-- context: <dir>/instructions.md -->`` banner for attribution),
    dictionary indexes merged first-folder-wins with collision reports
    (:func:`mooring.ai.datadictionary.merge_indexes`), and loaded_files/findings
    tuple-concatenated. The result keeps the single ``instructions``/``index`` shape
    so the egress choke point needs no change. A single folder is byte-identical to
    calling :func:`discover_context` directly. Returns empty when ``enabled`` is
    False — the opt-in gate.
    """
    if not enabled:
        return RepoContext.empty()
    # Sort+dedupe so the assembled system prompt is deterministic regardless of the
    # order the caller resolved the folders in (keeps the "matches today" snapshots
    # stable and gives first-folder-wins a well-defined winner).
    ordered = sorted({str(d).strip().strip("/") for d in dirs if str(d).strip().strip("/")})
    if not ordered:
        return RepoContext.empty()
    parts = [discover_context(workspace, context_dir=d, enabled=True, max_kb=max_kb) for d in ordered]
    return _merge_repo_contexts(list(zip(ordered, parts)))


def _merge_repo_contexts(labelled: "list[tuple[str, RepoContext]]") -> RepoContext:
    """Fold ``(dir, RepoContext)`` pairs (already sorted) into one RepoContext."""
    instr_parts = [(d, p.instructions) for d, p in labelled if p.instructions]
    if len(instr_parts) == 1:
        instructions = instr_parts[0][1]  # single folder: byte-identical, no banner
    elif instr_parts:
        instructions = "\n\n".join(
            f"<!-- context: {d}/{_INSTRUCTIONS} -->\n{txt}" for d, txt in instr_parts
        )
        cap = _MAX_TOTAL_INSTR_KB * 1024
        if len(instructions.encode("utf-8")) > cap:
            # Safe to trim: every folder's text was already secret-scanned (and withheld
            # whole if it tripped) BEFORE the join, so only clean text can be cut here.
            instructions = instructions.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
            instructions += "\n\n[trimmed: combined team instructions exceeded the size cap]"
    else:
        instructions = ""
    index = merge_indexes([p.index for _, p in labelled])
    loaded = tuple(f for _, p in labelled for f in p.loaded_files)
    findings = tuple(f for _, p in labelled for f in p.findings)
    return RepoContext(
        instructions=instructions,
        index=index,
        loaded_files=loaded,
        findings=findings,
    )


def _read_instructions(workspace: Path, context_dir: str, max_kb: int) -> str:
    target = (workspace / context_dir / _INSTRUCTIONS).resolve()
    try:
        target.relative_to(workspace.resolve())  # reject escapes / symlinks out
    except ValueError:
        return ""
    if not target.is_file():
        return ""
    try:
        text = target.read_text("utf-8", errors="replace")
    except OSError:
        return ""
    text = _strip_frontmatter(text)
    cap = max(0, max_kb) * 1024
    if cap and len(text.encode("utf-8")) > cap:
        text = text.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
        text += "\n\n[trimmed: instructions exceeded the size cap]"
    return text.strip()


def _drop_lines(text: str, linenos: set[int]) -> str:
    """Return ``text`` with the 1-based ``linenos`` removed (a per-line redaction)."""
    kept = [ln for i, ln in enumerate(text.splitlines(), start=1) if i not in linenos]
    return "\n".join(kept).strip()


def _desc_kind(desc: str) -> str | None:
    """First secret-or-PII kind in a free-text description, or None if clean."""
    hits = secrets.scan(desc) or pii.scan(desc)
    return hits[0].kind if hits else None


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 1)
            return text[nl + 1 :] if nl != -1 else ""
    return text


def _scrub_index(index: DictionaryIndex) -> tuple[DictionaryIndex, list[Finding]]:
    """Drop any column/table description that trips the secret OR PII scanner (the
    one free-text slot), recording a value-free finding for each."""
    findings: list[Finding] = []
    new_tables: list[Table] = []
    for table in index.tables:
        tdesc = table.description
        if tdesc:
            kind = _desc_kind(tdesc)
            if kind:
                findings.append(Finding(table.qualified, "description", kind))
                tdesc = ""
        cols = []
        for col in table.columns:
            cdesc = col.description
            if cdesc:
                kind = _desc_kind(cdesc)
                if kind:
                    findings.append(Finding(f"{table.qualified}.{col.name}", "description", kind))
                    cdesc = ""
            cols.append(replace(col, description=cdesc) if cdesc != col.description else col)
        new_tables.append(replace(table, description=tdesc, columns=tuple(cols)))
    return replace(index, tables=tuple(new_tables)), findings
