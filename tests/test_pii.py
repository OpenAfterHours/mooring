"""The best-effort structured-PII scanner and its outbound-egress wiring.

Mirrors test_secrets.py: high precision, value-free findings. The fixtures use
synthetic-but-valid identifiers (a Luhn-valid card NOT on any test-PAN list, a
mod-97-valid IBAN, a mod-11-valid NHS number) and assert the matched VALUE never
appears in a finding, an SSE event, or the CLI — the value-free contract.
"""

from __future__ import annotations

import json
import queue

import pytest

from mooring import config
from mooring.ai import context as ctxmod
from mooring.ai import pii
from mooring.ai.chat import StubChatSession

# Synthetic, Luhn/mod-checksum-valid identifiers used as positive fixtures. The
# card is deliberately NOT one of the canonical industry test PANs.
VALID_CARD = "4012888888881881"
VALID_IBAN = "GB82WEST12345698765432"
VALID_NHS = "9434765919"


def kinds(text: str) -> set[str]:
    return {f.kind for f in pii.scan(text)}


@pytest.fixture
def clean_config(tmp_path, monkeypatch):
    """Resolve config against an empty user dir so a developer's real config.toml
    (e.g. ai.pii enabled) can't make the default-config assertions flaky."""
    from mooring import paths

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "cfg")


# -- detection: checksum-validated kinds ---------------------------------------


def test_detects_valid_card_contiguous_and_spaced():
    assert pii.CARD in kinds(f"charge {VALID_CARD} now")
    assert pii.CARD in kinds("card 4012 8888 8888 1881")


def test_detects_iban_and_nhs():
    assert pii.IBAN in kinds(f"iban {VALID_IBAN}")
    assert pii.NHS in kinds(f"nhs {VALID_NHS}")
    assert pii.NHS in kinds("nhs 943 476 5919")


# -- precision: placeholders and non-checksum runs are NOT cards ---------------


def test_canonical_test_pans_are_not_flagged():
    for pan in ("4111111111111111", "4242424242424242", "5555555555554444", "378282246310005"):
        assert pii.CARD not in kinds(pan), pan


def test_placeholders_and_non_luhn_are_not_cards():
    assert kinds("0000000000000000") == set()  # all-identical, and leading 0
    assert kinds("1111111111111111") == set()  # leading 1, not a card network
    assert kinds("id=1234567890123456") == set()  # 16 digits, not Luhn
    assert pii.IBAN not in kinds("GB00WEST12345698765432")  # bad check digits
    assert pii.NHS not in kinds("9434765910")  # bad mod-11 check digit


# -- shape-anchored kinds and their exclusions ---------------------------------


def test_email_detected_but_not_asset_shapes():
    assert pii.EMAIL in kinds("contact alice@bank.com")
    assert kinds("sprite arr@2x.png") == set()
    assert kinds("logo@3x.jpg") == set()


def test_nino_detected_but_not_bad_prefix():
    assert pii.NINO in kinds("ni AB123456C")
    assert pii.NINO not in kinds("GB123456C")  # GB is a never-issued prefix


# -- out-of-scope: locked, intentional gaps (regression) -----------------------


@pytest.mark.parametrize(
    "text",
    [
        "John Smith",  # a person name (needs NER, Phase 2)
        "sort code 12-34-56",  # UK sort code (no checksum)
        "ssn 123-45-6789",  # US SSN
        "phone +447911123456",  # phone number
        "pinned numpy 1.2.3.4 and 127.0.0.1",  # version string / IP (IPv4 dropped)
        "loan_status: current servicing status; FK -> dim_status.code",  # plain schema text
    ],
)
def test_out_of_scope_produces_no_findings(text):
    assert pii.scan(text) == []


# -- value-free contract -------------------------------------------------------


def test_findings_never_carry_the_value():
    text = f"line one\npan {VALID_CARD}\nmail bob@bank.com"
    findings = pii.scan(text)
    blob = repr(findings)
    assert VALID_CARD not in blob and "bob@bank.com" not in blob
    assert {(f.line, f.kind) for f in findings} == {(2, pii.CARD), (3, pii.EMAIL)}


def test_suppress_marker_skips_the_line():
    assert kinds(f"pan {VALID_CARD}  # mooring: pii-ok") == set()


# -- scrub_columns: a PII value promoted to a column name ----------------------


