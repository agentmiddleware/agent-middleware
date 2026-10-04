"""Proof that B2AEdgeClient bypasses the trust-plane loop."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from b2a_sdk.edge_client import B2AEdgeClient
from tests.test_trust_helpers import provision_agent_wallet


@pytest.mark.anyio
async def test_edge_client_call_mcp_tool_double_charges_on_replay(
    clean_database,
) -> None:
    """The edge client's call_mcp_tool bypasses idempotency and double-charges.

    This is the honesty gap: calling call_mcp_tool twice with the same
    arguments dispatches and charges twice, because there is no idempotency
    key or replay protection. The governed flow (invoke_tool with an
    idempotency key) replays without a second charge.
    """
    registry = get_service_registry()
    calls = 0

    def edge_echo(message: str) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"message": message, "call_number": calls}

    registry.register_local(
        service_id="edge-replay-echo",
        name="Edge Replay Echo",
        description="Edge client replay gap test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=edge_echo,
        credits_per_unit=2,
        unit_name="call",
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as raw_client:
            provisioned = await provision_agent_wallet(raw_client)
            wallet_id = provisioned["agent_wallet_id"]
            api_key = provisioned["agent_headers"]["X-API-Key"]

            edge = B2AEdgeClient(
                api_url="http://test",
                api_key=api_key,
                wallet_id=wallet_id,
            )
            edge._client = AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            )

            # Call the same tool twice with the same arguments.
            first = await edge.call_mcp_tool(
                "edge-replay-echo",
                {"message": "hello"},
            )
            second = await edge.call_mcp_tool(
                "edge-replay-echo",
                {"message": "hello"},
            )

            # Fetch the wallet before closing clients.
            wallet_resp = await raw_client.get(
                f"/v1/billing/wallets/{wallet_id}",
                headers=provisioned["agent_headers"],
            )
            wallet = wallet_resp.json()

            await edge.close()

        # The edge client has no idempotency key, so both calls dispatched.
        assert calls == 2, "edge client did not dispatch twice"
        assert (
            first["result"]["content"][0]["text"]
            != second["result"]["content"][0]["text"]
        )

        # Started with 1000 credits, charged 2 credits twice = 4 credits spent.
        assert wallet["balance"] == 996.0, (
            f"balance should be 996 but was {wallet['balance']}"
        )
    finally:
        registry.unregister_local("edge-replay-echo")
