"""Hub-level recorder tests for the initial trusted-routing boundary."""

from __future__ import annotations

import hashlib
import types

import pytest
from starlette.testclient import TestClient

from mooring import config
from mooring.ai.base import AIError
from mooring.ai.chat import ChatBroadcaster
from mooring.ai.datadictionary import DictionaryIndex
from mooring.ai import openai_provider
from mooring.ai.routed_session import GENERAL_ZONE, TRUSTED_ZONE
from mooring.ai.trusted import BLOCK, GENERAL_OK, TRUSTED_REQUIRED, InspectionVerdict
from mooring.ai_config import AiConfig, RoutingConfig
from mooring.hub.server import Hub, create_app


@pytest.fixture(autouse=True)
def _managed_trusted_credential(monkeypatch):
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda: "managed-key")


class _Inspector:
    def __init__(self, verdict):
        self.verdict = verdict
        self.seen: list[tuple[str, str]] = []

    def inspect(self, text, *, purpose):
        self.seen.append((text, purpose))
        return self.verdict


class _Child(ChatBroadcaster):
    def send(self, text, live_schema_text=""):
        return None

    def set_initial_live_schema(self, text):
        return None

    def prepare_pii_model(self):
        return None

    def run_failure_report(self, failures):
        return failures


def _hub(
    tmp_path,
    *,
    endpoint="https://approved.example/v1",
    coding_models=(),
    profile_label="Firm approved AI",
):
    routing = RoutingConfig(
        enabled=True,
        trusted_base_url=endpoint,
        classifier_model="approved-inspector",
        coding_model="approved-coder",
        coding_models=coding_models,
        profile_label=profile_label,
    )
    repo = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path))
    return Hub(
        config.AppConfig(
            repos=(repo,),
            active_alias="ws",
            ai=AiConfig(provider="copilot", routing=routing),
        )
    )


def _bundle(raw="RAW-CUSTOMER-MARKER", general="GENERAL-SCRUBBED"):
    tail = (DictionaryIndex(), [], "", [], None, None)
    return types.SimpleNamespace(
        trusted=(raw, *tail),
        general=(general, *tail),
        source_digest=hashlib.sha256(b"source").digest(),
    )


def test_initial_context_is_inspected_before_any_general_session_is_constructed(
    tmp_path, monkeypatch
):
    hub = _hub(tmp_path)
    inspector = _Inspector(
        InspectionVerdict(GENERAL_OK, ("schema_or_metadata",), ("schema_only",))
    )
    order = []

    def inspect(text, *, purpose):
        order.append(("inspect", text))
        return inspector.verdict

    inspector.inspect = inspect

    def make(context, _workspace, _notebook, **kwargs):
        order.append(("coder", kwargs["routing_zone"], context))
        return _Child()

    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: inspector)
    monkeypatch.setattr(hub, "_make_chat_session", make)
    session = hub._make_routed_chat_session(
        _bundle(), tmp_path, "nb.py", "", model="best-general"
    )

    assert session.zone == GENERAL_ZONE
    assert order == [
        ("inspect", "RAW-CUSTOMER-MARKER"),
        ("coder", GENERAL_ZONE, "GENERAL-SCRUBBED"),
    ]
    assert "RAW-CUSTOMER-MARKER" not in order[1][2]


def test_sensitive_or_uncertain_initial_context_constructs_only_trusted_coder(
    tmp_path, monkeypatch
):
    hub = _hub(tmp_path)
    inspector = _Inspector(
        InspectionVerdict(
            TRUSTED_REQUIRED, ("customer_data",), ("customer_context",)
        )
    )
    calls = []
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: inspector)
    monkeypatch.setattr(
        hub,
        "_make_chat_session",
        lambda context, _workspace, _notebook, **kwargs: (
            calls.append((kwargs["routing_zone"], context)) or _Child()
        ),
    )

    session = hub._make_routed_chat_session(_bundle(), tmp_path, "nb.py", "")

    assert session.zone == TRUSTED_ZONE
    assert calls == [(TRUSTED_ZONE, "RAW-CUSTOMER-MARKER")]
    assert inspector.seen == [("RAW-CUSTOMER-MARKER", "initial_chat_context")]