def test_scrub_columns_withholds_only_checksum_named_columns():
    # A pivot on a PII key promotes a VALUE to a column name. Only checksum-validated
    # kinds are confident enough to withhold; a shape-only email/NINO header is KEPT
    # (silently dropping a legit column would hand the model an incomplete schema).
    cols = (
        ("id", "Int64"),
        (VALID_CARD, "Float64"),  # checksum-valid card -> withheld
        ("support@acme.com", "Float64"),  # email shape -> kept (could be a real column)
        ("AB123456C", "Float64"),  # NINO shape -> kept (could be a product code)
        ("amt", "Float64"),
    )
    kept, findings = pii.scrub_columns(cols)
    assert [c[0] for c in kept] == ["id", "support@acme.com", "AB123456C", "amt"]
    assert {f.kind for f in findings} == {pii.CARD}
    assert VALID_CARD not in repr(findings)


# -- guard_prompt: the shared valve and its fail mode --------------------------


def test_guard_prompt_modes():
    assert pii.guard_prompt(f"x {VALID_CARD}", enabled=False, block=True) == (False, [], "")
    hold, findings, err = pii.guard_prompt(f"x {VALID_CARD}", enabled=True, block=True)
    assert hold is True and findings and err == ""
    hold, findings, err = pii.guard_prompt(f"x {VALID_CARD}", enabled=True, block=False)
    assert hold is False and findings and err == ""  # warn-only: forward, but flagged
    assert pii.guard_prompt("nothing here", enabled=True, block=True) == (False, [], "")


def test_guard_prompt_fails_open_loud_on_scan_error(monkeypatch):
    def boom(_text):
        raise RuntimeError("scanner blew up")

    monkeypatch.setattr(pii, "scan", boom)
    hold, findings, err = pii.guard_prompt("anything", enabled=True, block=True)
    # the STRUCTURED scan failed -> the prompt went truly unchecked (fail OPEN, reported)
    assert hold is False and findings == [] and err == "structured"


# -- Phase 2: NER name detection wired into the prose scanners -----------------


def test_scan_prose_includes_names_when_enabled(monkeypatch):
    from mooring.ai import ner

    monkeypatch.setattr(ner, "scan_names", lambda text, **kw: [pii.Finding(1, ner.NAME)])
    out = pii.scan_prose("sum col_1 where name = Jon Harrison", names=True)
    assert ner.NAME in {f.kind for f in out}


def test_scan_prose_is_silent_when_ner_unavailable(monkeypatch):
    # Advisory path (source banner / CLI): a missing extra degrades to structured-only.
    from mooring.ai import ner

    def boom(_text, **_kw):
        raise ner.NerUnavailable("no extra")

    monkeypatch.setattr(ner, "scan_names", boom)
    assert pii.scan_prose("contact Jon Harrison", names=True) == []  # must not raise


def test_guard_prompt_holds_on_name(monkeypatch):
    from mooring.ai import ner

    monkeypatch.setattr(ner, "scan_names", lambda text, **kw: [pii.Finding(1, ner.NAME)])
    hold, findings, err = pii.guard_prompt(
        "sum for Jon Harrison", enabled=True, block=True, names=True
    )
    assert hold is True and err == ""
    assert ner.NAME in {f.kind for f in findings}


def test_guard_prompt_name_pass_unavailable_is_loud(monkeypatch):
    # Enforcement path: detect_names configured but the name pass raised must FAIL
    # OPEN (don't block on nothing) yet report scan_error="names" — the STRUCTURED
    # scan still ran, so the caller can say so rather than claim "unchecked".
    from mooring.ai import ner

    def boom(_text, **_kw):
        raise ner.NerUnavailable("no extra")

    monkeypatch.setattr(ner, "scan_names", boom)
    hold, findings, err = pii.guard_prompt(
        "sum for Jon Harrison", enabled=True, block=True, names=True
    )
    assert hold is False and findings == [] and err == "names"


def test_name_prompt_held_then_confirmed_value_free(monkeypatch):
    from mooring.ai import ner

    monkeypatch.setattr(ner, "available", lambda backend="gliner": True)
    monkeypatch.setattr(ner, "is_cached", lambda mid=None: True)  # model ready -> name pass runs
    monkeypatch.setattr(ner, "scan_names", lambda text, **kw: [pii.Finding(1, ner.NAME)])
    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=True)
    q = sess.subscribe()
    sess.send("can you sum col_1 where the name equals Jon Harrison")
    held = _drain(q)
    assert [e.kind for e in held] == ["pii"]  # nothing forwarded — held
    ev = held[0].data
    assert ev["token"] and ev["findings"][0]["kind"] == ner.NAME
    assert "Jon Harrison" not in json.dumps(ev)  # value-free over the wire

    sess.send_confirmed(ev["token"])
    assert "idle" in [e.kind for e in _drain(q)]  # forwarded after confirm


