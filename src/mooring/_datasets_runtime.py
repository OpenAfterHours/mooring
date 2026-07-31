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

# A credential in an innocently-named field (an embedded `password=` pair, a DSN with
# inline credentials). Dropped on read here for the same reason it is refused on write —
# the library and the kernel must agree, or the kernel FETCHES what `datasets list` says
# it dropped.
_SECRET_VALUE_PATTERN = (
    r"(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|account[_-]?key|credential|bearer)\s*[=:]"
    r"|[a-z][a-z0-9+.\-]*://[^\s/@]+:[^\s/@]+@"
)
# A pre-signed / SAS URL is itself the credential — no field name to catch, so the VALUE
# is matched, STRUCTURALLY: any query string, fragment or userinfo on a URL.
_URL_SCHEME_PATTERN = r"\A[a-z][a-z0-9+.\-]*://"
_URL_SECRET_PATTERN = (
    r"[?&#/](?:sig|signature|sas|sas[_-]?token|token|access[_-]?token|api[_-]?key|apikey|"
    r"authorization|tempauth|rlkey|auth|key|password|passwd|pwd|secret|credential|"
    r"awsaccesskeyid|x-amz-signature|x-amz-credential|x-amz-security-token|"
    r"x-goog-signature|x-goog-credential)="
)
# A dataset name becomes a DIRECTORY COMPONENT under .mooring/datasets/cache, and
# mooring.toml is SYNCED — so the name is an ALLOWLIST, not a denylist. Without it a
# pushed [datasets."../../pwned"] or [datasets."c:/users/public/x"] would make this
# kernel write outside the workspace (and .mooring/pylib is on sys.path).
_NAME_PATTERN = r"[a-z0-9][a-z0-9._-]*\Z"
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
     *(f"lpt{i}" for i in range(1, 10))}
)
_CONTROL_CHARS = "".join(chr(c) for c in (*range(0x00, 0x20), 0x7F))
_NAME_TRANSLATION = str.maketrans("", "", _CONTROL_CHARS)

# Hosts a dataset may never be fetched from, whatever the pointer says. A synced
# mooring.toml is attacker-reachable input, so an https pointer is an SSRF primitive
# aimed at whatever this machine can reach: 127.0.0.1 is mooring's own hub, and
# 169.254.169.254 is the cloud instance-metadata endpoint. PRIVATE ranges are
# deliberately allowed — an intranet file server is the point of the feature — which is
# recorded in docs/admins/threat-model.md rather than silently assumed.
_ALLOWED_SCHEMES = ("http://", "https://")


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
    """A dataset's path-safe identity key, or ``""`` — see ``_NAME_PATTERN``. Mirrors
    :func:`mooring.workspace_config.normalize_dataset_name`."""
    import re

    key = str(name).translate(_NAME_TRANSLATION).strip().strip("/").replace(" ", "_").lower()
    if not key or not re.match(_NAME_PATTERN, key):
        return ""
    return "" if key.split(".", 1)[0] in _RESERVED_DEVICE_NAMES else key


def _is_secret_field(name: str) -> bool:
    norm = str(name).strip().lower().replace("-", "_")
    return norm in _SECRET_EXACT or any(tok in norm for tok in _SECRET_TOKENS)


def _value_looks_secret(value) -> bool:
    import re

    return isinstance(value, str) and bool(re.search(_SECRET_VALUE_PATTERN, value, re.IGNORECASE))


def _location_looks_secret(value) -> bool:
    """Structural for a URL — any query string, fragment or userinfo — with the token
    pattern as a floor. Mirrors :func:`mooring.workspace_config.location_looks_secret`."""
    import re

    if not isinstance(value, str):
        return False
    text = value.strip()
    if re.match(_URL_SCHEME_PATTERN, text, re.IGNORECASE):
        rest = text.split("://", 1)[1]
        authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
        if "@" in authority or "?" in rest or "#" in rest:
            return True
    return bool(re.search(_URL_SECRET_PATTERN, text, re.IGNORECASE))


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
        key = _normalize(name)
        if not key or not isinstance(shape, dict):
            continue  # an unsafe name is not a dataset
        out[key] = {
            k: v
            for k, v in shape.items()
            if isinstance(k, str)
            and not _is_secret_field(k)
            and not _value_looks_secret(v)
            and not _location_looks_secret(v)
            and isinstance(v, (str, int, float, bool))
        }
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
    """Where a kind=https dataset is cached, under the sync-excluded ``.mooring``. BOTH
    halves of the path are sanitised: the dataset name through ``_normalize`` (an
    allowlist — it is a directory component) and the URL's last segment down to one bare
    filename, keeping only its EXTENSION-bearing characters. Either one unsanitised is an
    arbitrary-file-write primitive driven by a synced file."""
    ws = _workspace() or Path.cwd()
    key = _normalize(name)
    if not key:
        raise ValueError(f"{name!r} is not a usable dataset name.")
    tail = str(url).split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    safe = "".join(c for c in tail if c.isalnum() or c in "._-").strip("._-")
    return ws / _STATE_DIR / _CACHE_DIRNAME / "cache" / key / (safe or "data")


