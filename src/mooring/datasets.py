"""Point at the big data files that live OUTSIDE the repo — the file-shaped sibling of
:mod:`mooring.connections`.

A push warns at 10 MB and refuses at 45 MB (``sync._read_checked``), so a team's real
source data — the 400 MB parquet on ``\\\\fileserver\\finance`` — cannot travel with the
notebook. Today "elsewhere" means a UNC path typed into one analyst's cell: invisible to
everyone else, and broken the moment IT remounts the share. This module gives that
location a NAME, split the same two ways a connection is:

* the POINTER (name, kind, location) lives in the synced ``mooring.toml``
  ``[datasets]`` table (see :mod:`mooring.workspace_config`) — it travels with the repo,
  so ``md.path("sales")`` means the same file for the whole team, with a
  credential-bearing location (a SAS / pre-signed URL) REFUSED on write;
* the per-machine OVERRIDE lives ONLY in a local source this module resolves — a
  ``MOORING_DATASET_<NAME>_PATH`` environment variable, or a
  ``.mooring/datasets.local.toml`` file that :func:`mooring.sync.is_synced_path` excludes
  on both scan sides — so the teammate whose share is mounted at ``D:`` redirects the
  name without touching what the team sees.

A notebook resolves the two at run time via the injected ``mooring_datasets`` helper
(:mod:`mooring._datasets_runtime`, installed onto the kernel path like
``mooring_connections``)::

    import mooring_datasets as md
    sales = pl.read_parquet(md.path("sales"))
    mi.fingerprint(sales, "sales", path=md.path("sales"))   # composes with mooring_inputs

The pointer says WHERE the data came from; the input fingerprint proves it hasn't moved.

Resolution order (one rule, everywhere — env, then local file, then the synced pointer)
is implemented twice on purpose: here for the CLI's diagnostics, and standalone in the
kernel payload, which cannot import mooring. Keep the two in step.

Lean-core leaf: imports only :mod:`mooring.workspace_config`, :mod:`mooring.paths` and
the standard library — no path to marimo / the Copilot SDK. It also does NO networking:
downloading a ``kind=https`` dataset is the kernel payload's job, at run time, into the
sync-excluded cache.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple

import tomli_w

from mooring import paths, workspace_config
from mooring.workspace_config import dataset_location, normalize_dataset_name

STATE_DIR = ".mooring"
PYLIB_DIRNAME = "pylib"
LOCAL_OVERRIDE_NAME = "datasets.local.toml"
CACHE_DIRNAME = "datasets"
ENV_PREFIX = "MOORING_DATASET_"

# The packaged payload (this file's sibling) and the importable name it is written out
# as in the notebook kernel.
_RUNTIME_SRC = "_datasets_runtime.py"
_MODULE_NAME = "mooring_datasets.py"

# Re-exported so callers have one import for the pointer helpers + the name key.
__all__ = [
    "Resolved",
    "cache_dir",
    "clear_local_override",
    "dataset_location",
    "env_var_name",
    "install_runtime",
    "local_override",
    "local_override_path",
    "normalize_dataset_name",
    "pylib_dir",
    "resolve",
    "set_local_override",
    "copilot_guide",
]


class Resolved(NamedTuple):
    """Where a dataset name lands on THIS machine. ``source`` is which plane won —
    ``"env"``, ``"local"`` (the sync-excluded override file), ``"share"`` (the team's
    pointer) or ``"cache"`` (where a ``kind=https`` download would be kept)."""

    name: str
    shape: dict
    path: str
    source: str
    exists: bool


def pylib_dir(workspace: Path | str) -> Path:
    """The kernel import-path dir holding the injected ``mooring_datasets`` module
    (shared with ``mooring_checks`` / ``mooring_inputs`` / ``mooring_connections``)."""
    return Path(workspace) / STATE_DIR / PYLIB_DIRNAME


def local_override_path(workspace: Path | str) -> Path:
    """The LOCAL, sync-excluded file this machine's redirects live in. Under
    ``.mooring``, which :func:`mooring.sync.is_synced_path` excludes on both scan sides —
    so one person's drive letter can never ride a push and break everyone else."""
    return Path(workspace) / STATE_DIR / LOCAL_OVERRIDE_NAME


