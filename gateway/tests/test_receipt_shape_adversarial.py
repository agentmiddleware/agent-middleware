from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.idempotency import get_idempotency_service
from app.services.receipts import ReceiptError, get_receipt_service
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _setup(client, tag: str):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=f"receipt-shape-{tag}",
        idem_key=f"receipt-shape-permit-{tag}",
    )
    return provisioned, permit


def _kwargs(provisioned, permit, tag: str, **overrides):
    base = {
        "permit_id": permit["permit_id"],
        "wallet_id": provisioned["agent_wallet_id"],
        "key_id": provisioned["key_id"],
        "tool": f"receipt-shape-{tag}",
        "request_payload": {"attempt": tag},
        "response_payload": {"ok": True},
        "ledger_entry_id": None,
        "credits_authorized": Decimal("1"),
        "credits_charged": Decimal("0"),
        "outcome": "success",
        "audit_event_id": None,
    }
    base.update(overrides)
    return base


@pytest.mark.anyio
async def test_receipt_rejects_unknown_outcome(client, clean_database):
    """Only outcomes with a defined consumer may be signed into a receipt."""
    provisioned, permit = await _setup(client, "outcome")
    service = get_receipt_service()
    for bogus in ["bogus", "SUCCESS", "success ", "", "denied;DROP", "x" * 33]:
        with pytest.raises(ReceiptError, match="receipt_outcome_invalid"):
            await service.create_receipt(
                **_kwargs(
                    provisioned,
                    permit,
                    f"outcome-{len(bogus)}",
                    request_payload={"attempt": bogus},
                    outcome=bogus,
                )
            )
    for valid in (
        "success",
        "denied",
        "insufficient_funds",
        "failed_refunded",
        "failed_unrefunded",
        "delivery_uncertain",
        "response_rejected",
    ):
        receipt = await service.create_receipt(
            **_kwargs(
                provisioned,
                permit,
                f"valid-{valid}",
                request_payload={"attempt": valid},
                outcome=valid,
                reason_code=None if valid == "success" else "permit_denied",
            )
        )
        assert receipt.outcome == valid


@pytest.mark.anyio
async def test_receipt_denied_with_charge_rejected(client, clean_database):
    """A refused call moves no money, so a denied receipt must charge zero."""
    provisioned, permit = await _setup(client, "deniedcharge")
    service = get_receipt_service()
    for outcome in ("denied", "insufficient_funds"):
        with pytest.raises(ReceiptError, match="receipt_denied_charge_invalid"):
            await service.create_receipt(
                **_kwargs(
                    provisioned,
                    permit,
                    f"charged-{outcome}",
                    outcome=outcome,
                    credits_charged=Decimal("5"),
                    reason_code="permit_budget_exceeded",
                )
            )
    charged_failure = await service.create_receipt(
        **_kwargs(
            provisioned,
            permit,
            "charged-failed-unrefunded",
            outcome="failed_unrefunded",
            credits_charged=Decimal("5"),
            reason_code="upstream_error",
        )
    )
    assert charged_failure.credits_charged == Decimal("5")
    denied = await service.create_receipt(
        **_kwargs(
            provisioned,
            permit,
            "clean-denied",
            outcome="denied",
            reason_code="permit_budget_exceeded",
        )
    )
    assert denied.credits_charged == Decimal("0")
    assert (await service.verify_receipt(denied.receipt_id))[0] is True


@pytest.mark.anyio
async def test_receipt_idempotency_conflict_on_constraints_divergence(
    client, clean_database
):
    """Reusing one idempotency record for different evidence must conflict."""
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="receipt-shape-const",
        idem_key="receipt-shape-permit-const",
    )
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint="/mcp/messages",
        idempotency_key="receipt-shape-const-invoke",
        request_payload={"a": 1},
    )
    assert begun.replay is None
    service = get_receipt_service()
    kwargs = {
        "permit_id": permit["permit_id"],
        "wallet_id": provisioned["agent_wallet_id"],
        "key_id": provisioned["key_id"],
        "tool": "receipt-shape-const",
        "request_payload": {"a": 1},
        "response_payload": {"ok": True},
        "ledger_entry_id": None,
        "credits_authorized": Decimal("1"),
        "credits_charged": Decimal("1"),
        "outcome": "success",
        "audit_event_id": None,
        "idempotency_record_id": begun.record_id,
    }
    first = await service.create_receipt(**kwargs, constraints_evaluated={"a": 1})
    replay = await service.create_receipt(**kwargs, constraints_evaluated={"a": 1})
    assert replay.receipt_id == first.receipt_id
    with pytest.raises(ReceiptError, match="receipt_idempotency_conflict"):
        await service.create_receipt(**kwargs, constraints_evaluated={"a": 2})
