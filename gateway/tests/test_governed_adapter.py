from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.auth import get_auth_context
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.service_registry import get_service_registry
from app.trust.adapters import GovernedRequest, McpGovernedAdapter
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_mcp_adapter_runs_governed_pipeline_and_charges_once(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    api_key = provisioned["agent_headers"]["X-API-Key"]
    registry = get_service_registry()

    registry.register_local(
        service_id="adapter-echo",
        name="Adapter Echo",
        description="Governed adapter test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": {"message": message},
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=wallet_id,
            key_id=provisioned["key_id"],
            tool_name="adapter-echo",
        )

        # Authenticate the same way the HTTP layer does so the adapter receives
        # a real AuthContext rather than a stub.
        auth = await get_auth_context(api_key=api_key)
        money = get_agent_money()
        adapter = McpGovernedAdapter()

        raw = {
            "params": {
                "name": "adapter-echo",
                "arguments": {"message": "hi"},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "adapter-invoke-1",
                },
            }
        }

        request = await adapter.normalize_request(raw, auth=auth, money=money)
        assert isinstance(request, GovernedRequest)
        assert request.protocol == "mcp"
        assert request.tool_name == "adapter-echo"
        assert request.permit_id == permit["permit_id"]

        result = await adapter.invoke(request)
        assert result.is_error is False
        assert result.outcome == "success"
        assert result.receipt is not None
        assert result.receipt["permit_id"] == permit["permit_id"]
        assert result.ledger_entry_id

        normalized = await adapter.normalize_response(result)
        assert normalized["receipt"]["receipt_id"] == result.receipt["receipt_id"]

        # The receipt the adapter produced must verify through the public API,
        # i.e. it is a real governed receipt, not a synthetic one.
        verify = await client.post(
            "/v1/receipts/verify",
            json={"receipt_id": result.receipt["receipt_id"]},
            headers=provisioned["agent_headers"],
        )
        assert verify.status_code == 200
        assert verify.json()["valid"] is True

        ledger = await client.get(
            f"/v1/billing/ledger/{wallet_id}",
            headers=provisioned["agent_headers"],
        )
        debits = [
            entry
            for entry in ledger.json()["entries"]
            if entry["service_category"] == "agent_comms"
            and "adapter-echo" in entry["description"]
        ]
        assert len(debits) == 1
    finally:
        registry.unregister_local("adapter-echo")
