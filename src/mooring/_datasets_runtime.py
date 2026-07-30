"""mooring_datasets — resolve a team-shared dataset POINTER to a file on THIS machine.

mooring INJECTS this module into ``<workspace>/.mooring/pylib/mooring_datasets.py`` and
puts that directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can read data that is far too
big to sync without anyone hard-coding where it lives::

    import mooring_datasets as md
    sales = pl.read_parquet(md.path("sales"))

The POINTER (name, kind, location) comes from the synced ``mooring.toml`` ``[datasets]``
table — the same for the whole team. The per-machine REDIRECT, for the teammate whose
share is mounted on another drive, comes from LOCAL sources only: a
``MOORING_DATASET_<NAME>_PATH`` environment variable, or a ``.mooring/datasets.local.toml``
file that never syncs.

Resolution order, highest first — pinned by tests and documented in
docs/users/daily-workflow.md:

1. ``MOORING_DATASET_<NAME>_PATH``
2. ``.mooring/datasets.local.toml``
3. the synced pointer — its ``path`` for ``kind=share``; for ``kind=https``, the file
   cached under ``.mooring/datasets/cache/`` (downloaded on first use)

A name that does not resolve to an existing file raises ``FileNotFoundError`` naming the
dataset, where it looked, which plane sent it there, and the exact command to redirect it
locally — nobody should have to read source at 8am to fix a moved share.

Pairs with ``mooring_inputs``: the pointer says WHERE the data came from,
``mi.fingerprint(df, "sales", path=md.path("sales"))`` proves it hasn't moved.

Standalone by design: imports only the standard library (``tomllib`` is stdlib on the
Python 3.12+ mooring targets), so it works in the team's locked uv env and the frozen
bundle. Do not import mooring here.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath

_STATE_DIR = ".mooring"
_CONFIG_NAME = "mooring.toml"
_LOCAL_OVERRIDE_NAME = "datasets.local.toml"
_CACHE_DIRNAME = "datasets"
_ENV_PREFIX = "MOORING_DATASET_"
_DOWNLOAD_TIMEOUT = 300

# Field-name substrings that mark a value as a SECRET, so a hand-edited credential in the
# SYNCED pointers is dropped here too (defence in depth — mirrors
# mooring.workspace_config.is_secret_field).
# Kept in sync with mooring.workspace_config._SECRET_TOKENS/_SECRET_EXACT/
# _URL_SECRET_PATTERN — a test pins that the three match (this standalone kernel module
# can't import workspace_config).
_SECRET_TOKENS = (
    "password",
    "passwd",
    "passphrase",
    "pwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "credential",
    "sas",
    "connectionstring",
    "connection_string",
    "conn_str",
    "dsn",
    "private_key",
    "privatekey",
    "access_key",
    "accesskey",
    "account_key",
    "accountkey",
    "signing",
    "bearer",
    "cert",
    "key",
)
_SECRET_EXACT = {"pass", "auth", "pat", "cred", "creds"}

# A pre-signed / SAS URL is itself the credential — no field name to catch, so the VALUE
# is matched. Dropped on read here for the same reason it is refused on write.
_URL_SECRET_PATTERN = (
    r"[?&](?:sig|signature|sas|sas[_-]?token|token|access[_-]?token|api[_-]?key|apikey|"
    r"auth|password|passwd|pwd|secret|credential|awsaccesskeyid|x-amz-signature|"
    r"x-amz-credential|x-amz-security-token|x-goog-signature|x-goog-credential)="
)


class Dataset:
    """A resolved pointer: ``name``, ``kind``, ``location`` (as the team wrote it) and
    ``local_path`` (where it lands here). ``exists`` says whether the file is there now."""

    def __init__(self, name: str, shape: dict, local_path: str, source: str) -> None:
        self.name = name
        self.shape = dict(shape)
        self.kind = str(shape.get("kind", "")).strip().lower()
        self.location = _location(shape)
        self.local_path = local_path
        self.source = source

    @property
    def exists(self) -> bool:
        return bool(self.local_path) and Path(self.local_path).is_file()

    def __repr__(self) -> str:
        state = "present" if self.exists else "MISSING"
        return f"<dataset {self.name}: kind={self.kind or '?'}, via {self.source}, {state}>"


def _normalize(name: str) -> str:
    return str(name).strip().strip("/").replace(" ", "_").lower()


def _is_secret_field(name: str) -> bool:
    norm = str(name).strip().lower().replace("-", "_")
    return norm in _SECRET_EXACT or any(tok in norm for tok in _SECRET_TOKENS)


def _location_looks_secret(value) -> bool:
    import re

    return isinstance(value, str) and bool(re.search(_URL_SECRET_PATTERN, value, re.IGNORECASE))


def _workspace() -> Path | None:
    # <ws>/.mooring/pylib/mooring_datasets.py -> parents[2] == <ws>
    try:
        return Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        return None


def _all_pointers() -> dict[str, dict]:
    ws = _workspace()
    if ws is None:
        return {}
    try:
        data = tomllib.loads((ws / _CONFIG_NAME).read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}
    sets = data.get("datasets") if isinstance(data, dict) else None
    if not isinstance(sets, dict):
        return {}
    out: dict[str, dict] = {}
    for name, shape in sets.items():
        if not isinstance(shape, dict):
            continue
        clean = {
            k: v
            for k, v in shape.items()
            if isinstance(k, str)
            and not _is_secret_field(k)
            and not _location_looks_secret(v)
            and isinstance(v, (str, int, float, bool))
        }
        out[_normalize(name)] = clean
    return out


def _location(shape: dict) -> str:
    """The single location a pointer carries — ``url`` for kind=https, ``path`` otherwise
    (mirrors :func:`mooring.workspace_config.dataset_location`)."""
    kind = str(shape.get("kind", "")).strip().lower()
    order = ("url", "path") if kind == "https" else ("path", "url")
    for field in order:
        value = shape.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _env_var(name: str) -> str:
    token = _normalize(name).upper().replace("-", "_").replace(".", "_")
    return f"{_ENV_PREFIX}{token}_PATH"


def _override(name: str) -> tuple[str, str] | None:
    """This machine's redirect as ``(location, source)`` — env var first, then the
    sync-excluded local file. ``None`` when the team's pointer stands."""
    key = _normalize(name)
    env = os.environ.get(_env_var(key))
    if env and env.strip():
        return env.strip(), "env"
    ws = _workspace()
    if ws is None:
        return None
    try:
        data = tomllib.loads((ws / _STATE_DIR / _LOCAL_OVERRIDE_NAME).read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    table = data.get(key) if isinstance(data, dict) else None
    if isinstance(table, dict):
        value = table.get("path")
        if isinstance(value, str) and value.strip():
            return value.strip(), "local"
    return None


def _local_path(location: str) -> str:
    """``location`` as a path here: absolute/UNC as written, anything else relative to the
    workspace. ``PureWindowsPath`` is consulted on every platform because ``mooring.toml``
    is synced — a UNC path authored on Windows must not be mistaken for a relative one."""
    text = str(location).strip()
    if not text:
        return ""
    if PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute():
        return str(Path(text))
    ws = _workspace()
    return str(ws / text) if ws is not None else text


def _cache_target(name: str, url: str) -> Path:
    """Where a kind=https dataset is cached, under the sync-excluded ``.mooring``. The
    URL's last segment is kept for its EXTENSION but sanitised to one bare filename, so a
    crafted URL cannot write outside the cache."""
    ws = _workspace() or Path.cwd()
    tail = str(url).split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    safe = "".join(c for c in tail if c.isalnum() or c in "._-").strip("._-")
    return ws / _STATE_DIR / _CACHE_DIRNAME / "cache" / _normalize(name) / (safe or "data")


def _download(url: str, target: Path) -> None:
    """Fetch ``url`` into ``target`` (atomically, via a sibling temp file).

    http(s) ONLY, checked here and not just on write: ``urlopen`` serves ``file://``
    happily, and the URL comes from a SYNCED file, so without this check one pushed
    ``mooring.toml`` would make every teammate's kernel read an arbitrary local path.
    """
    import urllib.request

    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError(f"Refusing to fetch {url!r}: a dataset URL must be http:// or https://.")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "mooring-datasets"})
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
        with open(tmp, "wb") as handle:
            shutil.copyfileobj(response, handle)
    os.replace(tmp, target)


