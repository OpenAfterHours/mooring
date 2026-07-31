"""The value-blind notebook-catalog trio, gated on a passed Catalog (build_tool_specs).

The leak test that matters: a data value planted where a notebook can actually hold one —
a cell body, a filter literal, a computed path, and a markdown PARAGRAPH — must not reach
the model through any of the three tools, and no tool may serve another notebook's source.

Scope note, kept honest: the markdown-paragraph case passes STRUCTURALLY (prose has no
slot in the model), not because a scanner caught it. The one slot that is merely scanned
is the H1 title; ``test_notebookindex.py`` covers that one and labels it best-effort.
"""

from __future__ import annotations

from types import SimpleNamespace

from mooring.ai import tools
from mooring.ai.notebookindex import loader

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
CARD = "4012888888881881"  # a valid Luhn card


def _inv(args):
    return SimpleNamespace(arguments=args)


def _specs(tmp_path, catalog, **kw):
    return tools.build_tool_specs(
        workspace=tmp_path,
        folders=(),
        notebook_rel="nb.py",
        emit_proposal=lambda *a: None,
        catalog=catalog,
        **kw,
    )


_RECON = (
    "import marimo\n\napp = marimo.App()\n\n"
    "@app.cell\n"
    "def _():\n"
    "    import marimo as mo\n"
    "    import mooring_inputs as mi\n"
    "    import mooring_checks as mc\n"
    '    mo.md("""# Month End Recon\n'
    "\n"
    "    Ties the ledger to the GL feed. Top account SECRET_VALUE_DO_NOT_LEAK.\n"
    "    | Region | Revenue | Top customer |\n"
    '    | EMEA | 4,231,999 | SECRET_VALUE_DO_NOT_LEAK |""")\n'
    "    return\n\n"
    "@app.cell\n"
    "def _():\n"
    '    df = pl.read_parquet("data/ledger.parquet")\n'
    '    hit = df.filter(pl.col("acct") == "SECRET_VALUE_DO_NOT_LEAK")\n'
    "    total = 4012888888881881\n"
    '    mi.fingerprint(df, "ledger", path="data/ledger.parquet")\n'
    '    mc.unique_key(df, "id")\n'
    "    res = mo.sql(\"\"\"select id from gl_feed where n like '%paid from ACME_Holdings_Ltd%'\"\"\")\n"
    "    hit\n"
    "    return\n\n"
    'if __name__ == "__main__":\n    app.run()\n'
)


def _catalog(tmp_path):
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "reports" / "recon.py").write_text(_RECON, "utf-8")
    return loader.load_catalog(tmp_path, ["reports"])


def _all_outputs(tmp_path, catalog) -> str:
    """Everything all three tools can be made to say about the workspace."""
    specs = {s.name: s for s in _specs(tmp_path, catalog)}
    out = [
        specs["mooring_list_notebooks"].handler(None).text,
        specs["mooring_search_notebooks"].handler(_inv({"query": "recon"})).text,
        specs["mooring_search_notebooks"].handler(_inv({"query": "ledger"})).text,
        specs["mooring_describe_notebook"].handler(_inv({"notebook": "reports/recon.py"})).text,
        specs["mooring_describe_notebook"].handler(_inv({"notebook": "Month End Recon"})).text,
    ]
    return "\n".join(out)


def test_no_data_value_reaches_the_model_through_any_catalog_tool(tmp_path):
    text = _all_outputs(tmp_path, _catalog(tmp_path))
    assert SECRET not in text  # a cell body AND a markdown paragraph
    assert CARD not in text
    assert "4,231,999" not in text  # a pasted result table in a markdown cell
    assert "ACME_Holdings_Ltd" not in text  # a narrative inside a SQL string literal
    # ...while the value-free facts DO come through, so the tools are actually working.
    assert "reports/recon.py" in text and "Month End Recon" in text
    assert "data/ledger.parquet" in text and "unique_key" in text and "gl_feed" in text


def test_no_tool_returns_another_notebooks_source(tmp_path):
    text = _all_outputs(tmp_path, _catalog(tmp_path))
    for code_fragment in ("pl.read_parquet", "df.filter", "@app.cell", "mi.fingerprint"):
        assert code_fragment not in text


