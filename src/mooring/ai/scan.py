"""Offline scan-target policy for the AI pre-flight checks.

The file-walk shared by ``mooring ai pii check`` and ``mooring ai dictionary
check``: which files to scan (the ``context/`` folder, the parsed dictionary
files, the notebook sources), how to dedupe and read them, and value-free finding
collection. It lives here rather than in ``cli.py`` so the policy has one tested
home and the hub can reuse it. Findings are value-free ``(rel_path, line, kind)``
tuples — never a matched value.
"""

from __future__ import annotations

from pathlib import Path

from mooring.ai import pii, secrets


def _ctx_dirs(ctx_dirs) -> list[str]:
    """Accept a single context-folder string OR an iterable of them, deduped."""
    dirs = (ctx_dirs,) if isinstance(ctx_dirs, str) else tuple(ctx_dirs)
    return list(dict.fromkeys(d for d in dirs if d))


def scan_pii_targets(
    workspace: Path,
    ctx_dirs,
    folders: tuple[str, ...],
    index,
    notebook_rel: str | None,
    *,
    names: bool = False,
    labels: tuple[str, ...] | None = None,
    threshold: float = 0.7,
    model=None,
    backend: str = "gliner",
) -> list[tuple[str, int, str]]:
    """Structured-PII (and optional NER name) findings across the scannable files:
    each team ``<ctx>/instructions.md``, the parsed dictionary files, every ``*.py``
    in the synced folders, and the open notebook. ``ctx_dirs`` is a single context
    folder or an iterable of them (the team offer). Paths are deduped by real path.
    ``backend`` picks the NER backend (``"gliner"`` or the offline ``"spacy"``)."""
    targets = [workspace / d / "instructions.md" for d in _ctx_dirs(ctx_dirs)]
    targets += [workspace / r.path for r in index.reports if not r.error]
    for folder in folders:
        root = workspace / folder
        if root.is_dir():
            targets += sorted(root.rglob("*.py"))
    if notebook_rel:
        targets.append(workspace / notebook_rel)
    findings: list[tuple[str, int, str]] = []
    seen: set[Path] = set()
    for path in targets:
        rp = path.resolve()
        if rp in seen or not path.is_file():
            continue
        seen.add(rp)
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = path.relative_to(workspace).as_posix()
        except ValueError:
            rel = str(path)
        findings += [
            (rel, f.line, f.kind)
            for f in pii.scan_prose(
                text, names=names, labels=labels, threshold=threshold, model=model, backend=backend
            )
        ]
    return findings


def scan_context_secrets(workspace: Path, ctx_dirs, index) -> list[tuple[str, int, str]]:
    """High-confidence secret findings in the team-authored context files (each
    ``<ctx>/instructions.md`` + the parsed dictionary files). ``ctx_dirs`` is a
    single context folder or an iterable of them (the team offer)."""
    targets = [workspace / d / "instructions.md" for d in _ctx_dirs(ctx_dirs)]
    targets += [workspace / r.path for r in index.reports if not r.error]
    findings: list[tuple[str, int, str]] = []
    for path in targets:
        if not path.is_file():
            continue
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(workspace).as_posix()
        findings += [(rel, f.line, f.kind) for f in secrets.scan(text)]
    return findings