def _missing_error(name: str, target: str, source: str, shape: dict) -> FileNotFoundError:
    """The 8am error: what failed, where it looked, which plane sent it there, and the one
    command that fixes it — no source-reading required."""
    kind = str(shape.get("kind", "")).strip().lower() or "?"
    # A pointer whose location was DROPPED on read (a credential-bearing URL) has nowhere
    # to look; say so rather than naming the cache slot it would have landed in.
    located = bool(_location(shape)) or source in ("env", "local")
    lines = [f"Dataset {name!r} did not resolve to a file on this machine."]
    lines.append(f"  looked for : {target if located else '(the pointer has no location)'}")
    if source == "env":
        lines.append(f"  redirected by: the {_env_var(name)} environment variable")
    elif source == "local":
        lines.append(
            f"  redirected by: {_STATE_DIR}/{_LOCAL_OVERRIDE_NAME} (this machine only)"
        )
    else:
        lines.append(
            f"  pointer    : {_CONFIG_NAME} [datasets.{name}] (kind={kind}, shared with "
            "your team)"
        )
    lines.append("")
    lines.append(
        "If the file is somewhere else here (a different drive letter or mount point), "
        "point THIS machine at it — the redirect is never synced:"
    )
    lines.append(f'    mooring datasets set-local {name} "<path to the file>"')
    lines.append(f"  or set the {_env_var(name)} environment variable.")
    if not located:
        lines.append(
            "  (No location survived the read — run `mooring datasets check`; a "
            "credential-bearing URL is dropped on purpose.)"
        )
    return FileNotFoundError("\n".join(lines))


