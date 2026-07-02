"""Tests for the in-hub Settings page (server endpoints + the settings registry).

The hub is built from a real (empty) user config in a tmp dir, so it boots in
local mode (no repo) and a settings write's whole-config re-read is stable.
"""

import tomllib

import pytest
from starlette.testclient import TestClient

from mooring import config, paths, telemetry
from mooring.hub import settings_schema
from mooring.hub.server import Hub, create_app

# Env vars that would shadow a config.toml write and break the round-trip tests.
_AI_ENV = [
    "MOORING_UI_THEME",
    "MOORING_AI_ENABLED",
    "MOORING_AI_MODEL",
    "MOORING_AI_REASONING_EFFORT",
    "MOORING_AI_CHAT_IDLE_SEC",
    "MOORING_AI_LIVE_SCHEMA",
    "MOORING_AI_SEMANTIC_MODEL",
    "MOORING_AI_TRACEBACK_GUARD",
    "MOORING_AI_CONTEXT",
    "MOORING_AI_CONTEXT_DIR",
    "MOORING_AI_CONTEXT_MAX_KB",
    "MOORING_AI_PII",
    "MOORING_AI_PII_BLOCK_PROMPT",
    "MOORING_AI_PII_SCAN_SOURCE",
    "MOORING_AI_PII_NAMES",
    "MOORING_AI_PII_NAME_BACKEND",
    "MOORING_AI_PII_NAME_THRESHOLD",
    "MOORING_AI_BATCH",
    "MOORING_AI_BATCH_MAX_JOBS",
    "MOORING_AI_BATCH_MAX_CONCURRENCY",
    "MOORING_AI_BATCH_JOB_TIMEOUT_SEC",
    "MOORING_AI_BATCH_FOLLOW_UP_TURNS",
    "MOORING_AI_BATCH_PII_POLICY",
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    monkeypatch.delenv("MOORING_GITHUB_HOST", raising=False)
    for var in _AI_ENV:
        monkeypatch.delenv(var, raising=False)
    hub = Hub(config.load_app_config())
    with TestClient(create_app(hub)) as c:
        yield c, hub


def _config_data():
    return tomllib.loads(paths.user_config_file().read_text("utf-8"))


# -- registry (pure) ---------------------------------------------------------


def test_every_editable_key_roundtrips_through_the_loader(tmp_path, monkeypatch):
    """The single most important invariant: each editable key is the TOML key the
    loader reads, so set_value(key) is observable on the live AppConfig via accessor.
    Catches the silent 'wrote ai.pii.names instead of ai.pii.detect_names' bug class."""
    # Isolate like the client fixture does: without this, the writes below land in
    # the DEVELOPER'S REAL config.toml (and a set env var would shadow the read-back).
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    for var in _AI_ENV:
        monkeypatch.delenv(var, raising=False)
    samples = {
        "bool": lambda s: not bool(s.default),
        "int": lambda s: int(s.default) + 1,
        "float": lambda s: 0.5,
        "str": lambda s: "x" if s.allow_empty or s.default else "x",
        "enum": lambda s: next(v for v in s.enum_values if v != s.default),
        "list": lambda s: ["person", "org"],
    }
    for spec in settings_schema.EDITABLE:
        value = settings_schema.coerce(spec, samples[spec.type](spec))
        config_store_set(spec.key, value)
        cfg = config.load_app_config()
        got = getattr(cfg, spec.accessor)
        if isinstance(got, tuple):
            got = list(got)
        assert got == value, f"{spec.key} -> {spec.accessor} did not round-trip"


def config_store_set(key, value):
    from mooring import config_store

    config_store.set_value(key, value)


def test_coerce_rejects_bad_input():
    enabled = settings_schema.by_key("ai.pii.enabled")
    with pytest.raises(ValueError):
        settings_schema.coerce(enabled, "yes")  # not a bool
    jobs = settings_schema.by_key("ai.batch.max_jobs")
    with pytest.raises(ValueError):
        settings_schema.coerce(jobs, 0)  # below min
    backend = settings_schema.by_key("ai.pii.name_backend")
    with pytest.raises(ValueError):
        settings_schema.coerce(backend, "bogus")  # not an enum value
    labels = settings_schema.by_key("ai.pii.name_labels")
    with pytest.raises(ValueError):
        settings_schema.coerce(labels, [])  # empty list


def test_no_admin_or_guarantee_key_is_editable():
    """The allowlist must exclude org/identity/governance keys; the four structural
    value-blindness guarantees have no flag, so they cannot appear at all."""
    forbidden = {
        "logging.endpoint",
        "logging.level",
        "github.client_id",
        "github.owner",
        "github.repo",
        "github.host",
        "ai.provider",
        "ai.pii.name_model",
        "ai.pii.name_model_revision",
    }
    editable = {spec.key for spec in settings_schema.EDITABLE}
    assert forbidden.isdisjoint(editable)


# -- endpoints ---------------------------------------------------------------


def test_settings_page_serves_html(client):
    c, _ = client
    resp = c.get("/settings")
    assert resp.status_code == 200
    assert "Settings" in resp.text
    assert "__MOORING_DEFAULT_THEME__" not in resp.text  # theme inlined


def test_get_settings_shape(client):
    c, _ = client
    data = c.get("/api/settings").json()
    assert {"groups", "editable", "admin", "pii"} <= data.keys()
    keys = {row["key"] for row in data["editable"]}
    assert "ui.theme" in keys
    assert "ai.pii.enabled" in keys
    # Admin block is read-only display, never the literal client id / endpoint.
    labels = {row["label"] for row in data["admin"]}
    assert "Central logging" in labels
    assert "GitHub OAuth client id" in labels


def test_set_persists_and_goes_live(client):
    c, hub = client
    resp = c.post("/api/settings", json={"key": "ai.chat_idle_timeout_sec", "value": 1200})
    assert resp.status_code == 200
    assert hub.app_cfg.ai_chat_idle_timeout == 1200  # live, no full reload
    assert _config_data()["ai"]["chat_idle_timeout_sec"] == 1200  # persisted
    row = next(r for r in resp.json()["editable"] if r["key"] == "ai.chat_idle_timeout_sec")
    assert row["value"] == 1200


def test_unknown_key_rejected(client):
    c, _ = client
    assert c.post("/api/settings", json={"key": "logging.endpoint", "value": "x"}).status_code == 400
    assert c.post("/api/settings", json={"key": "foo.bar", "value": 1}).status_code == 400


def test_bad_value_rejected(client):
    c, _ = client
    resp = c.post("/api/settings", json={"key": "ai.batch.max_jobs", "value": 0})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_weakening_flip_needs_confirm(client):
    c, hub = client
    # Enabling team context is the weakening direction -> held until confirmed.
    resp = c.post("/api/settings", json={"key": "ai.context", "value": True})
    assert resp.status_code == 409
    body = resp.json()
    assert body["needs_confirm"] is True and body["message"]
    assert hub.app_cfg.ai_context is False  # not applied
    # With confirm it goes through.
    ok = c.post("/api/settings", json={"key": "ai.context", "value": True, "confirm": True})
    assert ok.status_code == 200
    assert hub.app_cfg.ai_context is True


def test_traceback_guard_off_needs_confirm(client):
    c, hub = client
    # ON by default; turning the sanitise-and-hold OFF is the weakening direction
    # (raw tracebacks — which can embed values — would reach the model).
    assert hub.app_cfg.ai_traceback_guard is True
    resp = c.post("/api/settings", json={"key": "ai.traceback_guard", "value": False})
    assert resp.status_code == 409
    body = resp.json()
    assert body["needs_confirm"] is True and "RAW" in body["message"]
    assert hub.app_cfg.ai_traceback_guard is True  # not applied
    ok = c.post("/api/settings", json={"key": "ai.traceback_guard", "value": False, "confirm": True})
    assert ok.status_code == 200 and hub.app_cfg.ai_traceback_guard is False
    # Turning it back ON is the safe direction — no confirm required.
    back = c.post("/api/settings", json={"key": "ai.traceback_guard", "value": True})
    assert back.status_code == 200 and hub.app_cfg.ai_traceback_guard is True


def test_non_weakening_direction_needs_no_confirm(client):
    c, hub = client
    # Turning context back OFF is the safe direction — no confirm required.
    c.post("/api/settings", json={"key": "ai.context", "value": True, "confirm": True})
    resp = c.post("/api/settings", json={"key": "ai.context", "value": False})
    assert resp.status_code == 200
    assert hub.app_cfg.ai_context is False


def test_block_prompt_confirm_gated_on_scan_state(client):
    c, hub = client
    # Scan off by default: warn-only flip changes nothing real, so no scary confirm.
    resp = c.post("/api/settings", json={"key": "ai.pii.block_prompt", "value": False})
    assert resp.status_code == 200
    assert hub.app_cfg.ai_pii_block_prompt is False
    # With the scan ON, downgrading to warn-only DOES weaken -> needs confirm.
    c.post("/api/settings", json={"key": "ai.pii.enabled", "value": True})  # safe direction
    c.post("/api/settings", json={"key": "ai.pii.block_prompt", "value": True})  # restore
    resp = c.post("/api/settings", json={"key": "ai.pii.block_prompt", "value": False})
    assert resp.status_code == 409 and resp.json()["needs_confirm"] is True
    ok = c.post(
        "/api/settings", json={"key": "ai.pii.block_prompt", "value": False, "confirm": True}
    )
    assert ok.status_code == 200 and hub.app_cfg.ai_pii_block_prompt is False


def test_reset_of_pii_scan_needs_confirm(client):
    c, hub = client
    c.post("/api/settings", json={"key": "ai.pii.enabled", "value": True})  # deliberately on
    # Resetting reverts to the OFF default — the weakening direction, so Reset must
    # require the same acknowledgement the toggle does (not slip past it).
    resp = c.post("/api/settings/reset", json={"key": "ai.pii.enabled"})
    assert resp.status_code == 409 and resp.json()["needs_confirm"] is True
    assert hub.app_cfg.ai_pii is True  # not reset
    ok = c.post("/api/settings/reset", json={"key": "ai.pii.enabled", "confirm": True})
    assert ok.status_code == 200 and hub.app_cfg.ai_pii is False


def test_reset_of_safe_setting_needs_no_confirm(client):
    c, hub = client
    c.post("/api/settings", json={"key": "sync.warn_file_mb", "value": 25})
    assert c.post("/api/settings/reset", json={"key": "sync.warn_file_mb"}).status_code == 200
    assert hub.app_cfg.warn_file_mb == 10


def test_enum_options_carry_display_labels(client):
    c, _ = client
    rows = {r["key"]: r for r in c.get("/api/settings").json()["editable"]}
    assert {"value": "system", "label": "System"} in rows["ui.theme"]["enum_options"]
    policy = rows["ai.batch.pii_policy"]["enum_options"]
    assert any(o["value"] == "block_batch" and o["label"] != "block_batch" for o in policy)


def test_reset_reverts_to_default(client):
    c, hub = client
    c.post("/api/settings", json={"key": "sync.warn_file_mb", "value": 25})
    assert hub.app_cfg.warn_file_mb == 25
    resp = c.post("/api/settings/reset", json={"key": "sync.warn_file_mb"})
    assert resp.status_code == 200
    assert hub.app_cfg.warn_file_mb == 10  # packaged default
    assert "warn_file_mb" not in _config_data().get("sync", {})  # key removed


def test_env_override_is_surfaced(client, monkeypatch):
    c, hub = client
    monkeypatch.setenv("MOORING_AI_MODEL", "pinned-model")
    hub.app_cfg = config.load_app_config()  # re-read so the env override is live
    row = next(r for r in c.get("/api/settings").json()["editable"] if r["key"] == "ai.model")
    assert row["env_overridden"] is True
    assert row["value"] == "pinned-model"


def test_disabling_ai_closes_open_chats(client):
    c, hub = client

    class _FakeChat:
        def __init__(self):
            self.closed = False

        def idle_seconds(self):
            return 0

        def close(self):
            self.closed = True

    chat = _FakeChat()
    hub._chats["sid1"] = chat
    hub._chat_targets["sid1"] = ("ws", "nb.py")
    resp = c.post("/api/settings", json={"key": "ai.enabled", "value": False})
    assert resp.status_code == 200
    assert hub.app_cfg.ai_enabled is False
    assert chat.closed is True
    assert hub._chats == {}


def test_telemetry_is_value_free(client, monkeypatch):
    c, _ = client
    events = []
    monkeypatch.setattr(telemetry, "log_event", lambda name, **kw: events.append((name, kw)))
    # A string setting logs the key but NOT the value (could be a model id).
    c.post("/api/settings", json={"key": "ai.model", "value": "secret-model-id"})
    name, kw = events[-1]
    assert name == "settings_change" and kw == {"key": "ai.model"}
    # A bool setting may log the new boolean (value-free).
    c.post("/api/settings", json={"key": "ai.live_schema", "value": False})
    name, kw = events[-1]
    assert name == "settings_change" and kw == {"key": "ai.live_schema", "value": False}
