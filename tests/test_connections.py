"""Connection definitions: a value-free SHAPE syncs; the SECRET stays local, always.

The load-bearing guarantee is that a credential can NEVER ride a push: it is refused from
the synced mooring.toml on write, and its local store lives under sync-excluded .mooring.
These pin that, the local-only resolution, and the injected kernel helper.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io

import pytest

from mooring import cli, connections, sync, workspace_config
from mooring.config import Config

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


def _imported_roots(src: bytes) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src.decode("utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _load_payload(ws):
    connections.install_runtime(ws)
    mod_path = connections.pylib_dir(ws) / "mooring_connections.py"
    spec = importlib.util.spec_from_file_location("mooring_connections_under_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- the synced shape (value-free) ----------------------------------------------


def test_set_and_read_a_value_free_shape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace_config.set_connection(
        ws, "warehouse", {"kind": "snowflake", "account": "acme", "database": "ANALYTICS"}
    )
    conns = workspace_config.connections(ws)
    assert conns == {"warehouse": {"kind": "snowflake", "account": "acme", "database": "ANALYTICS"}}


def test_set_connection_refuses_a_secret_shaped_field(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    for field in ("password", "token", "api_key", "secret", "PWD", "access_key", "dsn"):
        with pytest.raises(ValueError):
            workspace_config.set_connection(ws, "warehouse", {"host": "h", field: SECRET})
    # ...and nothing was written for the rejected calls.
    assert workspace_config.connections(ws) == {}


def test_read_drops_a_hand_added_secret_field(tmp_path):
    # Defence in depth: even if someone hand-edits a secret into mooring.toml, the READ
    # side drops it, so it never reaches a caller or the copilot.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "mooring.toml").write_text(
        f'[connections.warehouse]\nhost = "h"\npassword = "{SECRET}"\n', "utf-8"
    )
    conns = workspace_config.connections(ws)
    assert conns == {"warehouse": {"host": "h"}}  # password dropped
    # ...but connections_raw (used only by `connections check`) can still SEE it to warn.
    assert "password" in workspace_config.connections_raw(ws)["warehouse"]


def test_remove_connection(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace_config.set_connection(ws, "a", {"host": "x"})
    workspace_config.set_connection(ws, "b", {"host": "y"})
    assert workspace_config.remove_connection(ws, "a") is True
    assert set(workspace_config.connections(ws)) == {"b"}
    assert workspace_config.remove_connection(ws, "nope") is False


def test_is_secret_field_allows_shape_names(tmp_path):
    for ok in ("host", "port", "database", "warehouse", "role", "schema", "account", "user", "kind"):
        assert workspace_config.is_secret_field(ok) is False
    for bad in ("password", "secret", "token", "api_key", "key", "pat", "sas", "dsn", "auth"):
        assert workspace_config.is_secret_field(bad) is True


def test_connections_hint_is_value_free(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace_config.set_connection(ws, "warehouse", {"kind": "snowflake", "account": "acme"})
    connections.set_local_secret(ws, "warehouse", SECRET)
    hint = workspace_config.connections_hint(ws)
    assert "warehouse" in hint and "snowflake" in hint
    assert SECRET not in hint  # the secret is NEVER in what the copilot sees
    assert workspace_config.connections_hint(tmp_path / "empty") == ""


# -- the local secret (never synced) --------------------------------------------


def test_secret_is_refused_from_the_synced_file_and_kept_local(tmp_path):
    # THE guarantee: after defining a shape + storing a secret, the secret is in the LOCAL
    # file (sync-excluded) and NOT in the synced mooring.toml.
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace_config.set_connection(ws, "warehouse", {"host": "db.example.com"})
    connections.set_local_secret(ws, "warehouse", SECRET)

    assert SECRET not in (ws / "mooring.toml").read_text("utf-8")
    assert SECRET in connections.local_secret_path(ws).read_text("utf-8")


def test_local_secret_store_is_structurally_unsyncable():
    assert sync.is_synced_path(".mooring/connections.local.toml") is False
    assert sync.is_synced_path(".mooring/connections.local.toml", exclude=("*.toml",)) is False


def test_local_secret_resolves_env_then_file(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    connections.set_local_secret(ws, "warehouse", "file-secret")
    assert connections.local_secret(ws, "warehouse") == "file-secret"
    # env var wins over the file
    monkeypatch.setenv(connections.env_var_name("warehouse"), "env-secret")
    assert connections.local_secret(ws, "warehouse") == "env-secret"


def test_clear_local_secret(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    connections.set_local_secret(ws, "warehouse", SECRET)
    assert connections.clear_local_secret(ws, "warehouse") is True
    assert connections.local_secret(ws, "warehouse") is None
    assert connections.clear_local_secret(ws, "warehouse") is False


def test_resolve_returns_shape_and_local_secret(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace_config.set_connection(ws, "warehouse", {"host": "h", "database": "d"})
    connections.set_local_secret(ws, "warehouse", SECRET)
    shape, secret = connections.resolve(ws, "warehouse")
    assert shape == {"host": "h", "database": "d"} and secret == SECRET
    with pytest.raises(KeyError):
        connections.resolve(ws, "missing")


# -- the injected kernel helper -------------------------------------------------


def test_install_runtime_writes_importable_stdlib_only_payload(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    connections.install_runtime(ws)
    src = (connections.pylib_dir(ws) / "mooring_connections.py").read_bytes()
    assert b"def get" in src and b"class Connection" in src
    assert "mooring" not in _imported_roots(src)  # standalone in the kernel
    assert _imported_roots(src) <= {"__future__", "os", "tomllib", "pathlib"}


def test_kernel_get_assembles_shape_and_secret(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace_config.set_connection(
        ws, "warehouse", {"account": "acme", "database": "ANALYTICS", "role": "ANALYST"}
    )
    connections.set_local_secret(ws, "warehouse", SECRET)
    mc = _load_payload(ws)

    assert mc.names() == ["warehouse"]
    c = mc.get("warehouse")
    assert c.account == "acme" and c.database == "ANALYTICS"
    assert c.secret == SECRET and c.has_secret is True
    # The repr must NEVER print the secret.
    assert SECRET not in repr(c)
    assert "secret set: yes" in repr(c)
    with pytest.raises(KeyError):
        mc.get("nope")


def test_kernel_get_drops_a_hand_added_secret_field(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "mooring.toml").write_text(
        f'[connections.warehouse]\naccount = "acme"\npassword = "{SECRET}"\n', "utf-8"
    )
    mc = _load_payload(ws)
    c = mc.get("warehouse")
    assert c.account == "acme"
    assert "password" not in c.as_dict()  # secret-shaped shape field is dropped
    assert c.secret is None  # no LOCAL secret set -> None (never reads the synced one)


def test_kernel_secret_env_override(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace_config.set_connection(ws, "warehouse", {"host": "h"})
    connections.set_local_secret(ws, "warehouse", "file-secret")
    monkeypatch.setenv(connections.env_var_name("warehouse"), "env-secret")
    mc = _load_payload(ws)
    assert mc.get("warehouse").secret == "env-secret"


# -- copilot context ------------------------------------------------------------


def test_build_system_context_folds_in_connections_help(tmp_path):
    from mooring.ai import egress

    ws = tmp_path / "ws"
    ws.mkdir()
    workspace_config.set_connection(ws, "warehouse", {"kind": "snowflake"})
    connections.set_local_secret(ws, "warehouse", SECRET)
    ctx = egress.build_system_context(
        schema_text="amount: float",
        notebook_source="df = 1",
        notebook_rel="nb.py",
        connections_help=workspace_config.connections_hint(ws),
    )
    assert "warehouse" in ctx and "snowflake" in ctx
    assert SECRET not in ctx  # value-blindness holds for connections too

    without = egress.build_system_context(
        schema_text="amount: float", notebook_source="df = 1", notebook_rel="nb.py"
    )
    assert "CONNECTIONS" not in without  # omitted unless explicitly provided


# -- the CLI --------------------------------------------------------------------


def _cfg(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return Config(client_id="", owner="", repo="", workspace_path=str(ws))


def _ns(**kw):
    return argparse.Namespace(**kw)


def test_cli_add_writes_shape_and_refuses_secret(tmp_path):
    cfg = _cfg(tmp_path)
    rc = cli.cmd_connections(
        cfg, _ns(connections_command="add", name="warehouse", fields=["kind=snowflake", "account=acme"])
    )
    assert rc == 0
    assert workspace_config.connections(cfg.workspace())["warehouse"]["kind"] == "snowflake"
    # a secret-shaped field aborts (SystemExit) and writes nothing new
    with pytest.raises(SystemExit):
        cli.cmd_connections(
            cfg, _ns(connections_command="add", name="warehouse", fields=[f"password={SECRET}"])
        )


def test_cli_check_flags_a_hand_added_secret(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    (cfg.workspace() / "mooring.toml").write_text(
        f'[connections.warehouse]\nhost = "h"\ntoken = "{SECRET}"\n', "utf-8"
    )
    rc = cli.cmd_connections(cfg, _ns(connections_command="check"))
    assert rc == 1  # non-zero: a problem was found
    out = capsys.readouterr().out
    assert "token" in out
    assert SECRET not in out  # value-free report — never echoes the secret


def test_cli_set_secret_stdin_stores_locally_not_synced(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    workspace_config.set_connection(ws, "warehouse", {"host": "h"})
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    rc = cli.cmd_connections(
        cfg, _ns(connections_command="set-secret", name="warehouse", stdin=True, clear=False)
    )
    assert rc == 0
    assert connections.local_secret(ws, "warehouse") == SECRET
    assert SECRET not in (ws / "mooring.toml").read_text("utf-8")


def test_cli_check_clean(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    workspace_config.set_connection(cfg.workspace(), "warehouse", {"host": "h", "database": "d"})
    assert cli.cmd_connections(cfg, _ns(connections_command="check")) == 0
    assert "No secrets" in capsys.readouterr().out
