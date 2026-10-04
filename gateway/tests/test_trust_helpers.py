from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from httpx import AsyncClient


BOOTSTRAP_HEADERS = {"X-API-Key": "test-key"}


async def provision_agent_wallet(client: AsyncClient) -> dict[str, Any]:
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Trust Sponsor",
            "email": "trust@example.com",
            "initial_credits": 10000,
            "require_kyc": False,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert sponsor_resp.status_code == 201
    sponsor_wallet_id = sponsor_resp.json()["wallet_id"]

    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_wallet_id,
            "agent_id": "trust-agent",
            "budget_credits": 1000,
            "daily_limit": 500,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert agent_resp.status_code == 201
    agent_wallet_id = agent_resp.json()["wallet_id"]

    key_resp = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": agent_wallet_id,
            "key_name": "trust-runtime",
            "expires_in_days": 30,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert key_resp.status_code == 201
    key_payload = key_resp.json()
    return {
        "sponsor_wallet_id": sponsor_wallet_id,
        "agent_wallet_id": agent_wallet_id,
        "agent_headers": {"X-API-Key": key_payload["api_key"]},
        "key_id": key_payload["key_id"],
    }


async def create_tool_permit(
    client: AsyncClient,
    *,
    wallet_id: str,
    key_id: str | None,
    tool_name: str,
    max_credits: int = 50,
    idem_key: str = "permit-create-1",
    requires_human_approval: bool = False,
) -> dict[str, Any]:
    permit_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": key_id,
            "allowed_tools": [tool_name],
            "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
            "max_credits": max_credits,
            "requires_human_approval": requires_human_approval,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": idem_key},
    )
    assert permit_resp.status_code == 201
    return permit_resp.json()
