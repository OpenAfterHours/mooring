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


# -- discover_contexts: reading SEVERAL folders at once ----------------------


def _write_in(tmp_path, folder, rel, text):
    p = tmp_path / folder / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")


def test_contexts_disabled_returns_empty(tmp_path):
    _write_in(tmp_path, "context", "instructions.md", "report in GBP")
    rc = ctxmod.discover_contexts(tmp_path, ["context"], enabled=False)
    assert rc.is_empty() and rc.instructions == ""


def test_single_folder_is_byte_identical_to_discover_context(tmp_path):
    _write(tmp_path, "instructions.md", "---\ntitle: x\n---\nReport amounts in GBP millions.")
    one = ctxmod.discover_context(tmp_path, enabled=True)
    many = ctxmod.discover_contexts(tmp_path, ["context"], enabled=True)
    assert many.instructions == one.instructions  # no banner for a lone folder
    assert many.loaded_files == one.loaded_files


def test_two_clean_folders_concatenate_in_sorted_order_with_banners(tmp_path):
    _write_in(tmp_path, "bbb", "instructions.md", "beta guidance")
    _write_in(tmp_path, "aaa", "instructions.md", "alpha guidance")
    rc = ctxmod.discover_contexts(tmp_path, ["bbb", "aaa"], enabled=True)
    # Sorted-folder order, each behind a value-free path banner.
    assert rc.instructions == (
        "<!-- context: aaa/instructions.md -->\nalpha guidance\n\n"
        "<!-- context: bbb/instructions.md -->\nbeta guidance"
    )
    assert rc.loaded_files == ("aaa/instructions.md", "bbb/instructions.md")


def test_secret_in_one_folder_withholds_only_that_folder(tmp_path):
    _write_in(tmp_path, "clean", "instructions.md", "report in GBP")
    _write_in(tmp_path, "dirty", "instructions.md", f"use this dsn: {SECRET_DSN}")
    rc = ctxmod.discover_contexts(tmp_path, ["clean", "dirty"], enabled=True)
    # The clean sibling survives intact; the poisoned folder is blanked but recorded.
    assert rc.instructions == "report in GBP"  # lone survivor: no banner, unaltered
    assert SECRET_DSN not in rc.instructions
    assert "clean/instructions.md" in rc.loaded_files
    assert "dirty/instructions.md" not in rc.loaded_files
    assert any(f.source == "dirty/instructions.md" for f in rc.findings)


def test_dedupes_and_ignores_blank_dirs(tmp_path):
    _write_in(tmp_path, "aaa", "instructions.md", "alpha")
    rc = ctxmod.discover_contexts(tmp_path, ["aaa", "aaa", "", "/aaa/"], enabled=True)
    assert rc.instructions == "alpha"  # one folder after normalise+dedupe


def test_aggregate_instructions_cap(tmp_path, monkeypatch):
    # The combined (multi-folder) instructions are bounded on top of the per-file cap.
    monkeypatch.setattr(ctxmod, "_MAX_TOTAL_INSTR_KB", 1)
    _write_in(tmp_path, "aaa", "instructions.md", "a" * 800)
    _write_in(tmp_path, "bbb", "instructions.md", "b" * 800)
    rc = ctxmod.discover_contexts(tmp_path, ["aaa", "bbb"], enabled=True)
    assert "trimmed: combined team instructions" in rc.instructions
    assert len(rc.instructions.encode()) <= 1024 + 80  # cap + the trim marker