def test_structured_hold_survives_name_pass_failure(monkeypatch):
    # detect_names ON but the NER backend missing must NOT bypass the structured
    # guard: a card still HOLDS (not forwarded unchecked), with the name-pass failure
    # subordinate to the real finding.
    from mooring.ai import ner

    def boom(_text, **_kw):
        raise ner.NerUnavailable("no extra")

    monkeypatch.setattr(ner, "available", lambda backend="gliner": True)
    monkeypatch.setattr(
        ner, "is_cached", lambda mid=None: True
    )  # ready -> name pass runs (and fails)
    monkeypatch.setattr(ner, "scan_names", boom)
    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=True)
    q = sess.subscribe()
    sess.send(f"charge {VALID_CARD} now")
    evs = _drain(q)
    assert [e.kind for e in evs] == ["pii"]  # held — nothing forwarded
    ev = evs[0].data
    assert ev.get("token") and ev["findings"][0]["kind"] == pii.CARD
    assert "idle" not in [e.kind for e in evs]


def test_scan_error_alone_forwards_loud(monkeypatch):
    # No actionable finding + a failed name pass: forward, but flag scan_error loudly.
    from mooring.ai import ner

    def boom(_text, **_kw):
        raise ner.NerUnavailable("no extra")

    monkeypatch.setattr(ner, "available", lambda backend="gliner": True)
    monkeypatch.setattr(
        ner, "is_cached", lambda mid=None: True
    )  # ready -> name pass runs (and fails)
    monkeypatch.setattr(ner, "scan_names", boom)
    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=True)
    q = sess.subscribe()
    sess.send("nothing sensitive here")
    evs = _drain(q)
    assert any(e.kind == "pii" and e.data.get("scan_error") for e in evs)
    assert "idle" in [e.kind for e in evs]  # forwarded (fail open)


# -- Phase 2: NER model prepare (background download with progress) ------------


def _await_ner(q, *, until):
    states = []
    for _ in range(20):
        ev = q.get(timeout=2)
        if ev.kind != "ner":
            continue
        states.append((ev.data.get("state"), ev.data.get("pct")))
        if ev.data.get("state") == until:
            break
    return states


def test_prepare_pii_model_streams_progress_then_ready(monkeypatch):
    from mooring.ai import ner

    monkeypatch.setattr(ner, "available", lambda backend="gliner": True)
    monkeypatch.setattr(ner, "is_cached", lambda mid=None: False)
    # Pin GLiNER: a locally-installed spaCy extra would otherwise make
    # resolve_backend("auto") -> "spacy" and report the model already ready,
    # bypassing the is_cached download path this test exercises.
    monkeypatch.setattr(ner, "resolve_backend", lambda b=None: "gliner")

    def fake_download(mid=None, on_progress=None):
        assert on_progress is not None
        on_progress(50, 100)
        on_progress(100, 100)

    monkeypatch.setattr(ner, "download_model", fake_download)
    monkeypatch.setattr(ner, "load_model", lambda mid=None: object())

    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=True)
    q = sess.subscribe()
    sess.prepare_pii_model()
    states = _await_ner(q, until="ready")
    assert ("downloading", None) in states  # initial, indeterminate
    assert ("downloading", 50) in states and ("downloading", 100) in states
    assert states[-1] == ("ready", None)
    assert sess.ner_status == {"state": "ready"}  # replayable to a late subscriber


def test_prepare_pii_model_reports_error(monkeypatch):
    from mooring.ai import ner

    monkeypatch.setattr(ner, "available", lambda backend="gliner": True)
    monkeypatch.setattr(ner, "is_cached", lambda mid=None: False)
    # Pin GLiNER so a locally-installed spaCy extra can't short-circuit the
    # download path (resolve_backend("auto") -> "spacy" would report ready).
    monkeypatch.setattr(ner, "resolve_backend", lambda b=None: "gliner")

    def boom(mid=None, on_progress=None):
        raise ner.NerUnavailable("network down")

    monkeypatch.setattr(ner, "download_model", boom)
    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=True)
    q = sess.subscribe()
    sess.prepare_pii_model()
    states = _await_ner(q, until="error")
    assert states[-1] == ("error", None)


