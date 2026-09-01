"""Tests for the in-hub Settings page (server endpoints + the settings registry).

The hub is built from a real (empty) user config in a tmp dir, so it boots in
local mode (no repo) and a settings write's whole-config re-read is stable.
"""

import tomllib

import pytest
from starlette.testclient import TestClient

from mooring import config, paths, telemetry
from mooring.ai import openai_provider
from mooring.hub import settings_schema
from mooring.hub.routes import settings as settings_routes
from mooring.hub.server import Hub, create_app

# Env vars that would shadow a config.toml write and break the round-trip tests.
_AI_ENV = [
    "MOORING_UI_THEME",
    "MOORING_AI_ENABLED",
    "MOORING_AI_PROVIDER",
    "MOORING_AI_MODEL",
    "MOORING_AI_REASONING_EFFORT",
    "MOORING_AI_OPENAI_BASE_URL",
    "MOORING_AI_OPENAI_API_VERSION",
    "MOORING_AI_OPENAI_TIMEOUT_SEC",
    "MOORING_AI_ROUTING",
    "MOORING_AI_TRUSTED_BASE_URL",
    "MOORING_AI_TRUSTED_API_VERSION",
    "MOORING_AI_TRUSTED_CLASSIFIER_MODEL",
    "MOORING_AI_TRUSTED_CODING_MODEL",
    "MOORING_AI_TRUSTED_CODING_MODELS",
    "MOORING_AI_TRUSTED_PROFILE_LABEL",
    "MOORING_AI_CHAT_IDLE_SEC",
    "MOORING_AI_LIVE_SCHEMA",
    "MOORING_AI_SEMANTIC_MODEL",
    "MOORING_AI_TRACEBACK_GUARD",
    "MOORING_AI_APPLY_GUARD",
    "MOORING_AI_APPLY_RUNS",
    "MOORING_AI_AUTO_APPLY",
    "MOORING_AI_AUTO_RUN_REPORT",
    "MOORING_AI_MAX_TOOL_ITERS",
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


def _enable_trusted_routing(hub, monkeypatch):
    managed = {
        "MOORING_AI_ROUTING": "1",
        "MOORING_AI_TRUSTED_BASE_URL": "https://approved.example/v1",
        "MOORING_AI_TRUSTED_CLASSIFIER_MODEL": "approved-inspector",
        "MOORING_AI_TRUSTED_CODING_MODEL": "approved-coder",
        "MOORING_AI_TRUSTED_CODING_MODELS": "approved-coder,approved-coder-fast",
        "MOORING_AI_TRUSTED_PROFILE_LABEL": "Firm approved AI",
    }
    for key, value in managed.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda **_: "managed-key")
    hub.app_cfg = config.load_app_config()


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
    # ai.provider is DELIBERATELY editable (a per-machine choice: GitHub Copilot vs an
    # OpenAI-compatible endpoint) — it was forbidden while copilot was the only backend
    # and there was nothing to switch to. It is not identity/telemetry, and switching
    # stays value-blind; the SettingSpec marks it needs_care and its help spells out the
    # egress-destination change. Identity/telemetry/model-pin keys stay forbidden.
    forbidden = {
        "logging.endpoint",
        "logging.level",
        "github.client_id",
        "github.owner",
        "github.repo",
        "github.host",
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
    assert data["routing"] == {"enabled": False}
    keys = {row["key"] for row in data["editable"]}
    assert "ui.theme" in keys
    assert "ai.pii.enabled" in keys
    # The self-configured profile is ALWAYS offered: it is how a user with no
    # managed deployment turns the feature on in the first place.
    assert {
        "ai.routing.enabled",
        "ai.routing.base_url",
        "ai.routing.api_version",
        "ai.routing.classifier_model",
        "ai.routing.coding_model",
        "ai.routing.coding_models",
    } <= keys
    # The two picker rows still need a live profile to pick within.
    assert "ai.trusted_model" not in keys
    assert "ai.routing_preference" not in keys
    assert data["routing_source"] == "off"
    assert data["routing_local_allowed"] is True
    # Admin block is read-only display, never the literal client id / endpoint.
    labels = {row["label"] for row in data["admin"]}
    assert "Central logging" in labels
    assert "GitHub OAuth client id" in labels


def test_trusted_defaults_appear_only_with_a_complete_managed_profile(client, monkeypatch):
    c, hub = client
    _enable_trusted_routing(hub, monkeypatch)

    payload = c.get("/api/settings").json()
    rows = {row["key"]: row for row in payload["editable"]}

    model = rows["ai.trusted_model"]
    assert model["value"] == ""
    assert model["default"] == ""
    assert model["enum_options"] == [
        {"value": "approved-coder", "label": "approved-coder"},
        {"value": "approved-coder-fast", "label": "approved-coder-fast"},
    ]
    routing = rows["ai.routing_preference"]
    assert routing["value"] == "auto"
    assert routing["enum_options"] == [
        {"value": "auto", "label": "Automatic"},
        {"value": "trusted", "label": "Always use approved"},
    ]
    assert payload["routing"] == hub._trusted_routing_metadata()


def test_trusted_defaults_are_shown_disabled_and_explained_when_profile_unavailable(
    client, monkeypatch
):
    """A mistyped endpoint has to look different from a feature nobody turned on:
    with routing ON but unusable the rows stay, carrying the reason."""
    c, hub = client
    _enable_trusted_routing(hub, monkeypatch)
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda **_: "")

    payload = c.get("/api/settings").json()
    rows = {row["key"]: row for row in payload["editable"]}

    assert "credential is unavailable" in rows["ai.trusted_model"]["unavailable_note"]
    assert rows["ai.trusted_model"]["enum_options"] == []
    assert rows["ai.trusted_model"]["value"] == ""
    assert rows["ai.routing_preference"]["unavailable_note"]
    # An ordinary row never carries the note.
    assert rows["ai.pii.enabled"]["unavailable_note"] == ""
    assert payload["routing"] == {
        "enabled": True,
        "source": "managed",
        "profile_label": "Approved AI",
        "trusted_models": [],
        "managed_default_trusted_model": "",
        "default_trusted_model": "",
        "default_routing_preference": "trusted",
        "error": "The approved AI profile is unavailable.",
    }
    rendered = repr(payload["routing"])
    assert "approved.example" not in rendered
    assert "approved-inspector" not in rendered
    assert "managed-key" not in rendered


