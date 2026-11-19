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
    state: dict[str, Any] = {"runs": 0, "key_id": None, "lose_key": True}

    async def run() -> dict[str, Any]:
        state["runs"] += 1
        if state["lose_key"]:
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


@pytest.mark.anyio
async def test_a_tampered_stored_payload_is_never_signed(
    client: AsyncClient, clean_database: None, key_lost_mid_call_tool
) -> None:
    """The signer refuses a payload that no longer hashes to the caller's response.

    The unsigned item keeps the response payload and, separately, its hash as
    recorded when the call failed. If the stored payload changes afterwards (a
    JSON round trip that alters a value, or an edit), signing it would put the
    gateway's signature on a response the caller never got. The item stays
    unsigned and the record is left exactly as it was.
    """
    await _run_call_that_loses_its_key(
        client, key_lost_mid_call_tool, key="refund-unsigned-tampered"
    )
    record = await _record_for("refund-unsigned-tampered")
    stored = json.loads(record.response_json or "{}")
    # The injected fault: change the stored payload, not the recorded hash.
    stored[UNSIGNED_RECEIPT_KEY]["inputs"]["response_payload"] = {
        "error": "not what the caller was told"
    }
    tampered_json = json.dumps(stored)
    async with get_session_factory()() as session:
        row = await session.get(IdempotencyRecordModel, record.record_id)
        assert row is not None
        row.response_json = tampered_json
        session.add(row)
        await session.commit()
    receipts_before = await _count(ReceiptModel)
    # A key that can sign, so the hash check is the only thing that refuses.
    await _set_key_status(key_lost_mid_call_tool["key_id"], "active")

    (
        signed,
        still_unsigned,
    ) = await get_refund_reconciliation_service().sign_unsigned_pending()

    assert (signed, still_unsigned) == (0, 1)
    assert await _count(ReceiptModel) == receipts_before
    after = await _record_for("refund-unsigned-tampered")
    assert after.response_reference is None
    assert after.response_json == tampered_json
    assert after.status_code == record.status_code