def test_prepare_pii_model_silent_when_already_cached(monkeypatch):
    from mooring.ai import ner

    monkeypatch.setattr(ner, "available", lambda backend="gliner": True)
    monkeypatch.setattr(ner, "is_cached", lambda mid=None: True)
    monkeypatch.setattr(ner, "load_model", lambda mid=None: object())
    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=True)
    q = sess.subscribe()
    sess.prepare_pii_model()
    assert _drain(q) == []  # cached -> warm in the background, no download UI


def test_prepare_pii_model_noop_when_names_off(monkeypatch):
    from mooring.ai import ner

    monkeypatch.setattr(ner, "available", lambda backend="gliner": True)
    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=False)
    q = sess.subscribe()
    sess.prepare_pii_model()
    assert _drain(q) == []


def test_pii_gate_skips_name_pass_until_model_ready(monkeypatch):
    from mooring.ai import ner

    monkeypatch.setattr(ner, "available", lambda backend="gliner": True)
    monkeypatch.setattr(ner, "is_cached", lambda mid=None: False)  # still downloading
    # Pin GLiNER so the "not ready" state holds even when a spaCy extra is
    # installed locally (else resolve_backend("auto") -> "spacy" reports ready
    # and the name pass would run instead of being skipped).
    monkeypatch.setattr(ner, "resolve_backend", lambda b=None: "gliner")

    def explode(*_a, **_k):
        raise AssertionError("scan_names must not run before the model is ready")

    monkeypatch.setattr(ner, "scan_names", explode)
    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=True)
    q = sess.subscribe()
    sess.send("contact Jon Harrison")
    kinds = [e.kind for e in _drain(q)]
    assert "pii" not in kinds  # not held, no scan_error — just structurally scanned
    assert "idle" in kinds  # forwarded


def test_pii_gate_runs_name_pass_once_ready(monkeypatch):
    from mooring.ai import ner

    monkeypatch.setattr(ner, "available", lambda backend="gliner": True)
    monkeypatch.setattr(ner, "is_cached", lambda mid=None: True)
    monkeypatch.setattr(ner, "scan_names", lambda text, **kw: [pii.Finding(1, ner.NAME)])
    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=True)
    q = sess.subscribe()
    sess.send("contact Jon Harrison")
    evs = _drain(q)
    assert [e.kind for e in evs] == ["pii"]  # held on the name once the model is ready
    assert evs[0].data["findings"][0]["kind"] == ner.NAME


def test_pii_gate_skips_name_pass_when_backend_unavailable(monkeypatch):
    # detect_names ON but the NER extra/model is absent: the name pass is skipped
    # gracefully (the structured scan still runs), so NO "sent unchecked" scan_error
    # fires on every message. The topbar "PII-partial" badge carries the signal.
    from mooring.ai import ner

    monkeypatch.setattr(ner, "available", lambda backend="gliner": False)

    def explode(*_a, **_k):
        raise AssertionError("scan_names must not run when the backend is unavailable")

    monkeypatch.setattr(ner, "scan_names", explode)
    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=True)
    q = sess.subscribe()
    sess.send("contact Jon Harrison")
    kinds = [e.kind for e in _drain(q)]
    assert "pii" not in kinds  # no scan_error, no hold — structured scan ran clean
    assert "idle" in kinds  # forwarded


def test_pii_gate_skips_name_pass_for_unavailable_spacy(monkeypatch):
    # spaCy backend selected but not installed: previously the name pass ran and
    # raised on EVERY message (the contradiction); now it is skipped like any other
    # unavailable backend — no scan_error.
    from mooring.ai import ner

    monkeypatch.setattr(ner, "available", lambda backend="gliner": backend != "spacy")

    def explode(*_a, **_k):
        raise AssertionError("scan_names must not run when spaCy is unavailable")

    monkeypatch.setattr(ner, "scan_names", explode)
    sess = StubChatSession(
        pii_enabled=True, pii_block=True, pii_names=True, pii_name_backend="spacy"
    )
    q = sess.subscribe()
    sess.send("contact Jon Harrison")
    kinds = [e.kind for e in _drain(q)]
    assert "pii" not in kinds
    assert "idle" in kinds


