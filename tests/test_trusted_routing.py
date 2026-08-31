"""Hub-level recorder tests for the initial trusted-routing boundary."""

from __future__ import annotations

import hashlib
import types

import pytest

from mooring import config
from mooring.ai.base import AIError
from mooring.ai.chat import ChatBroadcaster
from mooring.ai.datadictionary import DictionaryIndex
from mooring.ai.routed_session import GENERAL_ZONE, TRUSTED_ZONE
from mooring.ai.trusted import BLOCK, GENERAL_OK, TRUSTED_REQUIRED, InspectionVerdict
from mooring.ai_config import AiConfig, RoutingConfig
from mooring.hub.server import Hub


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


def _hub(tmp_path, *, endpoint="https://approved.example/v1"):
    routing = RoutingConfig(
        enabled=True,
        trusted_base_url=endpoint,
        classifier_model="approved-inspector",
        coding_model="approved-coder",
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
