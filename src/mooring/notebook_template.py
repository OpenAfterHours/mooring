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


DEFAULT_FOLDER = "notebooks"


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise ValueError(f"Cannot make a file name out of {name!r}")
    return slug.removesuffix(".py")


def normalize_folder(folder: str | None) -> str:
    """A destination folder as a workspace-relative POSIX path with no surrounding
    slashes; an empty/None value falls back to :data:`DEFAULT_FOLDER`."""
    norm = str(folder or "").replace("\\", "/").strip().strip("/")
    return norm or DEFAULT_FOLDER


def split_target(raw: str, default_folder: str = DEFAULT_FOLDER) -> tuple[str, str]:
    """Split a free-form create input into ``(folder, name)``.

    ``"sales"`` → ``("notebooks", "sales")``; ``"packages/finance/notebooks/sales"`` →
    ``("packages/finance/notebooks", "sales")``. Backslashes are normalized and
    surrounding slashes stripped, so a path from any caller behaves the same. The
    trailing segment is the (still-unslugged) leaf name; everything before it is the
    folder. With no slash, ``default_folder`` is used.
    """
    s = str(raw).replace("\\", "/").strip().strip("/")
    folder, sep, name = s.rpartition("/")
    if not sep:
        return default_folder, s
    return (folder.strip("/") or default_folder), name


def create(workspace: Path, name: str, *, folder: str = DEFAULT_FOLDER, title: str | None = None) -> str:
    """Write a new notebook into ``folder`` (default ``notebooks/``) and return its
    workspace-relative path.

    The file name is the slug of ``name``; the markdown title is ``title`` when
    given, else ``name`` (so a caller that has resolved a unique, slug-shaped name
    can still keep a human-readable title — see :func:`create_unique`). ``folder`` is
    trusted: callers that accept a user-supplied path (see :func:`create_from_input`)
    validate it stays inside the workspace first.
    """
    import marimo

    slug = slugify(name)
    rel_path = f"{normalize_folder(folder)}/{slug}.py"
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


def create_from_input(
    workspace: Path,
    raw: str,
    *,
    folders: tuple[str, ...],
    exclude: tuple[str, ...] = (),
) -> str:
    """Create a notebook from a free-form ``raw`` input that may include a sub-folder
    (e.g. ``"packages/finance/notebooks/sales"``), returning its workspace-relative path.

    Shared by the hub's ``/api/new`` and the CLI's ``mooring new`` so both validate and
    register identically. The target is rejected only when it is genuinely unsafe — it
    escapes the workspace, or lands somewhere sync would never carry it (a dotfile /
    machine dir / a ``[sync] exclude`` match). A folder that simply isn't synced *yet*
    is fine: when the chosen ``folder`` falls outside ``folders`` (the effective sync
    scope) it is recorded in the synced ``mooring.toml`` so it — and future notebooks
    there — ride sync for the whole team (see :func:`mooring.workspace_config.add_extra_folder`).
    """
    from mooring import sync, workspace_config

    folder, name = split_target(raw)
    folder = normalize_folder(folder)
    slug = slugify(name)  # raises ValueError on an empty/unslugable leaf name
    rel_path = f"{folder}/{slug}.py"

    # Path-escape guard (the _open idiom): resolve and confirm the target is under the
    # workspace, so a "../" or absolute path can't write outside it.
    target = (workspace / rel_path).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ValueError("That path is outside the workspace.") from exc
    if not sync.is_synced_path(rel_path, exclude):
        raise ValueError(f"{rel_path} is not a syncable location.")

    created = create(workspace, name, folder=folder)
    if not sync.within_folders(folder, folders):
        workspace_config.add_extra_folder(workspace, folder)
    return created