def _safe_url(url: str) -> str:
    """``scheme://host`` — never the path, query or fragment, any of which can carry a
    credential. Error text is a plausible paste into the copilot chat, so it must not be
    the one place a token survives."""
    text = str(url).strip()
    if "://" not in text:
        return "(not a URL)"
    scheme, rest = text.split("://", 1)
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return f"{scheme}://{host.rsplit('@', 1)[-1]}"


def _redact_urls(text: str) -> str:
    """Every URL inside ``text`` cut down to ``scheme://host``. Transport errors can echo
    the request URL back, so this runs over any message that reaches the analyst."""
    import re

    return re.sub(r"[a-zA-Z][a-zA-Z0-9+.\-]*://\S+", lambda m: _safe_url(m.group(0)), str(text))


def _check_fetchable(url: str) -> None:
    """Refuse a URL this kernel must never fetch: a non-http(s) scheme, or a loopback /
    link-local host.

    Both are mooring's OWN guards, not a dependency's. ``urlopen`` serves ``file://``
    happily, and while CPython's redirect handler happens to restrict targets to
    http/https/ftp, "happens to" is not a control — so the scheme is re-checked on the
    RESPONSE url too (below). The host check closes the SSRF the synced pointer opens:
    ``127.0.0.1`` is mooring's own hub and ``169.254.169.254`` is the cloud
    instance-metadata endpoint. Ordinary private ranges stay allowed — an intranet file
    server is the point of the feature. DNS rebinding between this check and the fetch is
    NOT defended; see docs/admins/threat-model.md.
    """
    import ipaddress
    import socket

    text = str(url).strip()
    if not text.lower().startswith(_ALLOWED_SCHEMES):
        raise ValueError(
            f"Refusing to fetch {_safe_url(text)}: a dataset URL must be http:// or https://."
        )
    authority = text.split("://", 1)[1].split("/", 1)[0].rsplit("@", 1)[-1]
    host = authority.rsplit(":", 1)[0] if authority.count(":") == 1 else authority
    host = host.strip("[]")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return  # unresolvable: let the fetch fail with its own transport error
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if address.is_loopback or address.is_link_local:
            raise ValueError(
                f"Refusing to fetch {_safe_url(text)}: it resolves to a loopback or "
                "link-local address (this machine's own services, or a cloud metadata "
                "endpoint). A dataset pointer is a synced file — it must not be able to "
                "aim your kernel at your own network stack."
            )


def _opener():
    """A urllib opener that re-runs :func:`_check_fetchable` on every REDIRECT target,
    before that hop is issued — so the guard holds for the whole chain and not just the
    first request."""
    import urllib.request

    class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _check_fetchable(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(_GuardedRedirect)


def _download(url: str, target: Path) -> None:
    """Fetch ``url`` into ``target`` (atomically, via a sibling temp file), after
    :func:`_check_fetchable` — on the URL, on every redirect hop, and once more on the URL
    actually served, so nothing lands in the cache from a host the pre-check refused."""
    import urllib.request

    _check_fetchable(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "mooring-datasets"})
    with _opener().open(request, timeout=_DOWNLOAD_TIMEOUT) as response:
        _check_fetchable(getattr(response, "url", None) or url)
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
            # scheme://host only, in the message AND in the wrapped transport error: a
            # path, query or fragment can carry a credential, and this text is a plausible
            # paste into the copilot chat.
            raise FileNotFoundError(
                f"Dataset {dataset.name!r} could not be downloaded from "
                f"{_safe_url(dataset.location)}: {_redact_urls(exc)}"
                f"\n  Once you have a copy, point this machine at it: mooring datasets "
                f'set-local {dataset.name} "<path to the file>"'
            ) from exc
    if target is None or not target.is_file():
        raise _missing_error(dataset.name, dataset.local_path, dataset.source, dataset.shape)
    return str(target)
