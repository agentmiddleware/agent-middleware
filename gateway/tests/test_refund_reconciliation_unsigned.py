"""A pending refund is recorded even when its receipt cannot be signed yet.

The refund-failed path runs after the tool ran and the wallet was charged. Its
``failed_unrefunded`` receipt and the operator work item used to be written in
one transaction that needed a usable signing key. If the key became unusable
after the call was admitted (disabled, or retired once a retired key refuses
to sign), ``create_pending`` raised a ``SigningKeyError`` that nothing
classified: the caller got ``internal_error`` and neither the receipt nor the
work item was written, so the debt was recorded nowhere (issue #280).

``create_pending`` now records the work item without a receipt in that case,
with an ``unsigned_receipt`` marker holding the receipt inputs, and
``sign_unsigned_pending`` signs the receipt from a replica whose key can sign.
These tests pin both halves and that the item then flows through the normal
operator path.
"""

from __future__ import annotations

from decimal import Decimal
import json
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.database import get_session_factory
from app.db.models import (
    IdempotencyRecordModel,
    LedgerEntryModel,
    ReceiptModel,
    SigningKeyModel,
)
from app.main import app
from app.routers.mcp import INTERNAL_ERROR_MESSAGE
from app.schemas.billing import ServiceCategory
from app.services.refund_reconciliation import (
    UNSIGNED_RECEIPT_KEY,
    get_refund_reconciliation_service,
)
from app.services.service_registry import get_service_registry
from app.services.signing_keys import get_signing_key_service
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

TOOL_COST = 2.0
TOOL_NAME = "refund-unsigned-tool"


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def _set_key_status(key_id: str, status: str) -> None:
    async with get_session_factory()() as session:
        row = await session.get(SigningKeyModel, key_id)
        assert row is not None
        row.status = status
        session.add(row)
        await session.commit()


@pytest.fixture
def key_lost_mid_call_tool():
    """A tool that, while it runs, loses this replica its signing key.

    It stands in for an operator disabling (or, once retired keys refuse,
    retiring) the key after the call was admitted and charged. Then the tool
    fails, so the call goes down the refund path with no usable key.
    """
    state: dict[str, Any] = {"runs": 0, "key_id": None}

    async def run() -> dict[str, Any]:
        state["runs"] += 1
        await _set_key_status(state["key_id"], "disabled")
        raise RuntimeError("tool exploded")

    get_service_registry().register_local(
        service_id=TOOL_NAME,
        name="Refund unsigned tool",
        description="Pending refund without a signable receipt test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=run,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        yield state
    finally:
        get_service_registry().unregister_local(TOOL_NAME)


async def _failing_refund(self, **_kwargs):  # noqa: ANN001
    raise RuntimeError("refund down")


async def _record_for(key: str) -> IdempotencyRecordModel:
    async with get_session_factory()() as session:
        record = (
            await session.execute(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.idempotency_key == key
                )
            )
        ).scalar_one()
        return record


