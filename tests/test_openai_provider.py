"""OpenAIProvider units that need no ``openai`` package and no network.

The API key must be resolved from LOCAL sources only (env / keyring), the model
listing must drop non-chat ids, and the factory must dispatch ``provider="openai"``.
"""

from __future__ import annotations

import sys
import types

import pytest

from mooring.ai import base
from mooring.ai.base import AIError, AINotConnectedError
from mooring.ai.openai_provider import OpenAIProvider, resolve_api_key, resolve_trusted_api_key


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


def test_trusted_key_never_falls_back_to_general_credentials(monkeypatch):
    monkeypatch.delenv("MOORING_AI_TRUSTED_API_KEY", raising=False)
    monkeypatch.setenv("MOORING_OPENAI_API_KEY", "sk-general")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-sdk-general")
    assert resolve_trusted_api_key() is None

    monkeypatch.setenv("MOORING_AI_TRUSTED_API_KEY", "sk-approved")
    assert resolve_trusted_api_key() == "sk-approved"


def test_trusted_key_never_falls_back_to_user_keyring(monkeypatch):
    class PopulatedKeyring:
        @staticmethod
        def get_password(_service, _user):
            return "sk-user-controlled"

    monkeypatch.delenv("MOORING_AI_TRUSTED_API_KEY", raising=False)
    monkeypatch.setattr("mooring.ai.openai_provider._keyring", PopulatedKeyring)
    assert resolve_trusted_api_key() is None


def test_trusted_provider_requires_its_dedicated_key_even_with_base_url(monkeypatch):
    provider = OpenAIProvider(
        base_url="https://approved.example/v1",
        api_key_resolver=lambda: None,
        require_api_key=True,
        follow_redirects=False,
        name="trusted-openai",
    )
    monkeypatch.setattr(provider, "available", lambda: True)
    with pytest.raises(AINotConnectedError, match="MOORING_AI_TRUSTED_API_KEY"):
        provider.make_client()


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
    from mooring.ai_config import AiConfig

    app_cfg = types.SimpleNamespace(ai_provider="openai", ai_model="gpt-4o", ai=AiConfig())
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
    # ``**_`` swallows the metadata overrides (_metadata_client passes its own timeout
    # and retry cap); WHAT those overrides are is pinned separately, against a fake
    # ``openai`` module that records the real client kwargs.
    monkeypatch.setattr(provider, "_make_client", lambda **_: client)
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
    monkeypatch.setattr(provider, "_make_client", lambda **_: client)
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
    monkeypatch.setattr(provider, "_make_client", lambda **_: client)
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


# -- HTTP timeouts: the split, the knob, the retry cap -------------------------
#
# The bug these pin: a BARE float handed to the OpenAI SDK is expanded by httpx into
# Timeout(connect=N, read=N, write=N, pool=N), so one flat 30.0 also gave the READ
# leg 30 seconds. On a streaming request the read timeout is the maximum gap allowed
# BETWEEN chunks — including the wait before the first one — so a reasoning model
# behind a gateway that buffers the response tripped it every time.


def _fake_openai_module():
    """A stand-in for the ``openai`` module that records what it was constructed with.

    ``build_client`` imports openai (and httpx) lazily, INSIDE the function — the
    property that keeps this module importable when the ``mooring[openai]`` extra is
    absent, as it is in the test environment. That laziness is exactly what lets a
    fake in ``sys.modules`` capture what the real SDK would have been handed.
    """
    import httpx

    calls: list[tuple[str, dict]] = []

    def _record(name):
        def factory(**kwargs):
            calls.append((name, kwargs))
            # A ``models`` attribute so the same fake also serves the metadata-probe
            # tests, which go all the way through ``models.list()``.
            return types.SimpleNamespace(models=_FakeModels(["gpt-4o"]), **kwargs)

        return factory

    module = types.ModuleType("openai")
    module.OpenAI = _record("OpenAI")
    module.AzureOpenAI = _record("AzureOpenAI")
    module.DefaultHttpxClient = _record("DefaultHttpxClient")
    # The SDK re-exports its OWN http library's Timeout — httpx's up to openai 2.x,
    # httpx2's from openai 3.x. The real openai 2.x export is exactly this class.
    module.Timeout = httpx.Timeout
    module.calls = calls
    return module


