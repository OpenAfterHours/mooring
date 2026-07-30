"""Dataset pointers: a value-free LOCATION syncs; the file, and any credential, never do.

The file-shaped sibling of test_connections.py. Three guarantees are pinned here: a
credential-bearing location (a SAS / pre-signed URL) can NEVER ride a push, this machine's
redirect is structurally unsyncable, and the copilot sees dataset NAMES and formats only —
never where the data lives. Plus the bit an analyst meets at 8am: the missing-file error.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
from pathlib import Path

import polars as pl
import pytest

from mooring import cli, datasets, inputs, sync, workspace_config
from mooring.config import Config

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
UNC = "//fileserver/finance/sales.parquet"
UNC_BACKSLASH = r"\\fileserver\finance\sales.parquet"


def _imported_roots(src: bytes) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src.decode("utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _load_payload(ws):
    """Install and import the payload the way a marimo kernel would."""
    datasets.install_runtime(ws)
    mod_path = datasets.pylib_dir(ws) / "mooring_datasets.py"
    spec = importlib.util.spec_from_file_location("mooring_datasets_under_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- the synced pointer (a location, never a credential) -------------------------


def test_set_and_read_a_value_free_pointer(tmp_path):
    ws = _ws(tmp_path)
    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": UNC})
    assert workspace_config.datasets(ws) == {"sales": {"kind": "share", "path": UNC}}


def test_set_dataset_refuses_a_credential_shaped_field(tmp_path):
    ws = _ws(tmp_path)
    for field in ("password", "token", "sas", "api_key", "secret", "access_key"):
        with pytest.raises(ValueError):
            workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": UNC, field: SECRET})
    assert workspace_config.datasets(ws) == {}  # nothing was written for a rejected call


def test_set_dataset_refuses_a_presigned_or_sas_url(tmp_path):
    # The dataset-specific hazard: an innocently NAMED `url` field whose query string IS
    # the credential. There is no field name to catch, so the VALUE must be inspected.
    ws = _ws(tmp_path)
    for url in (
        f"https://acct.blob.core.windows.net/fin/sales.parquet?sv=2021-08-06&sig={SECRET}",
        f"https://bucket.s3.amazonaws.com/sales.parquet?X-Amz-Signature={SECRET}",
        f"https://example.org/sales.csv?access_token={SECRET}",
        f"https://u:{SECRET}@example.org/sales.csv",
    ):
        with pytest.raises(ValueError):
            workspace_config.set_dataset(ws, "sales", {"kind": "https", "url": url})
    assert workspace_config.datasets(ws) == {}
    assert not (ws / "mooring.toml").is_file()  # a rejected pointer writes nothing at all


def test_a_plain_url_with_a_harmless_query_is_allowed(tmp_path):
    # Over-refusing costs a `set-local`, but it must not refuse an ordinary download link.
    ws = _ws(tmp_path)
    workspace_config.set_dataset(
        ws, "archive", {"kind": "https", "url": "https://example.org/archive.csv?download=1"}
    )
    assert workspace_config.datasets(ws)["archive"]["url"].endswith("download=1")


def test_read_drops_a_hand_added_credential(tmp_path):
    # Defence in depth: even hand-edited into mooring.toml, a credential never reaches a
    # caller, the kernel or the copilot.
    ws = _ws(tmp_path)
    (ws / "mooring.toml").write_text(
        "[datasets.sales]\nkind = \"share\"\npath = \"//fs/fin/sales.parquet\"\n"
        f'token = "{SECRET}"\n\n[datasets.archive]\nkind = "https"\n'
        f'url = "https://x.example/archive.csv?sig={SECRET}"\n',
        "utf-8",
    )
    read = workspace_config.datasets(ws)
    assert "token" not in read["sales"]  # credential-named field dropped
    assert "url" not in read["archive"]  # credential-bearing LOCATION dropped
    assert SECRET not in repr(read)
    # ...but datasets_raw (used only by `datasets check`) can still SEE them to warn.
    raw = workspace_config.datasets_raw(ws)
    assert "token" in raw["sales"] and "url" in raw["archive"]


def test_set_dataset_validates_kind_and_location(tmp_path):
    ws = _ws(tmp_path)
    with pytest.raises(ValueError):  # no kind
        workspace_config.set_dataset(ws, "sales", {"path": UNC})
    with pytest.raises(ValueError):  # unknown kind
        workspace_config.set_dataset(ws, "sales", {"kind": "ftp", "path": UNC})
    with pytest.raises(ValueError):  # no location
        workspace_config.set_dataset(ws, "sales", {"kind": "share"})
    with pytest.raises(ValueError):  # a URL under kind=share
        workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": "https://x/y.csv"})
    with pytest.raises(ValueError):  # a non-http scheme under kind=https
        workspace_config.set_dataset(ws, "sales", {"kind": "https", "url": "file:///etc/passwd"})
    assert workspace_config.datasets(ws) == {}


def test_set_dataset_merges_and_lowercases_kind(tmp_path):
    ws = _ws(tmp_path)
    workspace_config.set_dataset(ws, "Sales", {"kind": "Share", "path": UNC})
    workspace_config.set_dataset(ws, "sales", {"owner": "finance"})
    assert workspace_config.datasets(ws)["sales"] == {
        "kind": "share",
        "path": UNC,
        "owner": "finance",
    }


def test_remove_dataset(tmp_path):
    ws = _ws(tmp_path)
    workspace_config.set_dataset(ws, "a", {"kind": "share", "path": UNC})
    workspace_config.set_dataset(ws, "b", {"kind": "share", "path": UNC})
    assert workspace_config.remove_dataset(ws, "a") is True
    assert set(workspace_config.datasets(ws)) == {"b"}
    assert workspace_config.remove_dataset(ws, "nope") is False


# -- the local redirect (never synced) ------------------------------------------


def test_local_redirect_and_cache_are_structurally_unsyncable():
    # THE guarantee: one person's drive letter — and a cached 400 MB parquet — cannot ride
    # a push, even against a custom exclude that would otherwise make them visible.
    assert sync.is_synced_path(".mooring/datasets.local.toml") is False
    assert sync.is_synced_path(".mooring/datasets.local.toml", exclude=("*.toml",)) is False
    assert sync.is_synced_path(".mooring/datasets/cache/sales/sales.parquet") is False
    assert sync.is_synced_path(".mooring/datasets/cache/sales/sales.parquet", exclude=("x",)) is False


def test_local_redirect_stays_out_of_the_synced_file(tmp_path):
    ws = _ws(tmp_path)
    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": UNC})
    datasets.set_local_override(ws, "sales", "D:/mnt/finance/sales.parquet")
    assert "D:/mnt" not in (ws / "mooring.toml").read_text("utf-8")
    assert "D:/mnt" in datasets.local_override_path(ws).read_text("utf-8")


def test_clear_local_override(tmp_path):
    ws = _ws(tmp_path)
    datasets.set_local_override(ws, "sales", "D:/x.parquet")
    assert datasets.clear_local_override(ws, "sales") is True
    assert datasets.local_override(ws, "sales") is None
    assert datasets.clear_local_override(ws, "sales") is False


def test_a_corrupt_local_file_degrades_to_no_override(tmp_path):
    ws = _ws(tmp_path)
    datasets.local_override_path(ws).parent.mkdir(parents=True, exist_ok=True)
    datasets.local_override_path(ws).write_text("not [ valid toml", "utf-8")
    assert datasets.local_override(ws, "sales") is None  # unavailable, never a crash


# -- resolution order ------------------------------------------------------------


def test_resolution_order_env_then_local_then_pointer(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    shared = tmp_path / "shared.parquet"
    shared.write_bytes(b"x")
    mine = tmp_path / "mine.parquet"
    mine.write_bytes(b"y")
    ci = tmp_path / "ci.parquet"
    ci.write_bytes(b"z")

    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": str(shared)})
    assert datasets.resolve(ws, "sales").source == "share"
    assert datasets.resolve(ws, "sales").path == str(shared)

    datasets.set_local_override(ws, "sales", str(mine))
    assert datasets.resolve(ws, "sales") == datasets.Resolved(
        "sales", {"kind": "share", "path": str(shared)}, str(mine), "local", True
    )

    monkeypatch.setenv(datasets.env_var_name("sales"), str(ci))
    found = datasets.resolve(ws, "sales")
    assert (found.source, found.path) == ("env", str(ci))

    with pytest.raises(KeyError):
        datasets.resolve(ws, "nope")


def test_a_unc_pointer_is_never_joined_onto_the_workspace(tmp_path):
    # The Windows case this feature exists for. A UNC path is ABSOLUTE on every platform
    # (mooring.toml is synced, so a path authored on Windows must read the same on macOS);
    # joining it onto the workspace root would report a nonsense location in the error.
    ws = _ws(tmp_path)
    for location in (UNC, UNC_BACKSLASH, "D:/finance/sales.parquet", r"C:\finance\sales.parquet"):
        assert datasets.is_absolute_location(location) is True
        resolved = datasets.local_path(ws, location)
        assert str(ws) not in resolved
    assert datasets.is_absolute_location("data/sales.parquet") is False


def test_a_relative_pointer_resolves_against_the_workspace(tmp_path):
    # A pointer may also name a file that DOES sync (a small lookup table), so relative
    # locations resolve against the workspace root rather than the process cwd.
    ws = _ws(tmp_path)
    (ws / "data").mkdir()
    (ws / "data" / "lookup.csv").write_text("a\n1\n", "utf-8")
    workspace_config.set_dataset(ws, "lookup", {"kind": "share", "path": "data/lookup.csv"})
    found = datasets.resolve(ws, "lookup")
    assert found.exists is True
    assert found.path.endswith("lookup.csv") and str(ws) in found.path


def test_https_pointer_resolves_to_the_sync_excluded_cache(tmp_path):
    ws = _ws(tmp_path)
    workspace_config.set_dataset(
        ws, "archive", {"kind": "https", "url": "https://example.org/a/archive.csv"}
    )
    found = datasets.resolve(ws, "archive")
    assert found.source == "cache" and found.exists is False
    assert found.path.replace("\\", "/").endswith(".mooring/datasets/cache/archive/archive.csv")


def test_cache_target_cannot_escape_the_cache_dir(tmp_path):
    ws = _ws(tmp_path)
    target = datasets.cache_target(ws, "evil", "https://x.example/../../../../etc/passwd?a=b")
    assert datasets.cache_dir(ws) in target.parents


# -- the injected kernel helper -------------------------------------------------


def test_install_runtime_writes_importable_stdlib_only_payload(tmp_path):
    ws = _ws(tmp_path)
    datasets.install_runtime(ws)
    src = (datasets.pylib_dir(ws) / "mooring_datasets.py").read_bytes()
    assert b"def path" in src and b"class Dataset" in src
    assert "mooring" not in _imported_roots(src)  # standalone in the kernel
    assert _imported_roots(src) <= {"__future__", "os", "re", "shutil", "tomllib", "pathlib", "urllib"}


def test_secret_detectors_match_the_runtime(tmp_path):
    # The duplicated detectors must not drift between the two modules.
    md = _load_payload(_ws(tmp_path))
    assert tuple(md._SECRET_TOKENS) == tuple(workspace_config._SECRET_TOKENS)
    assert set(md._SECRET_EXACT) == set(workspace_config._SECRET_EXACT)
    assert md._URL_SECRET_PATTERN == workspace_config._URL_SECRET_PATTERN


def test_kernel_path_resolves_the_same_order(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    shared = tmp_path / "shared.parquet"
    shared.write_bytes(b"x")
    mine = tmp_path / "mine.parquet"
    mine.write_bytes(b"y")
    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": str(shared)})
    md = _load_payload(ws)

    assert md.names() == ["sales"]
    assert md.path("SALES") == str(shared)  # any casing resolves
    assert md.exists("sales") is True
    datasets.set_local_override(ws, "sales", str(mine))
    assert md.path("sales") == str(mine)
    monkeypatch.setenv(datasets.env_var_name("sales"), str(shared))
    assert md.path("sales") == str(shared)


def test_kernel_drops_a_hand_added_credential(tmp_path):
    ws = _ws(tmp_path)
    (ws / "mooring.toml").write_text(
        f'[datasets.archive]\nkind = "https"\nurl = "https://x.example/a.csv?sig={SECRET}"\n',
        "utf-8",
    )
    md = _load_payload(ws)
    assert md.info("archive").location == ""  # the credential-bearing URL never surfaces
    with pytest.raises(FileNotFoundError) as excinfo:
        md.path("archive")
    assert SECRET not in str(excinfo.value)
    assert "datasets check" in str(excinfo.value)


def test_kernel_missing_file_error_names_dataset_location_and_fix(tmp_path):
    ws = _ws(tmp_path)
    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": UNC})
    md = _load_payload(ws)
    with pytest.raises(FileNotFoundError) as excinfo:
        md.path("sales")
    message = str(excinfo.value)
    assert "'sales'" in message  # the dataset
    assert "fileserver" in message  # where it looked
    assert "mooring datasets set-local sales" in message  # how to fix it here
    assert "MOORING_DATASET_SALES_PATH" in message  # ...or the env var
    assert str(ws) not in message  # never a workspace-joined nonsense location


def test_kernel_missing_file_error_names_the_redirect_that_failed(tmp_path):
    ws = _ws(tmp_path)
    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": UNC})
    datasets.set_local_override(ws, "sales", "D:/gone/sales.parquet")
    md = _load_payload(ws)
    with pytest.raises(FileNotFoundError) as excinfo:
        md.path("sales")
    assert "datasets.local.toml" in str(excinfo.value)  # blames the plane that won


def test_kernel_undefined_name_lists_what_is_defined(tmp_path):
    ws = _ws(tmp_path)
    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": UNC})
    md = _load_payload(ws)
    with pytest.raises(KeyError) as excinfo:
        md.path("sails")
    assert "sales" in str(excinfo.value) and "mooring datasets add" in str(excinfo.value)


def test_kernel_downloads_an_https_dataset_into_the_cache(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    workspace_config.set_dataset(
        ws, "archive", {"kind": "https", "url": "https://example.org/a/archive.csv"}
    )
    md = _load_payload(ws)
    calls: list[str] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        return io.BytesIO(b"a,b\n1,2\n")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    got = md.path("archive")
    assert calls == ["https://example.org/a/archive.csv"]
    assert md.Path(got).read_bytes() == b"a,b\n1,2\n"
    assert ".mooring" in got.replace("\\", "/")
    md.path("archive")  # cached: no second fetch
    assert len(calls) == 1
    md.path("archive", refresh=True)
    assert len(calls) == 2


def test_kernel_refuses_a_non_http_download(tmp_path):
    # A hand-edited file:// URL in the SYNCED file would otherwise make every teammate's
    # kernel read an arbitrary local path (urlopen serves file:// happily).
    ws = _ws(tmp_path)
    (ws / "mooring.toml").write_text(
        '[datasets.evil]\nkind = "https"\nurl = "file:///etc/passwd"\n', "utf-8"
    )
    md = _load_payload(ws)
    with pytest.raises(FileNotFoundError) as excinfo:
        md.path("evil")
    assert "http" in str(excinfo.value)


def test_it_composes_with_mooring_inputs(tmp_path):
    # The pointer says WHERE the data came from; the fingerprint proves it hasn't moved.
    ws = _ws(tmp_path)
    (ws / "notebooks").mkdir()
    notebook = ws / "notebooks" / "recon.py"
    notebook.write_text("# notebook\n", "utf-8")
    source = tmp_path / "sales.csv"
    source.write_text("amount\n1\n", "utf-8")
    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": str(source)})

    md = _load_payload(ws)
    inputs.install_runtime(ws)
    spec = importlib.util.spec_from_file_location(
        "mooring_inputs_for_datasets", inputs.pylib_dir(ws) / "mooring_inputs.py"
    )
    mi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mi)

    df = pl.DataFrame({"amount": [1]})
    g = {"mi": mi, "md": md, "df": df, "__file__": str(notebook)}
    exec('r = mi.fingerprint(df, "sales", path=md.path("sales"))', g)
    assert bool(g["r"]) is True  # first sighting counts as unchanged
    assert inputs.read_results(ws)["notebooks/recon.py"]["total"] == 1

    source.write_text("amount\n1\n2\n", "utf-8")  # the file moved on underneath us
    exec('r = mi.fingerprint(df, "sales", path=md.path("sales"))', g)
    assert bool(g["r"]) is False  # ...and the fingerprint catches it


# -- copilot context ------------------------------------------------------------


def test_copilot_guide_exposes_names_and_formats_but_no_location(tmp_path):
    ws = _ws(tmp_path)
    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": UNC})
    workspace_config.set_dataset(
        ws, "archive", {"kind": "https", "url": "https://example.org/a/archive.csv"}
    )
    guide = datasets.copilot_guide(ws)
    assert "sales: parquet" in guide and "archive: csv" in guide
    assert "fileserver" not in guide  # never the share
    assert "example.org" not in guide  # never the URL
    assert "md.path" in guide and "mi.fingerprint" in guide  # it can author the wiring
    assert datasets.copilot_guide(tmp_path / "empty") == ""


def test_copilot_guide_never_carries_a_credential(tmp_path):
    ws = _ws(tmp_path)
    (ws / "mooring.toml").write_text(
        f'[datasets.archive]\nkind = "https"\nurl = "https://x.example/a.csv?sig={SECRET}"\n'
        f'[datasets.other]\nkind = "share"\npath = "//fs/x.parquet"\ntoken = "{SECRET}"\n',
        "utf-8",
    )
    assert SECRET not in datasets.copilot_guide(ws)


def test_nothing_reaches_ai_except_the_guide():
    # The guide is the ONE thing that crosses into ai/: it is passed IN by the app layer
    # (app.chat_service), so no ai/ module can reach the pointers — and therefore the
    # locations — for itself. A new `from mooring import datasets` under ai/ would be a
    # second, unreviewed channel.
    ai_dir = Path(datasets.__file__).parent / "ai"
    offenders = []
    for module in sorted(ai_dir.rglob("*.py")):
        tree = ast.parse(module.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                a.name == "mooring.datasets" for a in node.names
            ):
                offenders.append(module.name)
            elif isinstance(node, ast.ImportFrom) and node.module in ("mooring", "mooring.datasets"):
                if node.module == "mooring.datasets" or any(
                    a.name == "datasets" for a in node.names
                ):
                    offenders.append(module.name)
    assert offenders == []


def test_build_system_context_folds_in_the_datasets_guide(tmp_path):
    from mooring.ai import egress

    ws = _ws(tmp_path)
    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": UNC})
    ctx = egress.build_system_context(
        schema_text="amount: float",
        notebook_source="df = 1",
        notebook_rel="nb.py",
        datasets_help=datasets.copilot_guide(ws),
    )
    assert "sales" in ctx and "md.path" in ctx
    assert "fileserver" not in ctx  # value-blindness holds for datasets too

    without = egress.build_system_context(
        schema_text="amount: float", notebook_source="df = 1", notebook_rel="nb.py"
    )
    assert "DATASETS" not in without  # omitted unless explicitly provided


# -- the CLI --------------------------------------------------------------------


def _cfg(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return Config(client_id="", owner="", repo="", workspace_path=str(ws))


def _ns(**kw):
    return argparse.Namespace(**kw)


def test_cli_add_writes_a_pointer_and_refuses_a_credential(tmp_path):
    cfg = _cfg(tmp_path)
    rc = cli.cmd_datasets(
        cfg, _ns(datasets_command="add", name="sales", fields=["kind=share", f"path={UNC}"])
    )
    assert rc == 0
    assert workspace_config.datasets(cfg.workspace())["sales"]["path"] == UNC
    with pytest.raises(SystemExit):
        cli.cmd_datasets(
            cfg,
            _ns(
                datasets_command="add",
                name="archive",
                fields=["kind=https", f"url=https://x/y.csv?sig={SECRET}"],
            ),
        )
    assert "archive" not in workspace_config.datasets(cfg.workspace())


def test_cli_add_rejects_a_high_entropy_token_in_a_location(tmp_path):
    # Defence in depth: the richer ai.secrets scan catches a credential the URL-parameter
    # floor would miss (here, a GitHub-PAT shape pasted into the path).
    cfg = _cfg(tmp_path)
    token = "ghp_" + "0123456789" * 3 + "0123456"
    with pytest.raises(SystemExit):
        cli.cmd_datasets(
            cfg, _ns(datasets_command="add", name="sales", fields=["kind=share", f"path=/x/{token}"])
        )
    assert workspace_config.datasets(cfg.workspace()) == {}


def test_cli_list_shows_where_it_resolves(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    cli.cmd_datasets(
        cfg, _ns(datasets_command="add", name="sales", fields=["kind=share", f"path={UNC}"])
    )
    capsys.readouterr()
    assert cli.cmd_datasets(cfg, _ns(datasets_command="list")) == 0
    out = capsys.readouterr().out
    assert "sales" in out and "team pointer" in out and "MISSING" in out


def test_cli_set_local_redirects_this_machine_only(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    ws = cfg.workspace()
    here = tmp_path / "here.parquet"
    here.write_bytes(b"x")
    workspace_config.set_dataset(ws, "sales", {"kind": "share", "path": UNC})
    rc = cli.cmd_datasets(
        cfg, _ns(datasets_command="set-local", name="sales", location=str(here), clear=False)
    )
    assert rc == 0
    assert datasets.resolve(ws, "sales").path == str(here)
    assert str(here) not in (ws / "mooring.toml").read_text("utf-8")
    assert cli.cmd_datasets(
        cfg, _ns(datasets_command="set-local", name="sales", location=None, clear=True)
    ) == 0
    assert datasets.local_override(ws, "sales") is None


def test_cli_rm(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    workspace_config.set_dataset(cfg.workspace(), "sales", {"kind": "share", "path": UNC})
    assert cli.cmd_datasets(cfg, _ns(datasets_command="rm", name="sales")) == 0
    assert workspace_config.datasets(cfg.workspace()) == {}


def test_cli_check_flags_a_hand_added_credential(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    (cfg.workspace() / "mooring.toml").write_text(
        f'[datasets.archive]\nkind = "https"\nurl = "https://x.example/a.csv?sig={SECRET}"\n'
        f'[datasets.sales]\nkind = "share"\npath = "//fs/x.parquet"\ntoken = "{SECRET}"\n',
        "utf-8",
    )
    rc = cli.cmd_datasets(cfg, _ns(datasets_command="check"))
    assert rc == 1  # non-zero: a problem was found
    out = capsys.readouterr().out
    assert "archive.url" in out and "sales.token" in out
    assert SECRET not in out  # value-free report — never echoes the credential


def test_cli_check_clean(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    workspace_config.set_dataset(cfg.workspace(), "sales", {"kind": "share", "path": UNC})
    assert cli.cmd_datasets(cfg, _ns(datasets_command="check")) == 0
    assert "No credentials" in capsys.readouterr().out


def test_cli_parser_accepts_the_datasets_verbs():
    parser = cli._build_parser()
    args = parser.parse_args(["datasets", "add", "sales", "kind=share", f"path={UNC}"])
    assert (args.command, args.datasets_command, args.fields[0]) == ("datasets", "add", "kind=share")
    args = parser.parse_args(["datasets", "set-local", "sales", "D:/x.parquet"])
    assert args.location == "D:/x.parquet"
    args = parser.parse_args(["datasets", "set-local", "sales", "--clear"])
    assert args.clear is True and args.location is None
