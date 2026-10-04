"""
Tests for Stripe Integration Service.
Validates fiat top-up flow, webhook handling, and idempotency.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe
from httpx import AsyncClient, ASGITransport
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from app.core.time import utc_now
from tests.conftest import interleaving_factory, requires_sqlite_row_lock_noop
from app.main import app
from app.services.stripe_integration import StripeIntegration, StripeSettlementError


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


@pytest.fixture
async def sponsor_wallet(client, api_headers):
    """Create a sponsor wallet for testing."""
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Stripe Test Sponsor",
            "email": "stripe-test@b2a.dev",
            "initial_credits": 0,
        },
        headers=api_headers,
    )
    return resp.json()


@pytest.fixture
async def wallet_hierarchy(client, api_headers):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Stripe Hierarchy Sponsor",
            "email": "stripe-hierarchy@b2a.dev",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    assert sponsor.status_code == 201, sponsor.text
    sponsor_id = sponsor.json()["wallet_id"]

    agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "stripe-agent",
            "budget_credits": 200,
        },
        headers=api_headers,
    )
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["wallet_id"]

    child = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": agent_id,
            "child_agent_id": "stripe-child",
            "budget_credits": 50,
            "max_spend": 50,
        },
        headers=api_headers,
    )
    assert child.status_code == 201, child.text

    return {
        "sponsor": sponsor_id,
        "agent": agent_id,
        "child": child.json()["wallet_id"],
    }


def succeeded_payment_intent(
    *,
    payment_intent_id: str,
    wallet_id: str,
    amount: int = 5000,
    amount_received: int = 5000,
    currency: str = "usd",
    credits: str = "50000",
) -> dict:
    return {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": payment_intent_id,
                "status": "succeeded",
                "amount": amount,
                "amount_received": amount_received,
                "currency": currency,
                "metadata": {
                    "wallet_id": wallet_id,
                    "credits": credits,
                },
            }
        },
    }


def refunded_charge(
    *,
    payment_intent_id: str,
    amount_refunded: int,
    amount: int = 5000,
    currency: str = "usd",
    charge_id: str = "ch_refund_test",
) -> dict:
    return {
        "payment_intent": payment_intent_id,
        "amount": amount,
        "amount_refunded": amount_refunded,
        "currency": currency,
        "id": charge_id,
    }


@pytest.mark.anyio
async def test_prepare_top_up_creates_payment_intent(
    client, sponsor_wallet, api_headers
):
    """Test that /top-up/prepare creates a Stripe PaymentIntent."""
    wallet_id = sponsor_wallet["wallet_id"]

    with patch(
        "app.services.stripe_integration.stripe.PaymentIntent.create"
    ) as mock_create:
        mock_create.return_value = MagicMock(
            id="pi_test123",
            client_secret="pi_test123_secret_xyz",
            status="requires_payment_method",
        )

        resp = await client.post(
            f"/v1/billing/top-up/prepare?wallet_id={wallet_id}&amount_fiat=50.0",
            headers=api_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["payment_intent_id"] == "pi_test123"
        assert data["client_secret"] == "pi_test123_secret_xyz"
        assert data["amount_credits"] == 50000  # $50 * 1000 credits/$
        assert data["amount_fiat"] == 50.0
        assert data["currency"] == "USD"

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["amount"] == 5000  # 50.0 * 100 cents
        assert call_kwargs["currency"] == "usd"
        assert call_kwargs["metadata"]["wallet_id"] == wallet_id
        assert call_kwargs["metadata"]["credits"] == "50000"


@pytest.mark.anyio
async def test_prepare_top_up_rejects_non_usd_in_api_and_service(
    client, sponsor_wallet, api_headers
):
    wallet_id = sponsor_wallet["wallet_id"]
    integration = StripeIntegration()

    with patch(
        "app.services.stripe_integration.stripe.PaymentIntent.create"
    ) as mock_create:
        response = await client.post(
            f"/v1/billing/top-up/prepare?wallet_id={wallet_id}"
            "&amount_fiat=50.0&currency=EUR",
            headers=api_headers,
        )
        assert response.status_code == 400

        with pytest.raises(ValueError, match="unsupported_top_up_currency"):
            await integration.create_top_up_intent(
                wallet_id=wallet_id,
                amount_fiat=Decimal("50"),
                currency="eur",
            )

    mock_create.assert_not_called()


@pytest.mark.anyio
async def test_prepare_top_up_rejects_agent_and_child_wallets(
    client, wallet_hierarchy, api_headers
):
    with patch(
        "app.services.stripe_integration.stripe.PaymentIntent.create"
    ) as mock_create:
        for wallet_type in ("agent", "child"):
            wallet_id = wallet_hierarchy[wallet_type]
            response = await client.post(
                f"/v1/billing/top-up/prepare?wallet_id={wallet_id}&amount_fiat=1",
                headers=api_headers,
            )
            assert response.status_code == 400
            assert response.json()["detail"]["error"] == "topup_prepare_error"

    mock_create.assert_not_called()


@pytest.mark.anyio
async def test_prepare_top_up_wallet_not_found(client, api_headers):
    """Test that /top-up/prepare returns 404 for non-existent wallet."""
    with patch(
        "app.services.stripe_integration.stripe.PaymentIntent.create"
    ) as mock_create:
        mock_create.side_effect = Exception("Should not be called")

        resp = await client.post(
            "/v1/billing/top-up/prepare?wallet_id=nonexistent&amount_fiat=50.0",
            headers=api_headers,
        )
        assert resp.status_code == 404


@pytest.mark.anyio
async def test_webhook_signature_verification(client):
    """Test that invalid Stripe signatures are rejected."""
    with (
        patch(
            "app.services.stripe_integration.stripe.Webhook.construct_event",
            side_effect=stripe.SignatureVerificationError(
                "Invalid signature", "invalid_sig"
            ),
        ),
        patch.object(
            StripeIntegration, "_mint_credits", new_callable=AsyncMock
        ) as mock_mint,
    ):
        resp = await client.post(
            "/v1/webhooks/stripe",
            content=b"invalid_payload",
            headers={"stripe-signature": "invalid_sig"},
        )

    assert resp.status_code == 400
    mock_mint.assert_not_awaited()


@pytest.mark.anyio
async def test_webhook_missing_signature(client):
    """Test that missing Stripe signatures are rejected."""
    resp = await client.post(
        "/v1/webhooks/stripe",
        content=b"some_payload",
    )
    assert resp.status_code == 400  # Missing signature header


@pytest.mark.anyio
async def test_webhook_rejects_metadata_credit_inflation(
    client, sponsor_wallet, api_headers
):
    """A signed Stripe event cannot mint more than its settled USD amount."""
    wallet_id = sponsor_wallet["wallet_id"]
    forged_event = succeeded_payment_intent(
        payment_intent_id="pi_forged_credits",
        wallet_id=wallet_id,
        credits="50000000",
    )

    with patch(
        "app.services.stripe_integration.stripe.Webhook.construct_event",
        return_value=forged_event,
    ):
        response = await client.post(
            "/v1/webhooks/stripe",
            content=b"signed_but_forged_metadata",
            headers={"stripe-signature": "valid_signature"},
        )

    assert response.status_code == 400
    wallet = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
    assert wallet.json()["balance"] == 0.0


@pytest.mark.anyio
async def test_webhook_rejects_invalid_settlement_fields_without_minting(
    client, sponsor_wallet, api_headers
):
    wallet_id = sponsor_wallet["wallet_id"]
    invalid_events = [
        succeeded_payment_intent(
            payment_intent_id="pi_non_usd",
            wallet_id=wallet_id,
            currency="eur",
        ),
        succeeded_payment_intent(
            payment_intent_id="pi_amount_mismatch",
            wallet_id=wallet_id,
            amount_received=1000,
            credits="10000",
        ),
        succeeded_payment_intent(
            payment_intent_id="pi_credit_metadata_missing",
            wallet_id=wallet_id,
            credits="",
        ),
    ]

    with patch(
        "app.services.stripe_integration.stripe.Webhook.construct_event",
        side_effect=invalid_events,
    ):
        for event in invalid_events:
            response = await client.post(
                "/v1/webhooks/stripe",
                content=event["data"]["object"]["id"].encode(),
                headers={"stripe-signature": "valid_signature"},
            )
            assert response.status_code == 400

    wallet = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
    assert wallet.json()["balance"] == 0.0


@pytest.mark.anyio
async def test_webhook_rejects_agent_and_child_wallets(
    client, wallet_hierarchy, api_headers
):
    events = [
        succeeded_payment_intent(
            payment_intent_id=f"pi_{wallet_type}_wallet",
            wallet_id=wallet_hierarchy[wallet_type],
            amount=100,
            amount_received=100,
            credits="1000",
        )
        for wallet_type in ("agent", "child")
    ]
    starting_balances = {}
    for wallet_type in ("agent", "child"):
        wallet_id = wallet_hierarchy[wallet_type]
        wallet = await client.get(
            f"/v1/billing/wallets/{wallet_id}", headers=api_headers
        )
        starting_balances[wallet_id] = wallet.json()["balance"]

    with patch(
        "app.services.stripe_integration.stripe.Webhook.construct_event",
        side_effect=events,
    ):
        for event in events:
            response = await client.post(
                "/v1/webhooks/stripe",
                content=event["data"]["object"]["id"].encode(),
                headers={"stripe-signature": "valid_signature"},
            )
            assert response.status_code == 400

    for wallet_id, starting_balance in starting_balances.items():
        wallet = await client.get(
            f"/v1/billing/wallets/{wallet_id}", headers=api_headers
        )
        assert wallet.json()["balance"] == starting_balance


class TestStripeWebhookIdempotency:
    """Tests for webhook idempotency via UNIQUE constraint."""

    def test_only_stripe_event_unique_errors_are_idempotent_for_refunds(self):
        """Only the refund event-id unique constraint is swallowed."""
        duplicate = IntegrityError(
            "insert",
            {},
            Exception("UNIQUE constraint failed: ledger_entries.stripe_event_id"),
        )
        foreign_key = IntegrityError(
            "insert",
            {},
            Exception("FOREIGN KEY constraint failed"),
        )
        assert StripeIntegration._is_duplicate_stripe_event_error(duplicate) is True
        assert StripeIntegration._is_duplicate_stripe_event_error(foreign_key) is False

    @pytest.mark.anyio
    async def test_redelivered_refund_debits_only_once(
        self, client, sponsor_wallet, api_headers
    ):
        """A redelivered charge.refunded event (same event id) must not debit
        the wallet twice."""
        from app.services.stripe_integration import get_stripe_integration
        from app.core.dependencies import get_agent_money

        wallet_id = sponsor_wallet["wallet_id"]
        integration = get_stripe_integration()
        money = get_agent_money()

        # Mint 50000 credits so there is a payment-intent ledger entry to refund.
        await integration._mint_credits(
            wallet_id=wallet_id,
            amount=Decimal("50000"),
            payment_intent_id="pi_refund_test",
            description="topup",
        )
        assert (await money.get_wallet(wallet_id)).balance == Decimal("50000")

        refund_charge = refunded_charge(
            payment_intent_id="pi_refund_test",
            amount_refunded=5000,
        )
        # First delivery debits.
        await integration._handle_refund(refund_charge, "evt_refund_1")
        assert (await money.get_wallet(wallet_id)).balance == Decimal("0")

        # Redelivery of the SAME event is a no-op, not a second debit.
        await integration._handle_refund(refund_charge, "evt_refund_1")
        assert (await money.get_wallet(wallet_id)).balance == Decimal("0")

        ledger = await money.get_ledger(wallet_id, 50)
        refunds = [e for e in ledger if e.action == "refund"]
        assert len(refunds) == 1

    @pytest.mark.anyio
    async def test_refund_clawback_advances_the_wallet_timestamp(
        self, client, sponsor_wallet, api_headers
    ):
        """A clawback must not leave the wallet row looking untouched.

        The clawback is a Core ``UPDATE``, which bypasses the ORM attribute
        write, and ``WalletModel.updated_at`` has a default but no
        ``onupdate`` -- so the timestamp only advances if the statement sets
        it. Without that, the most consequential balance move the wallet sees
        (real fiat returned by Stripe) is the one that leaves no trace in
        ``updated_at``, and anything reading the row for recency -- an
        operator triaging a disputed balance, a sweep ordered by staleness --
        is looking at the time of the top-up instead.
        """
        from app.services.stripe_integration import get_stripe_integration
        from app.db.database import get_session_factory
        from app.db.models import WalletModel

        wallet_id = sponsor_wallet["wallet_id"]
        integration = get_stripe_integration()
        factory = get_session_factory()

        await integration._mint_credits(
            wallet_id=wallet_id,
            amount=Decimal("50000"),
            payment_intent_id="pi_clawback_timestamp",
            description="topup",
        )

        # Backdate the row so any advance is unambiguous rather than a
        # sub-resolution tie against the mint that just ran.
        stale = utc_now() - timedelta(hours=1)
        async with factory() as session:
            async with session.begin():
                wallet = await session.get(WalletModel, wallet_id)
                assert wallet is not None
                wallet.updated_at = stale
                session.add(wallet)

        await integration._handle_refund(
            refunded_charge(
                payment_intent_id="pi_clawback_timestamp",
                amount_refunded=5000,
            ),
            "evt_clawback_timestamp",
        )

        async with factory() as session:
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            assert wallet.balance == Decimal("0"), "the clawback did not apply"
            assert wallet.updated_at > stale, (
                "the clawback moved the balance but left updated_at at "
                f"{wallet.updated_at}, the value set before it ran"
            )

    @requires_sqlite_row_lock_noop
    @pytest.mark.anyio
    async def test_a_concurrent_charge_cannot_vanish_under_a_refund_clawback(
        self, client, sponsor_wallet, api_headers, monkeypatch
    ):
        """A spend landing mid-clawback must not be erased.

        The clawback read the sponsor wallet, then several statements later
        wrote ``balance - refund_delta`` computed from that read. The
        cumulative-refund arithmetic above it makes a *redelivered event*
        idempotent, but it does nothing about a charge committing in the gap:
        that debit is simply overwritten. The discrepancy is against fiat
        Stripe has already returned, so the books are wrong in the direction
        that costs the operator real money.

        The interleave fires after the wallet SELECT and before the clawback
        write, and is forced rather than raced.
        """
        from app.services.stripe_integration import get_stripe_integration
        from app.db.database import get_session_factory
        from app.db.models import WalletModel

        wallet_id = sponsor_wallet["wallet_id"]
        integration = get_stripe_integration()
        real_factory = get_session_factory()

        await integration._mint_credits(
            wallet_id=wallet_id,
            amount=Decimal("50000"),
            payment_intent_id="pi_clawback_race",
            description="topup",
        )

        spend = Decimal("100")
        state: dict = {}

        async def _concurrent_spend() -> None:
            """A charge commits against the same wallet, mid-clawback."""
            async with real_factory() as s:
                async with s.begin():
                    await s.execute(
                        sa_update(WalletModel)
                        .where(WalletModel.wallet_id == wallet_id)
                        .values(balance=WalletModel.balance - spend)
                        .execution_options(synchronize_session=False)
                    )

        monkeypatch.setattr(
            integration,
            "_session_factory",
            # Statement 2 is the wallet SELECT. Firing after it returns puts
            # the spend between the read and the clawback write.
            interleaving_factory(real_factory, _concurrent_spend, state, fire_on=2),
        )

        # Stripe reports cents; the ledger is in credits at ten per cent (the
        # existing redelivery test pins the same ratio: 5000 cents claws back
        # the whole 50000-credit top-up).
        refunded_cents = 500
        refund_credits = Decimal(refunded_cents * 10)
        await integration._handle_refund(
            refunded_charge(
                payment_intent_id="pi_clawback_race",
                amount_refunded=refunded_cents,
            ),
            "evt_clawback_race",
        )

        assert state.get("fired"), "the interleave never ran — the test proved nothing"
        async with real_factory() as session:
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            # Both the clawback and the concurrent spend are on the balance.
            assert wallet.balance == Decimal("50000") - refund_credits - spend

    @pytest.mark.anyio
    async def test_successive_partial_refunds_apply_only_new_cumulative_delta(
        self, sponsor_wallet
    ):
        """Stripe's cumulative amount_refunded must not be applied repeatedly."""
        from app.core.dependencies import get_agent_money
        from app.services.stripe_integration import get_stripe_integration

        wallet_id = sponsor_wallet["wallet_id"]
        integration = get_stripe_integration()
        money = get_agent_money()
        await integration._mint_credits(
            wallet_id=wallet_id,
            amount=Decimal("50000"),
            payment_intent_id="pi_partial_refund_test",
            description="topup",
        )

        await integration._handle_refund(
            refunded_charge(
                payment_intent_id="pi_partial_refund_test",
                amount_refunded=1000,
            ),
            "evt_partial_refund_1",
        )
        assert (await money.get_wallet(wallet_id)).balance == Decimal("40000")

        await integration._handle_refund(
            refunded_charge(
                payment_intent_id="pi_partial_refund_test",
                amount_refunded=2000,
            ),
            "evt_partial_refund_2",
        )
        assert (await money.get_wallet(wallet_id)).balance == Decimal("30000")

        ledger = await money.get_ledger(wallet_id, 50)
        refunds = [entry for entry in ledger if entry.action == "refund"]
        assert [entry.amount for entry in refunds] == [-10000.0, -10000.0]
        assert [entry.metadata["stripe_amount_refunded"] for entry in refunds] == [
            2000,
            1000,
        ]  # Ledger responses are newest-first.

    @pytest.mark.anyio
    async def test_stale_partial_refund_event_is_a_noop(self, sponsor_wallet):
        from app.core.dependencies import get_agent_money
        from app.services.stripe_integration import get_stripe_integration

        wallet_id = sponsor_wallet["wallet_id"]
        integration = get_stripe_integration()
        money = get_agent_money()
        await integration._mint_credits(
            wallet_id=wallet_id,
            amount=Decimal("50000"),
            payment_intent_id="pi_stale_refund_test",
            description="topup",
        )

        await integration._handle_refund(
            refunded_charge(
                payment_intent_id="pi_stale_refund_test",
                amount_refunded=2000,
            ),
            "evt_stale_refund_newer",
        )
        await integration._handle_refund(
            refunded_charge(
                payment_intent_id="pi_stale_refund_test",
                amount_refunded=1000,
            ),
            "evt_stale_refund_older",
        )

        assert (await money.get_wallet(wallet_id)).balance == Decimal("30000")
        ledger = await money.get_ledger(wallet_id, 50)
        assert len([entry for entry in ledger if entry.action == "refund"]) == 1

    @pytest.mark.anyio
    async def test_refund_cannot_be_redirected_to_another_sponsor(
        self, client, sponsor_wallet, api_headers
    ):
        from app.core.dependencies import get_agent_money
        from app.services.stripe_integration import get_stripe_integration

        original_wallet_id = sponsor_wallet["wallet_id"]
        other_response = await client.post(
            "/v1/billing/wallets/sponsor",
            json={
                "sponsor_name": "Other Stripe Sponsor",
                "email": "other-stripe-sponsor@b2a.dev",
                "initial_credits": 0,
            },
            headers=api_headers,
        )
        assert other_response.status_code == 201, other_response.text
        other_wallet_id = other_response.json()["wallet_id"]

        integration = get_stripe_integration()
        money = get_agent_money()
        await integration._mint_credits(
            wallet_id=original_wallet_id,
            amount=Decimal("50000"),
            payment_intent_id="pi_tenant_bound_refund",
            description="topup",
        )
        charge = refunded_charge(
            payment_intent_id="pi_tenant_bound_refund",
            amount_refunded=1000,
        )
        # Charge metadata is not an authority for wallet selection. The
        # original, unique PaymentIntent ledger row remains the tenant binding.
        charge["metadata"] = {"wallet_id": other_wallet_id}
        await integration._handle_refund(charge, "evt_tenant_bound_refund")

        assert (await money.get_wallet(original_wallet_id)).balance == Decimal("40000")
        assert (await money.get_wallet(other_wallet_id)).balance == Decimal("0")
        original_ledger = await money.get_ledger(original_wallet_id, 50)
        other_ledger = await money.get_ledger(other_wallet_id, 50)
        assert (
            len([entry for entry in original_ledger if entry.action == "refund"]) == 1
        )
        assert not [entry for entry in other_ledger if entry.action == "refund"]

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("charge_overrides", "expected_error"),
        [
            ({"currency": "eur"}, "unsupported_refund_currency"),
            ({"amount_refunded": None}, "invalid_refund_amount"),
            ({"amount_refunded": 5001}, "refund_exceeds_charge_amount"),
            ({"amount": True}, "invalid_charge_amount"),
            ({"payment_intent": ""}, "missing_refund_payment_intent_id"),
        ],
    )
    async def test_invalid_refund_settlement_never_debits(
        self,
        sponsor_wallet,
        charge_overrides,
        expected_error,
    ):
        from app.core.dependencies import get_agent_money
        from app.services.stripe_integration import get_stripe_integration

        wallet_id = sponsor_wallet["wallet_id"]
        integration = get_stripe_integration()
        money = get_agent_money()
        payment_intent_id = f"pi_invalid_refund_{expected_error}"
        await integration._mint_credits(
            wallet_id=wallet_id,
            amount=Decimal("50000"),
            payment_intent_id=payment_intent_id,
            description="topup",
        )
        charge = refunded_charge(
            payment_intent_id=payment_intent_id,
            amount_refunded=1000,
        )
        charge.update(charge_overrides)

        with pytest.raises(StripeSettlementError, match=expected_error):
            await integration._handle_refund(charge, "evt_invalid_refund")

        assert (await money.get_wallet(wallet_id)).balance == Decimal("50000")
        ledger = await money.get_ledger(wallet_id, 50)
        assert not [entry for entry in ledger if entry.action == "refund"]

    @pytest.mark.anyio
    async def test_refund_larger_than_available_balance_records_liability_once(
        self, client, sponsor_wallet, api_headers
    ):
        from sqlalchemy import select

        from app.core.dependencies import get_agent_money
        from app.db.database import get_session_factory
        from app.db.models import BillingAlertModel
        from app.services.stripe_integration import get_stripe_integration

        wallet_id = sponsor_wallet["wallet_id"]
        integration = get_stripe_integration()
        money = get_agent_money()
        await integration._mint_credits(
            wallet_id=wallet_id,
            amount=Decimal("50000"),
            payment_intent_id="pi_over_balance_refund",
            description="topup",
        )
        allocation = await client.post(
            "/v1/billing/wallets/agent",
            json={
                "sponsor_wallet_id": wallet_id,
                "agent_id": "refund-balance-agent",
                "budget_credits": 45000,
            },
            headers=api_headers,
        )
        assert allocation.status_code == 201, allocation.text
        assert (await money.get_wallet(wallet_id)).balance == Decimal("5000")

        charge = refunded_charge(
            payment_intent_id="pi_over_balance_refund",
            amount_refunded=1000,
        )
        refund_event = {
            "id": "evt_over_balance_refund",
            "type": "charge.refunded",
            "data": {"object": charge},
        }
        with patch(
            "app.services.stripe_integration.stripe.Webhook.construct_event",
            return_value=refund_event,
        ):
            first = await client.post(
                "/v1/webhooks/stripe",
                content=b"valid_refund",
                headers={"stripe-signature": "valid_signature"},
            )
            redelivery = await client.post(
                "/v1/webhooks/stripe",
                content=b"valid_refund_redelivery",
                headers={"stripe-signature": "valid_signature"},
            )
        assert first.status_code == 200
        assert redelivery.status_code == 200

        # A distinct event reporting the same cumulative Stripe amount must
        # also converge without another debit, alert, or liability increase.
        await integration._handle_refund(charge, "evt_same_cumulative_refund")

        wallet = await money.get_wallet(wallet_id)
        assert wallet.balance == Decimal("-5000")
        assert wallet.status.value == "frozen"

        ledger = await money.get_ledger(wallet_id, 50)
        refunds = [entry for entry in ledger if entry.action == "refund"]
        assert len(refunds) == 1
        assert refunds[0].amount == -10000.0
        assert refunds[0].balance_after == -5000.0
        assert Decimal(refunds[0].metadata["refund_liability_credits"]) == Decimal(
            "5000"
        )
        assert refunds[0].metadata["wallet_frozen_for_refund_liability"] is True

        async with get_session_factory()() as session:
            alerts = list(
                (
                    await session.execute(
                        select(BillingAlertModel).where(
                            BillingAlertModel.wallet_id == wallet_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        liability_alerts = [
            alert
            for alert in alerts
            if alert.alert_type == "suspicious_activity"
            and alert.severity == "critical"
        ]
        assert len(liability_alerts) == 1
        assert liability_alerts[0].current_balance == Decimal("-5000")

        # A later, independently settled top-up pays down the liability, but
        # restoration of spending authority remains an explicit operator act.
        await integration._mint_credits(
            wallet_id=wallet_id,
            amount=Decimal("6000"),
            payment_intent_id="pi_refund_liability_repayment",
            description="liability repayment topup",
        )
        replenished_wallet = await money.get_wallet(wallet_id)
        assert replenished_wallet.balance == Decimal("1000")
        assert replenished_wallet.status.value == "frozen"

        # This is the exact containment boundary: a refund-induced freeze must
        # still block agent provisioning after a later top-up makes the balance
        # positive enough to fund it.
        blocked_provision = await client.post(
            "/v1/billing/wallets/agent",
            json={
                "sponsor_wallet_id": wallet_id,
                "agent_id": "blocked-after-liability-repayment",
                "budget_credits": 1,
            },
            headers=api_headers,
        )
        assert blocked_provision.status_code == 400
        assert blocked_provision.json()["detail"]["error"] == "wallet_error"
        assert "frozen" in blocked_provision.json()["detail"]["message"]

    @pytest.mark.anyio
    @pytest.mark.parametrize("existing_status", ["suspended", "closed"])
    async def test_refund_liability_preserves_existing_non_spendable_status(
        self, client, sponsor_wallet, api_headers, existing_status
    ):
        from sqlalchemy import select

        from app.core.dependencies import get_agent_money
        from app.db.database import get_session_factory
        from app.db.models import WalletModel
        from app.services.stripe_integration import get_stripe_integration

        wallet_id = sponsor_wallet["wallet_id"]
        payment_intent_id = f"pi_refund_existing_{existing_status}"
        integration = get_stripe_integration()
        money = get_agent_money()
        await integration._mint_credits(
            wallet_id=wallet_id,
            amount=Decimal("50000"),
            payment_intent_id=payment_intent_id,
            description="topup",
        )
        allocation = await client.post(
            "/v1/billing/wallets/agent",
            json={
                "sponsor_wallet_id": wallet_id,
                "agent_id": f"existing-{existing_status}-agent",
                "budget_credits": 45000,
            },
            headers=api_headers,
        )
        assert allocation.status_code == 201, allocation.text

        async with get_session_factory()() as session:
            wallet = (
                await session.execute(
                    select(WalletModel).where(WalletModel.wallet_id == wallet_id)
                )
            ).scalar_one()
            wallet.status = existing_status
            session.add(wallet)
            await session.commit()

        await integration._handle_refund(
            refunded_charge(
                payment_intent_id=payment_intent_id,
                amount_refunded=1000,
            ),
            f"evt_refund_existing_{existing_status}",
        )

        persisted_wallet = await money.get_wallet(wallet_id)
        assert persisted_wallet.balance == Decimal("-5000")
        assert persisted_wallet.status.value == existing_status
        refunds = [
            entry
            for entry in await money.get_ledger(wallet_id, 50)
            if entry.action == "refund"
        ]
        assert len(refunds) == 1
        assert refunds[0].metadata["wallet_status_before_refund"] == existing_status
        assert refunds[0].metadata["wallet_status_after_refund"] == existing_status
        assert refunds[0].metadata["wallet_frozen_for_refund_liability"] is False

    def test_only_payment_intent_unique_errors_are_idempotent(self):
        """Non-idempotency integrity errors must not be swallowed."""
        duplicate = IntegrityError(
            "insert",
            {},
            Exception("UNIQUE constraint failed: ledger_entries.payment_intent_id"),
        )
        foreign_key = IntegrityError(
            "insert",
            {},
            Exception("FOREIGN KEY constraint failed"),
        )

        assert StripeIntegration._is_duplicate_payment_intent_error(duplicate) is True
        assert (
            StripeIntegration._is_duplicate_payment_intent_error(foreign_key) is False
        )

    @pytest.mark.anyio
    async def test_duplicate_webhook_returns_200(
        self, client, sponsor_wallet, api_headers
    ):
        """
        Test that duplicate payment_intent webhooks don't cause errors.
        The UNIQUE constraint on payment_intent_id + IntegrityError catch
        should return 200 OK to stop Stripe's retry loop.
        """
        wallet_id = sponsor_wallet["wallet_id"]

        with patch(
            "app.services.stripe_integration.stripe.PaymentIntent.create"
        ) as mock_create:
            with patch(
                "app.services.stripe_integration.stripe.Webhook.construct_event"
            ) as mock_webhook:
                mock_create.return_value = MagicMock(
                    id="pi_duplicate_test",
                    client_secret="pi_duplicate_secret",
                )

                mock_webhook.return_value = succeeded_payment_intent(
                    payment_intent_id="pi_duplicate_test",
                    wallet_id=wallet_id,
                )

                resp1 = await client.post(
                    f"/v1/billing/top-up/prepare?wallet_id={wallet_id}&amount_fiat=50.0",
                    headers=api_headers,
                )
                assert resp1.status_code == 200

                resp2 = await client.post(
                    "/v1/webhooks/stripe",
                    content=b"duplicate_webhook_payload",
                    headers={"stripe-signature": "valid_sig_for_dup"},
                )
                assert resp2.status_code == 200

                wallet = await client.get(
                    f"/v1/billing/wallets/{wallet_id}", headers=api_headers
                )
                assert wallet.json()["balance"] == 50000.0

                # Stripe may redeliver the same verified event. The payment-intent
                # uniqueness constraint must keep the second delivery charge-neutral.
                resp3 = await client.post(
                    "/v1/webhooks/stripe",
                    content=b"duplicate_webhook_payload",
                    headers={"stripe-signature": "valid_sig_for_dup"},
                )
                assert resp3.status_code == 200

                wallet = await client.get(
                    f"/v1/billing/wallets/{wallet_id}", headers=api_headers
                )
                assert wallet.json()["balance"] == 50000.0


class TestNotificationService:
    """Tests for the notification service."""

    @pytest.mark.anyio
    async def test_low_balance_warning_skips_without_config(self):
        """Test that notifications are skipped when not configured."""
        from app.services.notifications import NotificationService

        service = NotificationService()
        service._slack_webhook_url = ""
        service._resend_api_key = ""

        await service.send_low_balance_warning(
            wallet_id="test-wallet",
            current_balance=Decimal("100"),
            threshold=Decimal("500"),
        )

    @pytest.mark.anyio
    async def test_wallet_frozen_alert_skips_without_config(self):
        """Test that frozen alerts are skipped when not configured."""
        from app.services.notifications import NotificationService

        service = NotificationService()
        service._slack_webhook_url = ""
        service._resend_api_key = ""

        await service.send_wallet_frozen_alert(
            wallet_id="test-wallet",
            reason="anomalous_spend",
        )

    @staticmethod
    def _service_with_transport(handler):
        import httpx

        from app.services.notifications import NotificationService

        service = NotificationService()
        service._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service._slack_webhook_url = ""
        service._resend_api_key = ""
        return service

    @pytest.mark.anyio
    @pytest.mark.parametrize("failure", ["status_404", "status_500", "transport"])
    async def test_failed_slack_alert_never_logs_the_webhook_url(self, caplog, failure):
        """The Slack webhook URL is the credential; a failed send must not log it."""
        import logging

        import httpx

        secret = "T0SENTINEL/B0SENTINEL/sentinelwebhooktoken"
        webhook = f"https://hooks.slack.com/services/{secret}"

        def handler(request: httpx.Request) -> httpx.Response:
            if failure == "transport":
                # Transport errors are free to embed the request URL too.
                raise httpx.ConnectError(f"connect to {request.url} failed")
            return httpx.Response(int(failure.split("_")[1]), text="no_service")

        service = self._service_with_transport(handler)
        service._slack_webhook_url = webhook
        try:
            with caplog.at_level(logging.DEBUG, logger="app.services.notifications"):
                await service.send_wallet_frozen_alert(
                    wallet_id="test-wallet", reason="anomalous_spend"
                )
        finally:
            await service.close()

        failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert failures, "the failed send should still be logged"
        assert "slack_alert_failed" in failures[0].getMessage()
        assert secret not in caplog.text
        assert "hooks.slack.com" not in caplog.text
        for record in caplog.records:
            assert secret not in record.getMessage()
            assert record.exc_info is None

    @pytest.mark.anyio
    async def test_failed_email_logs_status_not_exception_text(self, caplog):
        """A failed Resend call logs the status, never the HTTP stack's text."""
        import logging

        import httpx

        api_key = "re_sentinel_api_key_value"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == f"Bearer {api_key}"
            return httpx.Response(401, text="unauthorized")

        service = self._service_with_transport(handler)
        service._resend_api_key = api_key
        try:
            with caplog.at_level(logging.DEBUG, logger="app.services.notifications"):
                await service.send_email(
                    to="sponsor@example.com", subject="s", body="b"
                )
        finally:
            await service.close()

        failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert failures, "the failed send should still be logged"
        assert "status=401" in failures[0].getMessage()
        assert "api.resend.com" not in caplog.text
        assert api_key not in caplog.text
