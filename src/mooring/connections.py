"""Split a database connection's value-free SHAPE (synced) from its SECRET (local).

An analyst who works against a warehouse needs the connection's *shape* — host,
database, warehouse, role — to be the same for the whole team, but the *credential* must
never leave their machine. mooring keeps the two apart, structurally:

* the SHAPE lives in the synced ``mooring.toml`` ``[connections]`` table (see
  :mod:`mooring.workspace_config`) — value-free, travels with the repo, readable by the
  copilot, with a secret-shaped field REFUSED on write;
* the SECRET lives ONLY in a local source this module resolves — a ``MOORING_CONN_<NAME>_SECRET``
  environment variable, or a ``.mooring/connections.local.toml`` file that
  :func:`mooring.sync.is_synced_path` excludes on both scan sides, so it can never ride a
  push. (Windows integrated auth needs no secret at all.)

A notebook assembles the two at runtime via the injected ``mooring_connections`` helper
(:mod:`mooring._connections_runtime`, installed onto the kernel path like
``mooring_checks``): ``mc.get("warehouse")`` returns the shape fields plus ``.secret``,
resolved locally. The copilot only ever sees the value-free shape — never the secret,
which is a kernel-runtime value it has no channel to.

Deliberately NO driver provisioning: mooring never installs a database driver (a frozen
``.exe`` can't, and an ODBC MSI is admin-gated) — the analyst's own environment supplies
``snowflake-connector`` / ``pyodbc`` / etc.; mooring only brokers the shape + secret.

Lean-core leaf: imports only :mod:`mooring.workspace_config`, :mod:`mooring.paths`, and
the standard library — no path to marimo / the Copilot SDK.
"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

import tomli_w

from mooring import paths, workspace_config
from mooring.workspace_config import is_secret_field, normalize_connection_name

STATE_DIR = ".mooring"
PYLIB_DIRNAME = "pylib"
LOCAL_SECRET_NAME = "connections.local.toml"
ENV_PREFIX = "MOORING_CONN_"

# The packaged payload (this file's sibling) and the importable name it is written out
# as in the notebook kernel.
_RUNTIME_SRC = "_connections_runtime.py"
_MODULE_NAME = "mooring_connections.py"

# Re-exported so callers have one import for the guard + name key.
__all__ = [
    "is_secret_field",
    "normalize_connection_name",
    "local_secret_path",
    "env_var_name",
    "local_secret",
    "set_local_secret",
    "clear_local_secret",
    "resolve",
    "install_runtime",
    "pylib_dir",
]


def pylib_dir(workspace: Path | str) -> Path:
    """The kernel import-path dir holding the injected ``mooring_connections`` module
    (shared with ``mooring_checks``)."""
    return Path(workspace) / STATE_DIR / PYLIB_DIRNAME


def local_secret_path(workspace: Path | str) -> Path:
    """The LOCAL, sync-excluded file a connection secret is stored in. Under ``.mooring``,
    which :func:`mooring.sync.is_synced_path` excludes on both scan sides — so a secret
    written here can never ride a push."""
    return Path(workspace) / STATE_DIR / LOCAL_SECRET_NAME


def env_var_name(name: str) -> str:
    """The environment variable a connection's secret can be supplied in (the highest-
    priority local source, e.g. for CI): ``MOORING_CONN_<NAME>_SECRET``."""
    token = normalize_connection_name(name).upper().replace("-", "_").replace(".", "_")
    return f"{ENV_PREFIX}{token}_SECRET"


def local_secret(workspace: Path | str, name: str) -> str | None:
    """Resolve a connection's secret from LOCAL sources only — the env var first, then
    the sync-excluded local file — NEVER the synced ``mooring.toml``. ``None`` if unset."""
    key = normalize_connection_name(name)
    env = os.environ.get(env_var_name(key))
    if env:
        return env
    try:
        data = tomllib.loads(local_secret_path(workspace).read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    table = data.get(key) if isinstance(data, dict) else None
    if isinstance(table, dict):
        value = table.get("secret")
        if isinstance(value, str) and value:
            return value
    return None


def set_local_secret(workspace: Path | str, name: str, secret: str) -> Path:
    """Store a connection's secret in the LOCAL, sync-excluded file (owner-only perms
    where the OS supports it), preserving other connections' entries. Returns the file
    path. The file lives under ``.mooring`` — never synced by construction."""
    key = normalize_connection_name(name)
    path = local_secret_path(workspace)
    try:
        data = tomllib.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    table = data.get(key)
    if not isinstance(table, dict):
        table = {}
    table["secret"] = str(secret)
    data[key] = table
    path.parent.mkdir(parents=True, exist_ok=True)
    paths.safe_write_text(path, tomli_w.dumps(data))
    _lock_down(path)
    return path


def clear_local_secret(workspace: Path | str, name: str) -> bool:
    """Remove a connection's local secret. Returns whether one was removed."""
    key = normalize_connection_name(name)
    path = local_secret_path(workspace)
    try:
        data = tomllib.loads(path.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    if not (isinstance(data, dict) and key in data):
        return False
    del data[key]
    try:
        if data:
            paths.safe_write_text(path, tomli_w.dumps(data))
            _lock_down(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def resolve(workspace: Path | str, name: str) -> tuple[dict, str | None]:
    """The connection's value-free shape (from the synced definitions) plus its LOCAL
    secret (or ``None`` — e.g. integrated auth). Raises ``KeyError`` if the connection is
    not defined."""
    conns = workspace_config.connections(Path(workspace))
    key = normalize_connection_name(name)
    if key not in conns:
        raise KeyError(name)
    return conns[key], local_secret(workspace, key)


def _payload_source() -> bytes:
    return Path(__file__).with_name(_RUNTIME_SRC).read_bytes()


def install_runtime(workspace: Path | str) -> None:
    """Write the ``mooring_connections`` payload to ``<ws>/.mooring/pylib/``. Best-effort
    and idempotent (mirrors :func:`mooring.checks.install_runtime`)."""
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


def _lock_down(path: Path) -> None:
    """Best-effort owner-only permissions on the local secret file (POSIX ``chmod 600``;
    a no-op where unsupported)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
