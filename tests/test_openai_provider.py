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


# -- list_models / key validation (a fake client, no network) -------------------


class _FakeModels:
    def __init__(self, ids=None, error=None):
        self._ids = ids or []
        self._error = error

    def list(self):
        if self._error is not None:
            raise self._error
        return [types.SimpleNamespace(id=i) for i in self._ids]


def _fake_client(ids=None, error=None):
    return types.SimpleNamespace(models=_FakeModels(ids, error))


def _provider_with_client(monkeypatch, client):
    monkeypatch.setenv("MOORING_OPENAI_API_KEY", "sk-local")
    provider = OpenAIProvider(model="gpt-4o")
    monkeypatch.setattr(provider, "available", lambda: True)
    monkeypatch.setattr(provider, "_make_client", lambda: client)
    return provider


def test_list_models_filters_and_shapes(monkeypatch):
    client = _fake_client(ids=["gpt-4o", "text-embedding-3-large", "o3-mini", "whisper-1"])
    provider = _provider_with_client(monkeypatch, client)
    models = provider.list_models(force=True)
    assert [m["id"] for m in models] == ["gpt-4o", "o3-mini"]  # non-chat ids dropped, sorted
    assert models[0] == {
        "id": "gpt-4o",
        "name": "gpt-4o",
        "efforts": [],
        "default_effort": "",
        "multiplier": None,
    }
    assert provider.models_error() == ""


def test_list_models_reports_auth_error(monkeypatch):
    provider = _provider_with_client(monkeypatch, _fake_client(error=Exception("401 Unauthorized")))
    assert provider.list_models(force=True) == []
    assert "key" in provider.models_error().lower()


def test_status_force_validates_via_models_list(monkeypatch):
    provider = _provider_with_client(monkeypatch, _fake_client(ids=["gpt-4o"]))
    st = provider.status(force=True)
    assert st.connected is True and st.detail == "Connected."


def test_status_force_reports_a_bad_key(monkeypatch):
    provider = _provider_with_client(monkeypatch, _fake_client(error=Exception("401 invalid api key")))
    st = provider.status(force=True)
    assert st.connected is False and "key" in st.detail.lower()


# -- the hub POST /api/ai/key route --------------------------------------------


class _FakeKeyring:
    def __init__(self):
        self.store: dict = {}

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def _openai_hub_client(tmp_path, monkeypatch, fake_kr, provider="openai"):
    from starlette.testclient import TestClient

    from mooring import config
    from mooring.ai_config import AiConfig
    from mooring.hub.server import Hub, create_app

    monkeypatch.setattr("mooring.ai.openai_provider._keyring", lambda: fake_kr)
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    app_cfg = config.AppConfig(repos=(spec,), active_alias="ws", ai=AiConfig(provider=provider))
    return TestClient(create_app(Hub(app_cfg)))


def test_api_key_stores_for_openai_and_reprobes(tmp_path, monkeypatch):
    fake_kr = _FakeKeyring()
    with _openai_hub_client(tmp_path, monkeypatch, fake_kr) as client:
        resp = client.post("/api/ai/key", json={"key": "sk-hub-test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and "status" in body
    # The key was stored in the (fake) OS credential store, not any synced file.
    assert fake_kr.store[("mooring-openai", "default")] == "sk-hub-test"


def test_api_key_rejects_non_openai_provider(tmp_path, monkeypatch):
    fake_kr = _FakeKeyring()
    with _openai_hub_client(tmp_path, monkeypatch, fake_kr, provider="copilot") as client:
        resp = client.post("/api/ai/key", json={"key": "sk-x"})
    assert resp.status_code == 400
    assert "openai" in resp.json()["error"].lower()
    assert fake_kr.store == {}  # nothing stored


def test_api_key_rejects_empty(tmp_path, monkeypatch):
    fake_kr = _FakeKeyring()
    with _openai_hub_client(tmp_path, monkeypatch, fake_kr) as client:
        resp = client.post("/api/ai/key", json={"key": "   "})
    assert resp.status_code == 400