@pytest.fixture
def fake_openai(monkeypatch):
    module = _fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", module)
    return module


def _client_kwargs(module, name: str = "OpenAI") -> dict:
    return next(kwargs for called, kwargs in module.calls if called == name)


def test_client_timeout_is_split_not_one_flat_number(fake_openai):
    from mooring.ai.openai_provider import build_client

    build_client("sk-x", timeout=300)
    timeout = _client_kwargs(fake_openai)["timeout"]
    # The connect leg is the real "a hung gateway can't wedge us" guard and stays short.
    assert timeout.connect == 10.0
    # The read/write/pool legs are the model's own budget, and are NOT the connect one.
    assert timeout.read == 300.0 and timeout.write == 300.0 and timeout.pool == 300.0
    assert timeout.connect != timeout.read
    # The regression itself: a 30s read budget silently cut the SDK's own considered
    # default (600s, chosen because reasoning models go quiet for minutes) by 20x.
    assert timeout.read != 30.0


def test_the_timeout_class_comes_from_the_sdk_not_from_httpx(fake_openai):
    """openai <= 2.x is built on httpx, openai 3.x on httpx2, and their Timeout classes
    are unrelated. Handing an httpx.Timeout to an httpx2-backed client does not raise —
    it nests one object inside the other and produces four garbage legs, i.e. silently
    re-opens this bug for anyone on a current SDK. Taking the class off the SDK is right
    for whichever library that build actually ships with."""
    from mooring.ai.openai_provider import build_client

    class _SdkTimeout:
        def __init__(self, budget, *, connect):
            self.read = self.write = self.pool = budget
            self.connect = connect

    fake_openai.Timeout = _SdkTimeout
    build_client("sk-x", timeout=300)
    timeout = _client_kwargs(fake_openai)["timeout"]
    assert isinstance(timeout, _SdkTimeout)
    assert timeout.connect == 10.0 and timeout.read == 300.0


def test_an_sdk_with_no_timeout_export_still_gets_a_split_timeout(fake_openai):
    from mooring.ai.openai_provider import build_client

    del fake_openai.Timeout  # the httpx fallback
    build_client("sk-x", timeout=300)
    timeout = _client_kwargs(fake_openai)["timeout"]
    assert timeout.connect == 10.0 and timeout.read == 300.0


def test_a_timeout_from_either_http_library_is_recognised():
    """``httpx2.TimeoutException`` is NOT a subclass of ``httpx.TimeoutException``, so a
    check written against one library misses every timeout raised by the other — and a
    stall MID-STREAM raises the raw transport exception, not the SDK's wrapper."""
    import httpx

    from mooring.ai.openai_provider import _timeout_types

    types_ = _timeout_types()
    assert httpx.TimeoutException in types_
    assert TimeoutError in types_  # always present, even with no SDK installed
    for name in ("httpx", "httpx2"):
        module = sys.modules.get(name)
        if module is not None:  # whichever is installed must be covered
            assert module.TimeoutException in types_


def test_client_caps_retries_at_one(fake_openai):
    """The SDK's default of 2 means three attempts. A streaming completion that timed
    out has already made the upstream model generate, so each retry re-bills a full
    reasoning generation and multiplies the wall-clock before anything is shown."""
    from mooring.ai.openai_provider import build_client

    build_client("sk-x", timeout=300)
    assert _client_kwargs(fake_openai)["max_retries"] == 1


def test_azure_client_gets_the_same_split_timeout_and_retry_cap(fake_openai):
    from mooring.ai.openai_provider import build_client

    build_client(
        "sk-x",
        base_url="https://res.openai.azure.com",
        api_version="2024-10-21",
        timeout=120,
    )
    kwargs = _client_kwargs(fake_openai, "AzureOpenAI")
    assert kwargs["timeout"].read == 120.0 and kwargs["timeout"].connect == 10.0
    assert kwargs["max_retries"] == 1


def test_the_redirect_pinned_transport_carries_the_timeout_too(fake_openai):
    """``follow_redirects=False`` (the customer-data route) builds its own httpx
    client; it must not be left on the SDK's default transport timeout."""
    from mooring.ai.openai_provider import build_client

    build_client("sk-x", timeout=90, follow_redirects=False)
    http_client = _client_kwargs(fake_openai, "DefaultHttpxClient")
    assert http_client["follow_redirects"] is False
    assert http_client["timeout"].read == 90.0 and http_client["timeout"].connect == 10.0


