"""Debug test to understand max_calls enforcement."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import PermitModel, ReceiptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from sqlalchemy import select, func
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _register_test_tool(tool_name: str, credits: float = 1.0):
    """Register a simple echo tool for testing."""
    registry = get_service_registry()

    def test_echo(input: str = "ok") -> dict:
        return {"message": input}

    registry.register_local(
        service_id=tool_name,
        name="Test Echo",
        description="Test tool for max_calls debug",
        category=ServiceCategory.AGENT_COMMS,
        func=test_echo,
        credits_per_unit=credits,
        unit_name="call",
    )
    return registry


async def _invoke_governed(
    client: AsyncClient,
    *,
    wallet_id: str,
    permit_id: str,
    tool_name: str,
    arguments: dict | None = None,
    idem_key: str = "invoke-1",
    headers: dict | None = None,
):
    """Invoke a tool under a governed permit via JSON-RPC."""
    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit_id,
                    "idempotency_key": idem_key,
                },
            },
        },
        headers=headers or BOOTSTRAP_HEADERS,
    )
    return resp


@pytest.mark.anyio
async def test_max_calls_sequential(client, clean_database):
    """Test max_calls works with sequential calls (no concurrency)."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "debug-test-echo"

    _register_test_tool(tool_name, credits=1.0)
    try:
        # Create permit with limit of 2 calls
        permit_resp = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": key_id,
                "allowed_tools": [tool_name],
                "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                "max_credits": 50,
                "max_calls_per_tool": {tool_name: 2},
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=30)
                ).isoformat(),
            },
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "debug-permit-1"},
        )
        assert permit_resp.status_code == 201
        permit_id = permit_resp.json()["permit_id"]

        # Call 1
        r1 = await _invoke_governed(
            client,
            wallet_id=wallet_id,
            permit_id=permit_id,
            tool_name=tool_name,
            arguments={"input": "call1"},
            idem_key="debug-1",
            headers=agent_headers,
        )
        body1 = r1.json()
        print(f"\nCall 1: {'SUCCESS' if 'result' in body1 else 'ERROR'}")
        if "error" in body1:
            print(f"  Error: {body1['error'].get('message', 'unknown')}")

        # Check counter after call 1
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(PermitModel, permit_id)
            print(f"  Counter after call 1: {model.tool_call_counts_json}")
            print(f"  Spent credits: {model.spent_credits}")

        # Call 2
        r2 = await _invoke_governed(
            client,
            wallet_id=wallet_id,
            permit_id=permit_id,
            tool_name=tool_name,
            arguments={"input": "call2"},
            idem_key="debug-2",
            headers=agent_headers,
        )
        body2 = r2.json()
        print(f"Call 2: {'SUCCESS' if 'result' in body2 else 'ERROR'}")
        if "error" in body2:
            print(f"  Error: {body2['error'].get('message', 'unknown')}")

        # Check counter after call 2
        async with factory() as session:
            model = await session.get(PermitModel, permit_id)
            print(f"  Counter after call 2: {model.tool_call_counts_json}")
            print(f"  Spent credits: {model.spent_credits}")

        # Call 3 (should fail)
        r3 = await _invoke_governed(
            client,
            wallet_id=wallet_id,
            permit_id=permit_id,
            tool_name=tool_name,
            arguments={"input": "call3"},
            idem_key="debug-3",
            headers=agent_headers,
        )
        body3 = r3.json()
        print(f"Call 3: {'SUCCESS' if 'result' in body3 else 'ERROR'}")
        if "error" in body3:
            print(f"  Error: {body3['error'].get('message', 'unknown')}")

        # Check counter after call 3
        async with factory() as session:
            model = await session.get(PermitModel, permit_id)
            print(f"  Counter after call 3: {model.tool_call_counts_json}")
            print(f"  Spent credits: {model.spent_credits}")

        # Verify receipts
        async with factory() as session:
            result = await session.execute(
                select(func.count()).where(
                    ReceiptModel.permit_id == permit_id,
                    ReceiptModel.tool == tool_name,
                    ReceiptModel.outcome == "success",
                )
            )
            count = int(result.scalar() or 0)
            print(f"\nTotal successful receipts: {count}")

        assert count == 2, f"Expected 2 successful calls, got {count}"
        assert "error" in body3, "Call 3 should have failed"
        assert "permit_max_calls_exceeded" in body3["error"]["message"], (
            f"Wrong error: {body3['error']['message']}"
        )

    finally:
        get_service_registry().unregister_local(tool_name)


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
