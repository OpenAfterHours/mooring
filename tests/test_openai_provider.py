"""OpenAIProvider units that need no ``openai`` package and no network.

The API key must be resolved from LOCAL sources only (env / keyring), the model
listing must drop non-chat ids, and the factory must dispatch ``provider="openai"``.
"""

from __future__ import annotations

import types

import pytest

from mooring.ai import base
from mooring.ai.base import AIError
from mooring.ai.openai_provider import OpenAIProvider, resolve_api_key


@pytest.fixture(autouse=True)
def _no_keyring(monkeypatch):
    # Isolate from any real OS credential store so key-resolution tests are
    # deterministic (a developer's stored key must not change the outcome).
    monkeypatch.setattr("mooring.ai.openai_provider._keyring", lambda: None)


def test_resolve_api_key_prefers_mooring_env(monkeypatch):
    monkeypatch.setenv("MOORING_OPENAI_API_KEY", "sk-mooring")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    assert resolve_api_key() == "sk-mooring"  # MOORING_ wins (mirrors MOORING_TOKEN)


def test_resolve_api_key_falls_back_to_openai_env(monkeypatch):
    monkeypatch.delenv("MOORING_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    assert resolve_api_key() == "sk-openai"


def test_resolve_api_key_none_when_unset(monkeypatch):
    monkeypatch.delenv("MOORING_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert resolve_api_key() is None


def test_chat_model_filter():
    from mooring.ai.openai_provider import _is_chat_model

    for good in ("gpt-4o", "gpt-4.1", "o3-mini", "o4-mini", "gpt-5", "chatgpt-4o-latest"):
        assert _is_chat_model(good), good
    for bad in (
        "text-embedding-3-large",
        "whisper-1",
        "tts-1",
        "dall-e-3",
        "omni-moderation-latest",
        "gpt-4o-realtime-preview",
        "gpt-4o-search-preview",
    ):
        assert not _is_chat_model(bad), bad


def test_status_reports_missing_key(monkeypatch):
    monkeypatch.delenv("MOORING_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider(model="gpt-4o")
    monkeypatch.setattr(provider, "available", lambda: True)  # don't require the SDK installed
    st = provider.status()  # cheap path: key lookup only, no network
    assert st.available is True and st.connected is False
    assert "api key" in st.detail.lower()


def test_status_connected_when_key_present(monkeypatch):
    monkeypatch.setenv("MOORING_OPENAI_API_KEY", "sk-local")
    provider = OpenAIProvider(model="gpt-4o")
    monkeypatch.setattr(provider, "available", lambda: True)
    st = provider.status()
    assert st.connected is True
    # cached_status must also be network-free and agree.
    assert provider.cached_status().connected is True


def test_login_interactive_is_a_noop_that_succeeds(capsys):
    provider = OpenAIProvider()
    assert provider.login_interactive() == 0
    assert "MOORING_OPENAI_API_KEY" in capsys.readouterr().out


def test_get_provider_dispatches_openai():
    app_cfg = types.SimpleNamespace(
        ai_provider="openai",
        ai_model="gpt-4o",
        ai=types.SimpleNamespace(openai_base_url="", openai_api_version=""),
    )
    provider = base.get_provider(app_cfg)
    assert isinstance(provider, OpenAIProvider)
    assert provider.name == "openai" and provider.model == "gpt-4o"


def test_get_provider_unknown_lists_both():
    app_cfg = types.SimpleNamespace(ai_provider="mystery", ai_model="", ai=None)
    with pytest.raises(AIError) as exc:
        base.get_provider(app_cfg)
    assert "openai" in str(exc.value) and "copilot" in str(exc.value)
