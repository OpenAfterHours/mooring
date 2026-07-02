"""Shared notebook/workspace operations both adapters render.

Each function here used to exist twice — near-verbatim in ``cli.py`` and in
``hub/server.py`` — because the sibling-isolation rule (the hub must not import
the cli) left the duplicated policy no home. Any change to the open gate, the
shadow policy, the client construction, or the adopt flow had to be made twice
and kept behaviorally identical by hand. Now the POLICY lives here and the
adapters keep only their transport: the CLI prints and ``sys.exit``\\ s, the hub
returns JSON. Nothing here may exit the process — the hub calls these in-process,
so every refusal is an exception the adapter translates.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from mooring import auth, notebook_template, pyproject_env, shadow, sync, workspace_config
from mooring.config import Config
from mooring.github import AuthFailed, GitHubClient


class NotConfigured(AuthFailed):
    """The team repo (owner/repo/client_id) is not configured.

    A subclass of :class:`AuthFailed` so every hub path that already handles
    "can't reach GitHub as this user" degrades gracefully; the CLI catches it
    first to print its config-file guidance instead.
    """


def client_for(cfg: Config) -> GitHubClient:
    """Build the team GitHub client, RAISING on missing config or token.

    Never exits the process: an L3.5 helper that called ``sys.exit`` would take
    the whole hub down with it. The CLI adapter translates :class:`NotConfigured`
    / :class:`AuthFailed` into its exit messages; the hub lets them surface as
    401-shaped JSON errors. Constructor args are byte-identical to what both
    adapters built before, so sync behavior is untouched.
    """
    if not cfg.is_configured:
        raise NotConfigured("No team repo configured.")
    token = auth.get_token(host=cfg.host)
    if not token:
        raise AuthFailed("Not logged in.")
    return GitHubClient(token, cfg.owner, cfg.repo, host=cfg.host)


def ws_file(workspace: Path, rel: str, *, suffix: str | None = None) -> Path:
    """Resolve a workspace-relative path, rejecting escapes/missing files."""
    # Reject any dot-prefixed path component (mirrors sync.is_synced_path) so the
    # internal state dir — .mooring/ (manifest + undo snapshots) — is structurally
    # unreachable through this resolver regardless of caller. Defence in depth.
    if any(part.startswith(".") for part in Path(str(rel).replace("\\", "/")).parts):
        raise ValueError("Path is not allowed.")
    target = (workspace / rel).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ValueError("Path escapes the workspace.") from exc
    if suffix and not rel.endswith(suffix):
        raise ValueError(f"Expected a {suffix} file.")
    if not target.is_file():
        raise FileNotFoundError(rel)
    return target


class OpenRefused(Exception):
    """The path must not be opened; ``str(exc)`` is the user-facing reason."""


def openable_kind(target: Path, rel_path: str, *, display: str | None = None) -> str:
    """Gate an open request: ``"pbip"`` for a Power BI project, ``"notebook"``
    for a real marimo notebook (or a blank stub, which becomes one), and
    :class:`OpenRefused` for everything else.

    The module refusal is the load-bearing case: the marimo editor rewrites what
    it opens into notebook form on save, so opening a plain Python module (or a
    dunder package marker like ``__init__.py``, even empty) would corrupt it —
    see :func:`mooring.notebook_template.opens_as_notebook`. The whole file is
    read: the ``app = marimo.App(`` marker can sit past a large leading header.
    ``display`` is how the file is named in the refusal (the hub shows the
    basename, the CLI the workspace-relative path). Existence and
    workspace-containment stay adapter-side — they are transport-shaped
    (404 vs exit message).
    """
    if rel_path.endswith(".pbip"):
        return "pbip"
    if not rel_path.endswith(".py"):
        raise OpenRefused("Only .py notebooks and .pbip projects can be opened.")
    source = target.read_bytes().decode("utf-8", "ignore")
    if not notebook_template.opens_as_notebook(rel_path, source):
        shown = display if display is not None else rel_path.rsplit("/", 1)[-1]
        raise OpenRefused(
            f"{shown} is a Python module, not a marimo notebook — opening it in the "
            "editor could overwrite it. Import it from a notebook instead."
        )
    return "notebook"


def shadow_policy(workspace: Path) -> tuple[frozenset[str], frozenset[str]]:
    """The (extra, ignore) sets parameterising the shadow guard — THE single
    assembly point (previously duplicated as ``cli._shadow_policy`` and
    ``Hub._shadow_policy``, with a comment lamenting that the adapters couldn't
    share it)."""
    return (
        pyproject_env.importable_names(workspace),
        frozenset(workspace_config.shadow_ignored(workspace)),
    )


def open_shadow_findings(workspace: Path, rel_path: str) -> dict[str, str]:
    """Shadow findings relevant to opening ``rel_path``.

    The notebook's own folder AND the workspace root are both on the kernel's
    ``sys.path`` (the latter via ``runtime.pythonpath`` — see ``editor.py``), so
    both are scanned. Folder-scoped: opening an innocent notebook still warns
    when a sibling poisons the directory. Backend-independent — the plain
    ``sys.path[0]`` trap bites uv and frozen runs alike.
    """
    extra, ignore = shadow_policy(workspace)
    return {
        **shadow.root_shadows(workspace, extra=extra, ignore=ignore),
        **shadow.folder_shadows(rel_path, workspace=workspace, extra=extra, ignore=ignore),
    }


def resolve_adoptable(
    candidates, requested: list[str]
) -> tuple[list[str], list[str]]:
    """Normalize the requested folder names and split them against what discovery
    actually found: ``(chosen, unknown)``. Adopt must never register a typo or a
    non-existent folder; what to do about ``unknown`` is adapter policy (the CLI
    refuses the whole command, the hub silently adopts the valid subset)."""
    known = {c.folder for c in candidates}
    chosen: list[str] = []
    unknown: list[str] = []
    for raw in requested:
        norm = workspace_config.normalize_notebook(raw)
        (chosen if norm in known else unknown).append(norm)
    return chosen, unknown


def adopt_folders(client, cfg: Config, chosen: list[str]):
    """Register ``chosen`` in the synced ``mooring.toml`` and pull the widened scope.

    The registration goes to the SYNCED workspace config (via
    :func:`mooring.workspace_config.add_extra_folders`), so pushing it shares the
    new scope with the whole team; the pull runs with the freshly merged folder
    list so the adopted content lands immediately. Raises
    ``tomllib.TOMLDecodeError`` when ``mooring.toml`` is unparseable — the
    adapters own the phrasing of that error.
    """
    workspace = cfg.workspace()
    workspace_config.add_extra_folders(workspace, chosen)
    new_folders = workspace_config.merge_extra_folders(cfg.folders, workspace)
    return sync.pull(client, replace(cfg, folders=new_folders))