def test_pii_gate_holds_card_even_when_name_backend_unavailable(monkeypatch):
    # Skipping the unavailable name pass must NOT weaken the always-on structured
    # guard: a card still HOLDS (never forwarded), and the name pass never runs.
    from mooring.ai import ner

    monkeypatch.setattr(ner, "available", lambda backend="gliner": False)

    def explode(*_a, **_k):
        raise AssertionError("scan_names must not run when the backend is unavailable")

    monkeypatch.setattr(ner, "scan_names", explode)
    sess = StubChatSession(pii_enabled=True, pii_block=True, pii_names=True)
    q = sess.subscribe()
    sess.send(f"charge {VALID_CARD} now")
    evs = _drain(q)
    assert [e.kind for e in evs] == ["pii"]  # held — the structured guard still fires
    ev = evs[0].data
    assert ev.get("token") and ev["findings"][0]["kind"] == pii.CARD
    assert "idle" not in [e.kind for e in evs]  # NOT forwarded


# -- Channel A: prompt hold-and-confirm on a StubChatSession -------------------


def _drain(q) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_prompt_held_then_confirmed():
    sess = StubChatSession(pii_enabled=True, pii_block=True)
    q = sess.subscribe()
    sess.send(f"why does {VALID_CARD} fail validation?")
    held = _drain(q)
    assert [e.kind for e in held] == ["pii"]  # nothing forwarded — held
    ev = held[0].data
    assert ev["token"] and ev["findings"][0]["kind"] == pii.CARD
    assert VALID_CARD not in json.dumps(ev)  # value-free over the wire

    sess.send_confirmed(ev["token"])
    after = [e.kind for e in _drain(q)]
    assert "message" in after and "idle" in after  # forwarded exactly now

    # the token is single-use: a replay raises (and forwards nothing)
    from mooring.ai.base import AIError

    with pytest.raises(AIError):
        sess.send_confirmed(ev["token"])
    assert _drain(q) == []


def test_prompt_warn_only_when_block_disabled():
    sess = StubChatSession(pii_enabled=True, pii_block=False)
    q = sess.subscribe()
    sess.send(f"card {VALID_CARD}")
    evs = _drain(q)
    assert evs[0].kind == "pii" and "token" not in evs[0].data  # advisory only
    assert "idle" in [e.kind for e in evs]  # was forwarded


def test_send_confirmed_unknown_token_raises():
    # Both session classes must react the same way to a replayed/expired token: raise
    # (the hub maps it to a visible error) — never silently report success.
    from mooring.ai.base import AIError

    sess = StubChatSession(pii_enabled=True, pii_block=True)
    with pytest.raises(AIError):
        sess.send_confirmed("never-issued")


def test_close_clears_held_prompt_text():
    sess = StubChatSession(pii_enabled=True, pii_block=True)
    q = sess.subscribe()
    sess.send(f"card {VALID_CARD}")
    _drain(q)
    assert sess._pending  # the flagged plaintext is held pending confirmation
    sess.close()
    assert sess._pending == {}  # and never lingers past the session


def test_prompt_guard_off_is_passthrough():
    sess = StubChatSession(pii_enabled=False)
    q = sess.subscribe()
    sess.send(f"card {VALID_CARD}")
    kinds_seen = [e.kind for e in _drain(q)]
    assert "pii" not in kinds_seen and "idle" in kinds_seen


def test_prompt_fails_open_loud(monkeypatch):
    def boom(_text):
        raise RuntimeError("x")

    monkeypatch.setattr(pii, "scan", boom)
    sess = StubChatSession(pii_enabled=True, pii_block=True)
    q = sess.subscribe()
    sess.send(f"card {VALID_CARD}")
    evs = _drain(q)
    assert any(e.kind == "pii" and e.data.get("scan_error") for e in evs)  # loud
    assert "idle" in [e.kind for e in evs]  # but forwarded (fail open)


# -- traceback guard: sanitise-and-hold on a StubChatSession --------------------
# The combined hold contract: a traceback-bearing prompt is sanitised, PII-scanned
# (the SANITISED text), and held under ONE token that stores ONLY the sanitised
# rewrite — so the shared send_confirmed path cannot forward the raw paste.

TB_SECRET = "SECRET_VALUE_DO_NOT_LEAK"

_TB_PASTE = (
    "why does this fail?\n"
    "\n"
    "Traceback (most recent call last):\n"
    '  File "C:\\elsewhere\\pipeline.py", line 7, in run\n'
    "    value = row[key]\n"
    f"KeyError: '{TB_SECRET}'"
)


def test_traceback_prompt_is_sanitised_and_held():
    sess = StubChatSession(traceback_guard=True)
    q = sess.subscribe()
    sess.send(_TB_PASTE)
    events = _drain(q)
    assert [e.kind for e in events] == ["traceback"]  # held — nothing else fired
    assert sess.last_sent == ""  # nothing forwarded
    data = events[0].data
    assert data["token"]
    assert TB_SECRET not in json.dumps(data)  # the event is value-free end to end
    assert "KeyError: <redacted:" in data["preview"]
    assert data["redactions"] and all({"line", "kind"} == set(d) for d in data["redactions"])
    # The raw paste is dropped at the gate: only the sanitised rewrite is stored.
    assert list(sess._pending.values()) == [data["preview"]]


