"""Extract a value-free API skeleton from the team's importable .py modules.

The code-library analogue of :mod:`mooring.ai.datadictionary`: so the value-blind
copilot can DISCOVER and REUSE the team's helper functions/classes, mooring parses
each module with ``ast`` (**never importing or executing it**) and keeps only its
callable surface — names, structurally value-free signatures, sanitised type hints,
decorator/base name-heads, and a best-effort-scanned docstring. A function body, a
literal, a default value, a constant value, and a comment have **no slot**.

The frozen dataclasses in :mod:`.model` ARE the allowlist. This is a STRUCTURAL
value-blindness guarantee for everything except the one free-text ``docstring`` slot
(best-effort minimised + human-reviewed, the weaker dictionary-description tier) — and
it does NOT lean on the egress scrubber, which is only a checksum-PII floor.

Layout mirrors ``datadictionary``: :mod:`.model` is the model + renderers, :mod:`.ast_walk`
the allowlist walk, :mod:`.importpath` the (metadata-only) import resolver, :mod:`.docscan`
the docstring scanner, :mod:`.loader` the file discovery + orchestration.
"""

from __future__ import annotations

from mooring.ai.codelib.loader import DEFAULT_MAX_FILE_BYTES, load_index
from mooring.ai.codelib.model import (
    DOCSTRING_CAP,
    Class,
    CodeIndex,
    ExtractReport,
    Function,
    Module,
    render_listing,
    render_lookup,
    render_module,
    render_modules,
    render_modules_hint,
)

__all__ = [
    "Function",
    "Class",
    "Module",
    "ExtractReport",
    "CodeIndex",
    "load_index",
    "render_module",
    "render_modules",
    "render_listing",
    "render_lookup",
    "render_modules_hint",
    "DOCSTRING_CAP",
    "DEFAULT_MAX_FILE_BYTES",
]