def test_block_initial_context_constructs_no_coding_provider(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    inspector = _Inspector(
        InspectionVerdict(BLOCK, ("prohibited_data",), ("policy_prohibited",))
    )
    calls = []
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: inspector)
    monkeypatch.setattr(hub, "_make_chat_session", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(AIError, match="blocked"):
        hub._make_routed_chat_session(_bundle(), tmp_path, "nb.py", "")
    assert calls == []


def test_routed_general_disables_investigate_and_guards_proposal_results(
    tmp_path, monkeypatch
):
    hub = _hub(tmp_path)
    inspector = _Inspector(
        InspectionVerdict(
            TRUSTED_REQUIRED, ("customer_data",), ("customer_context",)
        )
    )
    opened = []

    class Provider:
        def open_chat(self, **kwargs):
            opened.append(kwargs)
            return _Child()

    monkeypatch.setattr(hub, "_provider_for", lambda: Provider())
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: inspector)

    hub._make_chat_session(
        "scrubbed context",
        tmp_path,
        "nb.py",
        routing_zone=GENERAL_ZONE,
    )

    assert len(opened) == 1
    assert opened[0]["run_investigation"] is None
    guard = opened[0]["output_guard"]
    assert callable(guard)
    assert guard("proposal validator output") is False
    assert inspector.seen[-1] == ("proposal validator output", "trusted_tool_output")


@pytest.mark.parametrize(
    "endpoint",
    ["", "http://approved.example/v1", "https://user:pass@approved.example/v1", "https://x/v1?q=1"],
)
def test_trusted_profile_requires_an_explicit_clean_https_endpoint(tmp_path, endpoint):
    hub = _hub(tmp_path, endpoint=endpoint)
    with pytest.raises(AIError, match="managed|HTTPS"):
        hub._trusted_provider_for()


def test_trusted_routing_metadata_contains_only_exact_safe_profile(tmp_path):
    hub = _hub(
        tmp_path,
        coding_models=("approved-coder", "approved-coder-fast"),
    )

    metadata = hub._trusted_routing_metadata()

    assert metadata == {
        "enabled": True,
        "profile_label": "Firm approved AI",
        "trusted_models": [
            {"id": "approved-coder", "name": "approved-coder"},
            {"id": "approved-coder-fast", "name": "approved-coder-fast"},
        ],
        "default_trusted_model": "approved-coder",
    }
    rendered = repr(metadata)
    assert "approved.example" not in rendered
    assert "approved-inspector" not in rendered
    assert "API_KEY" not in rendered


def test_models_endpoint_exposes_only_validated_safe_routing_metadata(tmp_path, monkeypatch):
    hub = _hub(
        tmp_path,
        coding_models=("approved-coder", "approved-coder-fast"),
    )

    class GeneralProvider:
        name = "copilot"

        def list_models(self):
            return []

    monkeypatch.setattr(hub, "_provider_for", lambda: GeneralProvider())

    with TestClient(create_app(hub)) as client:
        routing = client.get("/api/ai/models").json()["routing"]

    assert routing == hub._trusted_routing_metadata()
    assert "approved.example" not in repr(routing)
    assert "approved-inspector" not in repr(routing)


def test_models_endpoint_offers_no_choices_for_unavailable_profile(tmp_path, monkeypatch):
    hub = _hub(
        tmp_path,
        coding_models=("approved-coder", "approved-coder-fast"),
    )

    class GeneralProvider:
        def list_models(self):
            return []

    monkeypatch.setattr(hub, "_provider_for", lambda: GeneralProvider())
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda: "")

    with TestClient(create_app(hub)) as client:
        routing = client.get("/api/ai/models").json()["routing"]

    assert routing["enabled"] is True
    assert routing["trusted_models"] == []
    assert routing["default_trusted_model"] == ""
    assert "approved-coder" not in repr(routing)


def test_manually_incoherent_trusted_allowlist_fails_closed(tmp_path):
    hub = _hub(tmp_path, coding_models=("other-coder",))

    with pytest.raises(AIError, match="does not include the default"):
        hub._trusted_routing_metadata()


