"""Deep QA gaps across the money services.

Covers validation and safety behavior the existing suite does not pin down:

- pricing: exact-price override precedence and quote/charge unit conversion.
- child wallet TTL bounds (bools, zero, oversize).
- quote creation amount validation and release semantics.
- transfer argument validation and non-finite amounts moving no money.
- sponsor creation refusing negative seed credits (service level).
- refund idempotency and wallet scoping.
- receipt request-identity and reason-code validation.
- x402 requirement strictness (asset case, USDC precision, nonce determinism).
- Stripe settlement-field validation and the payment-failed notify path.

All tests are fast and deterministic: no sleeps, no network. DB-backed tests
use the shared ``clean_database`` fixture and the service facade directly.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, QuoteModel, WalletModel
from app.schemas.billing import ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.pricing import charge_units_for, tool_price
from app.services.quotes import QuoteError, QuoteService
from app.services.receipts import ReceiptError, ReceiptService
from app.services.stripe_integration import (
    StripeIntegration,
    StripeSettlementError,
)
from app.services.wallet_engine import (
    MAX_CHILD_WALLET_TTL_SECONDS,
    _validate_child_wallet_ttl,
)
from app.services.x402_engine import X402Error, X402PaymentHandler


def test_charge_units_for_roundtrips_through_default_price():
    for category in (
        ServiceCategory.AGENT_COMMS,
        ServiceCategory.PLATFORM_FEE,
        ServiceCategory.MEDIA_ENGINE,
    ):
        price = tool_price({}, category)
        credits = price * 3
        units = charge_units_for(credits, category)
        assert units == Decimal("3")


def test_tool_price_exact_override_wins_over_registered_price():
    service = {
        "credits_per_unit": Decimal("5.0"),
        "credits_per_unit_exact": Decimal("2.5"),
    }
    assert tool_price(service, ServiceCategory.AGENT_COMMS) == Decimal("2.5")


def test_tool_price_falls_back_to_category_default():
    assert tool_price({}, ServiceCategory.AGENT_COMMS) == Decimal("1.5")


@pytest.mark.parametrize(
    "bad_ttl",
    [True, False, 0, -1, -3600, MAX_CHILD_WALLET_TTL_SECONDS + 1, "3600", 3.5],
)
def test_child_wallet_ttl_rejects_bad_values(bad_ttl: Any):
    with pytest.raises((ValueError, TypeError)):
        _validate_child_wallet_ttl(bad_ttl)


@pytest.mark.parametrize("good_ttl", [None, 1, 60, MAX_CHILD_WALLET_TTL_SECONDS])
def test_child_wallet_ttl_accepts_bounds(good_ttl: Any):
    _validate_child_wallet_ttl(good_ttl)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_amount",
    [
        Decimal("-1"),
        Decimal("-0.00000001"),
        Decimal("0"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
async def test_quote_create_rejects_non_positive_or_non_finite(
    bad_amount: Decimal,
):
    service = QuoteService()
    with pytest.raises(QuoteError):
        await service.create_quote(
            wallet_id="wlt-qa-unused",
            tool="qa-tool",
            quoted_credits=bad_amount,
            category=ServiceCategory.AGENT_COMMS.value,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_amount", [Decimal("0"), Decimal("-5"), Decimal("NaN")])
async def test_transfer_rejects_non_positive_amounts(bad_amount: Decimal):
    money = get_agent_money()
    with pytest.raises(ValueError, match="Transfer amount must be positive"):
        await money.transfer(
            from_wallet_id="wlt-qa-a",
            to_wallet_id="wlt-qa-b",
            amount=bad_amount,
        )


@pytest.mark.asyncio
async def test_transfer_rejects_self_transfer():
    money = get_agent_money()
    with pytest.raises(ValueError, match="same wallet"):
        await money.transfer(
            from_wallet_id="wlt-qa-a",
            to_wallet_id="wlt-qa-a",
            amount=Decimal("1"),
        )


@pytest.mark.asyncio
async def test_is_wallet_or_descendant_matches_self_without_a_lookup():
    money = get_agent_money()
    assert await money.is_wallet_or_descendant("wlt-qa-a", "wlt-qa-a") is True


@pytest.mark.asyncio
async def test_sponsor_rejects_negative_seed_credits(clean_database):
    money = get_agent_money()
    with pytest.raises(ValueError, match="cannot be negative"):
        await money.create_sponsor_wallet(
            sponsor_name="Negative Seed",
            email="negative@example.com",
            initial_credits=Decimal("-100"),
        )
    factory = get_session_factory()
    async with factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(WalletModel)
                .where(WalletModel.owner_name == "Negative Seed")
            )
        ).scalar_one()
    assert count == 0


async def _funded_agent(money, *, sponsor_credits="1000", budget="100"):
    sponsor = await money.create_sponsor_wallet(
        sponsor_name="QA Sponsor",
        email="qa@example.com",
        initial_credits=Decimal(sponsor_credits),
        require_kyc=False,
    )
    agent = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor.wallet_id,
        agent_id="qa-agent",
        budget_credits=Decimal(budget),
    )
    return sponsor, agent


async def _ledger_count(wallet_id: str) -> int:
    factory = get_session_factory()
    async with factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(LedgerEntryModel.wallet_id == wallet_id)
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_non_finite_transfer_moves_no_money(clean_database):
    money = get_agent_money()
    sponsor, agent = await _funded_agent(money)
    before = (await money.get_wallet(agent.wallet_id)).balance
    with pytest.raises(Exception):
        await money.transfer(
            from_wallet_id=agent.wallet_id,
            to_wallet_id=sponsor.wallet_id,
            amount=Decimal("Infinity"),
        )
    with pytest.raises(Exception):
        await money.transfer(
            from_wallet_id=agent.wallet_id,
            to_wallet_id=sponsor.wallet_id,
            amount=Decimal("NaN"),
        )
    after = await money.get_wallet(agent.wallet_id)
    assert after.balance == before
    assert await _ledger_count(agent.wallet_id) == 1


@pytest.mark.asyncio
async def test_infinite_transfer_is_rejected_before_debit(clean_database):
    money = get_agent_money()
    sponsor, agent = await _funded_agent(money)
    with pytest.raises(ValueError, match="Transfer amount must be positive"):
        await money.transfer(
            from_wallet_id=agent.wallet_id,
            to_wallet_id=sponsor.wallet_id,
            amount=Decimal("Infinity"),
        )


@pytest.mark.asyncio
async def test_refund_recredit_is_idempotent_and_scoped(clean_database):
    money = get_agent_money()
    _, agent = await _funded_agent(money)
    charge = await money.charge(
        wallet_id=agent.wallet_id,
        service_category=ServiceCategory.PLATFORM_FEE,
        units=Decimal("2"),
    )
    assert charge.balance_after is not None
    first = await money.refund_charge(
        wallet_id=agent.wallet_id,
        charge_entry_id=charge.entry_id,
    )
    second = await money.refund_charge(
        wallet_id=agent.wallet_id,
        charge_entry_id=charge.entry_id,
    )
    assert first.entry_id == second.entry_id
    wallet = await money.get_wallet(agent.wallet_id)
    assert wallet.balance == 100.0
    with pytest.raises(ValueError, match="not found"):
        await money.refund_charge(
            wallet_id=agent.wallet_id,
            charge_entry_id="ledger-entry-that-does-not-exist",
        )


@pytest.mark.asyncio
async def test_refund_cannot_redeem_another_wallets_charge(clean_database):
    money = get_agent_money()
    sponsor, agent = await _funded_agent(money)
    other = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor.wallet_id,
        agent_id="qa-agent-two",
        budget_credits=Decimal("50"),
    )
    charge = await money.charge(
        wallet_id=agent.wallet_id,
        service_category=ServiceCategory.PLATFORM_FEE,
        units=Decimal("1"),
    )
    with pytest.raises(ValueError, match="not found"):
        await money.refund_charge(
            wallet_id=other.wallet_id,
            charge_entry_id=charge.entry_id,
        )


@pytest.mark.asyncio
async def test_quote_release_only_restores_a_live_consumed_quote(clean_database):
    money = get_agent_money()
    _, agent = await _funded_agent(money)
    service = QuoteService()
    quote = await service.create_quote(
        wallet_id=agent.wallet_id,
        tool="qa-tool",
        quoted_credits=Decimal("2"),
        category=ServiceCategory.AGENT_COMMS.value,
    )
    assert await service.release(quote.quote_id) is False
    assert await service.consume(quote.quote_id) is True
    assert await service.release(quote.quote_id) is True
    assert await service.consume(quote.quote_id) is True
    assert await service.release(quote.quote_id) is True
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            model = await session.get(QuoteModel, quote.quote_id)
            assert model is not None
            model.expires_at = model.expires_at - timedelta(seconds=7200)
    assert await service.consume(quote.quote_id) is False
    assert await service.release(quote.quote_id) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_amount", [Decimal("-1"), Decimal("NaN")])
async def test_receipt_rejects_unstorable_credit_amounts(bad_amount: Decimal):
    service = ReceiptService()
    with pytest.raises(ReceiptError, match="receipt_credits_invalid"):
        await service.create_receipt(
            permit_id="permit-qa-missing",
            wallet_id="wlt-qa-a",
            key_id=None,
            tool="qa-tool",
            request_payload={"hello": "world"},
            response_payload={"ok": True},
            ledger_entry_id=None,
            credits_authorized=bad_amount,
            credits_charged=Decimal("1"),
            outcome="success",
            audit_event_id=None,
        )


@pytest.mark.asyncio
async def test_receipt_rejects_success_with_a_reason_code(clean_database):
    service = ReceiptService()
    with pytest.raises(ReceiptError, match="receipt_reason_code_invalid"):
        await service.create_receipt(
            permit_id="permit-qa-missing",
            wallet_id="wlt-qa-a",
            key_id=None,
            tool="qa-tool",
            request_payload={"hello": "world"},
            response_payload={"ok": True},
            ledger_entry_id=None,
            credits_authorized=Decimal("1"),
            credits_charged=Decimal("1"),
            outcome="success",
            reason_code="permit_budget_exceeded",
            audit_event_id=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{}, {"request_hash": "ab" * 32}])
async def test_receipt_rejects_ambiguous_request_identity(
    clean_database, kwargs: dict[str, Any]
):
    service = ReceiptService()
    payload: dict[str, Any] | None = None
    if kwargs:
        payload = {"hello": "world"}
    with pytest.raises(ReceiptError, match="receipt_request_identity_invalid"):
        await service.create_receipt(
            permit_id="permit-qa-missing",
            wallet_id="wlt-qa-a",
            key_id=None,
            tool="qa-tool",
            request_payload=payload,
            response_payload={"ok": True},
            ledger_entry_id=None,
            credits_authorized=Decimal("1"),
            credits_charged=Decimal("1"),
            outcome="success",
            audit_event_id=None,
            **kwargs,
        )


EVM_PAY_TO = "0x1111111111111111111111111111111111111111"


def test_x402_build_requirement_accepts_six_decimal_usdc():
    handler = X402PaymentHandler()
    requirement = handler.build_requirement(
        amount="1.123456", pay_to=EVM_PAY_TO, network="base"
    )
    assert requirement.amount_usd == Decimal("1.123456")
    assert requirement.amount_base_units == 1123456
    assert requirement.asset == "USDC"


@pytest.mark.parametrize(
    "amount",
    ["0", "-1.5", "0.0000000", "1.1234567", "10000.000001", "nan", "inf", "abc", ""],
)
def test_x402_build_requirement_rejects_bad_amounts(amount: str):
    handler = X402PaymentHandler()
    with pytest.raises(X402Error):
        handler.build_requirement(amount=amount, pay_to=EVM_PAY_TO, network="base")


def test_x402_build_requirement_rejects_non_usdc_asset_case():
    handler = X402PaymentHandler()
    with pytest.raises(X402Error, match="x402_asset_unsupported"):
        handler.build_requirement(
            amount="1", pay_to=EVM_PAY_TO, network="base", asset="usdc"
        )


def test_x402_build_requirement_rejects_unknown_network_and_pay_to():
    handler = X402PaymentHandler()
    with pytest.raises(X402Error, match="x402_network_unsupported"):
        handler.build_requirement(amount="1", pay_to=EVM_PAY_TO, network="mainnet-evil")
    with pytest.raises(X402Error, match="x402_pay_to_invalid"):
        handler.build_requirement(amount="1", pay_to="not-an-address", network="base")


def test_x402_parse_accepts_any_header_case():
    handler = X402PaymentHandler()
    requirement = handler.parse_402(
        402,
        {
            "X-402-AMOUNT": "2.5",
            "X-402-PAYTO": EVM_PAY_TO,
            "X-402-NETWORK": "base",
        },
    )
    assert requirement.amount_usd == Decimal("2.5")


def test_x402_nonce_is_deterministic_per_permit_and_key():
    handler = X402PaymentHandler()
    first = handler._derive_nonce_hex("permit-1", "key-a")
    assert first == handler._derive_nonce_hex("permit-1", "key-a")
    assert len(first) == 64
    assert handler._derive_nonce_hex("permit-1", "key-b") != first
    assert handler._derive_nonce_hex("permit-2", "key-a") != first


def _payment_intent(**overrides: Any) -> dict[str, Any]:
    rate = get_settings().EXCHANGE_RATE
    credits = Decimal(500) * rate / Decimal("100")
    intent: dict[str, Any] = {
        "id": "pi_qa_1",
        "status": "succeeded",
        "currency": "usd",
        "amount": 500,
        "amount_received": 500,
        "metadata": {"wallet_id": "spn-qa", "credits": str(credits)},
    }
    intent.update(overrides)
    return intent


def test_stripe_payment_intent_validation_accepts_settled_usd():
    rate = get_settings().EXCHANGE_RATE
    _, wallet_id, credits = StripeIntegration._validate_succeeded_payment_intent(
        _payment_intent()
    )
    assert wallet_id == "spn-qa"
    assert credits == Decimal(500) * rate / Decimal("100")


@pytest.mark.parametrize(
    "override",
    [
        {"status": "requires_payment_method"},
        {"currency": "eur"},
        {"amount_received": 400},
        {"amount": 0},
        {"amount": True},
        {"metadata": {"wallet_id": "spn-qa", "credits": "999999"}},
        {"metadata": {"wallet_id": "", "credits": "5000"}},
        {"metadata": None},
        {"id": ""},
    ],
)
def test_stripe_payment_intent_validation_rejects(override: dict[str, Any]):
    with pytest.raises(StripeSettlementError):
        StripeIntegration._validate_succeeded_payment_intent(
            _payment_intent(**override)
        )


def _refund_charge(**overrides: Any) -> dict[str, Any]:
    charge: dict[str, Any] = {
        "id": "ch_qa_1",
        "payment_intent": "pi_qa_1",
        "currency": "usd",
        "amount": 500,
        "amount_refunded": 200,
    }
    charge.update(overrides)
    return charge


def test_stripe_refund_charge_validation_accepts_partial():
    payment_intent_id, charge_amount, refunded, _ = (
        StripeIntegration._validate_refund_charge(_refund_charge())
    )
    assert (payment_intent_id, charge_amount, refunded) == ("pi_qa_1", 500, 200)


@pytest.mark.parametrize(
    "override",
    [
        {"amount_refunded": 0},
        {"amount_refunded": 600},
        {"amount_refunded": True},
        {"amount": 0},
        {"currency": "eur"},
        {"payment_intent": ""},
        {"payment_intent": None},
    ],
)
def test_stripe_refund_charge_validation_rejects(override: dict[str, Any]):
    with pytest.raises(StripeSettlementError):
        StripeIntegration._validate_refund_charge(_refund_charge(**override))


@pytest.mark.asyncio
async def test_stripe_payment_failed_notifies_wallet_owner(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def _fake_alert(*, wallet_id, error_message, payment_intent_id):
        calls.append(
            {
                "wallet_id": wallet_id,
                "error_message": error_message,
                "payment_intent_id": payment_intent_id,
            }
        )

    class _FakeNotifications:
        send_payment_failed_alert = staticmethod(_fake_alert)

    monkeypatch.setattr(
        "app.services.notifications.get_notification_service",
        lambda: _FakeNotifications(),
    )
    integration = StripeIntegration()
    await integration._handle_payment_failed(
        {
            "id": "pi_qa_failed",
            "metadata": {"wallet_id": "spn-qa"},
            "last_payment_error": {"message": "card declined"},
        }
    )
    assert calls == [
        {
            "wallet_id": "spn-qa",
            "error_message": "card declined",
            "payment_intent_id": "pi_qa_failed",
        }
    ]


@pytest.mark.asyncio
async def test_stripe_payment_failed_without_wallet_sends_nothing(monkeypatch):
    def _boom() -> Any:
        raise AssertionError("no notification should be sent")

    monkeypatch.setattr("app.services.notifications.get_notification_service", _boom)
    integration = StripeIntegration()
    await integration._handle_payment_failed({"id": "pi_qa_failed", "metadata": {}})


@pytest.mark.asyncio
async def test_receipt_rejects_malformed_request_hash():
    service = ReceiptService()
    with pytest.raises(ReceiptError, match="receipt_request_hash_invalid"):
        await service.create_receipt(
            permit_id="permit-qa-missing",
            wallet_id="wlt-qa-a",
            key_id=None,
            tool="qa-tool",
            request_payload=None,
            response_payload={"ok": True},
            ledger_entry_id=None,
            credits_authorized=Decimal("1"),
            credits_charged=Decimal("1"),
            outcome="success",
            audit_event_id=None,
            request_hash="not-hex-" + "z" * 56,
        )