def cache_dir(workspace: Path | str) -> Path:
    """Where a ``kind=https`` dataset is downloaded to. Under ``.mooring`` for the same
    reason as the override file: a cached 400 MB parquet must be structurally incapable
    of being pushed."""
    return Path(workspace) / STATE_DIR / CACHE_DIRNAME / "cache"


def env_var_name(name: str) -> str:
    """The environment variable that redirects a dataset on this machine (the highest-
    priority local source, e.g. for CI): ``MOORING_DATASET_<NAME>_PATH``."""
    token = normalize_dataset_name(name).upper().replace("-", "_").replace(".", "_")
    return f"{ENV_PREFIX}{token}_PATH"


def local_override(workspace: Path | str, name: str) -> str | None:
    """This machine's redirect for ``name`` — the env var first, then the sync-excluded
    local file — NEVER the synced ``mooring.toml``. ``None`` when the team's pointer
    should be used as-is."""
    key = normalize_dataset_name(name)
    env = os.environ.get(env_var_name(key))
    if env and env.strip():
        return env.strip()
    try:
        data = tomllib.loads(local_override_path(workspace).read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None  # a corrupt local file degrades to "no override", never a crash
    table = data.get(key) if isinstance(data, dict) else None
    if isinstance(table, dict):
        value = table.get("path")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def set_local_override(workspace: Path | str, name: str, location: str) -> Path:
    """Redirect ``name`` on THIS machine, preserving other datasets' entries. Returns the
    file path. The file lives under ``.mooring`` — never synced by construction."""
    key = normalize_dataset_name(name)
    path = local_override_path(workspace)
    try:
        data = tomllib.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        data = {}
    table = data.get(key)
    if not isinstance(table, dict):
        table = {}
    table["path"] = str(location).strip()
    data[key] = table
    path.parent.mkdir(parents=True, exist_ok=True)
    paths.safe_write_text(path, tomli_w.dumps(data))
    return path


def clear_local_override(workspace: Path | str, name: str) -> bool:
    """Drop this machine's redirect for ``name``. Returns whether one was removed."""
    key = normalize_dataset_name(name)
    path = local_override_path(workspace)
    try:
        data = tomllib.loads(path.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    if not (isinstance(data, dict) and key in data):
        return False
    del data[key]
    try:
        if data:
            paths.safe_write_text(path, tomli_w.dumps(data))
        else:
            path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def is_absolute_location(location: str) -> bool:
    """Whether ``location`` already names a place on the machine rather than a
    workspace-relative path.

    ``PureWindowsPath`` is consulted on EVERY platform on purpose (the
    :func:`mooring.workspace_config.safe_folder` reasoning, inverted): ``mooring.toml`` is
    synced, so ``\\\\fileserver\\finance\\sales.parquet`` written on Windows must be
    recognised as absolute everywhere — treating it as relative would silently join it
    onto the workspace root and report a nonsense location in the error."""
    text = str(location).strip()
    if not text:
        return False
    return PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute()


def local_path(workspace: Path | str, location: str) -> str:
    """``location`` as a path on this machine: absolute/UNC as written, anything else
    resolved against the workspace root (so a pointer can also name a synced file)."""
    text = str(location).strip()
    if is_absolute_location(text):
        return str(Path(text))
    return str(Path(workspace) / text)


def cache_target(workspace: Path | str, name: str, url: str) -> Path:
    """Where a ``kind=https`` dataset is cached. The URL's last path segment is kept for
    its EXTENSION (``pl.read_parquet`` vs ``read_csv`` reads better than an opaque hash)
    but sanitised to one bare filename, so a crafted URL cannot escape the cache dir."""
    key = normalize_dataset_name(name)
    tail = str(url).split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    safe = "".join(c for c in tail if c.isalnum() or c in "._-").strip("._-")
    return cache_dir(workspace) / key / (safe or "data")


def resolve(workspace: Path | str, name: str) -> Resolved:
    """Where ``name`` lands on this machine, without touching the network.

    Resolution order — the ONE rule, mirrored in the kernel payload: the
    ``MOORING_DATASET_<NAME>_PATH`` env var, then ``.mooring/datasets.local.toml``, then
    the team's synced pointer (for ``kind=https``, the cache file its download would
    land in). Raises ``KeyError`` when the dataset is not defined."""
    ws = Path(workspace)
    key = normalize_dataset_name(name)
    defined = workspace_config.datasets(ws)
    if key not in defined:
        raise KeyError(name)
    shape = defined[key]
    override = local_override(ws, key)
    if override is not None:
        source = "env" if os.environ.get(env_var_name(key)) else "local"
        target = local_path(ws, override)
        return Resolved(key, shape, target, source, Path(target).is_file())
    location = dataset_location(shape)
    kind = str(shape.get("kind", "")).strip().lower()
    if kind == "https":
        target = cache_target(ws, key, location)
        return Resolved(key, shape, str(target), "cache", target.is_file())
    target_str = local_path(ws, location) if location else ""
    return Resolved(key, shape, target_str, "share", bool(target_str) and Path(target_str).is_file())


def _payload_source() -> bytes:
    return Path(__file__).with_name(_RUNTIME_SRC).read_bytes()


def install_runtime(workspace: Path | str) -> None:
    """Write the ``mooring_datasets`` payload to ``<ws>/.mooring/pylib/``. Best-effort
    and idempotent (mirrors :func:`mooring.connections.install_runtime`)."""
    try:
        src = _payload_source()
    except OSError:
        return
    target = pylib_dir(workspace) / _MODULE_NAME
    try:
        if target.is_file() and target.read_bytes() == src:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        paths.safe_write_bytes(target, src)
    except OSError:
        pass


def _format_hint(shape: dict) -> str:
    """The dataset's file FORMAT (``parquet``, ``csv``, …) from its location's suffix, or
    ``""``. Restricted to a short alphanumeric token so a user-authored location can only
    ever contribute an extension to :func:`copilot_guide`."""
    tail = dataset_location(shape).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    suffix = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
    return suffix if suffix.isalnum() and 1 <= len(suffix) <= 8 else ""


def copilot_guide(workspace: Path | str) -> str:
    """A value-free capability note for the AI system context: the dataset NAMES and file
    FORMATS, so the copilot can author ``md.path(...)`` wiring. ``""`` when none.

    Deliberately NARROWER than :func:`mooring.workspace_config.connections_hint`, which
    must expose host/database because the analyst passes those to a driver. Nothing calls
    for a dataset's location: ``md.path("sales")`` resolves it in the kernel, so the model
    needs the name (to write the call) and the format (to pick the reader) and nothing
    else. The location — a server name, a share layout, a URL — stays out.
    """
    defined = workspace_config.datasets(Path(workspace))
    if not defined:
        return ""
    lines = [
        "DATASETS (value-free pointers to files that live OUTSIDE the repo; you see "
        "NAMES and file formats only — never a path, a server, a URL or a credential):"
    ]
    for name in sorted(defined):
        fmt = _format_hint(defined[name])
        lines.append(f"- {name}: {fmt}" if fmt else f"- {name}")
    lines.append(
        "To read one, propose a cell that calls `import mooring_datasets as md` and "
        'resolves the name at run time, e.g. `df = pl.read_parquet(md.path("sales"))`. '
        "NEVER inline a file path, a UNC share or a URL, and never ask where a dataset "
        "lives — md.path finds it on this machine (the team's pointer, redirectable per "
        "machine). Pair it with the input fingerprint so a moved file is caught: "
        '`mi.fingerprint(df, "sales", path=md.path("sales"))`.'
    )
    return "\n".join(lines)
