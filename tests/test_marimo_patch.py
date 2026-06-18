"""The marimo IR patch seam: read_cells + apply_cell_patch (edit/delete/rewrite).

These exercise the codegen surface mooring couples to (private marimo API), so they
also document the behaviour the chat edit/rollback features rely on: a targeted edit
leaves the OTHER cells' emitted source byte-identical, a stale anchor is a loud
CellPatchConflict, and a malformed result is rejected before it can be written.
"""

from __future__ import annotations

import pytest

from mooring import marimo_rt
from mooring.marimo_rt import CellOp

NB = (
    "import marimo\n\n"
    '__generated_with = "0.23.9"\n'
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    seed = 1\n"
    "    return (seed,)\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    x = seed + 1\n"
    "    return (x,)\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    y = x * 2\n"
    "    return (y,)\n\n\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)


def _codes(source: str) -> list[str]:
    return [code for _, code in marimo_rt.read_cells(source)]


def test_read_cells_returns_indexed_bodies():
    assert marimo_rt.read_cells(NB) == [(0, "seed = 1"), (1, "x = seed + 1"), (2, "y = x * 2")]


def test_edit_replaces_only_the_target_cell():
    cells = marimo_rt.read_cells(NB)
    out = marimo_rt.apply_cell_patch(
        NB, [CellOp(op="edit", index=1, anchor=cells[1][1], code="x = seed + 100")]
    )
    assert _codes(out) == ["seed = 1", "x = seed + 100", "y = x * 2"]


def test_edit_leaves_other_emitted_blocks_byte_identical():
    # The load-bearing property for a clean diff + minimal re-run: editing cell 1
    # must not rewrite cells 0 and 2 in the generated file.
    cells = marimo_rt.read_cells(NB)
    baseline = marimo_rt.apply_cell_patch(NB, [])  # one normalization pass, no change
    out = marimo_rt.apply_cell_patch(
        NB, [CellOp(op="edit", index=1, anchor=cells[1][1], code="x = seed + 100")]
    )
    assert "    seed = 1\n" in out and "    y = x * 2\n" in out
    # Everything except the edited cell's body is unchanged vs the normalized baseline.
    assert baseline.replace("x = seed + 1", "EDITED") == out.replace("x = seed + 100", "EDITED")


def test_delete_removes_the_target_cell():
    cells = marimo_rt.read_cells(NB)
    out = marimo_rt.apply_cell_patch(NB, [CellOp(op="delete", index=1, anchor=cells[1][1])])
    assert _codes(out) == ["seed = 1", "y = x * 2"]


def test_combined_edit_delete_append_uses_original_indices():
    cells = marimo_rt.read_cells(NB)
    out = marimo_rt.apply_cell_patch(
        NB,
        [
            CellOp(op="edit", index=0, anchor=cells[0][1], code="seed = 9"),
            CellOp(op="delete", index=2, anchor=cells[2][1]),
            CellOp(op="append", code="done = True"),
        ],
    )
    assert _codes(out) == ["seed = 9", "x = seed + 1", "done = True"]


def test_replace_all_rebuilds_every_cell():
    out = marimo_rt.apply_cell_patch(NB, [CellOp(op="replace_all", cells=("a = 1", "b = a + 1"))])
    assert _codes(out) == ["a = 1", "b = a + 1"]


def test_replace_all_cannot_be_combined():
    with pytest.raises(ValueError, match="rewrite cannot be combined"):
        marimo_rt.apply_cell_patch(NB, [CellOp(op="replace_all", cells=("a = 1",)), CellOp(op="append", code="b = 2")])


def test_stale_anchor_raises_conflict():
    with pytest.raises(marimo_rt.CellPatchConflict):
        marimo_rt.apply_cell_patch(NB, [CellOp(op="edit", index=1, anchor="not the cell", code="x = 0")])


def test_out_of_range_index_raises_conflict():
    with pytest.raises(marimo_rt.CellPatchConflict):
        marimo_rt.apply_cell_patch(NB, [CellOp(op="edit", index=9, anchor="x", code="x = 0")])


def test_double_targeting_one_cell_is_rejected():
    cells = marimo_rt.read_cells(NB)
    with pytest.raises(ValueError, match="more than one operation"):
        marimo_rt.apply_cell_patch(
            NB,
            [
                CellOp(op="edit", index=1, anchor=cells[1][1], code="x = 2"),
                CellOp(op="delete", index=1, anchor=cells[1][1]),
            ],
        )


def test_deleting_every_cell_is_rejected():
    cells = marimo_rt.read_cells(NB)
    with pytest.raises(ValueError, match="empty the notebook"):
        marimo_rt.apply_cell_patch(NB, [CellOp(op="delete", index=i, anchor=c) for i, c in cells])


def test_a_result_that_would_not_parse_is_rejected():
    # An unbalanced paren would generate a file marimo can't re-parse — we must fail
    # loud here rather than write something --watch silently ignores.
    cells = marimo_rt.read_cells(NB)
    with pytest.raises(ValueError, match="would not parse"):
        marimo_rt.apply_cell_patch(NB, [CellOp(op="edit", index=0, anchor=cells[0][1], code="seed = (1")])


def test_read_cells_normalizes_marimo_parse_error_to_valueerror():
    # A truncated/partial notebook raises marimo's own MarimoFileError; the seam
    # normalizes it to ValueError so the ai/ layer (which can't import marimo) can
    # catch it.
    with pytest.raises(ValueError):
        marimo_rt.read_cells("import marimo\n# not a real notebook\n")


def test_edit_without_anchor_is_rejected():
    # A missing anchor must never be a free index-based clobber — it defeats conflict
    # detection, so apply_cell_patch refuses it.
    with pytest.raises(marimo_rt.CellPatchConflict):
        marimo_rt.apply_cell_patch(NB, [CellOp(op="edit", index=0, anchor=None, code="seed = 0")])


def test_rewrite_preserves_name_of_an_unchanged_cell():
    nb = (
        "import marimo\n\n"
        '__generated_with = "0.23.9"\n'
        "app = marimo.App()\n\n\n"
        "@app.cell\n"
        "def load_customers():\n"
        "    seed = 1\n"
        "    return (seed,)\n\n\n"
        'if __name__ == "__main__":\n'
        "    app.run()\n"
    )
    # Rewrite keeps the first cell's code byte-identical and adds a new one.
    out = marimo_rt.apply_cell_patch(nb, [CellOp(op="replace_all", cells=("seed = 1", "z = 2"))])
    assert "def load_customers():" in out  # the unchanged cell keeps its name
    assert _codes(out) == ["seed = 1", "z = 2"]


def test_edit_allows_top_level_await():
    # marimo cells may use top-level await — the parse-check must not reject it.
    cells = marimo_rt.read_cells(NB)
    out = marimo_rt.apply_cell_patch(
        NB, [CellOp(op="edit", index=0, anchor=cells[0][1], code="seed = await fetch()")]
    )
    assert "seed = await fetch()" in out


def test_append_cell_source_still_appends():
    out = marimo_rt.append_cell_source(NB, "total = 1")
    assert _codes(out) == ["seed = 1", "x = seed + 1", "y = x * 2", "total = 1"]


# -- normalize_cell_code: tolerate the wrapper/return the model copies from source ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("import marimo as mo\nreturn (mo,)", "import marimo as mo"),  # strip the cell return
        ("x = 1\ny = 2\nreturn (x, y)", "x = 1\ny = 2"),
        ("a = 1\nreturn (\n    a,\n)", "a = 1"),  # multi-line parenthesized return
        ("return", ""),  # a return-only cell collapses to empty
        ("x = await f()\nreturn (x,)", "x = await f()"),  # top-level await + return
        ("@app.cell\ndef _():\n    z = 9\n    return (z,)", "z = 9"),  # unwrap the wrapper
        ("seed = 1", "seed = 1"),  # already clean — unchanged
        ("mo.md(\"hi\")", 'mo.md("hi")'),
        ("def load():\n    return 1", "def load():\n    return 1"),  # nested return kept
    ],
)
def test_normalize_cell_code(raw, expected):
    assert marimo_rt.normalize_cell_code(raw) == expected


def test_rewrite_tolerates_returns_in_cell_bodies():
    # The exact failure the user hit: the model copied the auto-generated `return`
    # lines into the rewrite cells. Normalization makes it apply cleanly.
    out = marimo_rt.apply_cell_patch(
        NB,
        [marimo_rt.CellOp(op="replace_all", cells=("import marimo as mo\nreturn (mo,)", "z = 1\nreturn (z,)"))],
    )
    assert _codes(out) == ["import marimo as mo", "z = 1"]


def test_edit_tolerates_a_return_in_the_new_code():
    cells = marimo_rt.read_cells(NB)
    out = marimo_rt.apply_cell_patch(
        NB, [marimo_rt.CellOp(op="edit", index=0, anchor=cells[0][1], code="seed = 5\nreturn (seed,)")]
    )
    assert _codes(out)[0] == "seed = 5"