def test_unapproved_browser_model_is_rejected_before_context_or_inspector(
    tmp_path, monkeypatch
):
    hub = _hub(
        tmp_path,
        coding_models=("approved-coder", "approved-coder-fast"),
    )
    touched = []
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda: "")
    monkeypatch.setattr(hub, "_build_chat_context", lambda *a, **k: touched.append("context"))
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: touched.append("inspector"))

    with TestClient(create_app(hub)) as client:
        response = client.post(
            "/api/ai/chat/open",
            json={
                "notebook": "nb.py",
                "trusted_model": "browser-injected-model",
                "routing_preference": "auto",
            },
        )

    assert response.status_code == 400
    assert "not approved" in response.json()["error"]
    assert touched == []


def test_incomplete_managed_profile_is_rejected_before_context(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    touched = []
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda: "")
    monkeypatch.setattr(hub, "_build_chat_context", lambda *a, **k: touched.append("context"))
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: touched.append("inspector"))

    with TestClient(create_app(hub)) as client:
        response = client.post(
            "/api/ai/chat/open",
            json={"notebook": "nb.py", "routing_preference": "auto"},
        )

    assert response.status_code == 502
    assert "credential is unavailable" in response.json()["error"]
    assert touched == []


def test_trusted_options_are_rejected_when_routing_is_disabled(tmp_path, monkeypatch):
    repo = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(tmp_path))
    hub = Hub(
        config.AppConfig(
            repos=(repo,),
            active_alias="ws",
            ai=AiConfig(provider="copilot"),
        )
    )
    touched = []
    monkeypatch.setattr(hub, "_build_chat_context", lambda *a, **k: touched.append("context"))

    with TestClient(create_app(hub)) as client:
        response = client.post(
            "/api/ai/chat/open",
            json={
                "notebook": "nb.py",
                "trusted_model": "stale-approved-choice",
                "routing_preference": "auto",
            },
        )

    assert response.status_code == 400
    assert "not enabled" in response.json()["error"]
    assert touched == []


@pytest.mark.parametrize("preference", ["general", "off", "TRUSTED_ALWAYS"])
def test_invalid_routing_preference_is_rejected_before_context(
    tmp_path, monkeypatch, preference
):
    hub = _hub(tmp_path)
    touched = []
    monkeypatch.setattr(hub, "_build_chat_context", lambda *a, **k: touched.append("context"))

    with TestClient(create_app(hub)) as client:
        response = client.post(
            "/api/ai/chat/open",
            json={"notebook": "nb.py", "routing_preference": preference},
        )

    assert response.status_code == 400
    assert touched == []


def test_trusted_preference_forces_only_an_upward_route_and_keeps_block_check(
    tmp_path, monkeypatch
):
    hub = _hub(
        tmp_path,
        coding_models=("approved-coder", "approved-coder-fast"),
    )
    inspector = _Inspector(
        InspectionVerdict(GENERAL_OK, ("schema_or_metadata",), ("schema_only",))
    )
    calls = []
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: inspector)
    monkeypatch.setattr(
        hub,
        "_make_chat_session",
        lambda context, _workspace, _notebook, **kwargs: (
            calls.append((kwargs["routing_zone"], kwargs["trusted_model"], context)) or _Child()
        ),
    )

    session = hub._make_routed_chat_session(
        _bundle(),
        tmp_path,
        "nb.py",
        "",
        trusted_model="approved-coder-fast",
        routing_preference="trusted",
    )

    assert session.zone == TRUSTED_ZONE
    assert calls == [
        (TRUSTED_ZONE, "approved-coder-fast", "RAW-CUSTOMER-MARKER")
    ]
    assert inspector.seen == [("RAW-CUSTOMER-MARKER", "initial_chat_context")]

    blocked = _Inspector(
        InspectionVerdict(BLOCK, ("prohibited_data",), ("policy_prohibited",))
    )
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: blocked)
    with pytest.raises(AIError, match="blocked"):
        hub._make_routed_chat_session(
            _bundle(),
            tmp_path,
            "nb.py",
            "",
            trusted_model="approved-coder-fast",
            routing_preference="trusted",
        )
    assert len(calls) == 1