def test_traceback_confirm_forwards_only_the_sanitised_text():
    sess = StubChatSession(traceback_guard=True)
    q = sess.subscribe()
    sess.send(_TB_PASTE)
    token = _drain(q)[0].data["token"]
    sess.send_confirmed(token)
    after = [e.kind for e in _drain(q)]
    assert "message" in after and "idle" in after  # forwarded exactly now
    assert TB_SECRET not in sess.last_sent
    assert "KeyError: <redacted:" in sess.last_sent
    assert "why does this fail?" in sess.last_sent  # prose untouched
    # single-use token, like the PII hold
    from mooring.ai.base import AIError

    with pytest.raises(AIError):
        sess.send_confirmed(token)


def test_traceback_hold_carries_prose_pii_findings_on_the_same_card():
    # One COMBINED hold: prose PII around the traceback rides the traceback event
    # (pii_findings), never a second sequential hold; the one confirm forwards.
    sess = StubChatSession(traceback_guard=True, pii_enabled=True, pii_block=True)
    q = sess.subscribe()
    sess.send(f"card {VALID_CARD} keeps failing\n\n{_TB_PASTE}")
    events = _drain(q)
    assert [e.kind for e in events] == ["traceback"]  # no separate "pii" hold
    data = events[0].data
    assert any(f["kind"] == pii.CARD for f in data["pii_findings"])
    assert VALID_CARD not in json.dumps(data["pii_findings"])
    # The block verdict rides the hold, so an UNATTENDED consumer (the batch
    # worker) can honour it — an interactive confirm has the analyst looking.
    assert data["pii_hold"] is True
    sess.send_confirmed(data["token"])
    assert TB_SECRET not in sess.last_sent
    assert VALID_CARD in sess.last_sent  # prose is untouched — the analyst confirmed it


def test_traceback_hold_pii_verdict_reflects_warn_mode():
    # Warn mode: the findings still ride the card, but pii_hold is False — the
    # guard would not have held the prose on its own, so an unattended consumer
    # may auto-confirm exactly as warn mode allows a plain send to proceed.
    sess = StubChatSession(traceback_guard=True, pii_enabled=True, pii_block=False)
    q = sess.subscribe()
    sess.send(f"card {VALID_CARD} keeps failing\n\n{_TB_PASTE}")
    data = _drain(q)[0].data
    assert any(f["kind"] == pii.CARD for f in data["pii_findings"])
    assert data["pii_hold"] is False


def test_traceback_guard_off_is_passthrough():
    sess = StubChatSession()  # guard not armed
    q = sess.subscribe()
    sess.send(_TB_PASTE)
    kinds_seen = [e.kind for e in _drain(q)]
    assert "traceback" not in kinds_seen and "idle" in kinds_seen
    assert TB_SECRET in sess.last_sent  # forwarded raw — it IS the guard doing the work


def test_traceback_plain_prose_skips_the_hold():
    sess = StubChatSession(traceback_guard=True)
    q = sess.subscribe()
    sess.send("how do I aggregate revenue by month?")
    kinds_seen = [e.kind for e in _drain(q)]
    assert "traceback" not in kinds_seen and "idle" in kinds_seen


def test_traceback_close_clears_the_held_rewrite():
    sess = StubChatSession(traceback_guard=True)
    q = sess.subscribe()
    sess.send(_TB_PASTE)
    _drain(q)
    assert sess._pending
    sess.close()
    assert sess._pending == {}


def test_traceback_known_tokens_rescue_from_system_context():
    sess = StubChatSession(traceback_guard=True, system_context="DATASET SCHEMA:\nrevenue Int64")
    q = sess.subscribe()
    sess.send(
        "Traceback (most recent call last):\n"
        '  File "C:\\elsewhere\\lib.py", line 2, in f\n'
        "KeyError: 'revenue'"
    )
    preview = _drain(q)[0].data["preview"]
    assert "KeyError: 'revenue'" in preview  # already in-channel — nothing new revealed


