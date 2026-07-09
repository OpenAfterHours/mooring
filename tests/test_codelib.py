"""codelib: the ast API-skeleton extractor is value-free BY CONSTRUCTION (not via egress).

The suite-wide SECRET_VALUE_DO_NOT_LEAK sentinel must never survive into a rendered
skeleton through any Python construct, and extraction must never execute the module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mooring.ai import codelib
from mooring.ai.codelib import ast_walk, importpath, loader

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
CARD = "4012888888881881"  # a valid Luhn card (egress WOULD catch this; the drop is structural)


def _extract(source, rel="m.py", **kw):
    return ast_walk.extract_module(source, rel, **kw)


def _rendered(source):
    mod, _ = _extract(source)
    return codelib.render_module(mod)


# -- structural value-freeness: no slot for a value -----------------------------


@pytest.mark.parametrize(
    "source",
    [
        'def f(x):\n    secret = "SECRET_VALUE_DO_NOT_LEAK"\n    return secret\n',  # fn body
        'class C:\n    def m(self):\n        return "SECRET_VALUE_DO_NOT_LEAK"\n',  # method body
        'DATA = ["SECRET_VALUE_DO_NOT_LEAK", 1, {"k": "SECRET_VALUE_DO_NOT_LEAK"}]\n',  # literal
        'API_KEY = "SECRET_VALUE_DO_NOT_LEAK"\n',  # module constant value
        'def f(host="SECRET_VALUE_DO_NOT_LEAK"):\n    pass\n',  # string default
        'class C:\n    TOKEN = "SECRET_VALUE_DO_NOT_LEAK"\n    def __init__(self): pass\n',  # class attr
        'def f():\n    pass  # SECRET_VALUE_DO_NOT_LEAK\n',  # comment
        'LOG = f"loaded SECRET_VALUE_DO_NOT_LEAK rows"\ndef f(): pass\n',  # f-string constant
        'def f(x=4012888888881881):\n    pass\n',  # numeric default (card)
    ],
)
def test_value_never_leaks_structurally(source):
    assert SECRET not in _rendered(source)
    assert CARD not in _rendered(source)


def test_annotation_string_and_number_constants_blanked():
    for ann in ('Literal["SECRET_VALUE_DO_NOT_LEAK"]', 'Annotated[int, "SECRET_VALUE_DO_NOT_LEAK"]', '"SECRET_VALUE_DO_NOT_LEAK"'):
        src = f"def f(x: {ann}) -> {ann}:\n    pass\n"
        rendered = _rendered(src)
        assert SECRET not in rendered
        assert "def f(" in rendered and "x" in rendered  # the param NAME survives


def test_decorator_and_base_render_name_head_only():
    src = (
        "def deco(*a, **k):\n    return lambda f: f\n"
        '@deco("/route", token="SECRET_VALUE_DO_NOT_LEAK")\n'
        "def handler():\n    pass\n"
        'class C(Base, metaclass=Meta, tag="SECRET_VALUE_DO_NOT_LEAK"):\n'
        "    def __init__(self): pass\n"
    )
    mod, _ = _extract(src)
    rendered = codelib.render_module(mod)
    assert SECRET not in rendered
    handler = next(f for f in mod.functions if f.name == "handler")
    assert handler.decorators == ("deco",)  # the call args are gone
    cls = mod.classes[0]
    assert cls.bases == ("Base",)  # keyword args (metaclass/tag) dropped


def test_extract_report_error_carries_no_source():
    mod, report = _extract("def f(:\n    return 'SECRET_VALUE_DO_NOT_LEAK'\n")
    assert report.error.startswith("SyntaxError@")
    assert SECRET not in report.error
    assert "def f(" not in report.error  # no offending source line
    assert mod.functions == () and mod.classes == ()


# -- ast NEVER executes the module ----------------------------------------------


def test_ast_never_executes_side_effects(tmp_path):
    canary = tmp_path / "canary.txt"
    src = (
        "import pathlib\n"
        f"pathlib.Path({canary.as_posix()!r}).write_text('boom')\n"
        "raise RuntimeError('should not run')\n"
        "import sys; sys.exit(1)\n"
        "def reusable(x):\n    return x\n"
    )
    mod, _ = _extract(src)
    assert not canary.exists()  # the module-level side effect never ran
    assert any(f.name == "reusable" for f in mod.functions)


def test_codelib_source_never_imports_or_execs():
    # AST-based (not a text grep) so prose in a docstring never false-positives — assert
    # no codelib module CALLS eval/exec/compile/__import__/literal_eval/import_module or
    # IMPORTS importlib/pkgutil, any of which could execute the analysed source.
    import ast as _ast

    banned_calls = {"eval", "exec", "compile", "__import__"}
    banned_imports = {"importlib", "pkgutil"}
    for py in Path(codelib.__file__).parent.glob("*.py"):
        tree = _ast.parse(py.read_text("utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                target = _ast.unparse(node.func)
                assert target not in banned_calls, f"{py.name}: {target}()"
                assert not target.endswith(".literal_eval"), f"{py.name}: {target}()"
                assert "import_module" not in target, f"{py.name}: {target}()"
            if isinstance(node, _ast.Import):
                for n in node.names:
                    assert n.name.split(".")[0] not in banned_imports, f"{py.name}: import {n.name}"
            if isinstance(node, _ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned_imports, py.name


# -- API names + arity kept, values dropped -------------------------------------


def test_signature_covers_all_arg_categories():
    src = 'async def f(a, b: int, /, c=1, *args, d, e: str = "x", **kw) -> bool:\n    return True\n'
    (fn,) = _extract(src)[0].functions
    assert fn.is_async
    assert fn.signature == "(a, b: int, /, c=..., *args, d, e: str = ..., **kw) -> bool"
    assert fn.required_positional == 2  # a, b (c has a default)
    assert fn.has_vararg and fn.has_kwarg
    assert fn.kwonly == ("d", "e") and fn.required_kwonly == ("d",)


def test_guarded_and_class_defs_collected():
    src = (
        "import typing\n"
        "if typing.TYPE_CHECKING:\n    def guarded(x): ...\n"
        "try:\n    def fast(): ...\nexcept Exception:\n    def fast(): ...\n"
        "class Widget:\n"
        '    name: str = "SECRET_VALUE_DO_NOT_LEAK"\n'
        "    def render(self, n: int = 3): ...\n"
        "    def _private(self): ...\n"
    )
    mod, _ = _extract(src)
    assert {f.name for f in mod.functions} >= {"guarded", "fast"}
    widget = mod.classes[0]
    assert widget.fields == ("name",)  # field NAME kept, its value dropped
    assert {m.name for m in widget.methods} == {"render"}  # _private excluded
    assert SECRET not in codelib.render_module(mod)


def test_pep695_type_alias_is_dropped():
    # `type X = <expr>` (ast.TypeAlias, py3.12+; the project requires 3.12+).
    mod, _ = _extract('type Alias = list["SECRET_VALUE_DO_NOT_LEAK"]\ndef f(): pass\n')
    assert SECRET not in codelib.render_module(mod)


# -- marimo notebooks -----------------------------------------------------------


def test_marimo_notebook_cells_skipped_and_not_importable():
    src = (
        "import marimo\n"
        "app = marimo.App()\n"
        "@app.cell\n"
        'def _():\n    secret = "SECRET_VALUE_DO_NOT_LEAK"\n    return secret\n'
        "@app.function\n"
        "def reusable(x):\n    return x\n"
    )
    mod, _ = _extract(src, rel="nb.py", import_path="nb", importable=True)
    assert mod.is_marimo and not mod.importable and mod.import_note
    assert "_" not in {f.name for f in mod.functions}  # cell wrapper skipped
    assert "reusable" in {f.name for f in mod.functions}  # @app.function kept
    assert SECRET not in codelib.render_module(mod)


# -- docstrings: the ONE best-effort slot ---------------------------------------


def test_docstring_with_matchable_secret_is_withheld():
    token = "sk-" + "a" * 24
    (fn,) = _extract(f'def f():\n    """auth with {token}"""\n    pass\n')[0].functions
    assert fn.docstring == ""  # a high-confidence hit withholds the whole docstring
    (clean,) = _extract('def g():\n    """Normalize date columns to UTC."""\n')[0].functions
    assert clean.docstring == "Normalize date columns to UTC."


def test_docstring_bare_sentinel_is_a_documented_best_effort_gap():
    # DOCUMENTED LIMITATION: the docstring is a best-effort slot (same tier as a
    # dictionary description). A bare prose sentinel a scanner can't match survives; the
    # STRUCTURAL guarantee covers signatures/bodies/literals, NOT free-text docstrings.
    (fn,) = _extract(f'def f():\n    """internal note: {SECRET} lives here"""\n    pass\n')[0].functions
    assert SECRET in fn.docstring


# -- loader: discovery, import paths, robustness --------------------------------


def test_load_index_end_to_end(tmp_path):
    f = tmp_path / "utils" / "helpers.py"
    f.parent.mkdir(parents=True)
    f.write_text('def clean(df, cols: list):\n    """Normalize."""\n    return df\n', "utf-8")
    idx = loader.load_index(tmp_path, ["utils"])
    (mod,) = idx.modules
    assert mod.import_path == "utils.helpers" and mod.importable
    (fn,) = mod.functions
    assert fn.name == "clean" and fn.signature == "(df, cols: list)" and fn.docstring == "Normalize."


def test_load_index_ignores_vendored_trees(tmp_path):
    vend = tmp_path / "utils" / ".venv" / "lib"
    vend.mkdir(parents=True)
    (vend / "vendor.py").write_text(f'KEY = "{SECRET}"\ndef v(): pass\n', "utf-8")
    (tmp_path / "utils" / "ours.py").write_text("def ours(): pass\n", "utf-8")
    idx = loader.load_index(tmp_path, ["utils"])
    paths = {m.import_path for m in idx.modules}
    assert "utils.ours" in paths
    assert not any("vendor" in (m.import_path or "") for m in idx.modules)
    assert SECRET not in codelib.render_modules(idx.modules)


def test_load_index_oversized_rejected(tmp_path):
    (tmp_path / "big.py").write_text("def f(): pass\n" + "# pad\n" * 1000, "utf-8")
    idx = loader.load_index(tmp_path, ["."], max_file_bytes=50)
    assert idx.is_empty()
    assert any(r.error.startswith("TooLarge@") for r in idx.reports)


def test_load_index_excludes_the_open_notebook(tmp_path):
    (tmp_path / "utils" / "a.py").parent.mkdir(parents=True)
    (tmp_path / "utils" / "a.py").write_text("def a(): pass\n", "utf-8")
    (tmp_path / "utils" / "nb.py").write_text("def nb(): pass\n", "utf-8")
    idx = loader.load_index(tmp_path, ["utils"], exclude=["utils/nb.py"])
    assert {m.import_path for m in idx.modules} == {"utils.a"}


def test_importpath_variants(tmp_path):
    (tmp_path / "pkg" / "utils").mkdir(parents=True)
    helpers = tmp_path / "pkg" / "utils" / "helpers.py"
    helpers.write_text("x=1\n", "utf-8")
    assert importpath.dotted_path(helpers, tmp_path) == ("pkg.utils.helpers", True, "")

    init = tmp_path / "pkg" / "__init__.py"
    init.write_text("", "utf-8")
    ip, ok, _ = importpath.dotted_path(init, tmp_path)
    assert ip == "pkg" and ok

    (tmp_path / "my-pkg").mkdir()
    bad = tmp_path / "my-pkg" / "helpers.py"
    bad.write_text("x=1\n", "utf-8")
    ip, ok, note = importpath.dotted_path(bad, tmp_path)
    assert not ok and "my-pkg" in note


@pytest.mark.parametrize(
    "source",
    [
        'def f(x=(y := "SECRET_VALUE_DO_NOT_LEAK")):\n    pass\n',  # walrus in a default
        'def f(cb=lambda: "SECRET_VALUE_DO_NOT_LEAK"):\n    pass\n',  # lambda default
        'def f(x=make("SECRET_VALUE_DO_NOT_LEAK")):\n    pass\n',  # call default
        'def f(x: Dict["SECRET_VALUE_DO_NOT_LEAK", int]):\n    pass\n',  # subscript annotation literal
        'def f[T: "SECRET_VALUE_DO_NOT_LEAK"](x: T):\n    pass\n',  # PEP 695 type-param bound
        '__doc__ = "SECRET_VALUE_DO_NOT_LEAK"\ndef f(): pass\n',  # module __doc__ assignment
        'def f(a="CA" "RD", b=b"SECRET_VALUE_DO_NOT_LEAK"):\n    pass\n',  # concat + bytes default
        'token = "SECRET_VALUE_DO_NOT_LEAK"\ndef f(): pass\n',  # lowercase constant value
        '@registry["SECRET_VALUE_DO_NOT_LEAK"]\ndef f(): pass\n',  # subscript decorator index
        'def f():\n    match z:\n        case "SECRET_VALUE_DO_NOT_LEAK":\n            pass\n',  # match in body
        'X = Literal["SECRET_VALUE_DO_NOT_LEAK"]\ndef f(): pass\n',  # module-level Literal alias RHS
    ],
)
def test_adversarial_constructs_never_leak_values(source):
    # Regression net for the value-leak hunt (16-way adversarial probe): every VALUE
    # planted as CODE — never an identifier name — must be stripped from EVERY stored
    # field, checked over the render + the full Module/Report repr (not just what renders).
    mod, report = _extract(source)
    blob = codelib.render_module(mod) + repr(mod) + repr(report)
    assert SECRET not in blob


def test_helper_seed_is_empty_on_no_match(tmp_path):
    # Byte-identity guard: a non-empty index whose modules don't match the notebook seeds
    # nothing (no dangling head), so the system context is unchanged when nothing is relevant.
    from mooring.ai import locality

    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "h.py").write_text("def unrelated(): pass\n", "utf-8")
    idx = loader.load_index(tmp_path, ["utils"])
    mods, reasons, more = locality.helper_working_set(
        idx, notebook_source="import pandas\n", notebook_rel="nb.py"
    )
    assert locality.helper_seed_text(mods, reasons, more) == ""
    # but a notebook that imports it DOES seed
    mods2, r2, m2 = locality.helper_working_set(
        idx, notebook_source="import utils.h\n", notebook_rel="nb.py"
    )
    assert "unrelated" in locality.helper_seed_text(mods2, r2, m2)


def test_code_index_get_and_search(tmp_path):
    (tmp_path / "lib.py").write_text(
        "def alpha(x): pass\nclass Beta:\n    def m(self): pass\n", "utf-8"
    )
    idx = loader.load_index(tmp_path, ["."])
    assert idx.get("alpha") and idx.get("Beta") and idx.get("Beta.m")
    assert idx.get("../escape.py") == []  # a path-like argument finds nothing
    assert [m.name if hasattr(m, "name") else m for m in idx.search("alpha")]
