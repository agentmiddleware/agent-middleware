"""Keep terminal charge denials and exact ledger responses stable across retries."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, LedgerEntryModel, WalletModel
from app.main import app
from app.schemas.policies import PolicyBundleCreate, PolicyBundlePatch
from app.services.policies import create_policy_bundle, patch_policy_bundle
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value


@pytest.mark.anyio
async def test_terminal_policy_denial_replays_even_after_policy_changes(
    client, clean_database
):
    agent = await provision_agent_wallet(client)
    wallet_id = agent["agent_wallet_id"]
    policy = await create_policy_bundle(
        PolicyBundleCreate(
            wallet_id=wallet_id,
            name="deny billing",
            allowed_service_categories=["iot_bridge"],
        )
    )
    params = {"wallet_id": wallet_id, "service": "platform_fee"}
    headers = {**agent["agent_headers"], "Idempotency-Key": "policy-denial"}
    async with get_session_factory()() as session:
        before = await session.get(WalletModel, wallet_id)
        expected = (before.balance, before.lifetime_debits, before.daily_spent)
        ledger_ids = list(
            (await session.execute(select(LedgerEntryModel.entry_id))).scalars()
        )
    first = await client.post("/v1/billing/charge", params=params, headers=headers)
    assert first.status_code == 403
    assert first.json()["detail"]["error"] == "service_category_not_allowed"
    await patch_policy_bundle(
        policy.policy_id, PolicyBundlePatch(allowed_service_categories=None)
    )
    for _ in range(2):
        retry = await client.post("/v1/billing/charge", params=params, headers=headers)
        assert retry.status_code == first.status_code
        assert retry.json() == first.json()
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert (wallet.balance, wallet.lifetime_debits, wallet.daily_spent) == expected
        assert (
            list((await session.execute(select(LedgerEntryModel.entry_id))).scalars())
            == ledger_ids
        )


@pytest.mark.anyio
async def test_unknown_wallet_charge_is_404_with_or_without_key(client, clean_database):
    params = {"wallet_id": "missing-billing-wallet", "service": "platform_fee"}
    first = await client.post(
        "/v1/billing/charge", params=params, headers=BOOTSTRAP_HEADERS
    )
    assert first.status_code == 404
    for _ in range(2):
        keyed = await client.post(
            "/v1/billing/charge",
            params=params,
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "unknown-wallet"},
        )
        assert keyed.status_code == first.status_code
        assert keyed.json() == first.json()
    async with get_session_factory()() as session:
        assert (
            list((await session.execute(select(IdempotencyRecordModel))).scalars())
            == []
        )


@pytest.mark.anyio
async def test_charge_authorization_precedes_missing_wallet_lookup(
    client, clean_database
):
    agent = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    for wallet_id in (other["agent_wallet_id"], "unknown-other-wallet"):
        response = await client.post(
            "/v1/billing/charge",
            params={"wallet_id": wallet_id, "service": "platform_fee"},
            headers={
                **agent["agent_headers"],
                "Idempotency-Key": "cross-wallet-charge",
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "wallet_access_denied"
        ledger = await client.get(
            f"/v1/billing/ledger/{wallet_id}", headers=agent["agent_headers"]
        )
        assert ledger.status_code == 403
    async with get_session_factory()() as session:
        assert (
            list(
                (
                    await session.execute(
                        select(IdempotencyRecordModel).where(
                            IdempotencyRecordModel.idempotency_key
                            == "cross-wallet-charge"
                        )
                    )
                ).scalars()
            )
            == []
        )


@pytest.mark.anyio
@pytest.mark.parametrize("limit", [1, 2, 4])
async def test_ledger_exact_totals_use_exact_amounts_for_the_selected_page(
    client, clean_database, limit
):
    agent = await provision_agent_wallet(client)
    wallet_id = agent["agent_wallet_id"]
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, wallet_id)
        for offset, amount in enumerate(
            map(Decimal, ["0.1", "0.2", "-0.1", "-0.2"]), 1
        ):
            wallet.balance += amount
            session.add(
                LedgerEntryModel(
                    entry_id=str(uuid4()),
                    wallet_id=wallet_id,
                    action="credit" if amount > 0 else "debit",
                    amount=amount,
                    balance_after=wallet.balance,
                    timestamp=utc_now() + timedelta(seconds=offset),
                )
            )
        await session.commit()
    response = await client.get(
        f"/v1/billing/ledger/{wallet_id}",
        params={"limit": limit},
        headers=agent["agent_headers"],
    )
    assert response.status_code == 200
    payload = response.json()
    amounts = [Decimal(entry["amount_exact"]) for entry in payload["entries"]]
    assert len(amounts) == limit
    assert Decimal(payload["period_credits_exact"]) == sum(
        (a for a in amounts if a > 0), Decimal("0")
    )
    assert Decimal(payload["period_debits_exact"]) == sum(
        (-a for a in amounts if a < 0), Decimal("0")
    )


@pytest.mark.anyio
async def test_empty_ledger_exact_totals_are_zero(client, clean_database):
    async with get_session_factory()() as session:
        session.add(WalletModel(wallet_id="empty-exact-ledger", wallet_type="sponsor"))
        await session.commit()
    response = await client.get(
        "/v1/billing/ledger/empty-exact-ledger", headers=BOOTSTRAP_HEADERS
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["entries"] == []
    assert payload["period_credits_exact"] == payload["period_debits_exact"] == "0"
