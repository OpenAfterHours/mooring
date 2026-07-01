"""app/notebooks — the shared cli↔hub operations.

The contract that matters: these helpers RAISE, never exit (the hub calls them
in-process), and the gate/policy behavior each adapter previously duplicated is
preserved byte-for-byte (the adapters' own tests pin that end to end; these pin
the seam directly).
"""

from __future__ import annotations

import types

import pytest

from mooring import auth, config
from mooring.app import notebooks
from mooring.github import AuthFailed

# -- client_for: raises, never exits ------------------------------------------


def test_client_for_unconfigured_raises_not_configured(tmp_path):
    cfg = config.Config(workspace_path=str(tmp_path))
    with pytest.raises(notebooks.NotConfigured):
        notebooks.client_for(cfg)


def test_client_for_no_token_raises_auth_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "get_token", lambda host=None: None)
    cfg = config.Config(client_id="c", owner="acme", repo="nbs", workspace_path=str(tmp_path))
    with pytest.raises(AuthFailed) as exc:
        notebooks.client_for(cfg)
    # A missing token is an auth failure, not a config failure.
    assert not isinstance(exc.value, notebooks.NotConfigured)


def test_client_for_builds_the_same_client_both_adapters_did(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "get_token", lambda host=None: "tok")
    cfg = config.Config(client_id="c", owner="acme", repo="nbs", workspace_path=str(tmp_path))
    client = notebooks.client_for(cfg)
    # Constructor args byte-identical to the previous cli/hub construction sites.
    assert (client.owner, client.repo) == ("acme", "nbs")


def test_not_configured_is_an_auth_failed():
    # The hub's existing AuthFailed handling must degrade gracefully for the
    # unconfigured case too — the CLI catches NotConfigured FIRST for guidance.
    assert issubclass(notebooks.NotConfigured, AuthFailed)


# -- openable_kind: the shared open gate --------------------------------------


def test_pbip_opens_as_pbip_without_reading(tmp_path):
    # Returns before any file read — the target need not even exist.
    assert notebooks.openable_kind(tmp_path / "r.pbip", "reports/r.pbip") == "pbip"


def test_non_py_is_refused(tmp_path):
    with pytest.raises(notebooks.OpenRefused, match="Only .py notebooks and .pbip"):
        notebooks.openable_kind(tmp_path / "d.csv", "data/d.csv")


def test_module_is_refused_with_basename_by_default(tmp_path):
    target = tmp_path / "helper.py"
    target.write_text("x = 1\n", "utf-8")
    with pytest.raises(notebooks.OpenRefused, match=r"^helper\.py is a Python module"):
        notebooks.openable_kind(target, "notebooks/helper.py")


def test_module_refusal_display_override_for_the_cli(tmp_path):
    target = tmp_path / "helper.py"
    target.write_text("x = 1\n", "utf-8")
    with pytest.raises(notebooks.OpenRefused, match=r"^notebooks/helper\.py is a Python"):
        notebooks.openable_kind(target, "notebooks/helper.py", display="notebooks/helper.py")


def test_dunder_marker_is_refused_even_when_blank(tmp_path):
    target = tmp_path / "__init__.py"
    target.write_text("", "utf-8")
    with pytest.raises(notebooks.OpenRefused):
        notebooks.openable_kind(target, "notebooks/__init__.py")


def test_blank_stub_opens_as_notebook(tmp_path):
    target = tmp_path / "new.py"
    target.write_text("", "utf-8")
    assert notebooks.openable_kind(target, "notebooks/new.py") == "notebook"


def test_marimo_notebook_opens_as_notebook(tmp_path):
    target = tmp_path / "nb.py"
    target.write_text(
        "import marimo\napp = marimo.App()\n",
        "utf-8",
    )
    assert notebooks.openable_kind(target, "notebooks/nb.py") == "notebook"


# -- resolve_adoptable: normalize + partition ----------------------------------


def _candidates(*folders):
    return [types.SimpleNamespace(folder=f) for f in folders]


def test_resolve_adoptable_partitions_known_and_unknown():
    chosen, unknown = notebooks.resolve_adoptable(
        _candidates("models", "etl"), ["models", "typo"]
    )
    assert chosen == ["models"]
    assert unknown == ["typo"]


def test_resolve_adoptable_normalizes_requested_names():
    # Windows separators / stray slashes are normalized before matching, so a
    # pasted path still matches what discovery found.
    chosen, unknown = notebooks.resolve_adoptable(_candidates("etl"), ["etl\\", "etl/"])
    assert unknown == []
    assert chosen == ["etl", "etl"]