@pytest.mark.parametrize("configured", [45, 600, 3600])
def test_the_configured_timeout_reaches_the_built_client(monkeypatch, fake_openai, configured):
    monkeypatch.setenv("MOORING_OPENAI_API_KEY", "sk-local")
    OpenAIProvider(model="gpt-4o", timeout=configured)._make_client()
    assert _client_kwargs(fake_openai)["timeout"].read == float(configured)


@pytest.mark.parametrize(
    "bad",
    [
        0,
        -30,
        "abc",
        None,
        float("inf"),
        float("nan"),
        object(),
        # float() raises OverflowError here — neither a TypeError nor a ValueError, so
        # the original narrow except let it escape and made the provider itself
        # unconstructable through __init__. A knob documented as never raising must not
        # be the thing that stops a chat opening.
        10**400,
        # A bool is an int subclass, so True floated to 1.0: a ONE-SECOND budget, i.e.
        # every reasoning turn fails. (False already fell through as <= 0.)
        True,
        False,
        # Finite and positive, so nothing caught them — and no real endpoint can answer
        # inside a millisecond, so both are typos, not budgets.
        0.001,
        1e-9,
        0.999,
    ],
)
def test_a_nonsense_timeout_falls_back_to_the_default(bad):
    """A 0/negative/sub-second would fail every request instantly and an infinite one
    would never give up — all worse than the packaged default, and none worth raising
    over. The docstring promises "anything unusable falls back"; this is the whole
    table it has to keep that promise for."""
    from mooring.ai.openai_provider import timeout_seconds

    assert timeout_seconds(bad) == 300.0


@pytest.mark.parametrize("good", [1, 1.0, 45, 300, 600.5, 3600, "120"])
def test_a_usable_timeout_is_kept_verbatim(good):
    """The fallback must not swallow real values: the floor is one second, and a
    numeric string (what an env var is) counts."""
    from mooring.ai.openai_provider import timeout_seconds

    assert timeout_seconds(good) == float(good)


@pytest.mark.parametrize("bad", [0, -30, "abc"])
def test_a_nonsense_configured_timeout_never_builds_a_broken_client(monkeypatch, fake_openai, bad):
    monkeypatch.setenv("MOORING_OPENAI_API_KEY", "sk-local")
    OpenAIProvider(model="gpt-4o", timeout=bad)._make_client()
    assert _client_kwargs(fake_openai)["timeout"].read == 300.0


def test_the_packaged_default_is_the_one_in_ai_config():
    """One definition of "how long mooring waits": the provider imports it."""
    from mooring.ai import openai_provider
    from mooring.ai_config import OPENAI_TIMEOUT_DEFAULT, AiConfig

    assert AiConfig().openai_timeout_sec == OPENAI_TIMEOUT_DEFAULT == 300
    assert openai_provider.OPENAI_TIMEOUT_DEFAULT == 300
    assert OpenAIProvider()._timeout == 300.0


def test_the_knob_loads_from_toml_and_the_env_overrides_it():
    from mooring.ai_config import load_ai_config

    assert load_ai_config({}, {}).openai_timeout_sec == 300
    assert load_ai_config({"openai_timeout_sec": 900}, {}).openai_timeout_sec == 900
    env = {"MOORING_AI_OPENAI_TIMEOUT_SEC": "1200"}
    assert load_ai_config({"openai_timeout_sec": 900}, env).openai_timeout_sec == 1200
    # A hand-edited 0/negative or a typo'd env value keeps the default rather than
    # building a client that gives up before the model has said anything.
    assert load_ai_config({"openai_timeout_sec": 0}, {}).openai_timeout_sec == 300
    assert load_ai_config({"openai_timeout_sec": -5}, {}).openai_timeout_sec == 300
    assert load_ai_config({}, {"MOORING_AI_OPENAI_TIMEOUT_SEC": "soon"}).openai_timeout_sec == 300
    # Past an hour a timeout bounds nothing, and this one is what stops a stalled read
    # wedging a chat turn (the stream is read on a thread that cannot check Stop).
    assert load_ai_config({"openai_timeout_sec": 999_999}, {}).openai_timeout_sec == 3600


