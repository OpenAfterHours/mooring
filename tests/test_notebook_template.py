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


# -- opens_as_notebook: which .py the editor may open (incl. the __init__.py carve-out) --


def test_opens_as_notebook_true_for_marimo_app_any_name():
    src = "import marimo\napp = marimo.App()\n"
    assert notebook_template.opens_as_notebook("notebooks/real.py", src) is True


def test_opens_as_notebook_true_for_blank_stub():
    # A freshly created empty .py opens as a new notebook.
    assert notebook_template.opens_as_notebook("notebooks/draft.py", "   \n") is True


def test_opens_as_notebook_false_for_plain_module():
    assert notebook_template.opens_as_notebook("notebooks/helpers.py", "def f():\n    return 1\n") is False


def test_opens_as_notebook_false_for_empty_init_py():
    # An empty __init__.py is a package marker, NOT a nascent notebook: opening it in
    # marimo would rewrite it into notebook form (and autorun it), breaking the package.
    assert notebook_template.opens_as_notebook("pkg/__init__.py", "") is False
    assert notebook_template.opens_as_notebook("pkg/__init__.py", "\n") is False


def test_opens_as_notebook_false_for_empty_main_py():
    assert notebook_template.opens_as_notebook("pkg/__main__.py", "") is False


def test_opens_as_notebook_handles_windows_separators():
    assert notebook_template.opens_as_notebook("pkg\\__init__.py", "") is False


def test_opens_as_notebook_non_dunder_double_trailing_underscore_stays_stub():
    # Only true dunder names (__<name>__.py) are carved out; "foo__.py" is not a marker.
    assert notebook_template.opens_as_notebook("notebooks/foo__.py", "") is True


# -- duplicate_as_draft: the fearless personal copy ---------------------------

NB_SOURCE = "import marimo\napp = marimo.App()\n"


def _seed(workspace, rel, text=NB_SOURCE):
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8")
    return rel


def test_duplicate_names_the_draft_after_the_owner(tmp_path):
    _seed(tmp_path, "notebooks/sales.py")
    rel = notebook_template.duplicate_as_draft(tmp_path, "notebooks/sales.py", owner="phil")
    assert rel == "notebooks/sales-phil-draft.py"
    assert (tmp_path / rel).is_file()
    # The original is untouched.
    assert (tmp_path / "notebooks/sales.py").read_text("utf-8") == NB_SOURCE


def test_duplicate_without_owner_uses_plain_draft_suffix(tmp_path):
    # Local mode (or an offline hub) has no login: the suffix is just "-draft".
    _seed(tmp_path, "notebooks/sales.py")
    rel = notebook_template.duplicate_as_draft(tmp_path, "notebooks/sales.py", owner="")
    assert rel == "notebooks/sales-draft.py"


def test_duplicate_collision_appends_counter(tmp_path):
    _seed(tmp_path, "notebooks/sales.py")
    first = notebook_template.duplicate_as_draft(tmp_path, "notebooks/sales.py", owner="phil")
    second = notebook_template.duplicate_as_draft(tmp_path, "notebooks/sales.py", owner="phil")
    third = notebook_template.duplicate_as_draft(tmp_path, "notebooks/sales.py", owner="phil")
    assert first == "notebooks/sales-phil-draft.py"
    assert second == "notebooks/sales-phil-draft-2.py"
    assert third == "notebooks/sales-phil-draft-3.py"


def test_duplicate_of_a_draft_collapses_its_own_suffix(tmp_path):
    # A draft-of-a-draft numbers up (sales-phil-draft-2), never stacks
    # (…-draft-phil-draft) — the collapse strips the suffix this feature minted.
    _seed(tmp_path, "notebooks/sales-phil-draft.py")
    rel = notebook_template.duplicate_as_draft(
        tmp_path, "notebooks/sales-phil-draft.py", owner="phil"
    )
    assert rel == "notebooks/sales-phil-draft-2.py"


