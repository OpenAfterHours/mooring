"""Applying a proposed cell = writing value-free source into the notebook .py."""

from __future__ import annotations

import pytest

from mooring.ai.cellwrite import CellWriteError, append_cell

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
