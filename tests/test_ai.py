"""AI helper: provider seam, prompt building, and the hub endpoints.

The hub tests swap in a fake provider so they never spawn the real Copilot CLI,
and assert the end-to-end privacy property: only the schema reaches the provider.
"""

import polars as pl
import pytest
from starlette.testclient import TestClient

from mooring import config, paths
from mooring.ai import AIError, get_provider
from mooring.ai import prompt as ai_prompt
from mooring.ai.base import ProviderStatus
from mooring.ai.copilot import CopilotProvider, _extract_code, _friendly_error
from mooring.hub.server import Hub, create_app

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


# -- provider seam -----------------------------------------------------------


def test_get_provider_copilot():
    prov = get_provider(config.AppConfig(ai_provider="copilot", ai_model="gpt-5"))
    assert isinstance(prov, CopilotProvider)
    assert prov.model == "gpt-5"


def test_get_provider_unknown_raises():
    with pytest.raises(AIError, match="Unknown AI provider"):
        get_provider(config.AppConfig(ai_provider="hal9000"))


# -- prompt building ---------------------------------------------------------


def test_build_messages_carries_only_schema_and_instruction():
    system, user = ai_prompt.build_messages(
        schema_context="Columns: region: String", instruction="filter to EU", target="polars"
    )
    assert "polars" in system.lower()
    assert "never see the actual data" in system.lower()
    assert "region: String" in user
    assert "filter to EU" in user


def test_extract_code_unwraps_fenced_block():
    assert _extract_code("```python\ndf.head()\n```") == "df.head()"
    assert _extract_code("no fences here") == "no fences here"
    assert _extract_code("```\nx = 1\n```") == "x = 1"


def test_friendly_error_explains_authorization_policy():
    msg = _friendly_error(
        "Session error: You are not authorized to use this Copilot feature, it "
        "requires an enterprise or organization policy to be enabled."
    )
    assert "policy" in msg.lower()
    assert "admin" in msg.lower()
    # other errors pass through verbatim
    assert _friendly_error("boom") == "Copilot request failed: boom"


# -- hub endpoints (fake provider) -------------------------------------------


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.calls = []
        self.connected_calls = 0

    def available(self):
        return True

    def status(self, force=False):
        return ProviderStatus("fake", available=True, connected=True, account="phil", detail="ok")

    def cached_status(self):
        return self.status()

    def login_state(self):
        return {"running": False, "output": []}

    def connect(self):
        self.connected_calls += 1
        return ProviderStatus("fake", available=True, connected=False, detail="connecting")

    def generate(self, *, schema_context, instruction, target="polars"):
        self.calls.append({"schema": schema_context, "instruction": instruction, "target": target})
        return "df.filter(pl.col('region') == 'EU')"


@pytest.fixture
def ai_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    for var in ("MOORING_TOKEN", "MOORING_AI_ENABLED", "MOORING_AI_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    pl.DataFrame(
        {"region": ["EU", "US"], "amount": [1, 2], "note": [SECRET, SECRET + "_2"]}
    ).write_parquet(ws / "data" / "sales.parquet")

    fake = FakeProvider()
    monkeypatch.setattr("mooring.ai.get_provider", lambda app_cfg: fake)

    spec = config.RepoSpec(alias="team", owner="acme", repo="nbs", workspace_path=str(ws))
    app_cfg = config.AppConfig(
        client_id="cid", repos=(spec,), active_alias="team", ai_enabled=True
    )
    hub = Hub(app_cfg)
    with TestClient(create_app(hub)) as client:
        yield client, fake


def test_state_exposes_ai_enabled(ai_hub):
    client, _ = ai_hub
    assert client.get("/api/state").json()["ai_enabled"] is True


def test_ai_state_lists_datasets_and_status(ai_hub):
    client, _ = ai_hub
    body = client.get("/api/ai/state").json()
    assert body["enabled"] is True
    assert body["available"] is True
    assert body["connected"] is True
    assert body["datasets"] == ["data/sales.parquet"]


def test_ai_generate_sends_only_schema_not_values(ai_hub):
    client, fake = ai_hub
    resp = client.post(
        "/api/ai/generate",
        json={"dataset": "data/sales.parquet", "instruction": "filter to EU rows"},
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == "df.filter(pl.col('region') == 'EU')"
    # the provider was handed the schema, with column names but NO data values
    sent = fake.calls[0]["schema"]
    assert "region" in sent and "note" in sent
    assert SECRET not in sent
    assert "EU" not in sent and "US" not in sent


def test_ai_generate_requires_instruction(ai_hub):
    client, _ = ai_hub
    resp = client.post("/api/ai/generate", json={"dataset": "data/sales.parquet"})
    assert resp.status_code == 400


def test_ai_generate_rejects_traversal(ai_hub):
    client, _ = ai_hub
    resp = client.post(
        "/api/ai/generate", json={"dataset": "../secret.parquet", "instruction": "x"}
    )
    assert resp.status_code == 400


def test_ai_generate_missing_dataset_404(ai_hub):
    client, _ = ai_hub
    resp = client.post(
        "/api/ai/generate", json={"dataset": "data/nope.parquet", "instruction": "x"}
    )
    assert resp.status_code == 404


def test_ai_generate_requires_a_dataset(ai_hub):
    # No dataset path -> 400. There is deliberately no raw-text bypass: every
    # schema sent to the model goes through the value-stripping extractor.
    client, fake = ai_hub
    resp = client.post(
        "/api/ai/generate", json={"schema_text": "df: region: String", "instruction": "count"}
    )
    assert resp.status_code == 400
    assert fake.calls == []


def test_ai_connect_calls_provider(ai_hub):
    client, fake = ai_hub
    resp = client.post("/api/ai/connect", json={})
    assert resp.status_code == 200
    assert fake.connected_calls == 1


def test_ai_endpoints_disabled_when_ai_off(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    spec = config.RepoSpec(alias="team", owner="acme", repo="nbs", workspace_path=str(tmp_path))
    hub = Hub(config.AppConfig(client_id="cid", repos=(spec,), active_alias="team", ai_enabled=False))
    with TestClient(create_app(hub)) as client:
        assert client.get("/api/state").json()["ai_enabled"] is False
        assert client.get("/api/ai/state").json() == {"enabled": False}
