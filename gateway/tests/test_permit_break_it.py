"""Break-it probes for gateway permit reservation and denied receipts.

The tests in this file are the attacks: a negative or non-finite amount,
a release that would wipe the counter, a cap that forgets a receipt once
the reservation is handed back, an expired permit, and a denied receipt
whose stored outcome or charge is edited after signing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import PermitModel, ReceiptModel
from app.main import app
from app.schemas.trust import PermitCreateRequest
from app.services.ownership import CallerIdentity
from app.services.permits import PermitError, get_permit_service
from app.services.receipts import get_receipt_service
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _live_permit(
    client: AsyncClient,
    *,
    tool_name: str,
    max_credits: str = "100",
    aggregate_value_cap: str | None = None,
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    request = PermitCreateRequest(
        issuer_wallet_id=wallet_id,
        subject_wallet_id=wallet_id,
        subject_key_id=key_id,
        allowed_tools=[tool_name],
        scopes=[f"tool:{tool_name}:invoke", "billing:charge"],
        max_credits=Decimal(max_credits),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    if aggregate_value_cap is not None:
        request = request.model_copy(
            update={"aggregate_value_cap": Decimal(aggregate_value_cap)}
        )
    permit = await get_permit_service().create_permit(request)
    return provisioned, permit


def _caller(provisioned: dict) -> CallerIdentity:
    """Bound subject-key caller, as in test_owner_enforcement.py."""
    return CallerIdentity(
        wallet_id=provisioned["agent_wallet_id"], key_id=provisioned["key_id"]
    )


async def _spent(permit_id: str) -> Decimal:
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit_id)
        assert model is not None
        return model.spent_credits


async def _backdate_expiry(permit_id: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            model = await session.get(PermitModel, permit_id)
            assert model is not None
            model.expires_at = utc_now() - timedelta(minutes=5)
            session.add(model)


async def _orphan_receipt(
    *,
    permit_id: str,
    wallet_id: str,
    key_id: str,
    tool_name: str,
    amount: str = "2",
) -> None:
    """Reserve, write a positive receipt, then hand the reservation back.

    ``spent_credits`` returns to 0 while the receipt still records the charge.
    That is the state a later reservation must not treat as free cap room.
    """
    service = get_permit_service()
    credits = Decimal(amount)
    caller = CallerIdentity(wallet_id=wallet_id, key_id=key_id)
    await service.reserve_budget(permit_id, credits, caller=caller)
    await get_receipt_service().create_receipt(
        permit_id=permit_id,
        wallet_id=wallet_id,
        key_id=key_id,
        tool=tool_name,
        request_payload={"input": "history"},
        response_payload={"error": "refund_failed"},
        ledger_entry_id=None,
        credits_authorized=credits,
        credits_charged=credits,
        outcome="failed_unrefunded",
        audit_event_id=None,
    )
    await service.release_budget(permit_id, credits, caller=caller)
    assert await _spent(permit_id) == Decimal("0")


@pytest.mark.anyio
async def test_negative_estimated_credits_cannot_mint_budget(client, clean_database):
    """A negative estimate must not lower spent_credits or read as allowed."""
    provisioned, permit = await _live_permit(client, tool_name="break-negative")
    service = get_permit_service()
    caller = _caller(provisioned)
    await service.reserve_budget(permit.permit_id, Decimal("4"), caller=caller)
    assert await _spent(permit.permit_id) == Decimal("4")

    denied = await service.authorize_and_reserve(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="break-negative",
        estimated_credits=Decimal("-1"),
        key_id=provisioned["key_id"],
    )
    assert denied.allowed is False
    assert denied.reason == "permit_credits_invalid"
    assert await _spent(permit.permit_id) == Decimal("4")

    with pytest.raises(PermitError) as reserved:
        await service.reserve_budget(permit.permit_id, Decimal("-1"), caller=caller)
    assert reserved.value.reason == "permit_credits_invalid"
    assert await _spent(permit.permit_id) == Decimal("4")

    verified = await service.validate_for_action(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="break-negative",
        estimated_credits=Decimal("-3"),
        key_id=provisioned["key_id"],
    )
    assert verified.allowed is False
    assert verified.reason == "permit_credits_invalid"
    assert await _spent(permit.permit_id) == Decimal("4")


@pytest.mark.anyio
async def test_non_finite_estimated_credits_cannot_reserve(client, clean_database):
    """NaN and Infinity must not pass the budget comparison or reach SQL."""
    provisioned, permit = await _live_permit(client, tool_name="break-nan")
    service = get_permit_service()
    caller = _caller(provisioned)
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        denied = await service.authorize_and_reserve(
            permit_id=permit.permit_id,
            wallet_id=provisioned["agent_wallet_id"],
            tool_name="break-nan",
            estimated_credits=bad,
            key_id=provisioned["key_id"],
        )
        assert denied.allowed is False
        assert denied.reason == "permit_credits_invalid"
        with pytest.raises(PermitError) as reserved:
            await service.reserve_budget(permit.permit_id, bad, caller=caller)
        assert reserved.value.reason == "permit_credits_invalid"
    assert await _spent(permit.permit_id) == Decimal("0")


@pytest.mark.anyio
async def test_verify_rejects_negative_estimated_credits(client, clean_database):
    """POST /v1/permits/verify must not call a negative estimate valid."""
    provisioned, permit = await _live_permit(client, tool_name="break-verify-neg")
    response = await client.post(
        "/v1/permits/verify",
        json={
            "permit_id": permit.permit_id,
            "wallet_id": provisioned["agent_wallet_id"],
            "tool": "break-verify-neg",
            "estimated_credits": "-1",
        },
        headers=provisioned["agent_headers"],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["reason"] == "permit_credits_invalid"
    assert await _spent(permit.permit_id) == Decimal("0")


@pytest.mark.anyio
async def test_release_budget_rejects_amounts_that_grow_or_wipe_spend(
    client, clean_database
):
    """A release amount outside storage must not change spent_credits.

    A negative release increases the counter. A non-storable release used to
    trip the clamp and reset the counter to zero, which hands the budget back.
    A storable release larger than the current reservation still clamps to zero:
    that is the existing drift behavior for a real refund amount.
    """
    provisioned, permit = await _live_permit(client, tool_name="break-release")
    service = get_permit_service()
    caller = _caller(provisioned)
    await service.reserve_budget(permit.permit_id, Decimal("2"), caller=caller)

    with pytest.raises(PermitError) as negative:
        await service.release_budget(permit.permit_id, Decimal("-1"), caller=caller)
    assert negative.value.reason == "permit_credits_invalid"
    assert await _spent(permit.permit_id) == Decimal("2")

    with pytest.raises(PermitError) as huge:
        await service.release_budget(
            permit.permit_id, Decimal("1000000000000"), caller=caller
        )
    assert huge.value.reason == "permit_credits_invalid"
    assert await _spent(permit.permit_id) == Decimal("2")

    await service.release_budget(permit.permit_id, Decimal("5"), caller=caller)
    assert await _spent(permit.permit_id) == Decimal("0")


@pytest.mark.anyio
async def test_zero_credit_reservation_does_not_change_spend(client, clean_database):
    """Zero stays a legal estimate. It must not mint or burn budget."""
    provisioned, permit = await _live_permit(client, tool_name="break-zero")
    allowed = await get_permit_service().authorize_and_reserve(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="break-zero",
        estimated_credits=Decimal("0"),
        key_id=provisioned["key_id"],
    )
    assert allowed.allowed is True, allowed.reason
    assert await _spent(permit.permit_id) == Decimal("0")


@pytest.mark.anyio
async def test_released_receipt_history_is_absorbed_on_the_next_reservation(
    client, clean_database
):
    """Cap 5 with a released receipt of 2 admits one new reservation of 2.

    The receipt is still value. The next reservation has to count that 2 and
    its own 2 together, so spent_credits becomes 4 and a second reservation
    of 2 is over the cap. Letting the second one through spends 4 of new
    authority on top of the historical 2.
    """
    provisioned, permit = await _live_permit(
        client,
        tool_name="break-cap-seq",
        aggregate_value_cap="5",
    )
    await _orphan_receipt(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="break-cap-seq",
    )
    service = get_permit_service()

    async def reserve_once():
        return await service.authorize_and_reserve(
            permit_id=permit.permit_id,
            wallet_id=provisioned["agent_wallet_id"],
            tool_name="break-cap-seq",
            estimated_credits=Decimal("2"),
            key_id=provisioned["key_id"],
        )

    first = await reserve_once()
    assert first.allowed is True, first.reason
    assert await _spent(permit.permit_id) == Decimal("4")

    second = await reserve_once()
    assert second.allowed is False
    assert second.reason == "permit_aggregate_value_cap_exceeded"
    assert await _spent(permit.permit_id) == Decimal("4")


@pytest.mark.anyio
async def test_reserve_budget_absorbs_released_receipt_history(client, clean_database):
    """The AWI reserve path has the same cap as authorize_and_reserve."""
    provisioned, permit = await _live_permit(
        client,
        tool_name="break-cap-reserve",
        aggregate_value_cap="5",
    )
    await _orphan_receipt(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="break-cap-reserve",
    )
    service = get_permit_service()
    caller = _caller(provisioned)
    await service.reserve_budget(permit.permit_id, Decimal("2"), caller=caller)
    assert await _spent(permit.permit_id) == Decimal("4")
    with pytest.raises(PermitError) as denied:
        await service.reserve_budget(permit.permit_id, Decimal("2"), caller=caller)
    assert denied.value.reason == "permit_aggregate_value_cap_exceeded"
    assert await _spent(permit.permit_id) == Decimal("4")


@pytest.mark.anyio
async def test_concurrent_reservations_cannot_refill_a_released_receipt(
    client, clean_database
):
    """Six parallel reservations of 2 against cap 5 admit one, spent ends at 4.

    A single admission on the old counter still leaves spent at 2, because
    the receipt floor was only a predicate and was not written back. Two
    admissions leave spent at 4 and a cumulative value of 6. Neither result
    is one admission at spent 4.
    """
    provisioned, permit = await _live_permit(
        client,
        tool_name="break-cap-race",
        aggregate_value_cap="5",
    )
    await _orphan_receipt(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="break-cap-race",
    )
    service = get_permit_service()

    async def reserve_once():
        return await service.authorize_and_reserve(
            permit_id=permit.permit_id,
            wallet_id=provisioned["agent_wallet_id"],
            tool_name="break-cap-race",
            estimated_credits=Decimal("2"),
            key_id=provisioned["key_id"],
        )

    results = await asyncio.gather(*(reserve_once() for _ in range(6)))
    allowed = [result for result in results if result.allowed]
    denied = [result for result in results if not result.allowed]
    assert len(allowed) == 1
    assert all(
        result.reason == "permit_aggregate_value_cap_exceeded" for result in denied
    )
    assert await _spent(permit.permit_id) == Decimal("4")


@pytest.mark.anyio
async def test_absorption_cannot_push_spent_past_max_credits(client, clean_database):
    """Absorbing a receipt of 2 plus a new 2 must not write spent above max 3."""
    provisioned, permit = await _live_permit(
        client,
        tool_name="break-cap-max",
        max_credits="3",
        aggregate_value_cap="100",
    )
    await _orphan_receipt(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="break-cap-max",
    )
    service = get_permit_service()
    denied = await service.authorize_and_reserve(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="break-cap-max",
        estimated_credits=Decimal("2"),
        key_id=provisioned["key_id"],
    )
    assert denied.allowed is False
    assert denied.reason == "permit_budget_exceeded"
    assert await _spent(permit.permit_id) == Decimal("0")

    with pytest.raises(PermitError) as reserved:
        await service.reserve_budget(
            permit.permit_id, Decimal("2"), caller=_caller(provisioned)
        )
    assert reserved.value.reason == "permit_budget_exceeded"
    assert await _spent(permit.permit_id) == Decimal("0")


@pytest.mark.anyio
async def test_expired_permit_cannot_be_reserved(client, clean_database):
    """An active row whose deadline has passed must not move budget."""
    provisioned, permit = await _live_permit(client, tool_name="break-expired")
    await _backdate_expiry(permit.permit_id)
    service = get_permit_service()

    denied = await service.authorize_and_reserve(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="break-expired",
        estimated_credits=Decimal("1"),
        key_id=provisioned["key_id"],
    )
    assert denied.allowed is False
    assert denied.reason == "permit_expired"
    assert await _spent(permit.permit_id) == Decimal("0")

    with pytest.raises(PermitError) as reserved:
        await service.reserve_budget(
            permit.permit_id, Decimal("1"), caller=_caller(provisioned)
        )
    assert reserved.value.reason == "permit_expired"
    assert await _spent(permit.permit_id) == Decimal("0")

    # A negative amount on an expired permit is still a bad amount, and it
    # must not be applied as a budget change before the deadline is reported.
    negative = await service.authorize_and_reserve(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="break-expired",
        estimated_credits=Decimal("-1"),
        key_id=provisioned["key_id"],
    )
    assert negative.allowed is False
    assert negative.reason == "permit_credits_invalid"
    assert await _spent(permit.permit_id) == Decimal("0")


@pytest.mark.anyio
async def test_denied_receipt_signature_rejects_outcome_and_charge_tamper(
    client, clean_database
):
    """A stored denial does not verify once outcome or credits_charged change."""
    provisioned, permit = await _live_permit(client, tool_name="break-denial")
    service = get_receipt_service()
    receipt = await service.create_receipt(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="break-denial",
        request_payload={"input": "no"},
        response_payload={"error": "denied"},
        ledger_entry_id=None,
        credits_authorized=Decimal("1.10"),
        credits_charged=Decimal("0"),
        outcome="denied",
        audit_event_id=None,
        reason_code="permit_expired",
    )
    assert (await service.verify_receipt(receipt.receipt_id))[0] is True
    signing_input = await service.signing_input(receipt.receipt_id)
    assert signing_input is not None
    payload = json.loads(signing_input)
    assert payload["outcome"] == "denied"
    assert payload["credits_charged"] == "0"

    verify_http = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt.receipt_id},
        headers=provisioned["agent_headers"],
    )
    assert verify_http.status_code == 200
    assert verify_http.json()["valid"] is True

    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            model = (
                await session.execute(
                    select(ReceiptModel).where(
                        ReceiptModel.receipt_id == receipt.receipt_id
                    )
                )
            ).scalar_one()
            model.outcome = "success"
            session.add(model)

    tampered_outcome = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt.receipt_id},
        headers=provisioned["agent_headers"],
    )
    assert tampered_outcome.status_code == 200
    assert tampered_outcome.json()["valid"] is False
    assert tampered_outcome.json()["reason"] == "receipt_signature_invalid"

    async with factory() as session:
        async with session.begin():
            model = (
                await session.execute(
                    select(ReceiptModel).where(
                        ReceiptModel.receipt_id == receipt.receipt_id
                    )
                )
            ).scalar_one()
            model.outcome = "denied"
            model.credits_charged = Decimal("5")
            session.add(model)

    tampered_charge = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt.receipt_id},
        headers=provisioned["agent_headers"],
    )
    assert tampered_charge.status_code == 200
    assert tampered_charge.json()["valid"] is False
    assert tampered_charge.json()["reason"] == "receipt_signature_invalid"
