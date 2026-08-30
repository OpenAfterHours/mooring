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


def test_chat_model_filter_canonical_openai():
    from mooring.ai.openai_provider import _is_chat_model

    # require_prefix (canonical OpenAI): only known chat prefixes survive.
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
        "llama-3.1-70b",  # a non-OpenAI id is NOT a canonical-OpenAI chat model
    ):
        assert not _is_chat_model(bad), bad


def test_chat_model_filter_custom_endpoint_keeps_non_openai_ids():
    from mooring.ai.openai_provider import _is_chat_model

    # require_prefix=False (a custom base_url): keep anything not obviously non-chat,
    # so a gateway/local server's models are not hidden.
    for good in (
        "llama-3.1-70b",
        "qwen2.5-coder",
        "mistral-large",
        "deepseek-r1",
        "meta-llama/llama-3.1-70b-instruct",
    ):
        assert _is_chat_model(good, require_prefix=False), good
    for bad in ("text-embedding-3-large", "whisper-large-v3", "tts-1"):
        assert not _is_chat_model(bad, require_prefix=False), bad


def test_host_parses_without_urllib():
    from mooring.ai.openai_provider import _host

    assert _host("http://localhost:11434/v1") == "localhost:11434"
    assert _host("https://my-res.openai.azure.com") == "my-res.openai.azure.com"
    assert _host("https://openrouter.ai/api/v1") == "openrouter.ai"


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


# -- OpenAI-compatible endpoints (base_url): key optional, ids not prefix-filtered --


