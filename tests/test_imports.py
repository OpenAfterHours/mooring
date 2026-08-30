"""Every module in the package must parse, and must import.

A file that does not parse fails LOUDLY here — once, naming itself — instead of as a
scatter of unrelated collection errors in whichever test files happen to import it. The
prompt for this was real: a docstring in ``ai/tools.py`` contained the literal
``mo.md(\"\"\"`` (escaped here for exactly the reason it had to be escaped there), whose
triple quote closed the docstring early; the resulting SyntaxError landed nowhere near
the cause and broke collection in seven other files at once.

The compile pass covers EVERY module, including ones whose third-party dependency is an
optional extra this environment may not have. The import pass then goes further — it
catches a module that parses but blows up on a module-level statement — for every module
that can be imported here.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import mooring

_SRC = Path(mooring.__file__).resolve().parent


def _module_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _module_names() -> list[str]:
    return sorted(
        info.name
        for info in pkgutil.walk_packages(mooring.__path__, prefix="mooring.")
    )


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: str(p.relative_to(_SRC)))
def test_every_module_parses(path: Path):
    # `compile`, not `import`: it needs no dependency to be installed, so it covers the
    # optional-extra modules (the spaCy backend, anything reaching the Copilot SDK) that
    # the import pass below has to skip on a lean install.
    compile(path.read_text("utf-8"), str(path), "exec")


@pytest.mark.parametrize("name", _module_names())
def test_every_module_imports(name: str):
    try:
        importlib.import_module(name)
    except ImportError as exc:
        # An absent OPTIONAL third-party package is this environment's business, not a
        # defect: mooring ships lean and the extras (copilot, pii/pii-spacy, …) are
        # installed per-machine. A missing `mooring.*` module is always ours, so it still
        # fails — which is the whole point of importing rather than only compiling.
        missing = (exc.name or "").split(".")[0]
        if not missing or missing == "mooring":
            raise
        pytest.skip(f"optional dependency not installed: {missing}")