def names() -> list[str]:
    """The dataset names defined in the synced ``mooring.toml``."""
    return sorted(_all_pointers())


def info(name: str) -> Dataset:
    """The pointer for ``name`` — its kind, the team's location, and where it lands here —
    WITHOUT downloading or requiring the file to exist. Raises ``KeyError`` if undefined."""
    pointers = _all_pointers()
    key = _normalize(name)
    if key not in pointers:
        raise KeyError(
            f"No dataset named {name!r} in {_CONFIG_NAME}. Defined: "
            f"{', '.join(names()) or '(none)'}. Add one with: "
            f"mooring datasets add {key or 'name'} kind=share path=<file or UNC path>"
        )
    shape = pointers[key]
    override = _override(key)
    if override is not None:
        return Dataset(key, shape, _local_path(override[0]), override[1])
    location = _location(shape)
    if str(shape.get("kind", "")).strip().lower() == "https":
        return Dataset(key, shape, str(_cache_target(key, location)), "cache")
    return Dataset(key, shape, _local_path(location), "share")


def exists(name: str) -> bool:
    """Whether ``name`` resolves to a file that is here right now (no download)."""
    try:
        return info(name).exists
    except KeyError:
        return False


def path(name: str, *, refresh: bool = False) -> str:
    """The local filesystem path ``name`` resolves to, ready to hand to a reader::

        df = pl.read_parquet(md.path("sales"))

    A ``kind=https`` dataset is downloaded into the sync-excluded cache on first use (and
    re-fetched with ``refresh=True``); every other kind must already be reachable. Raises
    ``KeyError`` if the name is not defined, ``FileNotFoundError`` — naming the dataset,
    the location and the fix — if it is defined but not there."""
    dataset = info(name)
    target = Path(dataset.local_path) if dataset.local_path else None
    # No location survived the read (a credential-bearing one was dropped) — skip straight
    # to the missing-file error, which says to run `mooring datasets check`. Trying to
    # fetch "" would bury that behind a transport message.
    if (
        dataset.source == "cache"
        and dataset.location
        and target is not None
        and (refresh or not target.is_file())
    ):
        try:
            _download(dataset.location, target)
        except Exception as exc:  # noqa: BLE001 - any transport failure gets the same advice
            raise FileNotFoundError(
                f"Dataset {dataset.name!r} could not be downloaded: {exc}\n"
                f"  from: {dataset.location.split('?', 1)[0]}\n"
                f'  Once you have a copy, point this machine at it: mooring datasets '
                f'set-local {dataset.name} "<path to the file>"'
            ) from exc
    if target is None or not target.is_file():
        raise _missing_error(dataset.name, dataset.local_path, dataset.source, dataset.shape)
    return str(target)