def test_get_provider_threads_the_configured_timeout():
    from mooring.ai_config import AiConfig

    app_cfg = types.SimpleNamespace(
        ai_provider="openai",
        ai_model="gpt-4o",
        ai=AiConfig(openai_timeout_sec=123),
    )
    assert base.get_provider(app_cfg)._timeout == 123.0


def test_the_trusted_route_gets_the_configured_timeout_too(tmp_path, monkeypatch):
    """The customer-data route builds its OWN provider (hub/server.py::
    _trusted_provider_for), so a knob wired only into ``get_provider`` would leave it
    on the old flat client — the exact shape of the bug being fixed. If anything the
    trusted endpoint is MORE likely to sit behind a buffering corporate gateway.
    """
    from mooring import config
    from mooring.ai_config import AiConfig, RoutingConfig
    from mooring.hub.server import Hub

    monkeypatch.setattr(
        "mooring.ai.openai_provider.resolve_trusted_api_key", lambda **_: "approved-key"
    )
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    routing = RoutingConfig(
        enabled=True,
        trusted_base_url="https://approved.example/v1",
        classifier_model="approved-inspector",
        coding_model="approved-coder",
        coding_models=("approved-coder",),
    )
    app_cfg = config.AppConfig(
        repos=(spec,),
        active_alias="ws",
        ai=AiConfig(provider="openai", openai_timeout_sec=77, routing=routing),
    )
    provider = Hub(app_cfg)._trusted_provider_for()
    assert provider.name == "trusted-openai"
    assert provider._timeout == 77.0


# -- friendly_error: the timeout branch ----------------------------------------


# A 504 whose body ECHOES the request's parameters, the same trap the 401/429 fixtures
# above set. It pins that the timeout branch sits with the other STATUS branches and
# above the effort branch: read as an effort rejection, the real cause is thrown away.
_ECHOING_504 = (
    "Error code: 504 - {'error': {'message': 'Gateway timeout waiting for upstream "
    "(params: model=gpt-5, reasoning_effort=high, unsupported by this pool).', "
    "'type': 'gateway_error'}}"
)


def test_friendly_error_explains_a_timeout_with_an_empty_message():
    """A mid-stream ``httpx.ReadTimeout`` routinely stringifies to "" — so without the
    exception object the user gets "OpenAI request failed:" and nothing after it."""
    import httpx

    from mooring.ai.openai_provider import friendly_error

    out = friendly_error("", httpx.ReadTimeout(""))
    assert not out.startswith("OpenAI request failed:")
    assert "timed out" in out.lower()
    assert "buffer" in out.lower()  # names the likely cause, not just the symptom
    assert "ai.openai_timeout_sec" in out and "MOORING_AI_OPENAI_TIMEOUT_SEC" in out


def test_friendly_error_trusts_the_exception_over_the_text():
    from mooring.ai.openai_provider import friendly_error

    assert "timed out" in friendly_error("boom", TimeoutError()).lower()


def test_friendly_error_finds_a_timeout_wrapped_by_another_exception():
    import httpx

    from mooring.ai.openai_provider import friendly_error

    try:
        try:
            raise httpx.ReadTimeout("")
        except httpx.ReadTimeout as inner:
            raise RuntimeError("the stream failed") from inner
    except RuntimeError as exc:
        out = friendly_error(str(exc), exc)
    assert "timed out" in out.lower()


@pytest.mark.parametrize(
    "msg",
    [
        "Error code: 504 - upstream did not respond",
        "Error code: 408 - request timeout",
        "504 Gateway Time-out",  # nginx's own wording, no SDK status prefix
        "The request timed out.",
        "HTTP 524",  # Cloudflare, bare status
        "upstream connect error: deadline exceeded",
    ],
)
def test_friendly_error_catches_a_code_less_gateway_timeout(msg):
    from mooring.ai.openai_provider import friendly_error

    assert "timed out" in friendly_error(msg).lower()


def test_friendly_error_reports_an_echoing_504_as_a_timeout_not_a_rejected_effort():
    from mooring.ai.openai_provider import friendly_error

    out = friendly_error(_ECHOING_504)
    assert "timed out" in out.lower()
    assert "reasoning effort" not in out.lower()


