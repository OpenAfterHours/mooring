"""Applying a proposed change = writing value-free source into the notebook .py."""

from __future__ import annotations

import pytest

from mooring import marimo_rt
from mooring.ai.cellwrite import (
    CellApplyConflict,
    CellWriteError,
    append_cell,
    apply_patch,
    apply_wire_patch,
)

NB = (
    "import marimo\n\n"
    '__generated_with = "0.23.9"\n'
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    seed = 1\n"
    "    return (seed,)\n\n\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)


def test_appends_a_parseable_cell(tmp_path):
    p = tmp_path / "nb.py"
    p.write_text(NB, "utf-8")
    append_cell(p, "total = 41 + 1")
    out = p.read_text("utf-8")
    assert "total = 41 + 1" in out
    # still a valid marimo notebook with the extra cell
    from marimo._convert.converters import MarimoConvert

    ir = MarimoConvert.from_py(out).to_ir()
    assert len(ir.cells) == 2


def test_markdown_cell_is_preserved_even_if_reformatted(tmp_path):
    # marimo's codegen rewrites mo.md("x") into a triple-quoted form; the content
    # is preserved and the file still parses.
    p = tmp_path / "nb.py"
    p.write_text(NB, "utf-8")
    append_cell(p, 'mo.md("This is a test cell")')
    out = p.read_text("utf-8")
    assert "This is a test cell" in out
    from marimo._convert.converters import MarimoConvert

    assert len(MarimoConvert.from_py(out).to_ir().cells) == 2


def test_writes_no_bom(tmp_path):
    p = tmp_path / "nb.py"
    p.write_text(NB, "utf-8")
    append_cell(p, "x = 1")
    assert not p.read_bytes().startswith(b"\xef\xbb\xbf")  # marimo rejects a BOM


def test_missing_file_raises(tmp_path):
    with pytest.raises(CellWriteError):
        append_cell(tmp_path / "nope.py", "x = 1")


def test_apply_patch_edits_a_cell_in_place(tmp_path):
    p = tmp_path / "nb.py"
    p.write_text(NB, "utf-8")
    apply_patch(p, [marimo_rt.CellOp(op="edit", index=0, anchor="seed = 1", code="seed = 42")])
    out = p.read_text("utf-8")
    assert "seed = 42" in out and "seed = 1" not in out


def test_apply_patch_writes_no_bom(tmp_path):
    p = tmp_path / "nb.py"
    p.write_text(NB, "utf-8")
    apply_patch(p, [marimo_rt.CellOp(op="edit", index=0, anchor="seed = 1", code="seed = 2")])
    assert not p.read_bytes().startswith(b"\xef\xbb\xbf")  # atomic write, still no BOM


def test_apply_patch_stale_anchor_raises_conflict_and_leaves_file_untouched(tmp_path):
    p = tmp_path / "nb.py"
    p.write_text(NB, "utf-8")
    with pytest.raises(CellApplyConflict):
        apply_patch(p, [marimo_rt.CellOp(op="edit", index=0, anchor="WRONG", code="seed = 2")])
    assert p.read_text("utf-8") == NB  # the original is intact


def test_apply_wire_patch_converts_dict_ops(tmp_path):
    p = tmp_path / "nb.py"
    p.write_text(NB, "utf-8")
    apply_wire_patch(p, [{"op": "edit", "index": 0, "anchor": "seed = 1", "code": "seed = 7"}])
    assert "seed = 7" in p.read_text("utf-8")


def test_apply_wire_patch_unknown_op_raises(tmp_path):
    p = tmp_path / "nb.py"
    p.write_text(NB, "utf-8")
    with pytest.raises(CellWriteError):
        apply_wire_patch(p, [{"op": "nonsense"}])
