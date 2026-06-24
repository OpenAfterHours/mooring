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


@app.cell(hide_code=True)
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


def create(workspace: Path, name: str, *, title: str | None = None) -> str:
    """Write a new notebook into notebooks/ and return its workspace-relative path.

    The file name is the slug of ``name``; the markdown title is ``title`` when
    given, else ``name`` (so a caller that has resolved a unique, slug-shaped name
    can still keep a human-readable title — see :func:`create_unique`).
    """
    import marimo

    slug = slugify(name)
    rel_path = f"notebooks/{slug}.py"
    target = workspace / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    display = title if title is not None else name
    title_text = display.strip() or slug
    content = TEMPLATE.format(version=marimo.__version__, title=title_text)
    # Exclusive create ("x"): atomic existence-check-and-write, so two concurrent
    # creators (e.g. two batches racing on the same slug) can't both pass an exists()
    # check and have the second clobber the first — the loser gets FileExistsError,
    # which create_unique turns into the next numbered name.
    try:
        with open(target, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
    except FileExistsError as exc:
        raise FileExistsError(f"{rel_path} already exists") from exc
    return rel_path


def create_unique(workspace: Path, name: str) -> str:
    """Like :func:`create`, but never raises :class:`FileExistsError`: on a slug
    collision it appends ``-2``, ``-3``, … until a free file name is found, keeping
    the original ``name`` as the readable title.

    :func:`slugify` is not injective — two display names ("Q1 sales", "Q1: sales!")
    can collapse to one slug — so a *batch* of similar names would abort on the first
    collision. ``create`` stays strict (an interactive "New" should tell the user the
    name is taken); ``create_unique`` is for unattended batch creation, where silently
    de-duplicating is the right behaviour. Still raises :class:`ValueError` when
    ``name`` has no slug-able characters at all (an empty name is a real error).
    """
    base = slugify(name)  # raises ValueError on an unslugable name, like create()
    slug = base
    n = 1
    while True:
        try:
            return create(workspace, slug, title=name)
        except FileExistsError:
            n += 1
            slug = f"{base}-{n}"