def test_selected_trusted_model_is_retained_on_later_upgrade(tmp_path, monkeypatch):
    hub = _hub(
        tmp_path,
        coding_models=("approved-coder", "approved-coder-fast"),
    )
    (tmp_path / "nb.py").write_text("source", "utf-8")
    verdicts = iter(
        [
            InspectionVerdict(GENERAL_OK, ("schema_or_metadata",), ("schema_only",)),
            InspectionVerdict(
                TRUSTED_REQUIRED, ("customer_data",), ("customer_context",)
            ),
            InspectionVerdict(
                TRUSTED_REQUIRED, ("customer_data",), ("customer_context",)
            ),
        ]
    )

    class Inspector:
        def inspect(self, _text, *, purpose):
            return next(verdicts)

    calls = []
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: Inspector())
    monkeypatch.setattr(hub, "_build_chat_context", lambda *a, **k: _bundle())
    monkeypatch.setattr(
        hub,
        "_make_chat_session",
        lambda context, _workspace, _notebook, **kwargs: (
            calls.append((kwargs["routing_zone"], kwargs["trusted_model"], context)) or _Child()
        ),
    )

    session = hub._make_routed_chat_session(
        _bundle(),
        tmp_path,
        "nb.py",
        "",
        trusted_model="approved-coder-fast",
    )
    session.send("work with this customer record")

    assert session.zone == TRUSTED_ZONE
    assert [(zone, selected) for zone, selected, _context in calls] == [
        (GENERAL_ZONE, "approved-coder-fast"),
        (TRUSTED_ZONE, "approved-coder-fast"),
    ]


def test_shared_trusted_provider_uses_per_session_model_override(tmp_path, monkeypatch):
    hub = _hub(
        tmp_path,
        coding_models=("approved-coder", "approved-coder-fast"),
    )
    opened = []

    class Provider:
        configured_model = "approved-coder"

        def open_chat(self, **kwargs):
            opened.append(kwargs)
            return _Child()

    provider = Provider()
    inspector = _Inspector(
        InspectionVerdict(GENERAL_OK, ("schema_or_metadata",), ("schema_only",))
    )
    monkeypatch.setattr(hub, "_trusted_provider_for", lambda: provider)
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: inspector)

    hub._make_chat_session(
        "customer context",
        tmp_path,
        "nb.py",
        routing_zone=TRUSTED_ZONE,
        trusted_model="approved-coder",
    )
    hub._make_chat_session(
        "other customer context",
        tmp_path,
        "nb.py",
        routing_zone=TRUSTED_ZONE,
        trusted_model="approved-coder-fast",
    )

    assert [call["model"] for call in opened] == [
        "approved-coder",
        "approved-coder-fast",
    ]
    assert provider.configured_model == "approved-coder"


def test_trusted_investigator_uses_the_validated_per_session_model(tmp_path, monkeypatch):
    hub = _hub(
        tmp_path,
        coding_models=("approved-coder", "approved-coder-fast"),
    )
    opened = []

    class Provider:
        def open_chat(self, **kwargs):
            opened.append(kwargs)
            return _Child()

    inspector = _Inspector(
        InspectionVerdict(GENERAL_OK, ("schema_or_metadata",), ("schema_only",))
    )
    monkeypatch.setattr(hub, "_trusted_provider_for", lambda: Provider())
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: inspector)
    ctx = ("customer context", DictionaryIndex(), [], "", [], None, None)

    hub._make_investigator_session(
        ctx,
        tmp_path,
        "nb.py",
        routing_zone=TRUSTED_ZONE,
        trusted_model="approved-coder-fast",
    )

    assert opened[0]["model"] == "approved-coder-fast"
    assert opened[0]["read_only"] is True
    assert inspector.seen == [
        ("customer context", "trusted_investigation_context")
    ]


def test_routed_open_returns_approved_route_without_legacy_guard(tmp_path, monkeypatch):
    hub = _hub(
        tmp_path,
        coding_models=("approved-coder", "approved-coder-fast"),
    )
    child = _Child()
    child.zone = TRUSTED_ZONE
    child.profile_label = "Firm approved AI"
    child.trusted_model = "approved-coder-fast"
    monkeypatch.setattr(hub, "_build_chat_context", lambda *a, **k: _bundle())
    monkeypatch.setattr(hub, "_make_routed_chat_session", lambda *a, **k: child)

    with TestClient(create_app(hub)) as client:
        response = client.post(
            "/api/ai/chat/open",
            json={
                "notebook": "nb.py",
                "trusted_model": "approved-coder-fast",
                "routing_preference": "trusted",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["guard"] is None
    assert body["route"] == {
        "zone": TRUSTED_ZONE,
        "profile_label": "Firm approved AI",
        "model": "approved-coder-fast",
    }
