"""mooring_connections — a database connection's value-free SHAPE + its LOCAL secret.

mooring INJECTS this module into ``<workspace>/.mooring/pylib/mooring_connections.py``
and puts that directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can assemble a connection
without a credential ever living in the repo::

    import mooring_connections as mc
    c = mc.get("warehouse")            # shape from the synced mooring.toml
    conn = snowflake.connector.connect(
        account=c.account, database=c.database, warehouse=c.warehouse,
        role=c.role, user=c.user, password=c.secret,   # secret resolved LOCALLY
    )

The SHAPE (host/database/warehouse/role/…) comes from the synced ``mooring.toml``
``[connections]`` table — value-free, the same for the whole team. The SECRET
(``c.secret``) is resolved from LOCAL sources only — a ``MOORING_CONN_<NAME>_SECRET``
environment variable, or a ``.mooring/connections.local.toml`` file that never syncs — so
it can never ride a push. (Integrated auth needs no secret; ``c.secret`` is then ``None``.)

The secret is a kernel-runtime value: it appears only inside the running notebook, never
in the notebook SOURCE and never in anything mooring sends the AI copilot (which sees only
the value-free shape). The ``repr`` here deliberately never prints it.

Standalone by design: imports only the standard library (``tomllib`` is stdlib on the
Python 3.12+ mooring targets), so it works in the team's locked uv env and the frozen
bundle. Do not import mooring here. mooring does NOT install any database driver — your
own environment supplies the connector.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

_STATE_DIR = ".mooring"
_CONFIG_NAME = "mooring.toml"
_LOCAL_SECRET_NAME = "connections.local.toml"
_ENV_PREFIX = "MOORING_CONN_"

# Field-name substrings that mark a value as a SECRET, so a hand-edited secret in the
# SYNCED definitions is dropped here too (defence in depth — mirrors
# mooring.workspace_config.is_secret_field; the secret only ever comes from the local
# sources below).
_SECRET_TOKENS = (
    "password",
    "passwd",
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
)
_SECRET_EXACT = {"key", "pass", "auth", "pat", "cred", "creds"}


class Connection:
    """A resolved connection: its value-free shape fields as attributes, plus ``.secret``
    (the locally-resolved credential, or ``None``). Never prints the secret."""

    def __init__(self, name: str, shape: dict, secret: str | None) -> None:
        self.name = name
        self.secret = secret
        self._shape = dict(shape)
        for key, value in shape.items():
            if isinstance(key, str) and key.isidentifier() and not hasattr(self, key):
                setattr(self, key, value)

    def get(self, field: str, default=None):
        return self._shape.get(field, default)

    def as_dict(self) -> dict:
        """The value-free shape (no secret)."""
        return dict(self._shape)

    @property
    def has_secret(self) -> bool:
        return bool(self.secret)

    def __repr__(self) -> str:
        fields = ", ".join(sorted(self._shape))
        return f"<connection {self.name}: {fields}; secret set: {'yes' if self.secret else 'no'}>"


def _normalize(name: str) -> str:
    return str(name).strip().strip("/").replace(" ", "_")


def _is_secret_field(name: str) -> bool:
    norm = str(name).strip().lower().replace("-", "_")
    return norm in _SECRET_EXACT or any(tok in norm for tok in _SECRET_TOKENS)


def _workspace() -> Path | None:
    # <ws>/.mooring/pylib/mooring_connections.py -> parents[2] == <ws>
    try:
        return Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        return None


def _all_shapes() -> dict[str, dict]:
    ws = _workspace()
    if ws is None:
        return {}
    try:
        data = tomllib.loads((ws / _CONFIG_NAME).read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    conns = data.get("connections") if isinstance(data, dict) else None
    if not isinstance(conns, dict):
        return {}
    out: dict[str, dict] = {}
    for name, shape in conns.items():
        if not isinstance(shape, dict):
            continue
        clean = {
            k: v
            for k, v in shape.items()
            if isinstance(k, str) and not _is_secret_field(k) and isinstance(v, (str, int, float, bool))
        }
        out[_normalize(name)] = clean
    return out


def _env_var(name: str) -> str:
    token = _normalize(name).upper().replace("-", "_").replace(".", "_")
    return f"{_ENV_PREFIX}{token}_SECRET"


def _secret(name: str) -> str | None:
    key = _normalize(name)
    env = os.environ.get(_env_var(key))
    if env:
        return env
    ws = _workspace()
    if ws is None:
        return None
    try:
        data = tomllib.loads((ws / _STATE_DIR / _LOCAL_SECRET_NAME).read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    table = data.get(key) if isinstance(data, dict) else None
    if isinstance(table, dict):
        value = table.get("secret")
        if isinstance(value, str) and value:
            return value
    return None


def names() -> list[str]:
    """The connection names defined in the synced ``mooring.toml``."""
    return sorted(_all_shapes())


def get(name: str) -> Connection:
    """Resolve a connection: its value-free shape (synced) + local secret. Raises
    ``KeyError`` if it isn't defined in ``mooring.toml``."""
    shapes = _all_shapes()
    key = _normalize(name)
    if key not in shapes:
        raise KeyError(
            f"No connection named {name!r} in mooring.toml. Defined: {', '.join(names()) or '(none)'}"
        )
    return Connection(key, shapes[key], _secret(key))