def test_traceback_known_tokens_rescue_from_notebook_on_disk(tmp_path):
    (tmp_path / "nb.py").write_text('df = df.select("net revenue")\n', "utf-8")
    sess = StubChatSession(traceback_guard=True, workspace=tmp_path, notebook_rel="nb.py")
    q = sess.subscribe()
    sess.send(
        "Traceback (most recent call last):\n"
        '  File "C:\\elsewhere\\lib.py", line 2, in f\n'
        "KeyError: 'net revenue'"
    )
    preview = _drain(q)[0].data["preview"]
    assert "KeyError: 'net revenue'" in preview


def test_traceback_guard_survives_a_non_utf8_notebook(tmp_path):
    # A stray latin-1 byte in the notebook (hand-edit in a wrong-encoding editor)
    # must not break EVERY send while the default-on guard is armed: the
    # known-token rescue just gets fewer tokens and the turn goes through.
    (tmp_path / "nb.py").write_bytes(b"# caf\xe9\nrevenue = 1\n")
    sess = StubChatSession(traceback_guard=True, workspace=tmp_path, notebook_rel="nb.py")
    q = sess.subscribe()
    sess.send("plain question, no traceback")
    kinds_seen = [e.kind for e in _drain(q)]
    assert "idle" in kinds_seen  # the turn was answered, not a decode crash
    assert sess.last_sent == "plain question, no traceback"


def test_traceback_workspace_frame_rereads_source_from_disk(tmp_path):
    (tmp_path / "nb.py").write_text("import marimo\ntotal = df.sum()\n", "utf-8")
    sess = StubChatSession(traceback_guard=True, workspace=tmp_path, notebook_rel="nb.py")
    q = sess.subscribe()
    sess.send(
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "nb.py"}", line 2, in _\n'
        f"    total = df.sum({TB_SECRET!r})\n"  # doctored paste — must not be trusted
        "AttributeError: boom"
    )
    preview = _drain(q)[0].data["preview"]
    assert TB_SECRET not in preview
    assert 'File "nb.py", line 2, in _' in preview
    assert "total = df.sum()" in preview  # the disk truth


# -- Channel E: team context (fail-closed) -------------------------------------


def _write_ctx(tmp_path, rel, text):
    p = tmp_path / "context" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")


def test_instructions_with_card_are_withheld(tmp_path):
    _write_ctx(tmp_path, "instructions.md", f"Report in GBP.\nexample pan {VALID_CARD}")
    rc = ctxmod.discover_context(tmp_path, enabled=True)
    assert rc.instructions == ""  # whole file withheld on a checksum-valid card
    assert any(f.kind == pii.CARD for f in rc.findings)
    assert VALID_CARD not in repr(rc.findings)


def test_instructions_with_email_drop_line_keeps_file(tmp_path):
    _write_ctx(tmp_path, "instructions.md", "Report in GBP.\nping alice@bank.com\nUse fiscal year.")
    rc = ctxmod.discover_context(tmp_path, enabled=True)
    assert "Report in GBP." in rc.instructions and "Use fiscal year." in rc.instructions
    assert "alice@bank.com" not in rc.instructions  # only the offending line dropped
    assert "context/instructions.md" in rc.loaded_files
    assert any(f.kind == pii.EMAIL for f in rc.findings)


def test_instructions_all_soft_lines_dropped_is_not_loaded(tmp_path):
    # Every line is a shape-only email -> all dropped -> file contributes nothing,
    # so it must NOT be reported in loaded_files (the file did not survive to send).
    _write_ctx(tmp_path, "instructions.md", "alice@bank.com\nbob@bank.com\n")
    rc = ctxmod.discover_context(tmp_path, enabled=True)
    assert rc.instructions == ""
    assert "context/instructions.md" not in rc.loaded_files
    assert rc.is_empty()


def test_instructions_hard_withhold_still_records_soft_findings(tmp_path):
    # A checksum card withholds the whole file; the email on another line must still
    # be reported, so the value-free report never understates the file's contents.
    _write_ctx(tmp_path, "instructions.md", f"card {VALID_CARD}\nping alice@bank.com")
    rc = ctxmod.discover_context(tmp_path, enabled=True)
    assert rc.instructions == ""  # withheld on the card
    kinds = {f.kind for f in rc.findings}
    assert pii.CARD in kinds and pii.EMAIL in kinds


def test_dictionary_description_pii_is_scrubbed(tmp_path):
    _write_ctx(
        tmp_path,
        "dictionaries/credit.yaml",
        f"models:\n  - name: t\n    description: 'sample {VALID_CARD}'\n"
        "    columns:\n      - name: id\n        data_type: int\n",
    )
    rc = ctxmod.discover_context(tmp_path, enabled=True)
    table = rc.index.get("t")
    assert table is not None
    assert table.description == ""  # dropped
    assert any(f.source == "credit.t" and f.kind == pii.CARD for f in rc.findings)


