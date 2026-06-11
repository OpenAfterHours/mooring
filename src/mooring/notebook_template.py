"""Create new marimo notebooks from a minimal template."""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = '''import marimo

__generated_with = "{version}"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(r"""# {title}""")
    return


if __name__ == "__main__":
    app.run()
'''


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise ValueError(f"Cannot make a file name out of {name!r}")
    return slug.removesuffix(".py")


def create(workspace: Path, name: str) -> str:
    """Write a new notebook into notebooks/ and return its workspace-relative path."""
    import marimo

    slug = slugify(name)
    rel_path = f"notebooks/{slug}.py"
    target = workspace / rel_path
    if target.exists():
        raise FileExistsError(f"{rel_path} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    title = name.strip() or slug
    target.write_text(
        TEMPLATE.format(version=marimo.__version__, title=title),
        encoding="utf-8",
        newline="\n",
    )
    return rel_path