def test_the_timeout_branch_does_not_steal_auth_or_rate_faults():
    """Status still decides first: a 401/429 a gateway reworded to mention a timeout is
    an auth/rate fault, and the more specific claim about the response must win."""
    from mooring.ai.openai_provider import friendly_error

    assert "api key" in friendly_error("Error code: 401 - upstream timed out").lower()
    assert "rate-limited" in friendly_error("Error code: 429 - timed out waiting").lower()


def test_passing_a_non_timeout_exception_changes_no_other_verdict():
    """``exc`` is consulted for timeouts and nothing else, so every other branch reads
    exactly the text it always did — the one-argument calls elsewhere are unaffected."""
    from mooring.ai.openai_provider import friendly_error

    exc = ValueError("something else entirely")
    for msg in (_ECHOING_401, _ECHOING_429, _EFFORT_400, _MENTIONING_500, "boom"):
        assert friendly_error(msg, exc) == friendly_error(msg)
    assert friendly_error("boom", exc) == "OpenAI request failed: boom"


@pytest.mark.parametrize(
    "msg",
    [
        # Every one of these satisfies \b(?:408|504|524)\b, so the ORIGINAL
        # word-bounded pattern reported all three as a gateway timeout. A word
        # boundary only helps when the number sits inside a LONGER digit run — which
        # is never how a context-window error is worded.
        "you requested 504 tokens more than the 8192 limit",
        "max context length is 8192 tokens. You requested 408 tokens.",
        "model qwen-524 is not available",
        # ...and the longer-digit-run case, which was already safe.
        "model produced 15040 tokens of 25048 requested",
    ],
)
def test_an_ordinary_number_that_looks_like_a_status_is_not_a_timeout(msg):
    """A three-digit run is only a STATUS where a status goes: at the start of the
    message, or behind an HTTP/status introducer. A token count is not a verdict."""
    from mooring.ai.openai_provider import friendly_error

    assert friendly_error(msg).startswith("OpenAI request failed:")


@pytest.mark.parametrize(
    "msg",
    ["504 Gateway Time-out", "HTTP 524", "HTTP/1.1 504 upstream gone", "status: 408"],
)
def test_a_status_where_a_status_goes_is_still_a_timeout(msg):
    """Anchoring the pattern must not cost the case it exists for — a gateway that
    reports a bare status with no SDK ``Error code:`` prefix."""
    from mooring.ai.openai_provider import friendly_error

    assert "timed out" in friendly_error(msg).lower()


def test_a_wrapped_timeout_cannot_steal_an_auth_verdict():
    """Python sets ``__context__`` on ANY exception raised inside an ``except`` block,
    so a swallowed timeout followed by an unrelated re-raise leaves a ReadTimeout
    hanging off a message that plainly says 401. Walked evidence is consulted LAST, so
    the message's own verdict wins — as ``friendly_error``'s docstring promises."""
    import httpx

    from mooring.ai.openai_provider import friendly_error

    try:
        try:
            raise httpx.ReadTimeout("")
        except httpx.ReadTimeout:
            raise RuntimeError("Error code: 401 - {'error': {'message': 'Incorrect API key'}}")
    except RuntimeError as exc:
        out = friendly_error(str(exc), exc)
    assert "api key" in out.lower()
    assert "timed out" not in out.lower()


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        ("Error code: 429 - slow down", "rate-limited"),
        ("quota exceeded", "rate-limited"),
        ("reasoning_effort is not supported here", "reasoning effort"),
    ],
)
def test_a_wrapped_timeout_cannot_steal_any_other_verdict(msg, expected):
    import httpx

    from mooring.ai.openai_provider import friendly_error

    try:
        try:
            raise httpx.ReadTimeout("")
        except httpx.ReadTimeout:
            raise RuntimeError(msg)
    except RuntimeError as exc:
        assert expected in friendly_error(str(exc), exc).lower()


def test_a_code_less_effort_rejection_is_not_reported_as_a_timeout():
    """The timeout branch outranks the effort branch only where a STATUS CODE was
    given — the justification is "a 504's body echoes parameters", which says nothing
    about a code-less body. LiteLLM and vLLM do emit those, and an effort rejection
    that happens to mention a timeout is still an effort rejection."""
    from mooring.ai.openai_provider import friendly_error

    out = friendly_error("reasoning_effort is not supported; the request timed out earlier")
    assert "reasoning effort" in out.lower()
    assert "ai.openai_timeout_sec" not in out