async def _count(model: Any) -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _run_call_that_loses_its_key(
    client: AsyncClient, state: dict[str, Any], *, key: str
) -> dict[str, Any]:
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=TOOL_NAME,
        max_credits=10,
        idem_key=f"{key}-permit",
    )
    signing_key = await get_signing_key_service().ensure_active_key()
    state["key_id"] = signing_key.key_id
    body = {
        "jsonrpc": "2.0",
        "id": f"call-{key}",
        "method": "tools/call",
        "params": {
            "name": TOOL_NAME,
            "arguments": {},
            "mcpContext": {
                "wallet_id": ctx["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": key,
            },
        },
    }
    with patch("app.services.agent_money.AgentMoney.refund_charge", _failing_refund):
        resp = await client.post(
            "/mcp/messages", json=body, headers=ctx["agent_headers"]
        )
    return {"ctx": ctx, "permit": permit, "body": body, "resp": resp}


@pytest.mark.anyio
async def test_a_refund_whose_receipt_cannot_be_signed_is_still_recorded(
    client: AsyncClient, clean_database: None, key_lost_mid_call_tool
) -> None:
    receipts_before = await _count(ReceiptModel)
    case = await _run_call_that_loses_its_key(
        client, key_lost_mid_call_tool, key="refund-unsigned-1"
    )
    resp = case["resp"]

    assert key_lost_mid_call_tool["runs"] == 1
    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    # Not the unclassified answer: the reason and the debt are on the wire.
    assert error["message"] != INTERNAL_ERROR_MESSAGE, error
    reconciliation = error["data"]["refund_reconciliation"]
    assert reconciliation["status"] == "pending", reconciliation
    assert reconciliation["receipt_id"] is None, reconciliation
    assert reconciliation["receipt_status"] == "unsigned", reconciliation
    assert Decimal(reconciliation["credits"]) == Decimal("2")
    # Internal receipt inputs never reach the caller.
    assert UNSIGNED_RECEIPT_KEY not in json.dumps(resp.json())

    # The debt is durable: the record is completed with the work item and the
    # marker, and no receipt was written.
    assert await _count(ReceiptModel) == receipts_before
    record = await _record_for("refund-unsigned-1")
    assert record.response_reference is None
    assert record.status_code == 500
    stored = json.loads(record.response_json or "{}")
    assert stored["refund_reconciliation"]["status"] == "pending"
    marker = stored[UNSIGNED_RECEIPT_KEY]
    assert marker["signing_error"] == "signing_key_disabled"
    assert marker["inputs"]["ledger_entry_id"] == record.ledger_entry_id

    # The same key replays the same pending answer and never runs the tool.
    # (The key is usable again first: a disabled key also fails the permit's
    # signature check, which runs before the replay is served.)
    await _set_key_status(key_lost_mid_call_tool["key_id"], "active")
    with patch("app.services.agent_money.AgentMoney.refund_charge", _failing_refund):
        replay = await client.post(
            "/mcp/messages", json=case["body"], headers=case["ctx"]["agent_headers"]
        )
    assert key_lost_mid_call_tool["runs"] == 1
    replayed = replay.json()["error"]["data"]["refund_reconciliation"]
    assert replayed["status"] == "pending"
    assert replayed["receipt_id"] is None


@pytest.mark.anyio
async def test_a_replica_that_cannot_sign_leaves_the_item_unsigned(
    client: AsyncClient, clean_database: None, key_lost_mid_call_tool
) -> None:
    await _run_call_that_loses_its_key(
        client, key_lost_mid_call_tool, key="refund-unsigned-2"
    )
    service = get_refund_reconciliation_service()

    signed, still_unsigned = await service.sign_unsigned_pending()

    assert (signed, still_unsigned) == (0, 1)
    record = await _record_for("refund-unsigned-2")
    assert record.response_reference is None
    assert UNSIGNED_RECEIPT_KEY in json.loads(record.response_json or "{}")


@pytest.mark.anyio
async def test_a_replica_that_can_sign_completes_the_item_and_it_can_be_refunded(
    client: AsyncClient, clean_database: None, key_lost_mid_call_tool
) -> None:
    case = await _run_call_that_loses_its_key(
        client, key_lost_mid_call_tool, key="refund-unsigned-3"
    )
    wallet_id = case["ctx"]["agent_wallet_id"]
    service = get_refund_reconciliation_service()
    # The key is usable again (for example the replica was redeployed).
    await _set_key_status(key_lost_mid_call_tool["key_id"], "active")

    signed, still_unsigned = await service.sign_unsigned_pending()
    assert (signed, still_unsigned) == (1, 0)

    record = await _record_for("refund-unsigned-3")
    assert record.response_reference is not None
    stored = json.loads(record.response_json or "{}")
    assert UNSIGNED_RECEIPT_KEY not in stored
    assert stored["receipt"]["outcome"] == "failed_unrefunded"
    assert stored["refund_reconciliation"]["receipt_id"] == record.response_reference

    async with get_session_factory()() as session:
        receipt = await session.get(ReceiptModel, record.response_reference)
        assert receipt is not None
        assert receipt.outcome == "failed_unrefunded"
        assert receipt.ledger_entry_id == record.ledger_entry_id
        assert receipt.idempotency_record_id == record.record_id
        assert receipt.credits_charged == Decimal("2")

    # A second pass finds nothing left to sign.
    assert await service.sign_unsigned_pending() == (0, 0)

    # The signed item is now an ordinary pending work item for the operator.
    items, _total = await service.list_items(status="pending", wallet_id=wallet_id)
    assert [item.receipt_id for item in items] == [record.response_reference]
    assert await get_receipt_service_verifies(record.response_reference)

    # And the operator retry refunds it exactly once.
    item, already = await service.retry(record.response_reference)
    assert item.status == "resolved"
    assert already is False
    async with get_session_factory()() as session:
        refunds = int(
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(
                    LedgerEntryModel.wallet_id == wallet_id,
                    LedgerEntryModel.action == "refund",
                )
            )
            or 0
        )
    assert refunds == 1


async def get_receipt_service_verifies(receipt_id: str) -> bool:
    from app.services.receipts import get_receipt_service

    async with get_session_factory()() as session:
        receipt = await session.get(ReceiptModel, receipt_id)
        assert receipt is not None
        return await get_receipt_service().verify_model(receipt, session=session)
