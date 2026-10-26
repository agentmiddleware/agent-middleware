"""SEL-475: a cross-tenant refusal replays with the same error shape.

The request keys guide (agent-middleware PR #209) tells callers to use an LLM
``tool_call.id`` as the caller-owned idempotency key and to keep the key and
the full request stable across retries: once the first call has completed, a
retry with the same key returns the original result instead of dispatching
again. A refusal is a completed call, so retrying a refused cross-tenant call
must give back the same refusal, run nothing, charge nothing, and reveal
nothing about the other tenant.

Tenant A owns a wallet and a permit for a counted tool. Tenant B, using its
own API key, retries one tool call twice with the same ``tool_call.id`` style
key, in two shapes:

* B names A's wallet. The route refuses before any idempotency record exists,
  so the replay is re-evaluated and must be byte-identical.
* B names its own wallet but A's permit. The gateway stores the refusal under
  B's key, so the replay is served from that record. The guide says a replay
  returns the original result: the replay carries the same signed denial
  receipt and ``details`` the first answer did, byte for byte (SEL-482).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, PermitModel, ReceiptModel
from app.main import app
from app.services.receipts import get_receipt_service
from tests.test_mcp_idempotency_key_validation import counted_tool  # noqa: F401
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

pytestmark = pytest.mark.anyio

# Shaped like an LLM tool_call.id, as the request keys guide recommends.
TOOL_CALL_ID = "call_sel475_cross_tenant"
META_KEY = "io.agentmiddleware/idempotency_key"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def tenants(client, clean_database, counted_tool):  # noqa: F811
    tenant_a = await provision_agent_wallet(client)
    tenant_b = await provision_agent_wallet(client)
    permit_a = await create_tool_permit(
        client,
        wallet_id=tenant_a["agent_wallet_id"],
        key_id=tenant_a["key_id"],
        tool_name=counted_tool.name,
    )
    return tenant_a, tenant_b, permit_a


async def _state(client: AsyncClient, tenant: dict, permit_id: str) -> dict:
    """Everything a refused call must leave untouched for one tenant."""
    ledger = await client.get(
        f"/v1/billing/ledger/{tenant['agent_wallet_id']}",
        headers=tenant["agent_headers"],
    )
    assert ledger.status_code == 200, ledger.text
    balance = await client.get(
        f"/v1/billing/wallets/{tenant['agent_wallet_id']}",
        headers=tenant["agent_headers"],
    )
    assert balance.status_code == 200, balance.text
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, permit_id)
    return {
        "ledger": ledger.json()["entries"],
        "wallet": balance.json(),
        "permit_spent": permit.spent_credits,
    }


async def _count(model) -> int:
    async with get_session_factory()() as session:
        return int(
            (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
        )


async def _call_twice(client, tool_name, tenant_b, mcp_context):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"text": "one"},
            "mcpContext": mcp_context,
            "_meta": {META_KEY: TOOL_CALL_ID},
        },
    }
    headers = tenant_b["agent_headers"]
    url = "/mcp/messages"
    first = await client.post(url, json=body, headers=headers)
    replay = await client.post(url, json=body, headers=headers)
    return first, replay


@pytest.mark.parametrize("bad_key", ["   ", 7])
async def test_invalid_meta_key_refuses_before_cross_tenant_call(
    client,
    tenants,
    counted_tool,  # noqa: F811
    bad_key,
):
    _tenant_a, tenant_b, permit_a = tenants
    records_before = await _count(IdempotencyRecordModel)
    receipts_before = await _count(ReceiptModel)
    response = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": counted_tool.name,
                "arguments": {"text": "one"},
                "mcpContext": {
                    "wallet_id": tenant_b["agent_wallet_id"],
                    "permit_id": permit_a["permit_id"],
                },
                "_meta": {META_KEY: bad_key},
            },
        },
        headers=tenant_b["agent_headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["error"]["code"] == -32602
    assert response.json()["error"]["data"]["error"] == "invalid_idempotency_key"
    assert counted_tool.effects == []
    assert await _count(IdempotencyRecordModel) == records_before
    assert await _count(ReceiptModel) == receipts_before


def _assert_refusal_envelope(response, code: str) -> None:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"jsonrpc", "id", "error"}, payload
    assert payload["error"]["code"] == -32003
    assert payload["error"]["message"] == code


async def test_foreign_wallet_refusal_replays_identically(
    client,
    tenants,
    counted_tool,  # noqa: F811
):
    tenant_a, tenant_b, permit_a = tenants
    before_a = await _state(client, tenant_a, permit_a["permit_id"])
    before_b = await _state(client, tenant_b, permit_a["permit_id"])
    records_before = await _count(IdempotencyRecordModel)
    receipts_before = await _count(ReceiptModel)

    first, replay = await _call_twice(
        client,
        counted_tool.name,
        tenant_b,
        {"wallet_id": tenant_a["agent_wallet_id"], "permit_id": permit_a["permit_id"]},
    )

    _assert_refusal_envelope(first, "wallet_access_denied")
    assert "receipt" not in first.json()["error"].get("data", {})
    # Same status, same headers that matter, same bytes.
    assert replay.status_code == first.status_code
    assert replay.headers["content-type"] == first.headers["content-type"]
    assert replay.content == first.content

    # The refusal names no wallet, permit, balance, or receipt of tenant A.
    for response in (first, replay):
        assert tenant_a["agent_wallet_id"] not in response.text
        assert tenant_a["sponsor_wallet_id"] not in response.text
        assert "receipt" not in response.json()["error"].get("data", {})

    # Refused at the route: nothing ran, nothing was stored or charged.
    assert counted_tool.effects == []
    assert await _count(IdempotencyRecordModel) == records_before
    assert await _count(ReceiptModel) == receipts_before
    assert await _state(client, tenant_a, permit_a["permit_id"]) == before_a
    assert await _state(client, tenant_b, permit_a["permit_id"]) == before_b


async def test_foreign_permit_refusal_replay_has_no_side_effects(
    client,
    tenants,
    counted_tool,  # noqa: F811
):
    """Behavior that holds today for the stored-refusal case."""
    tenant_a, tenant_b, permit_a = tenants
    before_a = await _state(client, tenant_a, permit_a["permit_id"])
    before_b = await _state(client, tenant_b, permit_a["permit_id"])
    records_before = await _count(IdempotencyRecordModel)
    receipts_before = await _count(ReceiptModel)

    first, replay = await _call_twice(
        client,
        counted_tool.name,
        tenant_b,
        {"wallet_id": tenant_b["agent_wallet_id"], "permit_id": permit_a["permit_id"]},
    )

    _assert_refusal_envelope(first, "permit_wallet_mismatch")
    _assert_refusal_envelope(replay, "permit_wallet_mismatch")
    assert replay.json()["error"] == first.json()["error"]

    receipt = first.json()["error"]["data"]["receipt"]
    assert receipt["outcome"] == "denied"
    assert receipt["wallet_id"] == tenant_b["agent_wallet_id"]
    assert receipt["credits_charged"] == "0"
    assert receipt["ledger_entry_id"] is None

    # The denial receipt is durable, belongs to tenant B, names only B's
    # wallet and key, charges nothing, and still verifies against its
    # signature.
    async with get_session_factory()() as session:
        durable = await session.get(ReceiptModel, receipt["receipt_id"])
    assert durable is not None
    assert durable.wallet_id == tenant_b["agent_wallet_id"]
    assert durable.key_id == tenant_b["key_id"]
    assert durable.wallet_id != tenant_a["agent_wallet_id"]
    assert durable.permit_id == permit_a["permit_id"]
    assert durable.tool == counted_tool.name
    assert durable.outcome == "denied"
    assert durable.credits_charged == 0
    assert durable.ledger_entry_id is None
    valid, reason, verified = await get_receipt_service().verify_receipt(
        receipt["receipt_id"]
    )
    assert (valid, reason) == (True, None), reason
    assert verified is not None
    assert verified.receipt_id == receipt["receipt_id"]
    assert verified.wallet_id == tenant_b["agent_wallet_id"]
    assert verified.outcome == "denied"

    # The replay was served from the one stored refusal under B's wallet.
    assert await _count(IdempotencyRecordModel) == records_before + 1
    assert await _count(ReceiptModel) == receipts_before + 1
    async with get_session_factory()() as session:
        stored = (
            (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.idempotency_key == TOOL_CALL_ID
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [(r.wallet_id, r.status_code) for r in stored] == [
        (tenant_b["agent_wallet_id"], 403)
    ]

    for response in (first, replay):
        assert tenant_a["agent_wallet_id"] not in response.text
        assert tenant_a["sponsor_wallet_id"] not in response.text
    assert counted_tool.effects == []
    assert await _state(client, tenant_a, permit_a["permit_id"]) == before_a
    assert await _state(client, tenant_b, permit_a["permit_id"]) == before_b


async def test_foreign_permit_refusal_replays_identically_per_docs(
    client,
    tenants,
    counted_tool,  # noqa: F811
):
    """Documented behavior: the replay is the original refusal, byte for byte."""
    _tenant_a, tenant_b, permit_a = tenants
    first, replay = await _call_twice(
        client,
        counted_tool.name,
        tenant_b,
        {"wallet_id": tenant_b["agent_wallet_id"], "permit_id": permit_a["permit_id"]},
    )
    assert first.status_code == 200, first.text
    assert replay.status_code == first.status_code
    assert set(replay.json()["error"]["data"]) == set(first.json()["error"]["data"])
    assert replay.content == first.content
    assert counted_tool.effects == []
