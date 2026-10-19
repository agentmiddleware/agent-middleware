"""Pattern 3: money and permit services enforce caller ownership themselves.

Before this fix the routes were the only ownership check: every service
function below accepted a bare wallet or permit id and acted on it, so any
future (or compromised) caller past the route could move another tenant's
money. Each test names a stranger wallet and proves the service denies it,
then proves the owner (and the bootstrap admin) still succeeds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, McpDispatchAttemptModel, WalletModel
from app.main import app
from app.schemas.billing import LedgerAction, ServiceCategory
from app.schemas.trust import PermitCreateRequest
from app.services.agent_money import WalletNotFoundError, get_agent_money
from app.services.idempotency import get_idempotency_service
from app.services.ownership import CallerIdentity, OwnershipDeniedError
from app.services.permits import (
    PermitAccessDeniedError,
    get_permit_service,
)
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _owner_of(wallet_id: str) -> CallerIdentity:
    return CallerIdentity.for_wallet(wallet_id)


async def _permit(
    owner: dict,
    *,
    issuer_wallet_id: str | None = None,
    subject_wallet_id: str | None = None,
    max_credits: str = "50",
    tool: str = "owner-tool",
):
    return await get_permit_service().create_permit(
        PermitCreateRequest(
            issuer_wallet_id=issuer_wallet_id or owner["agent_wallet_id"],
            subject_wallet_id=subject_wallet_id or owner["agent_wallet_id"],
            subject_key_id=owner["key_id"],
            allowed_tools=[tool],
            scopes=[f"tool:{tool}:invoke"],
            max_credits=Decimal(max_credits),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).replace(
                tzinfo=None
            ),
        )
    )


@pytest.mark.anyio
async def test_transfer_denies_a_stranger(client, clean_database):
    owner = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    money = get_agent_money()

    with pytest.raises(OwnershipDeniedError):
        await money.transfer(
            owner["agent_wallet_id"],
            stranger["agent_wallet_id"],
            Decimal("10"),
            caller=_owner_of(stranger["agent_wallet_id"]),
        )

    receipt = await money.transfer(
        owner["agent_wallet_id"],
        stranger["agent_wallet_id"],
        Decimal("10"),
        caller=_owner_of(owner["agent_wallet_id"]),
    )
    assert receipt["status"] == "completed"

    receipt = await money.transfer(
        stranger["agent_wallet_id"],
        owner["agent_wallet_id"],
        Decimal("1"),
        caller=CallerIdentity.admin(),
    )
    assert receipt["status"] == "completed"


@pytest.mark.anyio
async def test_create_and_reclaim_child_wallet_deny_a_stranger(client, clean_database):
    owner = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    money = get_agent_money()

    with pytest.raises(OwnershipDeniedError):
        await money.create_child_wallet(
            owner["agent_wallet_id"],
            "stranger-child",
            Decimal("10"),
            Decimal("10"),
            caller=_owner_of(stranger["agent_wallet_id"]),
        )

    child = await money.create_child_wallet(
        owner["agent_wallet_id"],
        "owner-child",
        Decimal("10"),
        Decimal("10"),
        caller=_owner_of(owner["agent_wallet_id"]),
    )

    with pytest.raises(OwnershipDeniedError):
        await money.reclaim_child_wallet(
            child.wallet_id, caller=_owner_of(stranger["agent_wallet_id"])
        )

    reclaimed = await money.reclaim_child_wallet(
        child.wallet_id, caller=_owner_of(owner["agent_wallet_id"])
    )
    assert reclaimed["child_wallet_id"] == child.wallet_id


@pytest.mark.anyio
async def test_reclaim_denies_unowned_wallets_without_revealing_their_state(
    client, clean_database
):
    owner = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    money = get_agent_money()
    child = await money.create_child_wallet(
        owner["agent_wallet_id"],
        "owner-child",
        Decimal("10"),
        Decimal("10"),
        caller=_owner_of(owner["agent_wallet_id"]),
    )

    async def state():
        async with get_session_factory()() as session:
            wallets = (await session.execute(select(WalletModel))).scalars().all()
            ledger = (await session.execute(select(LedgerEntryModel))).scalars().all()
            return (
                {
                    wallet.wallet_id: (wallet.balance, wallet.status)
                    for wallet in wallets
                },
                len(ledger),
            )

    before = await state()
    for caller in (
        _owner_of(stranger["agent_wallet_id"]),
        CallerIdentity(),
        CallerIdentity.for_wallet(None),
    ):
        for wallet_id in (
            "missing-reclaim-wallet",
            owner["sponsor_wallet_id"],  # parentless wallet
            child.wallet_id,
        ):
            with pytest.raises(OwnershipDeniedError) as denied:
                await money.reclaim_child_wallet(wallet_id, caller=caller)
            assert denied.value.reason == "wallet_access_denied"
            assert str(denied.value) == "wallet_access_denied"
            assert await state() == before


@pytest.mark.anyio
async def test_reclaim_preserves_parent_child_engine_and_admin_access(
    client, clean_database
):
    owner = await provision_agent_wallet(client)
    money = get_agent_money()
    parent_id = owner["agent_wallet_id"]

    for index, caller_for_child in enumerate(
        (
            lambda child_id: CallerIdentity(wallet_id=parent_id),
            lambda child_id: CallerIdentity(wallet_id=child_id),
            lambda child_id: CallerIdentity.for_wallet(parent_id),
            lambda child_id: CallerIdentity.for_wallet(child_id),
            lambda child_id: CallerIdentity.admin(),
        )
    ):
        child = await money.create_child_wallet(
            parent_id,
            f"authorized-child-{index}",
            Decimal("10"),
            Decimal("10"),
            caller=_owner_of(parent_id),
        )
        reclaimed = await money.reclaim_child_wallet(
            child.wallet_id, caller=caller_for_child(child.wallet_id)
        )
        assert reclaimed["child_wallet_id"] == child.wallet_id
        wallet = await money.get_wallet(child.wallet_id)
        assert wallet is not None
        assert wallet.status.value == "closed"
        with pytest.raises(ValueError, match="Wallet already closed"):
            await money.reclaim_child_wallet(
                child.wallet_id, caller=caller_for_child(child.wallet_id)
            )

    with pytest.raises(WalletNotFoundError) as missing:
        await money.reclaim_child_wallet(
            "missing-reclaim-wallet", caller=CallerIdentity.admin()
        )
    assert missing.value.wallet_id == "missing-reclaim-wallet"
    with pytest.raises(ValueError, match="Child wallet has no parent"):
        await money.reclaim_child_wallet(
            owner["sponsor_wallet_id"],
            caller=CallerIdentity(wallet_id=owner["sponsor_wallet_id"]),
        )
    with pytest.raises(ValueError, match="Can only reclaim from child wallets"):
        await money.reclaim_child_wallet(
            parent_id, caller=CallerIdentity(wallet_id=parent_id)
        )


@pytest.mark.anyio
async def test_create_agent_wallet_denies_a_stranger(client, clean_database):
    owner = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    money = get_agent_money()

    with pytest.raises(OwnershipDeniedError):
        await money.create_agent_wallet(
            owner["sponsor_wallet_id"],
            "stranger-agent",
            Decimal("10"),
            caller=_owner_of(stranger["agent_wallet_id"]),
        )

    agent = await money.create_agent_wallet(
        owner["sponsor_wallet_id"],
        "owner-agent",
        Decimal("10"),
        caller=_owner_of(owner["sponsor_wallet_id"]),
    )
    assert agent.wallet_id


@pytest.mark.anyio
async def test_charge_and_refund_deny_a_stranger(client, clean_database):
    owner = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    money = get_agent_money()

    with pytest.raises(OwnershipDeniedError):
        await money.charge(
            owner["agent_wallet_id"],
            ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            caller=_owner_of(stranger["agent_wallet_id"]),
        )

    charge = await money.charge(
        owner["agent_wallet_id"],
        ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        caller=_owner_of(owner["agent_wallet_id"]),
    )
    assert hasattr(charge, "entry_id")

    with pytest.raises(OwnershipDeniedError):
        await money.refund_charge(
            wallet_id=owner["agent_wallet_id"],
            charge_entry_id=charge.entry_id,
            caller=_owner_of(stranger["agent_wallet_id"]),
        )

    refund = await money.refund_charge(
        wallet_id=owner["agent_wallet_id"],
        charge_entry_id=charge.entry_id,
        caller=_owner_of(owner["agent_wallet_id"]),
    )
    assert refund.entry_id == f"refund-{charge.entry_id}"
    assert refund.action == LedgerAction.REFUND


@pytest.mark.anyio
async def test_revoke_permit_denies_a_stranger_and_a_mere_subject(
    client, clean_database
):
    owner = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    service = get_permit_service()

    permit = await _permit(owner)

    with pytest.raises(PermitAccessDeniedError):
        await service.revoke_permit(
            permit.permit_id, caller=_owner_of(stranger["agent_wallet_id"])
        )
    stored = await service.get_permit(permit.permit_id)
    assert stored.status == "active"
    assert stored.revoked_at is None

    # The subject spends under the permit but does not own its authority:
    # revocation stays with the issuer, matching the route's check.
    sponsored = await _permit(
        owner,
        issuer_wallet_id=owner["sponsor_wallet_id"],
        subject_wallet_id=owner["agent_wallet_id"],
    )
    with pytest.raises(PermitAccessDeniedError):
        await service.revoke_permit(
            sponsored.permit_id, caller=_owner_of(owner["agent_wallet_id"])
        )

    # Revocation belongs to the issuer wallet, even when the permit is bound
    # to another key in that same wallet for spending/accounting.
    revoked = await service.revoke_permit(
        permit.permit_id,
        caller=CallerIdentity(
            wallet_id=owner["agent_wallet_id"], key_id="another-key-in-the-same-wallet"
        ),
    )
    assert revoked.status == "revoked"
    revoked = await service.revoke_permit(
        sponsored.permit_id, caller=CallerIdentity.admin()
    )
    assert revoked.status == "revoked"


@pytest.mark.anyio
async def test_permit_budget_mutations_deny_a_stranger(client, clean_database):
    owner = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    service = get_permit_service()
    stranger_caller = _owner_of(stranger["agent_wallet_id"])
    owner_caller = _owner_of(owner["agent_wallet_id"])
    other_key_caller = CallerIdentity(
        wallet_id=owner["agent_wallet_id"], key_id="another-key-in-the-same-wallet"
    )
    bound_key_caller = CallerIdentity(
        wallet_id=owner["agent_wallet_id"], key_id=owner["key_id"]
    )

    permit = await _permit(owner)

    with pytest.raises(PermitAccessDeniedError):
        await service.reserve_budget(
            permit.permit_id, Decimal("5"), caller=stranger_caller
        )
    with pytest.raises(PermitAccessDeniedError):
        await service.reserve_budget(
            permit.permit_id, Decimal("5"), caller=other_key_caller
        )
    with pytest.raises(PermitAccessDeniedError):
        await service.reserve_budget(
            permit.permit_id,
            Decimal("5"),
            caller=CallerIdentity.for_wallet(
                owner["agent_wallet_id"], key_id="another-key-in-the-same-wallet"
            ),
        )
    with pytest.raises(PermitAccessDeniedError):
        await service.reserve_budget(
            permit.permit_id,
            Decimal("5"),
            caller=CallerIdentity(wallet_id=owner["agent_wallet_id"]),
        )
    await service.reserve_budget(
        permit.permit_id, Decimal("5"), caller=bound_key_caller
    )

    with pytest.raises(PermitAccessDeniedError):
        await service.release_budget(
            permit.permit_id, Decimal("5"), caller=stranger_caller
        )
    with pytest.raises(PermitAccessDeniedError):
        await service.release_budget(
            permit.permit_id, Decimal("5"), caller=other_key_caller
        )
    await service.release_budget(
        permit.permit_id, Decimal("5"), caller=bound_key_caller
    )

    with pytest.raises(PermitAccessDeniedError):
        await service.release_tool_call(
            permit.permit_id, "owner-tool", caller=stranger_caller
        )
    with pytest.raises(PermitAccessDeniedError):
        await service.release_tool_call(
            permit.permit_id, "owner-tool", caller=other_key_caller
        )
    # No per-tool cap is configured, so the owner's release is a no-op.
    await service.release_tool_call(permit.permit_id, "owner-tool", caller=owner_caller)

    sponsored = await _permit(owner, issuer_wallet_id=owner["sponsor_wallet_id"])
    issuer_caller = CallerIdentity(
        wallet_id=owner["sponsor_wallet_id"], key_id="issuer-key"
    )
    await service.reserve_budget(
        sponsored.permit_id, Decimal("1"), caller=issuer_caller
    )
    await service.release_budget(
        sponsored.permit_id, Decimal("1"), caller=CallerIdentity.admin()
    )


@pytest.mark.anyio
async def test_release_dispatch_budget_denies_a_stranger(client, clean_database):
    owner = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    service = get_permit_service()
    wallet_id = owner["agent_wallet_id"]

    permit = await _permit(owner)
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=wallet_id,
        endpoint="/v1/owner-enforcement",
        idempotency_key="owner-dispatch-release",
        request_payload={"tool": "owner-tool"},
    )
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            session.add(
                McpDispatchAttemptModel(
                    attempt_id="att-owner-enforcement",
                    idempotency_record_id=begun.record_id,
                    wallet_id=wallet_id,
                    permit_id=permit.permit_id,
                    public_tool_id="owner-tool",
                    upstream_tool_name="partner_lookup",
                    upstream_origin="https://partner.example",
                    request_hash="a" * 64,
                    credits_authorized=Decimal("2"),
                    state="returned_error",
                )
            )

    with pytest.raises(PermitAccessDeniedError):
        await service.release_dispatch_budget_once(
            "att-owner-enforcement",
            caller=_owner_of(stranger["agent_wallet_id"]),
        )
    with pytest.raises(PermitAccessDeniedError):
        await service.release_dispatch_budget_once(
            "att-owner-enforcement",
            caller=CallerIdentity(wallet_id=wallet_id, key_id="another-key"),
        )

    assert (
        await service.release_dispatch_budget_once(
            "att-owner-enforcement", caller=_owner_of(wallet_id)
        )
        is True
    )


@pytest.mark.anyio
async def test_revoke_route_answers_403_for_a_stranger_key(client, clean_database):
    owner = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    permit = await _permit(owner)

    denied = await client.post(
        f"/v1/permits/{permit.permit_id}/revoke",
        headers=stranger["agent_headers"],
    )
    assert denied.status_code == 403

    revoked = await client.post(
        f"/v1/permits/{permit.permit_id}/revoke",
        headers=owner["agent_headers"],
    )
    assert revoked.status_code == 200, revoked.text


@pytest.mark.anyio
async def test_transfer_route_answers_403_for_a_stranger_key(client, clean_database):
    owner = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)

    denied = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": owner["agent_wallet_id"],
            "to_wallet_id": stranger["agent_wallet_id"],
            "amount": 5,
        },
        headers=stranger["agent_headers"],
    )
    assert denied.status_code == 403