# -- config defaults -----------------------------------------------------------


def test_config_defaults_and_env_override(clean_config):
    c = config.load_app_config(env={})
    assert c.ai_pii is False and c.ai_pii_block_prompt is True and c.ai_pii_scan_source is True
    c2 = config.load_app_config(
        env={"MOORING_AI_PII": "true", "MOORING_AI_PII_BLOCK_PROMPT": "false"}
    )
    assert c2.ai_pii is True and c2.ai_pii_block_prompt is False


def test_config_name_detection_defaults_and_env(clean_config):
    c = config.load_app_config(env={})
    assert c.ai_pii_names is False
    assert c.ai_pii_name_model == "gliner-community/gliner_small-v2.5"  # safetensors default
    assert c.ai_pii_name_revision == "f227d3cd637bd4e6757ae143935316d062393341"  # pinned
    assert c.ai_pii_name_variant == "bf16"
    assert c.ai_pii_name_labels == ("person", "name")
    assert c.ai_pii_name_threshold == pytest.approx(0.7)
    c2 = config.load_app_config(
        env={
            "MOORING_AI_PII_NAMES": "true",
            "MOORING_AI_PII_NAME_THRESHOLD": "0.5",
            "MOORING_AI_PII_NAME_VARIANT": "",
        }
    )
    assert c2.ai_pii_names is True and c2.ai_pii_name_threshold == pytest.approx(0.5)
    assert c2.ai_pii_name_variant == ""  # override to load a repo's default weights file


# -- "auto" name_backend resolution at the session boundaries ------------------


def test_configure_pii_resolves_auto_backend(monkeypatch):
    """configure_pii turns name_backend = "auto" into a concrete backend at
    construction — but only when name detection is armed, so a guard without names
    never imports spaCy just to choose."""
    from mooring.ai import ner_spacy

    # names armed + spaCy ready -> auto resolves to the offline backend
    monkeypatch.setattr(ner_spacy, "available", lambda: True)
    monkeypatch.setattr(ner_spacy, "is_ready", lambda model="": True)
    sess = StubChatSession(pii_enabled=True, pii_names=True, pii_name_backend="auto")
    assert sess._pii_name_backend == "spacy"

    # spaCy not present -> auto falls back to gliner (never a dead backend)
    monkeypatch.setattr(ner_spacy, "available", lambda: False)
    sess2 = StubChatSession(pii_enabled=True, pii_names=True, pii_name_backend="auto")
    assert sess2._pii_name_backend == "gliner"

    # name detection OFF -> backend left unresolved and spaCy is never probed
    def boom():
        raise AssertionError("must not probe spaCy when name detection is off")

    monkeypatch.setattr(ner_spacy, "available", boom)
    sess3 = StubChatSession(pii_enabled=True, pii_names=False, pii_name_backend="auto")
    assert sess3._pii_name_backend == "auto"


def test_open_chat_resolves_auto_and_shapes_model(monkeypatch):
    """CopilotProvider.open_chat resolves "auto" to a concrete backend and shapes
    the shared name_model for it before handing them to the session: the
    GLiNER-default id is meaningless to spaCy, so it becomes "" (the bundled model)."""
    from mooring.ai import ner_spacy
    from mooring.ai import session as session_mod
    from mooring.ai.copilot import CopilotProvider
    from mooring.ai_config import PiiConfig

    captured: dict = {}

    class _StubSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            return self

    monkeypatch.setattr(session_mod, "CopilotChatSession", _StubSession)
    monkeypatch.setattr(ner_spacy, "available", lambda: True)
    monkeypatch.setattr(ner_spacy, "is_ready", lambda model="": True)

    provider = CopilotProvider()
    monkeypatch.setattr(provider, "available", lambda: True)
    # name_model is left at its GLiNER default — auto picks spaCy, so it maps to ""
    pii_cfg = PiiConfig(enabled=True, names=True, name_backend="auto")
    provider.open_chat(
        system_context="ctx", workspace=".", folders=(), notebook_rel="nb.py", pii=pii_cfg
    )
    assert captured["pii_name_backend"] == "spacy"  # resolved, not the raw "auto"
    assert captured["pii_name_model"] == ""  # GLiNER-default id shaped to bundled model
    assert captured["pii_names"] is True