# -- the metadata probes keep their OWN budget ---------------------------------
#
# ``models.list`` is a LOOKUP: nothing on the other end is thinking, so it has no
# business inheriting the chat budget. Against a gateway that accepts the TCP
# connection and then sends nothing — the exact audience for a configurable timeout —
# a probe on the 300s default takes ~10 minutes and one on the 3600 ceiling ~2 hours.
# The hub's "Check" button calls status(force=True) AND list_models(force=True) back to
# back in ONE sync route (hub/routes/chat.py), and the Health check has the same shape
# (hub/routes/setup.py), both on a threadpool worker with nothing able to cancel them.
# The knob's promise is "how long the MODEL may think", not "how long a lookup may
# hang".


class _StubTimeout:
    def __init__(self, budget, *, connect):
        self.read = self.write = self.pool = budget
        self.connect = connect


def _all_client_kwargs(module, name: str = "OpenAI") -> list[dict]:
    return [kwargs for called, kwargs in module.calls if called == name]


@pytest.mark.parametrize("configured", [45, 300, 3600])
def test_the_status_probe_budget_is_short_and_independent_of_the_knob(
    monkeypatch, fake_openai, configured
):
    monkeypatch.setenv("MOORING_OPENAI_API_KEY", "sk-local")
    provider = OpenAIProvider(model="gpt-4o", timeout=configured)
    assert provider.status(force=True).connected is True
    timeout = _client_kwargs(fake_openai)["timeout"]
    assert timeout.read == 30.0  # NOT `configured`, at any setting of the knob
    assert timeout.connect == 10.0


@pytest.mark.parametrize("configured", [45, 300, 3600])
def test_the_model_listing_budget_is_short_and_independent_of_the_knob(
    monkeypatch, fake_openai, configured
):
    monkeypatch.setenv("MOORING_OPENAI_API_KEY", "sk-local")
    provider = OpenAIProvider(model="gpt-4o", timeout=configured)
    assert [m["id"] for m in provider.list_models(force=True)] == ["gpt-4o"]
    assert _client_kwargs(fake_openai)["timeout"].read == 30.0


def test_the_chat_client_and_the_probe_client_are_budgeted_differently(monkeypatch, fake_openai):
    """One provider, two clients. The chat one waits for a model to think and retries
    once (a retry re-bills a whole generation); the metadata one gives up in 30s and
    retries twice — cheap, idempotent, nothing generated, so resilience is free. Both
    keep the same short connect leg: that guard was never about the model."""
    monkeypatch.setenv("MOORING_OPENAI_API_KEY", "sk-local")
    provider = OpenAIProvider(model="gpt-4o", timeout=1800)
    provider._make_client()  # what open_chat's client_factory builds
    provider._metadata_client()  # what _probe / list_models build
    chat, probe = _all_client_kwargs(fake_openai)
    assert chat["timeout"].read == 1800.0 and chat["max_retries"] == 1
    assert probe["timeout"].read == 30.0 and probe["max_retries"] == 2
    assert chat["timeout"].connect == probe["timeout"].connect == 10.0


def test_the_trusted_route_probes_on_the_short_budget_too(monkeypatch, fake_openai):
    """The redirect-pinned client builds its own httpx transport, so the probe budget
    has to reach that too or the customer-data route keeps the long one."""
    provider = OpenAIProvider(
        model="approved-coder",
        base_url="https://approved.example/v1",
        api_key_resolver=lambda: "sk-approved",
        require_api_key=True,
        follow_redirects=False,
        timeout=3600,
        name="trusted-openai",
    )
    provider.list_models(force=True)
    assert _client_kwargs(fake_openai)["timeout"].read == 30.0
    assert _client_kwargs(fake_openai, "DefaultHttpxClient")["timeout"].read == 30.0


