"""The value-blind helper tool trio, gated on a passed CodeIndex (build_tool_specs)."""

from __future__ import annotations

from types import SimpleNamespace

from mooring.ai import tools
from mooring.ai.codelib import loader

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


def _inv(args):
    return SimpleNamespace(arguments=args)


def _specs(tmp_path, code_index):
    return tools.build_tool_specs(
        workspace=tmp_path,
        folders=(),
        notebook_rel="nb.py",
        emit_proposal=lambda *a: None,
        code_index=code_index,
    )


def _index(tmp_path):
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "h.py").write_text(
        "def clean(df, cols: list):\n"
        '    """Normalize date columns to UTC."""\n'
        '    key = "SECRET_VALUE_DO_NOT_LEAK"\n'  # a body literal — must never surface
        "    return key\n"
        "class Rate:\n"
        "    def __init__(self, n: int): ...\n",
        "utf-8",
    )
    return loader.load_index(tmp_path, ["utils"])


def test_helper_tools_registered_value_free_skip_permission(tmp_path):
    specs = {s.name: s for s in _specs(tmp_path, _index(tmp_path))}
    for name in tools.HELPER_TOOL_NAMES:
        assert name in specs
        assert specs[name].skip_permission is True
    out = specs["mooring_list_helpers"].handler(None)
    assert "clean" in out.text and "Rate" in out.text
    assert SECRET not in out.text  # the body literal is dropped


def test_describe_helper_renders_signature_and_import_line(tmp_path):
    specs = {s.name: s for s in _specs(tmp_path, _index(tmp_path))}
    describe = specs["mooring_describe_helper"]
    out = describe.handler(_inv({"name": "clean"}))
    assert "clean(df, cols: list)" in out.text
    assert "from utils.h import clean" in out.text
    assert SECRET not in out.text
    # a class + method lookups resolve
    assert "class Rate" in describe.handler(_inv({"name": "Rate"})).text
    assert "def __init__" in describe.handler(_inv({"name": "Rate.__init__"})).text


def test_describe_helper_miss_and_path_like_return_ok_not_error(tmp_path):
    describe = {s.name: s for s in _specs(tmp_path, _index(tmp_path))}["mooring_describe_helper"]
    miss = describe.handler(_inv({"name": "nope"}))
    assert "No helper named" in miss.text and not miss.is_error
    esc = describe.handler(_inv({"name": "../secret.py"}))  # a path-like arg finds nothing
    assert "No helper named" in esc.text and not esc.is_error


def test_search_helpers(tmp_path):
    search = {s.name: s for s in _specs(tmp_path, _index(tmp_path))}["mooring_search_helpers"]
    assert "clean" in search.handler(_inv({"query": "clean"})).text
    assert "No helpers match" in search.handler(_inv({"query": "zzzznope"})).text


def test_no_get_source_tool_exists(tmp_path):
    # get_source (real helper BODIES) is deliberately cut from v1 — no floor makes bodies
    # value-blind. (mooring_read_notebook_source is unrelated: the CURRENT notebook is
    # already fully in the system prompt.)
    names = [s.name for s in _specs(tmp_path, _index(tmp_path))]
    assert "mooring_get_source" not in names
    assert not any("helper" in n and "source" in n for n in names)


def test_helper_tools_absent_without_a_code_index(tmp_path):
    names = [
        s.name
        for s in tools.build_tool_specs(
            workspace=tmp_path, folders=(), notebook_rel="nb.py", emit_proposal=lambda *a: None
        )
    ]
    assert not any(n in names for n in tools.HELPER_TOOL_NAMES)


def test_helper_tools_absent_when_index_empty(tmp_path):
    empty = loader.load_index(tmp_path, ["nonexistent"])
    names = [s.name for s in _specs(tmp_path, empty)]
    assert not any(n in names for n in tools.HELPER_TOOL_NAMES)
