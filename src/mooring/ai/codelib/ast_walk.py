"""Statically extract a value-free API skeleton from Python source via ``ast``.

The load-bearing privacy property: this NEVER imports, executes, compiles-to-eval,
or ``ast.literal_eval``s the module — it only ``ast.parse``s text — so a module with
an import-time side effect is analysed without triggering it. And every KEPT field is
value-free BY CONSTRUCTION, because the extractor has no slot for a body, a literal,
a default value, a constant value, a decorator/base call argument, or a comment. It
does not rely on the egress scrubber (a checksum-PII floor only). The one exception is
docstrings, scanned best-effort here (see :mod:`mooring.ai.codelib.docscan`).
"""

from __future__ import annotations

import ast
import copy
from collections import Counter

from mooring.ai.codelib import docscan
from mooring.ai.codelib.model import DOCSTRING_CAP, Class, ExtractReport, Function, Module


def extract_module(
    source: str,
    rel: str,
    *,
    import_path: str = "",
    importable: bool = False,
    import_note: str = "",
    is_marimo: bool = False,
) -> tuple[Module, ExtractReport]:
    """Parse ``source`` into a value-free :class:`Module` + a drift :class:`ExtractReport`.

    Never raises for bad input: a ``SyntaxError`` (or any parse error) degrades to an
    empty module and a report whose ``error`` is the exception TYPE + line ONLY — never
    ``str(exc)``, whose message embeds the offending source line.
    """
    dropped: Counter = Counter()
    try:
        tree = ast.parse(source, type_comments=False)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return (
            Module(
                path=rel, import_path=import_path, importable=importable,
                import_note=import_note, is_marimo=is_marimo,
            ),
            ExtractReport(path=rel, error=f"{type(exc).__name__}@{getattr(exc, 'lineno', 0) or 0}"),
        )

    marimo = is_marimo or _detect_marimo(tree)
    if marimo and importable:
        # A marimo notebook runs its app on import — its cells are not the module's
        # public surface, so it is not a usable helper import target.
        importable = False
        import_note = "marimo notebook - cells are not an importable module surface"
    module_doc, m_withheld = _clean_doc(tree)
    if m_withheld:
        dropped["docstring"] += 1

    functions: list[Function] = []
    classes: list[Class] = []
    imports: list[str] = []
    aliases: list[tuple[str, str]] = []
    constants: list[str] = []
    state = {"star": False}

    def collect(stmt: ast.stmt) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if marimo and _is_marimo_cell(stmt):
                dropped["marimo_cell"] += 1
            elif _is_public(stmt.name):
                functions.append(_function(stmt, dropped))
            else:
                dropped["private"] += 1
        elif isinstance(stmt, ast.ClassDef):
            if _is_public(stmt.name):
                classes.append(_class(stmt, dropped))
            else:
                dropped["private"] += 1
        elif isinstance(stmt, ast.Import):
            for n in stmt.names:
                imports.append(n.name)
                if n.asname:
                    aliases.append((n.name, n.asname))
        elif isinstance(stmt, ast.ImportFrom):
            mod = ("." * (stmt.level or 0)) + (stmt.module or "")
            for n in stmt.names:
                if n.name == "*":
                    state["star"] = True
                else:
                    imports.append(f"{mod}.{n.name}" if mod else n.name)
                    if n.asname:
                        aliases.append((n.name, n.asname))
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    constants.append(t.id)
            dropped["constant_value"] += 1  # the RHS is NEVER read
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id.isupper():
                constants.append(stmt.target.id)
            if stmt.value is not None:
                dropped["constant_value"] += 1
        elif isinstance(stmt, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
            # Recurse ONLY into guarded bodies (if TYPE_CHECKING / try-import / version
            # guards) — still value-free, every non-def statement's value is never read.
            for sub in _guarded_bodies(stmt):
                collect(sub)
        elif isinstance(stmt, ast.Expr):
            pass  # a docstring or a bare expression — no API surface, no value read
        else:
            dropped[type(stmt).__name__] += 1

    for stmt in tree.body:
        collect(stmt)

    module = Module(
        path=rel,
        import_path=import_path,
        importable=importable,
        import_note=import_note,
        is_marimo=marimo,
        docstring=module_doc,
        functions=tuple(functions),
        classes=tuple(classes),
        imports=tuple(imports),
        import_aliases=tuple(aliases),
        star_imports=state["star"],
        constants=tuple(dict.fromkeys(constants)),
    )
    report = ExtractReport(
        path=rel,
        n_functions=len(functions),
        n_classes=len(classes),
        is_marimo=marimo,
        dropped_nodes=tuple(sorted(dropped.items())),
    )
    return module, report


# -- symbol builders ---------------------------------------------------------


def _function(node, dropped: Counter) -> Function:
    dropped["body"] += 1  # the body is NEVER read into any slot
    sig, arity = _build_signature(node)
    doc, withheld = _clean_doc(node)
    if withheld:
        dropped["docstring"] += 1
    return Function(
        name=node.name,
        signature=sig,
        docstring=doc,
        decorators=_decorator_heads(node.decorator_list, dropped),
        is_async=isinstance(node, ast.AsyncFunctionDef),
        lineno=getattr(node, "lineno", 0) or 0,
        end_lineno=getattr(node, "end_lineno", 0) or 0,
        **arity,
    )


def _class(node, dropped: Counter) -> Class:
    bases: list[str] = []
    for b in node.bases:
        head = _name_head(b)
        if head is None:
            dropped["base"] += 1
        else:
            bases.append(head)
    if node.keywords:
        dropped["class_keyword"] += len(node.keywords)  # metaclass= / tag="X" — dropped whole
    doc, withheld = _clean_doc(node)
    if withheld:
        dropped["docstring"] += 1
    methods: list[Function] = []
    fields: list[str] = []
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_public(stmt.name):
                methods.append(_function(stmt, dropped))
            else:
                dropped["private"] += 1
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.append(stmt.target.id)  # dataclass/attrs field NAME; value never read
            if stmt.value is not None:
                dropped["field_value"] += 1
    return Class(
        name=node.name,
        bases=tuple(bases),
        docstring=doc,
        decorators=_decorator_heads(node.decorator_list, dropped),
        methods=tuple(methods),
        fields=tuple(dict.fromkeys(fields)),
        lineno=getattr(node, "lineno", 0) or 0,
        end_lineno=getattr(node, "end_lineno", 0) or 0,
    )


# -- value-free renderers of sub-parts ---------------------------------------


def _build_signature(node) -> tuple[str, dict]:
    """Hand-build the signature from ``node.args`` — NEVER ``ast.unparse(node)`` (that
    emits the body). A present default renders as ``...`` (optionality, not the value)."""
    a = node.args
    posonly = list(a.posonlyargs)
    normal = list(a.args)
    pos = posonly + normal
    n_no_default = len(pos) - len(a.defaults)
    parts: list[str] = []
    idx = 0
    for arg in posonly:
        parts.append(_render_arg(arg, idx >= n_no_default))
        idx += 1
    if posonly:
        parts.append("/")
    for arg in normal:
        parts.append(_render_arg(arg, idx >= n_no_default))
        idx += 1
    if a.vararg is not None:
        parts.append("*" + _render_arg(a.vararg, False))
    elif a.kwonlyargs:
        parts.append("*")
    required_kwonly: list[str] = []
    for i, arg in enumerate(a.kwonlyargs):
        has_default = a.kw_defaults[i] is not None
        parts.append(_render_arg(arg, has_default))
        if not has_default:
            required_kwonly.append(arg.arg)
    if a.kwarg is not None:
        parts.append("**" + _render_arg(a.kwarg, False))
    sig = "(" + ", ".join(parts) + ")"
    if node.returns is not None:
        sig += " -> " + _sanitize_annotation(node.returns)
    arity = {
        "posonly": tuple(x.arg for x in posonly),
        "params": tuple(x.arg for x in normal),
        "has_vararg": a.vararg is not None,
        "kwonly": tuple(x.arg for x in a.kwonlyargs),
        "has_kwarg": a.kwarg is not None,
        "required_positional": n_no_default,
        "required_kwonly": tuple(required_kwonly),
    }
    return sig, arity


def _render_arg(arg, has_default: bool) -> str:
    s = arg.arg
    if arg.annotation is not None:
        s += ": " + _sanitize_annotation(arg.annotation)
        if has_default:
            s += " = ..."
    elif has_default:
        s += "=..."
    return s


def _sanitize_annotation(node) -> str:
    """Unparse a type annotation with every string/number Constant subnode BLANKED, so
    ``Literal["SECRET"]`` / ``Annotated[int, "x"]`` / a string forward-ref render
    value-free. Structural — NOT trusted to the egress floor. Degrades to ``...`` on any
    error rather than risk leaking."""
    try:
        clone = copy.deepcopy(node)
        for sub in ast.walk(clone):
            if isinstance(sub, ast.Constant) and sub.value is not None and not isinstance(
                sub.value, bool
            ):
                sub.value = ...  # blanks str/bytes/int/float; keeps None/bool (type-meaningful)
        return ast.unparse(clone)
    except Exception:  # noqa: BLE001 — never leak; a broken annotation degrades to "..."
        return "..."


def _decorator_heads(decorator_list, dropped: Counter) -> tuple[str, ...]:
    heads: list[str] = []
    for d in decorator_list:
        head = _name_head(d)
        if head is None:
            dropped["decorator"] += 1  # lambda / non-name decorator — dropped whole
        else:
            heads.append(head)
            if isinstance(d, (ast.Call, ast.Subscript)):
                dropped["decorator_arg"] += 1  # @route("/x", token="X") -> "route"
    return tuple(heads)


def _name_head(node):
    """The dotted NAME-HEAD of a decorator/base (``Name``/``Attribute`` chain), with any
    ``Call`` args or ``Subscript`` index dropped. ``None`` for a lambda/other so the whole
    node is dropped (never unparsed)."""
    target = node
    if isinstance(node, ast.Call):
        target = node.func
    elif isinstance(node, ast.Subscript):
        target = node.value
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        parts: list[str] = []
        cur = target
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    return None


def _clean_doc(node) -> tuple[str, bool]:
    """``(docstring, withheld)`` — capped and best-effort scanned; withheld -> ('' , True)."""
    try:
        d = ast.get_docstring(node, clean=True) or ""
    except Exception:  # noqa: BLE001 — a weird node yields no docstring, never a crash
        return "", False
    if not d:
        return "", False
    if len(d) > DOCSTRING_CAP:
        d = d[:DOCSTRING_CAP].rstrip() + " ...[trimmed]"
    if docscan.scan_docstring(d):
        return "", True
    return d, False


def _is_public(name: str) -> bool:
    return not name.startswith("_") or name == "__init__"


def _guarded_bodies(stmt) -> list:
    bodies: list = []
    for attr in ("body", "orelse", "finalbody"):
        bodies.extend(getattr(stmt, attr, None) or [])
    for handler in getattr(stmt, "handlers", None) or []:
        bodies.extend(handler.body or [])
    return bodies


def _detect_marimo(tree) -> bool:
    imports_marimo = any(
        (isinstance(s, ast.Import) and any(n.name.split(".")[0] == "marimo" for n in s.names))
        or (isinstance(s, ast.ImportFrom) and (s.module or "").split(".")[0] == "marimo")
        for s in tree.body
    )
    if not imports_marimo:
        return False
    for s in tree.body:
        if isinstance(s, ast.Assign) and isinstance(s.value, ast.Call):
            head = _name_head(s.value)
            if head and head.split(".")[-1] == "App" and any(
                isinstance(t, ast.Name) and t.id == "app" for t in s.targets
            ):
                return True
    return False


def _is_marimo_cell(fn) -> bool:
    for d in fn.decorator_list:
        head = _name_head(d)
        if head and head.split(".")[-1] == "cell":
            return True
    return fn.name == "_"
