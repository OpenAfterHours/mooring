"""The new-notebook template: a created notebook is a valid marimo file whose
title markdown cell ships hidden (so the analyst doesn't read the title twice —
source + render — in marimo's edit view)."""

from __future__ import annotations

import tomllib

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


# -- sub-folder targets -------------------------------------------------------

DEFAULT_FOLDERS = ("notebooks", "data", "reports")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("sales", ("notebooks", "sales")),
        ("notebooks/sales", ("notebooks", "sales")),
        ("packages/finance/notebooks/sales", ("packages/finance/notebooks", "sales")),
        ("packages\\finance\\sales", ("packages/finance", "sales")),  # backslashes
        ("/notebooks/sales/", ("notebooks", "sales")),  # surrounding slashes
        ("sales.py", ("notebooks", "sales.py")),  # .py stripped later by slugify
    ],
)
def test_split_target(raw, expected):
    assert notebook_template.split_target(raw) == expected


def test_create_into_a_subfolder(tmp_path):
    rel = notebook_template.create(tmp_path, "Sales", folder="packages/finance/notebooks")
    assert rel == "packages/finance/notebooks/Sales.py"
    assert (tmp_path / rel).is_file()


def test_create_default_folder_unchanged(tmp_path):
    assert notebook_template.create(tmp_path, "x") == "notebooks/x.py"


def test_create_from_input_default_does_not_register(tmp_path):
    rel = notebook_template.create_from_input(tmp_path, "scratch", folders=DEFAULT_FOLDERS)
    assert rel == "notebooks/scratch.py"
    assert (tmp_path / rel).is_file()
    assert not (tmp_path / "mooring.toml").exists()  # notebooks/ already synced


def test_create_from_input_subfolder_auto_registers(tmp_path):
    rel = notebook_template.create_from_input(
        tmp_path, "packages/finance/notebooks/sales", folders=DEFAULT_FOLDERS
    )
    assert rel == "packages/finance/notebooks/sales.py"
    assert (tmp_path / rel).is_file()
    data = tomllib.loads((tmp_path / "mooring.toml").read_text("utf-8"))
    assert data["sync"]["folders"] == ["packages/finance/notebooks"]


def test_create_from_input_nested_under_synced_folder_is_not_registered(tmp_path):
    # A folder already covered (nested under a synced root) needs no registration.
    rel = notebook_template.create_from_input(tmp_path, "notebooks/sub/x", folders=("notebooks",))
    assert rel == "notebooks/sub/x.py"
    assert not (tmp_path / "mooring.toml").exists()


def test_create_from_input_rejects_workspace_escape(tmp_path):
    with pytest.raises(ValueError):
        notebook_template.create_from_input(tmp_path, "../evil", folders=DEFAULT_FOLDERS)


def test_create_from_input_rejects_dotfile_location(tmp_path):
    with pytest.raises(ValueError):
        notebook_template.create_from_input(tmp_path, ".secret/x", folders=DEFAULT_FOLDERS)


def test_create_from_input_respects_exclude(tmp_path):
    with pytest.raises(ValueError):
        notebook_template.create_from_input(
            tmp_path, "drafts/x", folders=("notebooks",), exclude=("drafts",)
        )


# -- is_marimo_app: tell a notebook from a plain helper module ----------------


def test_is_marimo_app_detects_the_template():
    src = notebook_template.TEMPLATE.format(version="0.0.0", title="t")
    assert notebook_template.is_marimo_app(src) is True


def test_is_marimo_app_true_for_minimal_app():
    assert notebook_template.is_marimo_app("import marimo\napp = marimo.App()\n") is True


def test_is_marimo_app_false_for_plain_module():
    assert notebook_template.is_marimo_app("import pandas as pd\n\ndef clean(df):\n    return df\n") is False


def test_is_marimo_app_false_for_bare_marimo_import():
    # Imports marimo but builds no app — an incomplete stub, not a notebook.
    assert notebook_template.is_marimo_app("import marimo\n") is False


def test_is_marimo_app_false_for_empty():
    assert notebook_template.is_marimo_app("") is False


def test_is_marimo_app_ignores_leading_bom():
    assert notebook_template.is_marimo_app("﻿import marimo\napp = marimo.App()\n") is True


def test_is_marimo_app_anchored_not_bare_substring():
    # A helper module that merely MENTIONS marimo.App (comment, docstring, or a factory
    # `return marimo.App(...)`) is NOT a notebook — the match anchors to a top-level
    # `<name> = marimo.App(` assignment, so it isn't opened+rewritten by the editor.
    assert notebook_template.is_marimo_app("# wraps marimo.App(...) for tests\nx = 1\n") is False
    assert notebook_template.is_marimo_app('"""docs: marimo.App( usage."""\nx = 1\n') is False
    assert notebook_template.is_marimo_app("def make():\n    return marimo.App(width='full')\n") is False


def test_is_marimo_app_finds_marker_past_4kb():
    # A real notebook with a large leading header (e.g. a PEP 723 dependency block)
    # before `app = marimo.App(` must still be detected — the sniff reads full source.
    src = "# " + "x" * 6000 + "\nimport marimo\napp = marimo.App()\n"
    assert notebook_template.is_marimo_app(src) is True
