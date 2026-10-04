"""Test permit caching to prevent 409 IdempotencyConflictError on replay."""

from datetime import UTC, datetime

import httpx
import pytest

from crewai_b2a import B2AClient, CrewAIB2ATool


def _permit_payload() -> dict:
    return {
        "permit_id": "permit-1",
        "issuer_wallet_id": "wallet-1",
        "subject_wallet_id": "wallet-1",
        "subject_key_id": "key-1",
        "scopes": ["tool:partner.search:invoke", "billing:charge"],
        "allowed_tools": ["partner.search"],
        "max_credits": "100",
        "spent_credits": "0",
        "expires_at": datetime.now(UTC).isoformat(),
        "nonce": "nonce-1",
        "status": "active",
        "signature": "sig-permit-1",
        "key_id": "key-1",
        "issued_at": datetime.now(UTC).isoformat(),
        "revoked_at": None,
    }


def _receipt_payload(outcome: str = "success") -> dict:
    # Mirrors the receipt shape b2a_sdk.models.Receipt.from_dict requires
    # (see b2a_sdk/tests/test_trust_client.py).
    return {
        "receipt_id": f"receipt-{outcome}",
        "permit_id": "permit-1",
        "wallet_id": "wallet-1",
        "key_id": "key-1",
        "tool": "partner.search",
        "request_hash": "request-hash",
        "response_hash": "response-hash",
        "ledger_entry_id": "ledger-1",
        "dispatch_attempt_id": "dispatch-1",
        "credits_authorized": "2",
        "credits_charged": "2",
        "outcome": outcome,
        "audit_event_id": "audit-1",
        "created_at": datetime.now(UTC).isoformat(),
        "signature": f"sig-{outcome}",
        "signature_key_id": "signing-key-1",
    }


@pytest.mark.asyncio
async def test_permit_cache_prevents_duplicate_create_permit():
    """Test that replaying with same permit_idempotency_key reuses cached permit_id.

    This test verifies the 409 fix: server hashes the FULL permit request body
    including expires_at. Without caching, two calls with the same permit_idempotency_key
    but different expires_at would cause 409 IdempotencyConflictError.

    The fix: cache the permit_id and skip create_permit on replay.
    """
    permit_create_count = {"count": 0}
    invoke_count = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            permit_create_count["count"] += 1
            return httpx.Response(201, json=_permit_payload())

        if request.url.path == "/mcp/messages":
            invoke_count["count"] += 1
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "cache-test",
                    "result": {
                        "content": [{"type": "text", "text": '{"ok": true}'}],
                        "structuredContent": {"ok": True},
                        "isError": False,
                        "receipt": _receipt_payload(),
                    },
                },
            )

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    base_client = B2AClient(api_key="test-key", transport=transport)
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    tool.client = base_client

    # First call: creates permit
    result1 = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="invoke-1",
        permit_idempotency_key="permit-cache-test",
        arguments={},
    )

    # Replay: reuses cached permit (does NOT call create_permit again)
    result2 = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="invoke-2",  # different invoke key
        permit_idempotency_key="permit-cache-test",  # SAME permit key
        arguments={},
    )

    # Verify: create_permit called only ONCE (cached on second call)
    assert permit_create_count["count"] == 1, (
        "create_permit should be called once and cached"
    )
    # Verify: invoke_tool called TWICE (different invoke keys)
    assert invoke_count["count"] == 2, "invoke_tool should be called twice"

    assert "receipt-success" in result1
    assert "receipt-success" in result2

    await base_client.close()
