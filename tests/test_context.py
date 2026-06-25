"""discover_context: the opt-in gate, instructions handling, and the secret scrub."""

from __future__ import annotations

from mooring.ai import context as ctxmod

SECRET_DSN = "postgres://user:hunter2@db.internal/prod"


def _write(tmp_path, rel, text):
    p = tmp_path / "context" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")


def test_disabled_returns_empty(tmp_path):
    _write(tmp_path, "instructions.md", "report in GBP")
    rc = ctxmod.discover_context(tmp_path, enabled=False)
    assert rc.is_empty() and rc.instructions == ""


def test_reads_instructions_strips_frontmatter(tmp_path):
    _write(tmp_path, "instructions.md", "---\ntitle: x\n---\nReport amounts in GBP millions.")
    rc = ctxmod.discover_context(tmp_path, enabled=True)
    assert rc.instructions == "Report amounts in GBP millions."
    assert "context/instructions.md" in rc.loaded_files


def test_instructions_with_secret_is_withheld(tmp_path):
    _write(tmp_path, "instructions.md", f"use this dsn: {SECRET_DSN}")
    rc = ctxmod.discover_context(tmp_path, enabled=True)
    assert rc.instructions == ""  # never sent
    assert rc.findings and rc.findings[0].source == "context/instructions.md"
    assert "context/instructions.md" not in rc.loaded_files


def test_dictionary_description_secret_is_scrubbed(tmp_path):
    _write(
        tmp_path,
        "dictionaries/credit.yaml",
        f"models:\n  - name: t\n    description: 'conn {SECRET_DSN}'\n"
        "    columns:\n      - name: id\n        data_type: int\n",
    )
    rc = ctxmod.discover_context(tmp_path, enabled=True)
    table = rc.index.get("t")
    assert table is not None
    assert table.description == ""  # dropped
    assert any(f.source == "credit.t" for f in rc.findings)


def test_instructions_size_cap(tmp_path):
    _write(tmp_path, "instructions.md", "x" * 5000)
    rc = ctxmod.discover_context(tmp_path, enabled=True, max_kb=1)
    assert "trimmed" in rc.instructions and len(rc.instructions.encode()) <= 1024 + 64
