"""The value-minimised code model — the allowlist the AST extractor must produce.

The frozen dataclasses here ARE the privacy allowlist, exactly as
:mod:`mooring.ai.datadictionary.model` is for dictionaries. Only these fields may
ever reach the model: a symbol's NAME, a structurally value-free SIGNATURE (built
by hand from ``ast`` arg nodes — never by unparsing a function, which would emit
its body), sanitised type annotations (string/number constants blanked before
unparse), decorator/base NAME-HEADS (call args dropped), and a best-effort-scanned
DOCSTRING. A function/class BODY, any literal, a default-argument value, a
module-level constant value, and comments have **no slot** — a mis-detection
degrades to a missing field, never a leak.

This is a STRUCTURAL guarantee for everything except the one free-text slot,
``docstring`` — that is best-effort minimised (scanned at extraction; see
:mod:`mooring.ai.codelib.docscan`) and human-reviewed, the same weaker tier as a
data-dictionary description. Value-blindness here does NOT lean on the egress
scrubber, which is only a checksum-PII floor.
"""

from __future__ import annotations

from dataclasses import dataclass

DOCSTRING_CAP = 500  # max chars kept from any single docstring (the one free-text slot)


@dataclass(frozen=True)
class Function:
    """A function/method reduced to its callable API surface (never its body).

    ``signature`` is the rendered, value-free form for the model to read; the
    ``posonly``/``params``/``kwonly``/``has_*``/``required_*`` fields are a
    STRUCTURAL arity descriptor (names + counts only) the reuse-lint keys off, so
    it never has to re-parse the rendered string.
    """

    name: str
    signature: str = ""
    docstring: str = ""
    decorators: tuple[str, ...] = ()
    is_async: bool = False
    lineno: int = 0
    end_lineno: int = 0
    posonly: tuple[str, ...] = ()
    params: tuple[str, ...] = ()  # positional-or-keyword arg names
    has_vararg: bool = False
    kwonly: tuple[str, ...] = ()
    has_kwarg: bool = False
    required_positional: int = 0  # count of posonly+params with no default
    required_kwonly: tuple[str, ...] = ()  # kwonly names with no default


@dataclass(frozen=True)
class Class:
    name: str
    bases: tuple[str, ...] = ()  # base NAME-HEADS only (call args / keywords dropped)
    docstring: str = ""
    decorators: tuple[str, ...] = ()
    methods: tuple[Function, ...] = ()
    fields: tuple[str, ...] = ()  # dataclass/attrs field NAMES (values never read)
    lineno: int = 0
    end_lineno: int = 0


@dataclass(frozen=True)
class Module:
    path: str  # workspace-relative POSIX path
    import_path: str = ""  # dotted importable path, or "" when not importable
    importable: bool = False
    import_note: str = ""  # value-free reason when not importable
    is_marimo: bool = False
    docstring: str = ""
    functions: tuple[Function, ...] = ()
    classes: tuple[Class, ...] = ()
    imports: tuple[str, ...] = ()  # imported module/name strings (for the locality signal)
    import_aliases: tuple[tuple[str, str], ...] = ()  # (real, alias)
    star_imports: bool = False
    constants: tuple[str, ...] = ()  # module-level constant NAMES only (values never read)


@dataclass(frozen=True)
class ExtractReport:
    """What a single .py file yielded — surfaced so extraction is never silently wrong.

    ``error`` stores ONLY the exception TYPE name + line (e.g. ``"SyntaxError@42"``),
    NEVER ``str(exc)`` — a SyntaxError's message embeds the offending source line and
    a caret, which is value-bearing. No renderer that can reach the model ever emits
    an error string.
    """

    path: str
    error: str = ""
    n_functions: int = 0
    n_classes: int = 0
    is_marimo: bool = False  # a marimo notebook (its cells are skipped) — for the check output
    dropped_nodes: tuple[tuple[str, int], ...] = ()  # (kind, count) — value-free drift report


@dataclass
class CodeIndex:
    modules: tuple[Module, ...] = ()
    reports: tuple[ExtractReport, ...] = ()

    def is_empty(self) -> bool:
        return not self.modules

    def get(self, name: str) -> list:
        """All matches for ``name`` (a module import_path/stem, a bare function/class
        name, or ``Class.method``), case-insensitive, over the PRE-PARSED in-memory
        objects only — ``name`` is never a filesystem path, so a path-like argument
        finds nothing. Returns EVERY match (same-name ``@overload``/``@property``/
        ``@x.setter`` collide — never silently pick one)."""
        key = (name or "").strip().lower()
        if not key:
            return []
        out: list = []
        for module in self.modules:
            if key in (module.import_path.lower(), _stem(module.path).lower()):
                out.append(module)
            for fn in module.functions:
                if key == fn.name.lower():
                    out.append(fn)
            for cls in module.classes:
                if key == cls.name.lower():
                    out.append(cls)
                for m in cls.methods:
                    if key in (m.name.lower(), f"{cls.name}.{m.name}".lower()):
                        out.append(m)
        return out

    def list_modules(self) -> list[Module]:
        return list(self.modules)

    def search(self, query: str, limit: int = 8) -> list:
        """Substring match over module/function/class names + docstrings
        (value-minimised — the same shape as ``DictionaryIndex.search``)."""
        q = (query or "").strip().lower()
        if not q:
            return []
        scored: list[tuple[int, str, object]] = []
        for module in self.modules:
            score = 0
            if q in module.import_path.lower() or q in _stem(module.path).lower():
                score += 3
            if q in (module.docstring or "").lower():
                score += 1
            for fn in module.functions:
                if q in fn.name.lower():
                    score += 2
                elif q in (fn.docstring or "").lower():
                    score += 1
            for cls in module.classes:
                if q in cls.name.lower():
                    score += 2
                elif q in (cls.docstring or "").lower():
                    score += 1
            if score:
                scored.append((score, module.import_path or module.path, module))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [m for _, _, m in scored[:limit]]


