"""CLI-binary discovery for the Copilot provider.

github-copilot-sdk >=1.0.2 stopped bundling ``copilot.exe`` under ``<pkg>/bin/``
and instead downloads it to a shared cache at first use. These tests pin the
provider's discovery/availability contract against that change: a fresh
``mooring[copilot]`` install (SDK importable, CLI version pinned, binary not yet
cached) must report *available* and fetch the binary lazily for the login
subprocess — rather than the old ``bin/``-only check wrongly reporting the extra
as not installed.

The pinned dev/CI SDK is the legacy 1.0.1 (which bundles the binary and has no
``_cli_download``), so the new-SDK surface is simulated with fake submodules
rather than relying on whichever version happens to be installed.
"""

from __future__ import annotations

import sys
import types

import pytest

copilot = pytest.importorskip("copilot")

from mooring.ai.base import AIError  # noqa: E402
from mooring.ai.copilot import CopilotProvider  # noqa: E402


@pytest.fixture
def isolate_discovery(monkeypatch, tmp_path):
    """Neutralise every binary source except the simulated SDK download surface.

    Clears ``COPILOT_CLI_PATH``, points the SDK package at a ``bin``-less temp dir
    (so the legacy bundled-binary branch can't match the real 1.0.1 install), and
    makes ``PATH`` lookup miss — leaving the fake ``_cli_download`` / ``_cli_version``
    modules as the sole signal.
    """
    import mooring.ai.copilot as copilot_mod

    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    monkeypatch.setattr(copilot, "__file__", str(tmp_path / "__init__.py"))
    monkeypatch.setattr(copilot_mod.shutil, "which", lambda _name: None)


def _fake_sdk(monkeypatch, *, cached=None, version="1.0.65", downloaded=None, download_calls=None):
    """Install fake ``copilot._cli_download`` / ``copilot._cli_version`` modules."""
    dl = types.ModuleType("copilot._cli_download")
    dl.get_cached_cli_path = lambda *a, **k: cached

    def _get_or_download(*a, **k):
        if download_calls is not None:
            download_calls.append(True)
        return downloaded

    dl.get_or_download_cli = _get_or_download

    ver = types.ModuleType("copilot._cli_version")
    ver.CLI_VERSION = version

    monkeypatch.setitem(sys.modules, "copilot._cli_download", dl)
    monkeypatch.setitem(sys.modules, "copilot._cli_version", ver)
    monkeypatch.setattr(copilot, "_cli_download", dl, raising=False)
    monkeypatch.setattr(copilot, "_cli_version", ver, raising=False)


def test_available_when_version_pinned_but_binary_not_cached(isolate_discovery, monkeypatch):
    # The exact fresh-install state: SDK importable, CLI version pinned, nothing
    # downloaded yet. _cli_path() (no download) finds nothing, but available() is
    # True because the SDK can fetch the binary on first use.
    _fake_sdk(monkeypatch, cached=None, version="1.0.65")
    provider = CopilotProvider()
    assert provider._cli_path() is None
    assert provider.available() is True


def test_unavailable_when_no_version_and_no_binary(isolate_discovery, monkeypatch):
    # A source/editable SDK pins CLI_VERSION=None and can't auto-download, so with
    # no resolvable binary the provider must report unavailable.
    _fake_sdk(monkeypatch, cached=None, version=None)
    provider = CopilotProvider()
    assert provider.available() is False


def test_cli_path_prefers_cached_binary_without_downloading(isolate_discovery, monkeypatch, tmp_path):
    binary = tmp_path / "copilot.exe"
    binary.write_text("#!stub", "utf-8")
    download_calls: list[bool] = []
    _fake_sdk(monkeypatch, cached=str(binary), version="1.0.65", download_calls=download_calls)

    provider = CopilotProvider()
    assert provider._cli_path() == str(binary)
    assert provider._cli_path(download=True) == str(binary)
    # A cached binary must never trigger a network fetch.
    assert download_calls == []


def test_env_override_wins_over_cache(isolate_discovery, monkeypatch, tmp_path):
    override = tmp_path / "my-copilot.exe"
    override.write_text("#!stub", "utf-8")
    monkeypatch.setenv("COPILOT_CLI_PATH", str(override))
    _fake_sdk(monkeypatch, cached=str(tmp_path / "other.exe"), version="1.0.65")
    provider = CopilotProvider()
    assert provider._cli_path() == str(override)


def test_require_cli_downloads_on_first_use(isolate_discovery, monkeypatch, tmp_path):
    binary = tmp_path / "cache" / "copilot.exe"
    binary.parent.mkdir()
    binary.write_text("#!stub", "utf-8")
    download_calls: list[bool] = []
    _fake_sdk(
        monkeypatch,
        cached=None,
        version="1.0.65",
        downloaded=str(binary),
        download_calls=download_calls,
    )
    provider = CopilotProvider()
    assert provider._cli_path() is None  # nothing cached yet, no download without the flag
    assert provider._require_cli() == str(binary)
    assert download_calls == [True]


def test_require_cli_raises_when_unobtainable(isolate_discovery, monkeypatch):
    # Offline / fetch failed: get_or_download_cli returns None.
    _fake_sdk(monkeypatch, cached=None, version="1.0.65", downloaded=None)
    provider = CopilotProvider()
    with pytest.raises(AIError, match="Couldn't obtain the Copilot CLI"):
        provider._require_cli()