def test_trusted_default_writes_are_allowlisted_and_go_live(client, monkeypatch):
    c, hub = client
    _enable_trusted_routing(hub, monkeypatch)

    model = c.post(
        "/api/settings",
        json={"key": "ai.trusted_model", "value": "approved-coder-fast"},
    )
    routing = c.post(
        "/api/settings",
        json={"key": "ai.routing_preference", "value": "trusted"},
    )

    assert model.status_code == 200
    assert routing.status_code == 200
    assert hub.app_cfg.ai_default_trusted_model == "approved-coder-fast"
    assert hub.app_cfg.ai_routing_preference == "trusted"
    stored = _config_data()["ai"]
    assert stored["trusted_model"] == "approved-coder-fast"
    assert stored["routing_preference"] == "trusted"


def test_settings_metadata_distinguishes_managed_and_effective_trusted_default(
    client, monkeypatch
):
    c, hub = client
    _enable_trusted_routing(hub, monkeypatch)

    selected = c.post(
        "/api/settings",
        json={"key": "ai.trusted_model", "value": "approved-coder-fast"},
    ).json()

    assert selected["routing"]["managed_default_trusted_model"] == "approved-coder"
    assert selected["routing"]["default_trusted_model"] == "approved-coder-fast"
    row = next(r for r in selected["editable"] if r["key"] == "ai.trusted_model")
    assert row["value"] == "approved-coder-fast"

    reset = c.post("/api/settings/reset", json={"key": "ai.trusted_model"}).json()

    assert reset["routing"]["managed_default_trusted_model"] == "approved-coder"
    assert reset["routing"]["default_trusted_model"] == "approved-coder"
    row = next(r for r in reset["editable"] if r["key"] == "ai.trusted_model")
    assert row["value"] == ""


