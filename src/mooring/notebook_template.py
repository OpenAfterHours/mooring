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

# Every marimo notebook constructs its app with a top-level ``app = marimo.App(...)``
# statement — mooring's TEMPLATE and marimo's own codegen both emit it at column 0.
# Anchoring to that ASSIGNMENT (not a bare ``marimo.App(`` substring) is what tells a
# runnable notebook from a plain helper module that merely mentions the call in a
# comment, docstring, or factory (``return marimo.App(...)``) — the editor must NOT
# open such a module, since marimo would rewrite it into notebook form on save.
_MARIMO_APP_RE = re.compile(r"^\w+\s*=\s*marimo\.App\(", re.MULTILINE)


def is_marimo_app(source: str) -> bool:
    """Whether ``source`` looks like a marimo notebook rather than a plain Python
    module: it contains a top-level ``<name> = marimo.App(`` statement. Content-only
    and best-effort (not a full parse); a leading UTF-8 BOM doesn't affect the per-line
    match. Pass the WHOLE source, not a truncated head — a large leading header (e.g. a
    PEP 723 dependency block) can push the marker well past the first few KB. Empty or
    marker-less source is False; a caller that wants to allow opening a blank stub
    checks ``source.strip()`` separately."""
    return _MARIMO_APP_RE.search(source) is not None


# A markdown cell is authored as ``mo.md("…")`` (the template writes the title as
# ``mo.md(r"""# {title}""")``). Match the FIRST such call's string body — triple- or
# single-quoted, with an optional string prefix — non-greedily up to the matching quote.
_MD_CALL_RE = re.compile(r"""mo\.md\(\s*[rRfFbBuU]*('''|\"\"\"|'|")(.*?)\1""", re.DOTALL)
# A markdown H1: `# Heading` (allow up to 3 leading spaces and trailing ATX `#`s).
_MD_H1_RE = re.compile(r"^ {0,3}#\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _clean_title(text: str) -> str:
    return " ".join(text.split())[:120]


def notebook_title(source: str) -> str:
    """A human title for a notebook, harvested VALUE-FREE from its first markdown cell.

    Prefers the first ``# H1`` heading found across the ``mo.md(...)`` cells; failing
    that, the first non-empty line of the first markdown cell (its leading ``#``s
    trimmed). This is authored text — the same kind of content the notebook source
    already carries, never a data value — surfaced only in the local hub listing so a
    growing repo of files like ``q3_recon_v2.py`` is legible. Returns ``""`` when there
    is no markdown cell / heading. Best-effort and content-only (not a full parse)."""
    first_line = ""
    for match in _MD_CALL_RE.finditer(source):
        body = match.group(2)
        h1 = _MD_H1_RE.search(body)
        if h1:
            return _clean_title(h1.group(1))
        if not first_line:
            for line in body.splitlines():
                if line.strip():
                    first_line = _clean_title(line.strip().lstrip("#").strip())
                    break
    return first_line


def opens_as_notebook(name: str, source: str) -> bool:
    """Whether the ``.py`` at ``name`` (a filename or workspace-relative path) with body
    ``source`` should open in the marimo editor.

    True for a real marimo app (:func:`is_marimo_app`) or a blank stub — a freshly
    created empty ``.py`` becomes a new notebook. The one exception is a **dunder package
    marker** (``__init__.py`` / ``__main__.py``, i.e. ``__<name>__.py``): those are
    structural Python files that are legitimately empty, and opening one in marimo would
    rewrite it into notebook form on save (and, under ``--watch`` autorun, execute it),
    corrupting the package. Only ``name``'s final path component is inspected; a dunder
    file that genuinely contains a ``marimo.App`` marker still counts (that path is
    intentional and vanishingly rare). This is the single source of truth shared by the
    hub's listing sniff and both open guards — keep callers pointed here so a stale
    client or the CLI can't diverge from the badge."""
    if is_marimo_app(source):
        return True
    stem = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    is_dunder = stem.startswith("__") and stem.endswith("__.py")
    return not source.strip() and not is_dunder


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


def duplicate_as_draft(
    workspace: Path,
    rel_path: str,
    *,
    owner: str,
    exclude: tuple[str, ...] = (),
) -> str:
    """Byte-copy the notebook at ``rel_path`` to a personal draft sibling —
    ``{stem}-{owner}-draft.py`` (or plain ``-draft`` when ``owner`` is empty, e.g.
    local mode) — and return the copy's workspace-relative path.

    The copy is a safe playground: to the three-way sync engine it is just a new
    local file (no manifest base, no remote path), so it can never conflict with
    the team file and reaches the repo only on an explicit push. The bytes are
    copied verbatim (no parse, no re-encoding — so no UTF-8 BOM risk), and the
    always-present hyphen makes the stem a non-identifier, so the shadow guard
    structurally cannot fire on a draft. Duplicating a draft collapses its own
    suffix first (``sales-phil-draft`` → ``sales-phil-draft-2``, never
    ``…-draft-phil-draft``); the collapse is scoped to the exact suffixes this
    function mints, so a notebook that merely *ends* in another word keeps it.
    On a name collision the suffix gains a counter, the :func:`create_unique`
    idiom. The source must open as a notebook (:func:`opens_as_notebook` — a
    helper module or ``__init__.py`` is refused) and the target must pass
    ``sync.is_synced_path``, so a team ``[sync] exclude`` pattern like
    ``*-draft.py`` produces a clear error instead of an invisible file.
    """
    rel = str(rel_path).replace("\\", "/").strip().strip("/")
    if not rel:
        raise ValueError("No path given.")

    # Path-escape guard (the _open idiom): resolve and confirm the source is under
    # the workspace, so a "../" or absolute path can't read or write outside it.
    target = (workspace / rel).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ValueError("That path is outside the workspace.") from exc
    if not target.is_file():
        raise FileNotFoundError(f"No such notebook: {rel}")

    source = target.read_bytes()
    folder, _, name = rel.rpartition("/")
    if not name.endswith(".py") or not opens_as_notebook(rel, source.decode("utf-8", "ignore")):
        raise ValueError(f"{rel} is not a marimo notebook.")

    # Collapse a suffix THIS feature minted (-draft / -{owner}-draft, with an
    # optional counter) so a draft-of-a-draft numbers up instead of stacking.
    # Deliberately narrow: "annual-report-draft" only loses "-draft" — the
    # pattern never swallows words that aren't the caller's own suffix.
    stem = name.removesuffix(".py")
    own = f"{re.escape(owner)}-" if owner else ""
    stem = re.sub(rf"-(?:{own})?draft(?:-\d+)?$", "", stem) or stem

    from mooring import sync

    base = f"{stem}-{owner + '-' if owner else ''}draft"
    prefix = f"{folder}/" if folder else ""
    candidate = f"{prefix}{base}.py"
    if not sync.is_synced_path(candidate, exclude):
        raise ValueError(f"{candidate} is not a syncable location.")
    n = 1
    while True:
        # Exclusive create ("xb"), the create() idiom: the loser of a race gets
        # FileExistsError and takes the next numbered name. Bytes, not text —
        # the copy must be identical whatever the source encoding.
        try:
            with open(workspace / candidate, "xb") as fh:
                fh.write(source)
        except FileExistsError:
            n += 1
            candidate = f"{prefix}{base}-{n}.py"
        else:
            return candidate