def _stem(path: str) -> str:
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".py") else base


# -- rendering (what the tools / seeding serialise — names + signatures only) ------


def _render_function(fn: Function, *, indent: str = "") -> str:
    head = f"{indent}{'async ' if fn.is_async else ''}def {fn.name}{fn.signature}"
    if fn.decorators:
        head = f"{indent}@{' @'.join(fn.decorators)}\n{head}"
    if fn.docstring:
        head += f'\n{indent}    """{fn.docstring}"""'
    return head


def _render_class(cls: Class, *, indent: str = "", max_methods: int = 40) -> str:
    bases = f"({', '.join(cls.bases)})" if cls.bases else ""
    lines = [f"{indent}class {cls.name}{bases}"]
    if cls.docstring:
        lines.append(f'{indent}    """{cls.docstring}"""')
    if cls.fields:
        lines.append(f"{indent}    fields: {', '.join(cls.fields)}")
    for m in cls.methods[:max_methods]:
        lines.append(_render_function(m, indent=indent + "    "))
    return "\n".join(lines)


def _import_hint(module: Module, name: str) -> str:
    if module.importable and module.import_path:
        return f"  import: from {module.import_path} import {name}"
    if module.import_note:
        return f"  note: {module.import_note}"
    return ""


def render_module(module: Module, *, max_methods: int = 40) -> str:
    """One module as compact API text — import path, signatures, docstrings; NO bodies."""
    lines: list[str] = []
    header = f"Module `{module.import_path or _stem(module.path)}`"
    if module.is_marimo:
        header += " (marimo notebook)"
    lines.append(header)
    if module.importable and module.import_path:
        lines.append(f"  import: from {module.import_path} import <name>")
    elif module.import_note:
        lines.append(f"  note: {module.import_note}")
    if module.docstring:
        lines.append(f"  {module.docstring}")
    for fn in module.functions:
        lines.append(_render_function(fn, indent="  "))
    for cls in module.classes:
        lines.append(_render_class(cls, indent="  ", max_methods=max_methods))
    return "\n".join(lines)


def render_lookup(index: CodeIndex, name: str) -> str:
    """Render every match for ``name`` (a module import_path/stem, a bare function/class
    name, or ``Class.method``) with its suggested import line — the ``describe_helper``
    body. ``''`` when nothing matches. Value-free: names + signatures + scanned docstrings."""
    key = (name or "").strip().lower()
    if not key:
        return ""
    blocks: list[str] = []
    for module in index.modules:
        if key in (module.import_path.lower(), _stem(module.path).lower()):
            blocks.append(render_module(module))
            continue
        parts: list[str] = []
        for fn in module.functions:
            if fn.name.lower() == key:
                parts.append(_render_function(fn))
                parts.append(_import_hint(module, fn.name))
        for cls in module.classes:
            if cls.name.lower() == key:
                parts.append(_render_class(cls))
                parts.append(_import_hint(module, cls.name))
            for m in cls.methods:
                if key in (m.name.lower(), f"{cls.name}.{m.name}".lower()):
                    parts.append(_render_function(m))
                    parts.append(_import_hint(module, cls.name))
        if parts:
            blocks.append("\n".join(p for p in parts if p))
    return "\n\n".join(blocks)


def render_modules(modules, *, max_methods: int = 40) -> str:
    return "\n\n".join(render_module(m, max_methods=max_methods) for m in modules)


def render_listing(index: CodeIndex) -> str:
    """A grouped listing for ``mooring_list_helpers`` — import paths + symbol NAMES only."""
    out: list[str] = []
    for module in sorted(index.modules, key=lambda m: m.import_path or m.path):
        label = module.import_path or _stem(module.path)
        note = "" if module.importable else " (not importable)"
        out.append(f"{label}{note}")
        for fn in module.functions:
            out.append(f"  {fn.name}{fn.signature}")
        for cls in module.classes:
            out.append(f"  class {cls.name} ({len(cls.methods)} methods)")
    return "\n".join(out)


def render_modules_hint(modules) -> str:
    """A names-only capability hint (the seed text): module + symbol names, no signatures."""
    out: list[str] = []
    for module in modules:
        label = module.import_path or _stem(module.path)
        names = [fn.name for fn in module.functions] + [c.name for c in module.classes]
        out.append(f"- {label}: {', '.join(names)}" if names else f"- {label}")
    return "\n".join(out)
