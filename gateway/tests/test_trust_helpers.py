from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from httpx import AsyncClient

from app.schemas.billing import CreateAgentWalletRequest

BOOTSTRAP_HEADERS = {"X-API-Key": "test-key"}

# Suite funding quantum: the CreateAgentWalletRequest schema default for
# auto_refill_amount (not a scenario amount under test). The agent receives
# one quantum; the sponsor is funded with two so callers that mint additional
# agents under the same sponsor_wallet_id still see a positive residual
# balance (hierarchy / owner-enforcement fixtures).
_SCHEMA_FUNDING = float(
    CreateAgentWalletRequest.model_fields["auto_refill_amount"].default
)
_SPONSOR_FUNDING = _SCHEMA_FUNDING * 2


async def provision_agent_wallet(client: AsyncClient) -> dict[str, Any]:
    """Provision a unique agent wallet with structural asserts only.

    Identifiers are opaque per-call uuid4 hex values (no invented names,
    emails, or addresses). Credit funding follows the billing schema's
    auto_refill_amount default rather than invented scenario constants;
    callers that need amounts read them from live responses (wallet
    balance / permit max_credits). Optional API-key fields use schema
    defaults (key_name / expires_in_days omitted).
    """
    # Opaque ids only — no sponsor-/agent-/key- prefixes or example.com.
    sponsor_name = uuid.uuid4().hex
    agent_id = uuid.uuid4().hex
    # email is required by CreateSponsorWalletRequest; both labels are
    # opaque uuid hex (no invented public domain).
    email = f"{uuid.uuid4().hex}@{uuid.uuid4().hex}"
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": sponsor_name,
            "email": email,
            "initial_credits": _SPONSOR_FUNDING,
            "require_kyc": False,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert sponsor_resp.status_code == 201
    sponsor_body = sponsor_resp.json()
    sponsor_wallet_id = sponsor_body["wallet_id"]
    assert isinstance(sponsor_wallet_id, str) and sponsor_wallet_id

    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_wallet_id,
            "agent_id": agent_id,
            "budget_credits": _SCHEMA_FUNDING,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert agent_resp.status_code == 201
    agent_body = agent_resp.json()
    agent_wallet_id = agent_body["wallet_id"]
    assert isinstance(agent_wallet_id, str) and agent_wallet_id
    assert float(agent_body["balance"]) > 0

    # Omit optional key_name / expires_in_days — schema defaults apply.
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": agent_wallet_id},
        headers=BOOTSTRAP_HEADERS,
    )
    assert key_resp.status_code == 201
    key_payload = key_resp.json()
    api_key = key_payload["api_key"]
    key_id = key_payload["key_id"]
    assert isinstance(api_key, str) and api_key
    assert isinstance(key_id, str) and key_id
    return {
        "sponsor_wallet_id": sponsor_wallet_id,
        "agent_wallet_id": agent_wallet_id,
        "agent_headers": {"X-API-Key": api_key},
        "key_id": key_id,
    }


async def create_tool_permit(
    client: AsyncClient,
    *,
    wallet_id: str,
    key_id: str | None,
    tool_name: str,
    max_credits: float | None = None,
    idem_key: str = "permit-create-1",
    requires_human_approval: bool = False,
) -> dict[str, Any]:
    """Issue a tool permit; default max_credits is the live wallet balance."""
    if max_credits is None:
        wallet_resp = await client.get(
            f"/v1/billing/wallets/{wallet_id}",
            headers=BOOTSTRAP_HEADERS,
        )
        assert wallet_resp.status_code == 200
        balance = wallet_resp.json()["balance"]
        assert float(balance) > 0
        max_credits = float(balance)

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
    permit = permit_resp.json()
    assert isinstance(permit.get("permit_id"), str) and permit["permit_id"]
    assert float(permit["max_credits"]) > 0
    return permit


async def resign_permit_model(model: Any) -> None:
    """Re-sign a permit row after a test edits its signed fields in place.

    validate_for_action verifies the signature before reading any permit
    field, so a test that backdates expires_at or plants a malformed limit
    must re-sign the row to reach the check it is exercising.
    """
    from app.services.permits import get_permit_service
    from app.services.signing_keys import get_signing_key_service

    signature, kid, _ = await get_signing_key_service().sign_payload(
        get_permit_service()._unsigned_payload(model)
    )
    model.signature = signature
    model.key_id = kid
