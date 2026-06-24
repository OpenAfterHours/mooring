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


def test_create_unique_auto_numbers_on_collision(tmp_path):
    # A batch of similar names must not abort on the first collision: create_unique
    # keeps minting fresh files (sales, sales-2, sales-3) instead of raising.
    first = notebook_template.create_unique(tmp_path, "Sales")
    second = notebook_template.create_unique(tmp_path, "Sales")
    third = notebook_template.create_unique(tmp_path, "Sales!")  # slugifies to "Sales" too
    assert first == "notebooks/Sales.py"
    assert {second, third} == {"notebooks/Sales-2.py", "notebooks/Sales-3.py"}
    assert len({first, second, third}) == 3  # all distinct files


def test_create_unique_keeps_the_readable_title_when_numbered(tmp_path):
    # The numbered file still carries the original display name as its markdown title,
    # not the slug-with-number — so a batch of "Sales" notebooks all read "# Sales".
    notebook_template.create_unique(tmp_path, "Sales")
    rel = notebook_template.create_unique(tmp_path, "Sales")
    src = (tmp_path / rel).read_text("utf-8")
    assert rel == "notebooks/Sales-2.py"
    assert "# Sales" in src and "# Sales-2" not in src


def test_create_unique_still_rejects_an_unslugable_name(tmp_path):
    with pytest.raises(ValueError):
        notebook_template.create_unique(tmp_path, "  ///  ")