def test_unapproved_trusted_default_is_rejected_before_any_ai_or_context_call(
    client, monkeypatch
):
    c, hub = client
    _enable_trusted_routing(hub, monkeypatch)
    touched = []
    monkeypatch.setattr(hub, "_build_chat_context", lambda *a, **k: touched.append("context"))
    monkeypatch.setattr(hub, "_trusted_inspector_for", lambda: touched.append("inspector"))
    monkeypatch.setattr(hub, "_trusted_provider_for", lambda: touched.append("provider"))
    monkeypatch.setattr(
        settings_routes.config_store,
        "set_value",
        lambda *a, **k: touched.append("config_write"),
    )

    response = c.post(
        "/api/settings",
        json={"key": "ai.trusted_model", "value": "browser-invented"},
    )

    assert response.status_code == 400
    assert "not approved" in response.json()["error"]
    assert touched == []
    assert not paths.user_config_file().exists()


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


def test_turning_auto_apply_back_on_needs_confirm(client):
    c, hub = client
    # ON by default. Turning it OFF is the safe direction (the analyst gets the Apply
    # button back), so it goes straight through...
    assert hub.app_cfg.ai_auto_apply is True
    off = c.post("/api/settings", json={"key": "ai.auto_apply", "value": False})
    assert off.status_code == 200 and hub.app_cfg.ai_auto_apply is False
    # ...and turning it back on is the weakening direction, held for a confirm.
    resp = c.post("/api/settings", json={"key": "ai.auto_apply", "value": True})
    assert resp.status_code == 409
    assert resp.json()["needs_confirm"] is True and resp.json()["message"]
    assert hub.app_cfg.ai_auto_apply is False  # not applied
    ok = c.post("/api/settings", json={"key": "ai.auto_apply", "value": True, "confirm": True})
    assert ok.status_code == 200 and hub.app_cfg.ai_auto_apply is True


def test_turning_the_automatic_re_run_back_on_needs_confirm(client):
    c, hub = client
    assert hub.app_cfg.ai_auto_run_report is True
    off = c.post("/api/settings", json={"key": "ai.auto_run_report", "value": False})
    assert off.status_code == 200 and hub.app_cfg.ai_auto_run_report is False
    resp = c.post("/api/settings", json={"key": "ai.auto_run_report", "value": True})
    assert resp.status_code == 409 and resp.json()["needs_confirm"] is True
    assert hub.app_cfg.ai_auto_run_report is False
    ok = c.post(
        "/api/settings", json={"key": "ai.auto_run_report", "value": True, "confirm": True}
    )
    assert ok.status_code == 200 and hub.app_cfg.ai_auto_run_report is True


def test_the_tool_call_ceiling_is_editable_and_range_checked(client):
    c, hub = client
    assert hub.app_cfg.ai_max_tool_iters == 200
    ok = c.post("/api/settings", json={"key": "ai.max_tool_iters", "value": 500})
    assert ok.status_code == 200 and hub.app_cfg.ai_max_tool_iters == 500
    # A ceiling of 0 would end every turn before the model's first tool call, so it
    # is refused at the door rather than written and silently ignored on load.
    bad = c.post("/api/settings", json={"key": "ai.max_tool_iters", "value": 0})
    assert bad.status_code == 400
    assert hub.app_cfg.ai_max_tool_iters == 500
    # The page's range is the loader's range. settings_schema is a pure leaf and
    # cannot import ai_config, so the two numbers are written twice — pin them
    # together, or the page would happily accept a value the loader silently caps.
    from mooring.ai_config import MAX_TOOL_ITERS_CEILING, AiConfig

    spec = settings_schema.by_key("ai.max_tool_iters")
    assert spec.maximum == MAX_TOOL_ITERS_CEILING
    assert spec.default == AiConfig().max_tool_iters


def test_the_ai_request_timeout_is_editable_and_range_checked(client):
    c, hub = client
    assert hub.app_cfg.ai_openai_timeout_sec == 300
    ok = c.post("/api/settings", json={"key": "ai.openai_timeout_sec", "value": 900})
    assert ok.status_code == 200 and hub.app_cfg.ai_openai_timeout_sec == 900
    # A 0 would time out every request before the model had said anything, so it is
    # refused at the door rather than written and silently ignored on load.
    for bad in (0, 3601, True, 600.5):
        resp = c.post("/api/settings", json={"key": "ai.openai_timeout_sec", "value": bad})
        assert resp.status_code == 400, bad
    assert hub.app_cfg.ai_openai_timeout_sec == 900


