"""The new-notebook template: a created notebook is a valid marimo file whose
title markdown cell ships hidden (so the analyst doesn't read the title twice —
source + render — in marimo's edit view)."""

from __future__ import annotations

import pytest

from mooring import marimo_rt, notebook_template


def test_created_notebook_hides_the_title_markdown_cell(tmp_path):
    rel = notebook_template.create(tmp_path, "Quarterly Report")
    src = (tmp_path / rel).read_text("utf-8")
    # The title cell is hidden; the import cell is not.
    assert "@app.cell(hide_code=True)" in src
    assert src.count("@app.cell(hide_code=True)") == 1
    assert "# Quarterly Report" in src


def test_created_notebook_parses_through_the_seam(tmp_path):
    rel = notebook_template.create(tmp_path, "smoke")
    src = (tmp_path / rel).read_text("utf-8")
    # It round-trips through the marimo IR (a malformed decorator would raise here).
    cells = marimo_rt.read_cells(src)
    assert any(marimo_rt.is_markdown_cell(code) for _, code in cells)


def test_create_refuses_to_clobber_an_existing_notebook(tmp_path):
    notebook_template.create(tmp_path, "dup")
    with pytest.raises(FileExistsError):
        notebook_template.create(tmp_path, "dup")