@pytest.mark.anyio
async def test_a_receipt_signed_later_carries_the_fields_the_signed_path_writes(
    client: AsyncClient, clean_database: None, key_lost_mid_call_tool
) -> None:
    """Signing later must not lose or change what the receipt says about the call.

    Two calls on the same wallet, permit and tool, both ending refund-failed.
    The first keeps its key and gets its ``failed_unrefunded`` receipt on the
    spot. The second loses its key, is recorded unsigned, and is signed by
    ``sign_unsigned_pending``. Every receipt column that describes the call
    is compared; only the columns that identify the individual call differ.
    """
    state = key_lost_mid_call_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=TOOL_NAME,
        max_credits=10,
        idem_key="refund-unsigned-fields-permit",
    )
    state["key_id"] = (await get_signing_key_service().ensure_active_key()).key_id

    async def call(key: str) -> IdempotencyRecordModel:
        body = {
            "jsonrpc": "2.0",
            "id": "call-fields",
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
        with patch(
            "app.services.agent_money.AgentMoney.refund_charge", _failing_refund
        ):
            resp = await client.post(
                "/mcp/messages", json=body, headers=ctx["agent_headers"]
            )
        assert resp.json()["error"]["data"]["refund_reconciliation"]["status"] == (
            "pending"
        ), resp.text
        return await _record_for(key)

    state["lose_key"] = False
    signed_record = await call("refund-unsigned-fields-signed")
    assert signed_record.response_reference is not None

    state["lose_key"] = True
    unsigned_record = await call("refund-unsigned-fields-later")
    assert unsigned_record.response_reference is None
    await _set_key_status(state["key_id"], "active")
    assert await get_refund_reconciliation_service().sign_unsigned_pending() == (1, 0)
    later_record = await _record_for("refund-unsigned-fields-later")
    assert later_record.response_reference is not None

    async with get_session_factory()() as session:
        on_the_spot = await session.get(ReceiptModel, signed_record.response_reference)
        later = await session.get(ReceiptModel, later_record.response_reference)
    assert on_the_spot is not None and later is not None

    # Columns that name the individual call, checked against that call.
    per_call = {
        "receipt_id",
        "idempotency_record_id",
        "ledger_entry_id",
        "audit_event_id",
        "request_hash",
        "response_hash",
        "created_at",
        "signature",
    }
    same = set(ReceiptModel.model_fields) - per_call
    assert {
        "tool",
        "key_id",
        "counterparty",
        "dispatch_attempt_id",
        "approval_id",
        "permit_id",
        "wallet_id",
        "outcome",
        "reason_code",
        "credits_authorized",
        "credits_charged",
    } <= same
    differing = {
        name: (getattr(on_the_spot, name), getattr(later, name))
        for name in sorted(same)
        if getattr(on_the_spot, name) != getattr(later, name)
    }
    assert differing == {}
    assert later.tool == TOOL_NAME
    assert later.key_id == ctx["key_id"]
    assert later.idempotency_record_id == later_record.record_id
    assert later.ledger_entry_id == later_record.ledger_entry_id
    assert later.request_hash == later_record.request_hash
    # Known limitation: the key was
    # lost before the pending audit event could be signed, so the later
    # receipt links no audit event. The on-the-spot receipt links its own.
    assert on_the_spot.audit_event_id is not None
    assert later.audit_event_id is None


@pytest.mark.anyio
async def test_the_periodic_cleanup_runs_the_unsigned_signer(
    clean_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sign_unsigned_pending`` only helps if the cleanup loop calls it.

    Runs the application lifespan with the loop's 300 second wait shortened to
    nothing for one pass, and observes the signer being called. The signer is
    replaced at class level so the pass itself is the thing under test.
    """
    import asyncio

    import app.main as main_module
    from app.services.refund_reconciliation import RefundReconciliationService

    called = asyncio.Event()
    real_sleep = asyncio.sleep
    long_waits = 0

    async def one_pass_sleep(delay: float, result: Any = None) -> Any:
        nonlocal long_waits
        if delay >= 60:
            long_waits += 1
            if long_waits > 1:
                # Second wait of the loop: block until shutdown cancels it.
                await asyncio.Event().wait()
            return result
        return await real_sleep(delay, result)

    async def observed(self: RefundReconciliationService, **_kwargs: Any):
        called.set()
        return (0, 0)

    async def no_upstream(settings: Any = None) -> None:
        return None

    monkeypatch.setattr(main_module.asyncio, "sleep", one_pass_sleep)
    monkeypatch.setattr(RefundReconciliationService, "sign_unsigned_pending", observed)
    monkeypatch.setattr(
        "app.services.upstream_mcp.register_configured_upstream_mcp", no_upstream
    )

    async with main_module.lifespan(main_module.app):
        await asyncio.wait_for(called.wait(), timeout=10)


@pytest.mark.anyio
async def test_an_item_that_can_never_be_signed_does_not_block_newer_items(
    client: AsyncClient, clean_database: None, key_lost_mid_call_tool
) -> None:
    """The scan moves past an item it refuses, within the same pass.

    The oldest unsigned item is made permanently unsignable (its stored
    payload no longer matches its recorded hash). A newer item is signable.
    With a page size of one, a scan that re-selected the oldest items on every
    pass would never reach the newer one, which would then stay invisible to
    the operator refund list for good.
    """
    await _run_call_that_loses_its_key(
        client, key_lost_mid_call_tool, key="refund-unsigned-blocked-old"
    )
    old = await _record_for("refund-unsigned-blocked-old")
    stored = json.loads(old.response_json or "{}")
    stored[UNSIGNED_RECEIPT_KEY]["inputs"]["response_payload"] = {
        "error": "not what the caller was told"
    }
    async with get_session_factory()() as session:
        row = await session.get(IdempotencyRecordModel, old.record_id)
        assert row is not None
        row.response_json = json.dumps(stored)
        session.add(row)
        await session.commit()

    # The first call disabled the key; the second call needs it to be admitted.
    await _set_key_status(key_lost_mid_call_tool["key_id"], "active")
    await _run_call_that_loses_its_key(
        client, key_lost_mid_call_tool, key="refund-unsigned-blocked-new"
    )
    new = await _record_for("refund-unsigned-blocked-new")
    assert (old.created_at, old.record_id) < (new.created_at, new.record_id)
    await _set_key_status(key_lost_mid_call_tool["key_id"], "active")

    service = get_refund_reconciliation_service()
    assert await service.sign_unsigned_pending(limit=1) == (1, 1)

    assert (await _record_for("refund-unsigned-blocked-old")).response_reference is None
    signed = await _record_for("refund-unsigned-blocked-new")
    assert signed.response_reference is not None
    assert UNSIGNED_RECEIPT_KEY not in json.loads(signed.response_json or "{}")
    # The blocked item is still counted on every later pass, never signed.
    assert await service.sign_unsigned_pending(limit=1) == (0, 1)