def test_the_ai_request_timeout_page_range_is_the_loaders_range():
    """The page's range is the loader's range. settings_schema is a pure leaf and
    cannot import ai_config, so the floor, the ceiling and the default are written
    twice — pin them together, or raising OPENAI_TIMEOUT_CEILING later would leave
    the Settings page 400-ing a value the loader would happily accept."""
    from mooring.ai_config import OPENAI_TIMEOUT_CEILING, OPENAI_TIMEOUT_DEFAULT, AiConfig

    spec = settings_schema.by_key("ai.openai_timeout_sec")
    assert spec.maximum == OPENAI_TIMEOUT_CEILING
    assert spec.default == OPENAI_TIMEOUT_DEFAULT == AiConfig().openai_timeout_sec
    # The loader's floor: ``_as_positive_int`` drops anything below 1 for the default,
    # so the page must not accept one either.
    assert spec.minimum == 1


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


# -- the self-configured profile ---------------------------------------------


def _fill_local_profile(c, *, models=("self-coder", "self-coder-fast")):
    """Write every field of a local profile EXCEPT the on switch."""
    for key, value in (
        ("ai.routing.base_url", "https://self.example/v1"),
        ("ai.routing.classifier_model", "self-inspector"),
        ("ai.routing.coding_model", "self-coder"),
        ("ai.routing.coding_models", list(models)),
    ):
        resp = c.post("/api/settings", json={"key": key, "value": value})
        assert resp.status_code == 200, (key, resp.json())


def test_local_routing_endpoint_must_be_clean_https_at_write_time(client):
    """The endpoint rule is enforced where the value is TYPED, not only where it is
    used, so a typo is a 400 now rather than a chat that mysteriously won't open."""
    c, _hub = client

    for bad in (
        "http://self.example/v1",
        "https://user:pw@self.example/v1",
        "https://self.example/v1?key=leak",
        "https://self.example/v1#frag",
        "self.example/v1",
    ):
        resp = c.post("/api/settings", json={"key": "ai.routing.base_url", "value": bad})
        assert resp.status_code == 400, bad
        assert "HTTPS" in resp.json()["error"]

    ok = c.post(
        "/api/settings",
        json={"key": "ai.routing.base_url", "value": "https://self.example/v1"},
    )
    assert ok.status_code == 200


def test_turning_local_routing_on_requires_a_complete_profile_and_a_key(
    client, monkeypatch
):
    c, hub = client
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda **_: "")

    def enable(confirm=True):
        return c.post(
            "/api/settings",
            json={"key": "ai.routing.enabled", "value": True, "confirm": confirm},
        )

    # Nothing filled in at all.
    resp = enable()
    assert resp.status_code == 400
    assert "endpoint" in resp.json()["error"]

    _fill_local_profile(c)

    # Everything filled in, but no credential stored.
    resp = enable()
    assert resp.status_code == 400
    assert "API key" in resp.json()["error"]

    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda **_: "local-key")
    assert enable().status_code == 200
    assert hub.app_cfg.ai_routing_source == "local"


def test_turning_local_routing_on_is_a_weakening_flip_that_needs_a_confirm(
    client, monkeypatch
):
    """It is the one switch that lets customer data leave, so it is gated like the
    other privacy-weakening flips rather than being a quiet toggle."""
    c, _hub = client
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda **_: "local-key")
    _fill_local_profile(c)

    unconfirmed = c.post("/api/settings", json={"key": "ai.routing.enabled", "value": True})

    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["needs_confirm"] is True
    assert "customer information" in unconfirmed.json()["message"]
    assert "Self-configured" in unconfirmed.json()["message"]


def test_a_local_coding_model_outside_its_own_offered_set_is_refused(client, monkeypatch):
    c, _hub = client
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda **_: "local-key")
    _fill_local_profile(c)
    c.post(
        "/api/settings",
        json={"key": "ai.routing.coding_models", "value": ["other-coder"]},
    )

    resp = c.post(
        "/api/settings",
        json={"key": "ai.routing.enabled", "value": True, "confirm": True},
    )

    assert resp.status_code == 400
    assert "must be one of the models offered" in resp.json()["error"]


