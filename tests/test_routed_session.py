"""Trust-zone session routing, all offline with recording fakes."""

from __future__ import annotations

import hashlib
import time

import pytest

from mooring.ai.base import AIError
from mooring.ai.chat import ChatBroadcaster, ChatEvent
from mooring.ai.routed_session import GENERAL_ZONE, TRUSTED_ZONE, RoutedChatSession
from mooring.ai.trusted import BLOCK, GENERAL_OK, TRUSTED_REQUIRED, InspectionVerdict


class _Child(ChatBroadcaster):
    def __init__(self, name: str, *, ready: bool = True, auto_idle: bool = True):
        super().__init__()
        self.name = name
        self.sent: list[tuple[str, str]] = []
        self.auto_idle = auto_idle
        self.closed = False
        self.schema = ""
        if not ready:
            self._mark_starting()

    def send(self, text: str, live_schema_text: str = "") -> None:
        if not self.is_ready():
            raise AIError("not ready")
        self.sent.append((text, live_schema_text))
        if self.auto_idle:
            self._broadcast(ChatEvent("idle"))

    def set_initial_live_schema(self, text: str) -> None:
        self.schema = text

    def prepare_pii_model(self) -> None:
        return None

    def run_failure_report(self, failures):
        return failures

    def _known_text(self) -> str:
        return f"known by {self.name}"

    def close(self) -> None:
        self.closed = True
        super().close()


class _Inspector:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def inspect(self, text: str, *, purpose: str) -> InspectionVerdict:
        self.calls.append((text, purpose))
        if "BLOCK-ME" in text:
            return InspectionVerdict(
                BLOCK, ("prohibited_data",), ("policy_prohibited",)
            )
        if "CUSTOMER" in text:
            return InspectionVerdict(
                TRUSTED_REQUIRED, ("customer_data",), ("customer_context",)
            )
        return InspectionVerdict(
            GENERAL_OK, ("schema_or_metadata",), ("schema_only",)
        )


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def _session(tmp_path, *, zone=GENERAL_ZONE, child=None, trusted=None, inspector=None):
    source = "x = 1\n"
    (tmp_path / "nb.py").write_text(source, "utf-8")
    child = child or _Child("general")
    trusted = trusted or _Child("trusted")
    inspector = inspector or _Inspector()
    handoffs = []

    def factory(handoff):
        handoffs.append(handoff)
        return trusted

    session = RoutedChatSession(
        initial_session=child,
        initial_zone=zone,
        inspector=inspector,
        trusted_session_factory=factory,
        workspace=tmp_path,
        notebook_rel="nb.py",
        initial_source_digest=_digest(source),
        traceback_guard=False,
    )
    return session, child, trusted, inspector, handoffs


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_safe_turn_stays_on_general_and_is_inspected_once(tmp_path):
    session, general, trusted, inspector, _ = _session(tmp_path)
    session.send("show the schema")
    _wait_for(lambda: len(general.sent) == 1)

    assert session.zone == GENERAL_ZONE
    assert general.sent == [("show the schema", "")]
    assert trusted.sent == []
    assert inspector.calls[-1] == ("show the schema", "interactive_chat_turn")


def test_sensitive_turn_upgrades_ready_child_exactly_once_and_is_sticky(tmp_path):
    session, general, trusted, _inspector, handoffs = _session(tmp_path)
    events = session.subscribe()

    session.send("CUSTOMER account needs a chart")
    _wait_for(lambda: len(trusted.sent) == 1)
    assert session.zone == TRUSTED_ZONE
    assert general.sent == [] and general.closed is True
    assert trusted.sent == [("CUSTOMER account needs a chart", "")]
    assert handoffs == [""]
    routing = events.get(timeout=1)
    assert routing.kind == "routing" and routing.data["zone"] == TRUSTED_ZONE

    _wait_for(lambda: session._turn_idle.is_set())
    session.send("now change the title")
    _wait_for(lambda: len(trusted.sent) == 2)
    assert session.zone == TRUSTED_ZONE
    assert trusted.sent[-1][0] == "now change the title"


def test_trusted_zone_still_blocks_prohibited_later_turn(tmp_path):
    session, _general, trusted, inspector, _ = _session(tmp_path, zone=TRUSTED_ZONE)
    with pytest.raises(AIError, match="blocked"):
        session.send("BLOCK-ME")
    assert trusted.sent == []
    assert inspector.calls[-1][1] == "interactive_chat_turn"


def test_local_secret_block_runs_before_inspector(tmp_path):
    session, general, _trusted, inspector, _ = _session(tmp_path)
    with pytest.raises(AIError, match="credential or secret"):
        session.send("token = sk-abcdefghijklmnopqrstuvwxyz")
    assert inspector.calls == []
    assert general.sent == []


def test_any_notebook_change_moves_general_conversation_to_trusted(tmp_path):
    session, general, trusted, inspector, _ = _session(tmp_path)
    (tmp_path / "nb.py").write_text("x = 2\n", "utf-8")
    session.send("continue")
    _wait_for(lambda: len(trusted.sent) == 1)
    assert session.zone == TRUSTED_ZONE
    assert general.sent == []
    assert any(purpose == "changed_notebook_source" for _, purpose in inspector.calls)


def test_unready_upgrade_fails_without_falling_back_or_sending(tmp_path):
    unready = _Child("trusted", ready=False)
    session, general, _trusted, _inspector, _ = _session(tmp_path, trusted=unready)
    with pytest.raises(AIError, match="did not become ready"):
        session.send("CUSTOMER text")
    assert session.zone == GENERAL_ZONE
    assert general.sent == [] and unready.sent == [] and unready.closed is True


def test_second_send_is_rejected_until_child_reports_idle(tmp_path):
    busy = _Child("general", auto_idle=False)
    session, _general, _trusted, _inspector, _ = _session(tmp_path, child=busy)
    session.send("first")
    with pytest.raises(AIError, match="current assistant turn"):
        session.send("second")
    busy._broadcast(ChatEvent("idle"))
    _wait_for(lambda: session._turn_idle.is_set())
    session.send("second")
    assert [text for text, _ in busy.sent] == ["first", "second"]


def test_general_transcript_is_carried_only_upward(tmp_path):
    session, general, trusted, _inspector, handoffs = _session(tmp_path)
    general.auto_idle = False
    session.send("make a summary")
    general._broadcast(ChatEvent("message", {"text": "Here is the summary."}))
    general._broadcast(ChatEvent("idle"))
    _wait_for(lambda: session._turn_idle.is_set())

    session.send("CUSTOMER: change that")
    _wait_for(lambda: len(trusted.sent) == 1)
    assert "USER:\nmake a summary" in handoffs[0]
    assert "ASSISTANT:\nHere is the summary." in handoffs[0]
    assert "CUSTOMER: change that" not in handoffs[0]