def test_duplicate_collapse_is_scoped_to_minted_suffixes(tmp_path):
    # The pin from the design review: the collapse regex must only strip suffixes
    # THIS feature mints. A notebook literally named first-draft.py loses only the
    # ownerless "-draft" tail; a stem that merely ENDS in another word before
    # "-draft" ("annual-report-draft") keeps that word — never "annual".
    _seed(tmp_path, "notebooks/first-draft.py")
    rel = notebook_template.duplicate_as_draft(tmp_path, "notebooks/first-draft.py", owner="phil")
    assert rel == "notebooks/first-phil-draft.py"

    _seed(tmp_path, "notebooks/annual-report-draft.py")
    rel = notebook_template.duplicate_as_draft(
        tmp_path, "notebooks/annual-report-draft.py", owner="phil"
    )
    assert rel == "notebooks/annual-report-phil-draft.py"


def test_duplicate_of_a_teammates_draft_keeps_their_name(tmp_path):
    # Duplicating maria's draft as phil strips only the bare "-draft" tail, so the
    # copy reads sales-maria-phil-draft.py — the provenance survives in the name.
    _seed(tmp_path, "notebooks/sales-maria-draft.py")
    rel = notebook_template.duplicate_as_draft(
        tmp_path, "notebooks/sales-maria-draft.py", owner="phil"
    )
    assert rel == "notebooks/sales-maria-phil-draft.py"


def test_duplicate_copies_bytes_verbatim(tmp_path):
    # Byte-for-byte, no decode/re-encode: odd encodings (here invalid UTF-8) survive
    # exactly, and no BOM can appear. The sniff decodes with errors="ignore" only.
    data = NB_SOURCE.encode("utf-8") + b"# caf\xe9 latin-1 comment\n"
    target = tmp_path / "notebooks/odd.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    rel = notebook_template.duplicate_as_draft(tmp_path, "notebooks/odd.py", owner="phil")
    assert (tmp_path / rel).read_bytes() == data


def test_duplicate_refuses_a_helper_module(tmp_path):
    _seed(tmp_path, "notebooks/helpers.py", "def clean(df):\n    return df\n")
    with pytest.raises(ValueError, match="not a marimo notebook"):
        notebook_template.duplicate_as_draft(tmp_path, "notebooks/helpers.py", owner="phil")


def test_duplicate_refuses_a_dunder_package_marker(tmp_path):
    _seed(tmp_path, "pkg/__init__.py", "")
    with pytest.raises(ValueError, match="not a marimo notebook"):
        notebook_template.duplicate_as_draft(tmp_path, "pkg/__init__.py", owner="phil")


def test_duplicate_rejects_workspace_escape(tmp_path):
    with pytest.raises(ValueError, match="outside the workspace"):
        notebook_template.duplicate_as_draft(tmp_path, "../evil.py", owner="phil")


def test_duplicate_refuses_an_exclude_hidden_target(tmp_path):
    # A team [sync] exclude like *-draft.py yields a clear error instead of
    # minting a file the hub listing (sync-scoped) would never show.
    _seed(tmp_path, "notebooks/sales.py")
    with pytest.raises(ValueError, match="not a syncable location"):
        notebook_template.duplicate_as_draft(
            tmp_path, "notebooks/sales.py", owner="phil", exclude=("*-draft.py",)
        )


def test_duplicate_missing_source_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        notebook_template.duplicate_as_draft(tmp_path, "notebooks/missing.py", owner="phil")


def test_draft_stem_is_never_an_identifier(tmp_path):
    # The shadow immunity: the always-present hyphen makes every minted stem a
    # non-identifier, so shadow.scan structurally cannot flag a draft.
    _seed(tmp_path, "notebooks/polars.py")
    for owner in ("phil", ""):
        rel = notebook_template.duplicate_as_draft(tmp_path, "notebooks/polars.py", owner=owner)
        stem = rel.rsplit("/", 1)[-1].removesuffix(".py")
        assert not stem.isidentifier()
