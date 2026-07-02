"""The cell-aware differ behind the hub's "Review changes" panel (celldiff.py):
per-cell matching with the honest unmatched bucket, and the loud degradations —
whole-file line diff for unparseable/non-notebook text, sizes only for
binary/oversized content. Pure and offline: bytes in, dataclasses out."""

import pytest

from mooring import celldiff


def nb(*cells: str) -> bytes:
    """A minimal valid marimo notebook with the given cell bodies, as bytes."""
    parts = ['import marimo\n\n__generated_with = "0.23.9"\napp = marimo.App()\n\n\n']
    for code in cells:
        body = "\n".join("    " + line for line in code.splitlines())
        parts.append(f"@app.cell\ndef _():\n{body}\n    return\n\n\n")
    parts.append('if __name__ == "__main__":\n    app.run()\n')
    return "".join(parts).encode("utf-8")


def statuses(result):
    return [c.status for c in result.cells]


def test_changed_cell_gets_a_per_cell_diff():
    result = celldiff.diff(nb("x = 1", "y = 2"), nb("x = 1", "y = 3"), "notebooks/a.py")
    assert result.kind == "cells"
    assert statuses(result) == ["unchanged", "changed"]
    changed = result.cells[1]
    assert (changed.index_base, changed.index_local) == (1, 1)
    assert "-y = 2" in changed.diff and "+y = 3" in changed.diff
    # Unchanged cells carry no diff text (they collapse to one line in the panel).
    assert result.cells[0].diff == ""


def test_added_cell():
    result = celldiff.diff(nb("x = 1"), nb("x = 1", "y = 2"), "notebooks/a.py")
    assert statuses(result) == ["unchanged", "added"]
    added = result.cells[1]
    assert added.index_base is None and added.index_local == 1
    assert "+y = 2" in added.diff


def test_removed_cell():
    result = celldiff.diff(nb("x = 1", "y = 2"), nb("x = 1"), "notebooks/a.py")
    assert statuses(result) == ["unchanged", "removed"]
    removed = result.cells[1]
    assert removed.index_base == 1 and removed.index_local is None
    assert "-y = 2" in removed.diff


def test_reordered_cells_match_exactly_and_keep_both_indices():
    result = celldiff.diff(nb("x = 1", "y = 2"), nb("y = 2", "x = 1"), "notebooks/a.py")
    assert statuses(result) == ["unchanged", "unchanged"]
    # The exact-source pass pairs moved cells; the indices expose the move.
    assert [(c.index_base, c.index_local) for c in result.cells] == [(1, 0), (0, 1)]


def test_exact_match_is_stable_for_duplicate_cells():
    result = celldiff.diff(nb("a = 1", "a = 1"), nb("a = 1", "a = 1", "a = 1"), "n.py")
    assert statuses(result) == ["unchanged", "unchanged", "added"]
    # Same-position pairs win first, so the duplicates don't cross over.
    assert [(c.index_base, c.index_local) for c in result.cells[:2]] == [(0, 0), (1, 1)]


def test_unmatched_bucket_renders_removed_then_added():
    rewritten = 'import polars as pl\ndf = pl.read_csv("sales.csv")\ndf.head()'
    result = celldiff.diff(nb("x = 1"), nb(rewritten), "notebooks/a.py")
    # Leftovers on both sides are ambiguous — never claimed as a "change".
    assert statuses(result) == ["unmatched", "unmatched"]
    removed, added = result.cells
    assert removed.index_base == 0 and removed.index_local is None
    assert "-x = 1" in removed.diff
    assert added.index_base is None and added.index_local == 0
    assert "+import polars as pl" in added.diff
    assert "could not be matched confidently" in result.note


def test_crlf_local_bytes_diff_clean_against_lf_base():
    # Windows editors flip line endings; the differ sees the same LF-normalized
    # text gitsha.read_for_push would push, so nothing shows as changed.
    base = nb("x = 1", "y = 2")
    local = base.replace(b"\n", b"\r\n")
    result = celldiff.diff(base, local, "notebooks/a.py")
    assert result.kind == "cells"
    assert statuses(result) == ["unchanged", "unchanged"]


def test_plain_module_py_gets_a_line_diff_not_a_fake_cell():
    # marimo's converter would happily WRAP a helper module into a one-cell
    # notebook; the sniff keeps modules on the honest whole-file line diff.
    result = celldiff.diff(b"x = 1\n", b"x = 2\n", "notebooks/helpers.py")
    assert result.kind == "lines"
    assert "-x = 1" in result.line_diff and "+x = 2" in result.line_diff
    assert result.note == ""
    assert result.cells == []


def test_corrupt_notebook_falls_back_to_line_diff():
    # Carries the marimo.App marker but does not parse: fall back, loudly.
    broken = b"import marimo\napp = marimo.App(\ndef broken(:\n"
    result = celldiff.diff(nb("x = 1"), broken, "notebooks/a.py")
    assert result.kind == "lines"
    assert "not readable as marimo cells" in result.note
    assert result.cells == []


def test_non_py_text_gets_a_plain_line_diff():
    result = celldiff.diff(b"a,b\n1,2\n", b"a,b\n1,3\n", "data/sales.csv")
    assert result.kind == "lines"
    assert "-1,2" in result.line_diff and "+1,3" in result.line_diff
    # Shaped like the version-history diff so one renderer serves both.
    assert "--- data/sales.csv (last synced)" in result.line_diff
    assert "+++ data/sales.csv (local)" in result.line_diff


def test_undecodable_bytes_show_sizes_only():
    base = b"\xff\xfe\x00\x01" * 4
    result = celldiff.diff(base, b"\xff\xfe\x00\x02" * 8, "data/model.bin")
    assert result.kind == "binary"
    assert "contents not shown" in result.note
    assert "16 B" in result.note and "32 B" in result.note
    assert result.line_diff == "" and result.cells == []


def test_oversized_content_shows_sizes_only():
    result = celldiff.diff(b"a" * 20, b"b" * 30, "data/big.csv", max_bytes=10)
    assert result.kind == "binary"
    assert "20 B" in result.note and "30 B" in result.note
    assert "contents not shown" in result.note


def test_base_none_is_all_added():
    result = celldiff.diff(None, nb("x = 1", "y = 2"), "notebooks/new.py")
    assert result.kind == "cells"
    assert statuses(result) == ["added", "added"]
    assert "new local notebook" in result.note


def test_local_none_is_all_removed():
    result = celldiff.diff(nb("x = 1", "y = 2"), None, "notebooks/gone.py")
    assert result.kind == "cells"
    assert statuses(result) == ["removed", "removed"]
    assert "deleted locally" in result.note


def test_local_none_non_notebook_line_fallback():
    result = celldiff.diff(b"v1\n", None, "notebooks/gone.py")
    assert result.kind == "lines"
    assert "-v1" in result.line_diff
    assert "deleted locally" in result.note


def test_both_none_raises():
    with pytest.raises(ValueError):
        celldiff.diff(None, None, "notebooks/a.py")