def test_base_url_endpoint_connects_without_a_key(monkeypatch):
    monkeypatch.delenv("MOORING_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider(model="llama-3.1-70b", base_url="http://localhost:11434/v1")
    monkeypatch.setattr(provider, "available", lambda: True)
    st = provider.status()  # cheap path: configured via base_url alone, no key needed
    assert st.connected is True
    assert "localhost:11434" in st.detail


def test_list_models_keeps_non_openai_ids_for_a_custom_endpoint(monkeypatch):
    monkeypatch.delenv("MOORING_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _fake_client(ids=["llama-3.1-70b", "qwen2.5", "text-embedding-3-large"])
    provider = OpenAIProvider(base_url="http://localhost:11434/v1")
    monkeypatch.setattr(provider, "available", lambda: True)
    monkeypatch.setattr(provider, "_make_client", lambda: client)
    ids = [m["id"] for m in provider.list_models(force=True)]
    assert "llama-3.1-70b" in ids and "qwen2.5" in ids  # not hidden by an OpenAI prefix filter
    assert "text-embedding-3-large" not in ids  # non-chat still dropped


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


# -- reasoning effort: the picker the analyst never had ------------------------


def test_reasoning_efforts_lead_with_the_send_nothing_sentinel():
    from mooring.ai.openai_provider import _efforts_for

    efforts = _efforts_for("o3-mini")
    # Order is a UI contract: chat.js/batch.js fall back to efforts[0] when the user
    # has configured no default, so "default" (= send no reasoning_effort at all)
    # MUST be first or making the picker visible would change what is sent.
    assert efforts == ["default", "none", "low", "medium", "high"]
    assert efforts[0] == "default"


@pytest.mark.parametrize(
    ("model_id", "offered"),
    [
        # Reasoning families: the picker must appear.
        ("gpt-5.6-sol", True),
        ("gpt-5", True),
        ("o1", True),
        ("o3-mini", True),
        ("o4-mini", True),
        ("o5-preview", True),
        ("my-reasoning-model", True),  # a gateway id that SAYS it reasons
        # Plain chat models and gateway ids: the picker must stay hidden, because the
        # request path would drop the param anyway.
        ("gpt-4o", False),
        ("gpt-4.1", False),
        ("chatgpt-4o-latest", False),
        ("llama-3-70b", False),
        ("qwen2.5-coder", False),
        ("", False),
    ],
)
def test_efforts_are_offered_for_reasoning_models_only(model_id, offered):
    """The picker offers efforts iff the request path would send one.

    The expectations are written out rather than derived from ``_is_reasoning_model``:
    computing them from the function under test made the assertion unfalsifiable, so
    the twelve ids proved nothing about the mapping.
    """
    from mooring.ai.openai_provider import _efforts_for

    assert bool(_efforts_for(model_id)) is offered, model_id


def test_efforts_are_a_fresh_list_the_caller_cannot_corrupt():
    """The advertised list is per-call, not the module tuple: the hub UNIONS a
    configured effort into it (hub/routes/chat.py::_offer_configured_effort), and one
    model's listing must never grow another's — or the module constant's."""
    from mooring.ai import openai_provider

    first = openai_provider._efforts_for("o3-mini")
    first.append("xhigh")
    assert openai_provider._efforts_for("o3-mini") == ["default", "none", "low", "medium", "high"]
    assert openai_provider._REASONING_EFFORTS == ("default", "none", "low", "medium", "high")


def test_list_models_offers_efforts_only_for_reasoning_models(monkeypatch):
    client = _fake_client(ids=["gpt-4o", "gpt-5.6-sol", "o3-mini"])
    provider = _provider_with_client(monkeypatch, client)
    by_id = {m["id"]: m for m in provider.list_models(force=True)}
    for reasoning in ("gpt-5.6-sol", "o3-mini"):
        assert by_id[reasoning]["efforts"] == ["default", "none", "low", "medium", "high"]
        assert by_id[reasoning]["default_effort"] == ""  # no per-model metadata to report
    # A plain chat model keeps an empty list, so its picker stays hidden as today.
    assert by_id["gpt-4o"]["efforts"] == []


def test_list_models_custom_endpoint_does_not_mislabel_non_openai_ids(monkeypatch):
    monkeypatch.delenv("MOORING_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _fake_client(ids=["llama-3-70b", "o3-mini"])
    provider = OpenAIProvider(base_url="http://localhost:11434/v1")
    monkeypatch.setattr(provider, "available", lambda: True)
    monkeypatch.setattr(provider, "_make_client", lambda: client)
    by_id = {m["id"]: m for m in provider.list_models(force=True)}
    # The base_url path skips prefix-filtering, so the gateway's own id must still be
    # listed — and must NOT be mislabelled as reasoning-capable.
    assert by_id["llama-3-70b"]["efforts"] == []
    assert by_id["o3-mini"]["efforts"][0] == "default"


_EFFORT_400 = (
    "Error code: 400 - {'error': {'message': \"Function tools with reasoning_effort are "
    "not supported for gpt-5.6-sol in /v1/chat/completions. To use function tools, use "
    "/v1/responses or set reasoning_effort to 'none'.\", 'type': 'invalid_request_error', "
    "'param': 'reasoning_effort', 'code': None}}"
)


# A 401 whose body ECHOES the request's parameters — routine — and whose own code
# ("invalid_api_key") carries a rejection word. It therefore matches BOTH the auth
# markers and the effort branch's two requirements, so it pins which branch wins.
_ECHOING_401 = (
    "Error code: 401 - {'error': {'message': \"Incorrect API key provided: sk-ab***. "
    "(request: model=gpt-5, reasoning_effort=high, tools=[mooring_get_schema])\", "
    "'type': 'invalid_request_error', 'code': 'invalid_api_key'}}"
)
# The same trap for rate limits: a gateway rewording a 429 while naming the parameter
# and saying it is not supported. Only the STATUS decides correctly here.
_ECHOING_429 = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for gpt-5 in "
    "organization org-x on tokens per min. Limit 30000, Used 30000 (params: "
    "reasoning_effort=high, not supported above your tier).', "
    "'type': 'rate_limit_exceeded'}}"
)
# Mentions the parameter with NO rejection word: a bare mention is not a rejection.
_MENTIONING_500 = (
    "Error code: 500 - {'error': {'message': 'The server had an error while processing "
    "your request (params: model=gpt-5, reasoning_effort=high). Sorry about that!', "
    "'type': 'server_error'}}"
)


def test_friendly_error_explains_a_reasoning_effort_rejection():
    from mooring.ai.openai_provider import friendly_error

    out = friendly_error(_EFFORT_400)
    assert "reasoning effort" in out.lower()
    assert "ai.reasoning_effort" in out and "MOORING_AI_REASONING_EFFORT" in out
    assert "'none'" in out and "'default'" in out  # what to pick instead
    assert "tools" in out.lower()  # says WHY: an effort alongside function tools


def test_friendly_error_points_at_the_control_that_actually_decides():
    """The remedy must name the effort PICKER, not only the Settings field.

    Clearing Settings -> 'Default reasoning effort' is inert while a stored pick
    exists: the pick beats the configured default (ChatCore.chooseEffort), so an
    instruction to clear the setting alone would send the user in a circle."""
    from mooring.ai.openai_provider import friendly_error

    out = friendly_error(_EFFORT_400).lower()
    assert "picker" in out
    assert "batch" in out  # the batch page carries the same control
    assert "override" in out  # says the pick beats the setting


def test_friendly_error_keeps_the_original_message():
    """The fixed guidance must not throw the server's own words away — they are the
    only place the model id and the real reason survive."""
    from mooring.ai.openai_provider import friendly_error

    out = friendly_error(_EFFORT_400)
    assert "gpt-5.6-sol" in out
    assert "/v1/responses" in out


def test_friendly_error_reports_an_echoing_401_as_an_auth_fault():
    # The branch ORDER is the property under test: an effort branch placed first (or
    # matching on a bare mention of the field) reports this as a rejected effort and
    # throws the real cause away.
    from mooring.ai.openai_provider import friendly_error

    out = friendly_error(_ECHOING_401)
    assert "api key" in out.lower()
    assert "reasoning" not in out.lower()


def test_friendly_error_reports_an_echoing_429_as_a_rate_limit():
    from mooring.ai.openai_provider import friendly_error

    out = friendly_error(_ECHOING_429)
    assert "rate-limited" in out.lower()
    assert "reasoning" not in out.lower()


def test_friendly_error_needs_a_rejection_not_a_mention():
    # A 500 that merely echoes the parameter is a server fault, not a rejected effort.
    from mooring.ai.openai_provider import friendly_error

    out = friendly_error(_MENTIONING_500)
    assert out.startswith("OpenAI request failed:")
    assert "reasoning effort" not in out.lower()


def test_friendly_error_keeps_mapping_the_other_failures():
    from mooring.ai.openai_provider import friendly_error

    assert "key" in friendly_error("401 Unauthorized").lower()
    assert "key" in friendly_error("Error code: 401 - invalid api key").lower()
    assert "rate-limited" in friendly_error("429 Too Many Requests").lower()
    assert "rate-limited" in friendly_error("Error code: 429 - too many requests").lower()
    # A code-less body (a gateway raising a bare error) still routes on its words.
    assert "key" in friendly_error("your api key is not valid").lower()
    assert "rate-limited" in friendly_error("quota exceeded").lower()
    assert friendly_error("boom") == "OpenAI request failed: boom"
