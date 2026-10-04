"""Regression coverage for raw API-key persistence boundaries."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import ServiceRegistryModel, WalletModel
from app.main import app
from tests.test_trust_helpers import provision_agent_wallet


class _RecordingState:
    """Enabled durable-state double that keeps saved JSON in memory."""

    enabled = True

    def __init__(self, initial: dict[str, Any] | None = None):
        self.saved: dict[str, Any] = copy.deepcopy(initial or {})

    async def load_json(self, key: str) -> Any | None:
        return copy.deepcopy(self.saved.get(key))

    async def save_json(self, key: str, value: Any) -> bool:
        self.saved[key] = json.loads(json.dumps(value, default=str))
        return True

    async def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self.saved if key.startswith(prefix))


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


def test_wallet_and_service_tables_have_no_raw_owner_key_columns():
    """Live API credentials must never be persisted as ownership metadata."""
    assert "owner_key" not in WalletModel.__table__.columns
    assert "owner_key" not in ServiceRegistryModel.__table__.columns


def test_docker_context_excludes_local_agent_and_secret_files():
    """Local Railway builds must not copy workstation-only control files."""
    ignored = {
        line.strip()
        for line in Path(".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {".agents", ".claude", ".envrc", ".vercel", ".venv"} <= ignored


@pytest.mark.anyio
async def test_wallet_scoped_key_registers_service_by_wallet_identity(
    client,
    api_headers,
    clean_database,
):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Service Owner",
            "email": "service-owner@example.com",
            "initial_credits": 100,
        },
        headers=api_headers,
    )
    assert sponsor.status_code == 201
    wallet_id = sponsor.json()["wallet_id"]

    key = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": "service-registration"},
        headers=api_headers,
    )
    assert key.status_code == 201

    response = await client.post(
        "/v1/billing/services",
        json={
            "name": "Wallet-owned service",
            "description": "Regression fixture",
            "category": "agent_comms",
            "credits_per_unit": 1,
            "owner_wallet_id": "spn-attacker-selected-owner",
        },
        headers={"X-API-Key": key.json()["api_key"]},
    )

    assert response.status_code == 201, response.text
    assert response.json()["owner_wallet_id"] == wallet_id

    factory = get_session_factory()
    async with factory() as session:
        service = await session.get(
            ServiceRegistryModel,
            response.json()["service_id"],
        )
    assert service is not None
    assert service.owner_wallet_id == wallet_id


@pytest.mark.anyio
async def test_unauthenticated_service_registration_is_rejected(
    client,
    clean_database,
):
    response = await client.post(
        "/v1/billing/services",
        json={
            "name": "Anonymous service",
            "category": "agent_comms",
            "credits_per_unit": 1,
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "missing_credentials"


@pytest.mark.anyio
async def test_bootstrap_key_cannot_register_unowned_service(
    client,
    api_headers,
    clean_database,
):
    response = await client.post(
        "/v1/billing/services",
        json={
            "name": "Unowned service",
            "category": "agent_comms",
            "credits_per_unit": 1,
        },
        headers=api_headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "wallet_identity_required"


@pytest.mark.proof
@pytest.mark.anyio
async def test_comms_registry_never_persists_raw_keys(
    client,
    api_headers,
    clean_database,
    monkeypatch,
):
    """Neither the registering caller's key nor the issued agent key may be
    written to the durable comms registry."""
    from app.core.dependencies import get_agent_comms

    wallet = await provision_agent_wallet(client)
    state = _RecordingState()
    monkeypatch.setattr(get_agent_comms().registry, "_state", state)

    issued: dict[str, str] = {}
    for headers in (wallet["agent_headers"], api_headers):
        response = await client.post(
            "/v1/comms/agents",
            json={"name": "persisted-agent", "capabilities": ["persist"]},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        # The caller still receives its agent key once, in plaintext.
        assert body["api_key"].startswith("ak-")
        issued[body["agent_id"]] = body["api_key"]

    registry = state.saved["comms.registry"]
    caller_keys = [wallet["agent_headers"]["X-API-Key"], api_headers["X-API-Key"]]
    for agent_id, agent_key in issued.items():
        record = json.dumps(registry[agent_id])
        assert agent_key not in record
        for caller_key in caller_keys:
            assert caller_key not in record
    whole = json.dumps(registry)
    assert wallet["agent_headers"]["X-API-Key"] not in whole
    for agent_key in issued.values():
        assert agent_key not in whole


@pytest.mark.proof
@pytest.mark.anyio
async def test_legacy_comms_registry_record_is_scrubbed_and_still_owned(
    client,
    clean_database,
):
    """A record persisted before the fix carries the owner's raw API key and
    the plaintext agent key. Hydration must rewrite both away and keep
    enforcing ownership against the original owner."""
    from app.core.dependencies import get_agent_comms
    from app.services.agent_comms import AgentComms

    owner = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    owner_key = owner["agent_headers"]["X-API-Key"]
    legacy_agent_key = "ak-legacy0123456789abcdef0123456789"
    state = _RecordingState(
        {
            "comms.registry": {
                "agent-legacy": {
                    "agent_id": "agent-legacy",
                    "name": "legacy",
                    "capabilities": ["legacy"],
                    "webhook_url": None,
                    "api_key": legacy_agent_key,
                    "owner_key": owner_key,
                    "status": "active",
                    "registered_at": "2026-01-01T00:00:00+00:00",
                    "last_seen": None,
                    "message_count": 0,
                }
            }
        }
    )
    comms = AgentComms()
    comms.registry._state = state
    app.dependency_overrides[get_agent_comms] = lambda: comms
    try:
        foreign = await client.get(
            "/v1/comms/messages/agent-legacy/inbox", headers=other["agent_headers"]
        )
        assert foreign.status_code == 403
        assert foreign.json()["detail"]["error"] == "access_denied"

        own = await client.get(
            "/v1/comms/messages/agent-legacy/inbox", headers=owner["agent_headers"]
        )
        assert own.status_code == 200
    finally:
        app.dependency_overrides.pop(get_agent_comms, None)

    persisted = json.dumps(state.saved["comms.registry"])
    assert owner_key not in persisted
    assert legacy_agent_key not in persisted
    agent = await comms.registry.get("agent-legacy")
    assert agent is not None
    assert owner_key not in agent.owner_key
    assert agent.api_key != legacy_agent_key
