"""Regression tests for refunded-receipt accounting evidence.

Every case first proves that the unmodified receipt and its evidence are valid,
then changes exactly one unsigned ledger amount. Receipt/audit signatures must
remain valid while the accounting linkage and both evidence shapes reject the
corrupted record. Only an in-process ASGI client and a failing local tool are used.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, ReceiptModel
from app.main import app
from app.schemas.billing import LedgerAction, ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def _seed_refunded_receipt(
    client: AsyncClient, *, case_name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"probe-refund-{case_name}"
    registry = get_service_registry()

    def failing_tool():
        raise RuntimeError("intentional local refund evidence fixture failure")

    registry.register_local(
        service_id=tool_name,
        name="Refund evidence probe",
        description="Local failure used to create a debit and complete refund",
        category=ServiceCategory.AGENT_COMMS,
        func=failing_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key=f"probe-refund-permit-{case_name}",
        )
        response = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": f"probe-refund-call-{case_name}",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "permit_id": permit["permit_id"],
                        "idempotency_key": f"probe-refund-invoke-{case_name}",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )
        payload = response.json()
        assert "error" in payload, payload
        receipt = payload["error"]["data"]["receipt"]
        assert receipt["outcome"] == "failed_refunded", receipt
        assert Decimal(receipt["credits_charged"]) == Decimal("0")
        assert receipt["ledger_entry_id"] is not None
        return provisioned, receipt
    finally:
        registry.unregister_local(tool_name)


async def _read_both_evidence_shapes(
    client: AsyncClient,
    *,
    receipt_id: str,
    headers: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    detailed_response = await client.get(
        f"/v1/receipts/{receipt_id}/evidence", headers=headers
    )
    flat_response = await client.get(f"/v1/evidence/{receipt_id}", headers=headers)
    assert detailed_response.status_code == 200, detailed_response.text
    assert flat_response.status_code == 200, flat_response.text
    return detailed_response.json(), flat_response.json()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("case_name", "changed_row", "amount_multiplier"),
    [
        pytest.param("partial-refund", "refund", Decimal("0.5"), id="partial-refund"),
        pytest.param("zero-refund", "refund", Decimal("0"), id="zero-refund"),
        pytest.param("excess-refund", "refund", Decimal("2"), id="excess-refund"),
        pytest.param(
            "wrong-sign-refund", "refund", Decimal("-1"), id="wrong-sign-refund"
        ),
        pytest.param("modified-debit", "debit", Decimal("2"), id="modified-debit"),
        pytest.param("zero-debit", "debit", Decimal("0"), id="zero-debit"),
        pytest.param("wrong-sign-debit", "debit", Decimal("-1"), id="wrong-sign-debit"),
    ],
)
async def test_refunded_evidence_rejects_inconsistent_ledger_amounts(
    client: AsyncClient,
    clean_database,
    case_name: str,
    changed_row: str,
    amount_multiplier: Decimal,
) -> None:
    provisioned, receipt = await _seed_refunded_receipt(client, case_name=case_name)
    read_kwargs = {
        "receipt_id": receipt["receipt_id"],
        "headers": provisioned["agent_headers"],
    }
    before_detailed, before_flat = await _read_both_evidence_shapes(
        client, **read_kwargs
    )
    before_checks = {check["name"]: check for check in before_detailed["checks"]}
    assert before_detailed["valid"] is True, before_detailed
    assert before_flat["valid"] is True, before_flat
    assert before_checks["ledger_linkage"]["status"] == "passed"
    assert before_checks["receipt_signature"]["status"] == "passed"
    assert before_checks["audit_chain"]["status"] == "passed"

    factory = get_session_factory()
    async with factory() as session:
        debit = await session.get(LedgerEntryModel, receipt["ledger_entry_id"])
        assert debit is not None
        assert debit.wallet_id == provisioned["agent_wallet_id"]
        assert debit.action == LedgerAction.DEBIT.value
        refund = (
            await session.execute(
                select(LedgerEntryModel).where(
                    LedgerEntryModel.wallet_id == provisioned["agent_wallet_id"],
                    LedgerEntryModel.action == LedgerAction.REFUND.value,
                    LedgerEntryModel.correlation_id == debit.entry_id,
                )
            )
        ).scalar_one()
        assert debit.amount < Decimal("0")
        assert refund.amount > Decimal("0")
        assert debit.amount + refund.amount == Decimal("0")

        changed = refund if changed_row == "refund" else debit
        old_amount = changed.amount
        changed.amount = old_amount * amount_multiplier
        assert changed.amount != old_amount
        assert debit.amount + refund.amount != Decimal("0")
        session.add(changed)
        await session.commit()

    detailed, flat = await _read_both_evidence_shapes(client, **read_kwargs)
    checks = {check["name"]: check for check in detailed["checks"]}
    # Only a financial row changed. These must not fail merely because the
    # test damaged receipt or audit data instead of the intended amount.
    assert detailed["receipt"] == before_detailed["receipt"]
    assert checks["receipt_signature"]["status"] == "passed", checks
    assert checks["audit_chain"]["status"] == "passed", checks
    assert checks["audit_event_linkage"]["status"] == "passed", checks

    accounting_verdict = {
        "case": case_name,
        "ledger_linkage": checks["ledger_linkage"],
        "detailed_valid": detailed["valid"],
        "flat_valid": flat["valid"],
    }
    assert (
        checks["ledger_linkage"]["status"],
        detailed["valid"],
        flat["valid"],
    ) == ("failed", False, False), accounting_verdict


@pytest.mark.anyio
async def test_refunded_receipt_whose_row_claims_a_charge_is_rejected(
    client: AsyncClient,
    clean_database,
) -> None:
    """A stored refunded receipt that claims a net charge fails the linkage.

    ``create_receipt`` refuses to sign one -- ``failed_refunded`` is an
    uncharged outcome -- so this state only arises from tampering with the
    stored row. The accounting linkage has to catch it on its own rather than
    leaning on the signature check, which is what keeps the two independent.
    """
    provisioned, receipt = await _seed_refunded_receipt(client, case_name="net-charge")

    factory = get_session_factory()
    async with factory() as session:
        stored = await session.get(ReceiptModel, receipt["receipt_id"])
        assert stored is not None
        assert stored.credits_charged == Decimal("0")
        stored.credits_charged = Decimal("2")
        session.add(stored)
        await session.commit()

    detailed, flat = await _read_both_evidence_shapes(
        client,
        receipt_id=receipt["receipt_id"],
        headers=provisioned["agent_headers"],
    )
    checks = {check["name"]: check for check in detailed["checks"]}
    linkage = checks["ledger_linkage"]
    assert linkage["status"] == "failed", linkage
    assert "refund_net_charge_mismatch" in (linkage["reason"] or "").split(";"), linkage
    assert detailed["valid"] is False
    assert flat["valid"] is False


@pytest.mark.anyio
async def test_refunded_receipt_with_its_refund_row_deleted_is_rejected(
    client: AsyncClient,
    clean_database,
) -> None:
    """Removing the refund row must fail the linkage rather than pass it."""
    provisioned, receipt = await _seed_refunded_receipt(client, case_name="no-refund")

    factory = get_session_factory()
    async with factory() as session:
        refund = (
            await session.execute(
                select(LedgerEntryModel).where(
                    LedgerEntryModel.wallet_id == provisioned["agent_wallet_id"],
                    LedgerEntryModel.action == LedgerAction.REFUND.value,
                    LedgerEntryModel.correlation_id == receipt["ledger_entry_id"],
                )
            )
        ).scalar_one()
        await session.delete(refund)
        await session.commit()

    detailed, flat = await _read_both_evidence_shapes(
        client,
        receipt_id=receipt["receipt_id"],
        headers=provisioned["agent_headers"],
    )
    checks = {check["name"]: check for check in detailed["checks"]}
    linkage = checks["ledger_linkage"]
    assert linkage["status"] == "failed", linkage
    assert "refund_entry_not_found" in (linkage["reason"] or "").split(";"), linkage
    assert detailed["valid"] is False
    assert flat["valid"] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "multiplier",
    [
        pytest.param(Decimal("500"), id="inflated-pair"),
        pytest.param(Decimal("0.5"), id="deflated-pair"),
        pytest.param(Decimal("0.00000001") / Decimal("2"), id="dust-pair"),
    ],
)
async def test_refunded_evidence_rejects_a_consistently_scaled_ledger_pair(
    client: AsyncClient,
    clean_database,
    multiplier: Decimal,
) -> None:
    """Scaling the debit and its refund together must still fail the linkage.

    The two rows go on offsetting each other exactly, so every check that
    compares them only to each other passes: the receipt still claims no net
    charge, the debit is still negative, and the refund still equals minus the
    debit. Only an anchor to a receipt-signed amount catches it. Without one a
    wallet can be shown as debited 1000 credits, or 0.000000005, for a call the
    receipt authorizes at 2, and the bundle certifies valid.
    """
    provisioned, receipt = await _seed_refunded_receipt(
        client, case_name=f"scaled-{multiplier}"
    )
    read_kwargs = {
        "receipt_id": receipt["receipt_id"],
        "headers": provisioned["agent_headers"],
    }
    before_detailed, before_flat = await _read_both_evidence_shapes(
        client, **read_kwargs
    )
    assert before_detailed["valid"] is True, before_detailed
    assert before_flat["valid"] is True, before_flat

    factory = get_session_factory()
    async with factory() as session:
        debit = await session.get(LedgerEntryModel, receipt["ledger_entry_id"])
        assert debit is not None
        refund = (
            await session.execute(
                select(LedgerEntryModel).where(
                    LedgerEntryModel.wallet_id == provisioned["agent_wallet_id"],
                    LedgerEntryModel.action == LedgerAction.REFUND.value,
                    LedgerEntryModel.correlation_id == debit.entry_id,
                )
            )
        ).scalar_one()
        debit.amount = debit.amount * multiplier
        refund.amount = refund.amount * multiplier
        # The pair still balances; only its magnitude moved.
        assert debit.amount + refund.amount == Decimal("0")
        assert debit.amount != -Decimal(receipt["credits_authorized"])
        session.add(debit)
        session.add(refund)
        await session.commit()

    detailed, flat = await _read_both_evidence_shapes(client, **read_kwargs)
    checks = {check["name"]: check for check in detailed["checks"]}
    # Nothing receipt-side was touched, so the signature checks must still pass
    # and the failure must be attributed to the accounting.
    assert checks["receipt_signature"]["status"] == "passed", checks
    assert checks["audit_chain"]["status"] == "passed", checks
    linkage = checks["ledger_linkage"]
    assert linkage["status"] == "failed", linkage
    assert "ledger_amount_mismatch" in (linkage["reason"] or "").split(";"), linkage
    assert detailed["valid"] is False
    assert flat["valid"] is False