def test_every_tool_result_passes_the_egress_floor(tmp_path):
    # The structural allowlist is the real guarantee, but the checksum-PII floor is the
    # backstop beneath it — and it must actually be wired into EACH of the three handlers.
    # The catalog is built by hand rather than extracted, precisely so the floor is tested
    # on its own: extraction's title scan would withhold the title before a handler saw
    # it, which would make this vacuous for the tools that render only a title.
    from mooring.ai.notebookindex import Catalog, Dataset, Notebook

    catalog = Catalog(
        notebooks=(
            Notebook(
                path="reports/ledger.py",
                title=f"Ledger for card {CARD}",
                datasets=(Dataset(name="ledger", path=f"data/{CARD}.parquet"),),
            ),
        )
    )
    specs = {s.name: s for s in _specs(tmp_path, catalog)}
    for name, args in (
        ("mooring_list_notebooks", None),
        ("mooring_search_notebooks", _inv({"query": "ledger"})),
        ("mooring_describe_notebook", _inv({"notebook": "reports/ledger.py"})),
    ):
        out = specs[name].handler(args).text
        assert CARD not in out, name
        # ...and the floor drops only the offending LINE, so the entry keeps its identity.
        assert "reports/ledger.py" in out, name


def test_catalog_tools_registered_value_free_skip_permission(tmp_path):
    specs = {s.name: s for s in _specs(tmp_path, _catalog(tmp_path))}
    for name in tools.CATALOG_TOOL_NAMES:
        assert name in specs
        assert specs[name].skip_permission is True


def test_describe_miss_and_path_like_return_ok_not_error(tmp_path):
    describe = {s.name: s for s in _specs(tmp_path, _catalog(tmp_path))}["mooring_describe_notebook"]
    miss = describe.handler(_inv({"notebook": "nope.py"}))
    assert "No notebook named" in miss.text and not miss.is_error
    # A path-like argument is a NAME lookup in memory — it can never reach a file.
    (tmp_path / "outside.py").write_text("SECRET = 1\n", "utf-8")
    esc = describe.handler(_inv({"notebook": "../outside.py"}))
    assert "No notebook named" in esc.text and not esc.is_error
    assert describe.handler(_inv({"notebook": ""})).is_error  # a blank arg is a usage error


def test_search_requires_a_query_and_reports_a_clean_miss(tmp_path):
    search = {s.name: s for s in _specs(tmp_path, _catalog(tmp_path))}["mooring_search_notebooks"]
    assert search.handler(_inv({})).is_error
    miss = search.handler(_inv({"query": "zzzznope"}))
    assert "No notebooks match" in miss.text and not miss.is_error


def test_catalog_tools_absent_without_a_catalog(tmp_path):
    names = [
        s.name
        for s in tools.build_tool_specs(
            workspace=tmp_path, folders=(), notebook_rel="nb.py", emit_proposal=lambda *a: None
        )
    ]
    assert not any(n in names for n in tools.CATALOG_TOOL_NAMES)


def test_catalog_tools_absent_when_catalog_empty(tmp_path):
    empty = loader.load_catalog(tmp_path, ["nonexistent"])
    names = [s.name for s in _specs(tmp_path, empty)]
    assert not any(n in names for n in tools.CATALOG_TOOL_NAMES)


def test_read_only_session_gets_the_catalog_tools_but_still_no_write_tool(tmp_path):
    # An investigate sub-agent is built with emit_proposal=None. The catalog tools are
    # READS, so they are registered outside that gate — "which notebook already does this?"
    # is exactly a branch's job. The gate itself must stay intact: no propose/edit tool.
    names = [
        s.name
        for s in tools.build_tool_specs(
            workspace=tmp_path, folders=(), notebook_rel="nb.py", catalog=_catalog(tmp_path)
        )
    ]
    assert all(n in names for n in tools.CATALOG_TOOL_NAMES)
    assert not any("propose" in n for n in names)
    assert "mooring_investigate" not in names