def test_a_probe_failure_reports_the_exception_not_only_its_text(monkeypatch, fake_openai):
    """An admin pointing mooring at a gateway meets a timeout HERE first, and that is
    the failure whose message is routinely empty — one-argument friendly_error would
    hand them "OpenAI request failed:" with nothing after the colon."""
    import httpx

    monkeypatch.setenv("MOORING_OPENAI_API_KEY", "sk-local")
    provider = OpenAIProvider(model="gpt-4o")
    monkeypatch.setattr(
        provider, "_make_client", lambda **_: _fake_client(error=httpx.ReadTimeout(""))
    )
    detail = provider.status(force=True).detail
    assert "timed out" in detail.lower() and "ai.openai_timeout_sec" in detail
    provider.list_models(force=True)
    assert "timed out" in provider.models_error().lower()


# -- the httpx / httpx2 fallback actually degrades -----------------------------


def test_the_timeout_class_falls_back_to_whichever_http_library_exists(monkeypatch, fake_openai):
    """``mooring[openai]`` on openai 3.x installs httpx2 and NOT httpx, so a fallback
    that only knew httpx would ImportError on exactly the SDK it would be reached on."""
    from mooring.ai.openai_provider import build_client

    del fake_openai.Timeout
    monkeypatch.setitem(sys.modules, "httpx", None)  # makes `import httpx` raise
    monkeypatch.setitem(sys.modules, "httpx2", types.SimpleNamespace(Timeout=_StubTimeout))
    build_client("sk-x", timeout=300)
    timeout = _client_kwargs(fake_openai)["timeout"]
    assert isinstance(timeout, _StubTimeout)
    assert timeout.read == 300.0 and timeout.connect == 10.0


def test_with_no_http_library_at_all_the_client_still_builds(monkeypatch, fake_openai):
    """Degrade, don't crash: a bare float re-applies one number to all four legs (the
    original bug), but the leg that matters — read — is still right, and a long connect
    leg beats a provider that cannot be constructed at all."""
    from mooring.ai.openai_provider import build_client

    del fake_openai.Timeout
    monkeypatch.setitem(sys.modules, "httpx", None)
    monkeypatch.setitem(sys.modules, "httpx2", None)
    build_client("sk-x", timeout=300)
    assert _client_kwargs(fake_openai)["timeout"] == 300.0


# -- the hub's provider caches must include the timeout ------------------------
#
# The budget is baked into the SDK client at construction, and the hub caches the
# provider. Drop ai_openai_timeout_sec from either cache key and raising the knob in
# Settings becomes a silent no-op until the hub is restarted — "a new setting does not
# apply on one of several paths" is a bug shape this codebase has shipped before, so
# both keys are pinned rather than trusted.


def _openai_app_cfg(tmp_path, seconds, *, routing=None):
    from mooring import config
    from mooring.ai_config import AiConfig, RoutingConfig

    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path / "ws"))
    ai = AiConfig(
        provider="openai",
        openai_timeout_sec=seconds,
        routing=routing or RoutingConfig(),
    )
    return config.AppConfig(repos=(spec,), active_alias="ws", ai=ai)


def test_changing_the_timeout_rebuilds_the_general_provider(tmp_path):
    from mooring.hub.server import Hub

    hub = Hub(_openai_app_cfg(tmp_path, 300))
    first = hub._provider_for()
    assert first._timeout == 300.0
    assert hub._provider_for() is first  # unchanged config still reuses the client

    hub.app_cfg = _openai_app_cfg(tmp_path, 900)
    second = hub._provider_for()
    assert second is not first, "the provider cache key ignored ai_openai_timeout_sec"
    assert second._timeout == 900.0


def test_changing_the_timeout_rebuilds_the_trusted_provider(tmp_path, monkeypatch):
    from mooring.ai_config import RoutingConfig
    from mooring.hub.server import Hub

    monkeypatch.setattr(
        "mooring.ai.openai_provider.resolve_trusted_api_key", lambda **_: "approved-key"
    )
    routing = RoutingConfig(
        enabled=True,
        trusted_base_url="https://approved.example/v1",
        classifier_model="approved-inspector",
        coding_model="approved-coder",
        coding_models=("approved-coder",),
    )
    hub = Hub(_openai_app_cfg(tmp_path, 300, routing=routing))
    first = hub._trusted_provider_for()
    assert first._timeout == 300.0
    assert hub._trusted_provider_for() is first

    hub.app_cfg = _openai_app_cfg(tmp_path, 900, routing=routing)
    second = hub._trusted_provider_for()
    assert second is not first, "the trusted cache key ignored ai_openai_timeout_sec"
    assert second._timeout == 900.0