def test_a_live_local_profile_is_labelled_self_configured_end_to_end(client, monkeypatch):
    """The honesty guarantee, checked where the browser actually reads it."""
    c, hub = client
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda **_: "local-key")
    _fill_local_profile(c)
    c.post("/api/settings", json={"key": "ai.routing.enabled", "value": True, "confirm": True})

    payload = c.get("/api/settings").json()
    metadata = hub._trusted_routing_metadata()

    assert payload["routing_source"] == "local"
    assert payload["routing_key_stored"] is True
    assert metadata["source"] == "local"
    assert metadata["profile_label"] == "Self-configured"
    assert metadata["trusted_models"] == [
        {"id": "self-coder", "name": "self-coder"},
        {"id": "self-coder-fast", "name": "self-coder-fast"},
    ]
    # The picker rows only exist once a profile is live.
    keys = {row["key"] for row in payload["editable"]}
    assert {"ai.trusted_model", "ai.routing_preference"} <= keys
    # And nothing anywhere calls this endpoint approved.
    assert "approved" not in repr(metadata).lower()


def test_a_managed_deployment_refuses_local_routing_writes_and_its_key(
    client, monkeypatch
):
    """MOORING_AI_ROUTING present means the launcher owns this; the analyst's own
    values must not be writable, and the credential is the launcher's to supply."""
    c, hub = client
    monkeypatch.setenv("MOORING_AI_ROUTING", "1")
    hub.app_cfg = config.load_app_config()

    write = c.post(
        "/api/settings",
        json={"key": "ai.routing.base_url", "value": "https://mine.example/v1"},
    )
    key = c.post("/api/ai/trusted-key", json={"key": "sneaky"})

    assert write.status_code == 400
    assert "managed by this deployment" in write.json()["error"]
    assert key.status_code == 400
    assert "comes from the environment" in key.json()["error"]
    assert c.get("/api/settings").json()["routing_local_allowed"] is False


def test_the_trusted_key_never_falls_back_to_the_general_openai_key(monkeypatch):
    """The two credentials live in different keyring slots on purpose."""
    monkeypatch.delenv("MOORING_AI_TRUSTED_API_KEY", raising=False)
    monkeypatch.delenv("MOORING_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    stored = {(openai_provider.KEYRING_SERVICE, openai_provider.KEYRING_USER): "general-key"}

    class _Ring:
        @staticmethod
        def get_password(service, user):
            return stored.get((service, user))

    monkeypatch.setattr(openai_provider, "_keyring", lambda: _Ring)

    assert openai_provider.resolve_api_key() == "general-key"
    # Even ASKING for the keyring must not reach the general slot.
    assert openai_provider.resolve_trusted_api_key(allow_keyring=True) is None
    # A managed profile does not read the keyring at all.
    stored[
        (openai_provider.KEYRING_SERVICE_TRUSTED, openai_provider.KEYRING_USER)
    ] = "trusted-key"
    assert openai_provider.resolve_trusted_api_key(allow_keyring=False) is None
    assert openai_provider.resolve_trusted_api_key(allow_keyring=True) == "trusted-key"


def test_a_team_policy_can_forbid_a_self_configured_profile(client, monkeypatch):
    """Policy's one rule holds here too: it can take the local profile away, and
    there is no shape of mooring.toml that can hand one out."""
    from mooring import policy

    c, hub = client
    monkeypatch.setattr(openai_provider, "resolve_trusted_api_key", lambda **_: "local-key")
    _fill_local_profile(c)
    c.post("/api/settings", json={"key": "ai.routing.enabled", "value": True, "confirm": True})
    assert hub.app_cfg.ai_routing_source == "local"

    pinned = policy.Policy(settings={"ai.routing.enabled": False})
    hub.app_cfg = policy.tighten(hub.app_cfg, pinned)

    assert hub.app_cfg.ai_routing_source == "off"
    assert hub.app_cfg.ai_routing_enabled is False
    assert hub.app_cfg.ai_trusted_base_url == ""

    # And the permissive direction is not expressible.
    loosened = policy.parse({"policy": {"settings": {"ai.routing.enabled": True}}})
    assert loosened.settings == {}
    assert any("stricter" in reason for reason in loosened.ignored)
