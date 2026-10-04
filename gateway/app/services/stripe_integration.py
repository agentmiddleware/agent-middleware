"""
Stripe Fiat Ingestion Service
Handles payment intents and webhook processing for fiat top-ups.

Architecture:
1. Client calls /top-up/prepare → creates Stripe PaymentIntent
2. Client completes payment via Stripe.js in browser
3. Stripe sends webhook to /webhooks/stripe
4. Webhook handler mints credits to the wallet
5. If Stripe retries webhook, only duplicate payment intents are treated as idempotent
"""

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, cast
from uuid import uuid4

import stripe
from anyio import to_thread
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select, update as sa_update
from sqlalchemy.sql.elements import ColumnElement

from ..db.database import get_session_factory
from ..db.models import BillingAlertModel, LedgerEntryModel, WalletModel
from ..core.config import get_settings
from ..core.time import utc_now
from ..core.resilience import run_with_write_conflict_retry
from ..schemas.billing import AlertSeverity, AlertType, WalletStatus
from .agent_money import WalletNotFoundError

logger = logging.getLogger(__name__)
settings = get_settings()

stripe.api_key = settings.STRIPE_SECRET_KEY

# Explicit HTTP timeout on the shared Stripe client (the SDK's RequestsClient
# default, stated here so it is a contract rather than an accident). The ACP
# bridge's stale-intent recovery depends on it: the only long-blocking step in
# a live checkout is this client's call, so its timeout must stay well under
# acp_bridge._INTENT_STALE_SECONDS (300s) for "idle past the threshold" to
# imply "not a live attempt".
STRIPE_HTTP_TIMEOUT_SECONDS = 80
stripe.default_http_client = stripe.new_default_http_client(
    timeout=STRIPE_HTTP_TIMEOUT_SECONDS
)

SUPPORTED_TOP_UP_CURRENCY = "usd"


class StripeSettlementError(ValueError):
    """A verified Stripe event does not prove an acceptable top-up settlement."""


class _RefundSnapshotChanged(RuntimeError):
    """A competing cumulative refund committed after the ledger was read."""


class StripeIntegration:
    """
    Handles fiat top-ups via Stripe Payment Intents.

    Uses async SQLAlchemy sessions for non-blocking database operations.
    Implements idempotency via UNIQUE constraint on payment_intent_id.
    """

    def __init__(self):
        self._session_factory = get_session_factory

    @staticmethod
    def _stripe_value(container: Any, key: str, default: Any = None) -> Any:
        """Read dict and stripe.StripeObject fields without trusting attributes."""
        try:
            return container[key]
        except (KeyError, TypeError, AttributeError):
            return default

    @staticmethod
    def _is_duplicate_payment_intent_error(exc: IntegrityError) -> bool:
        """Return true only for the idempotency unique constraint."""
        message = str(getattr(exc, "orig", exc)).lower()
        return "payment_intent_id" in message and (
            "unique" in message or "duplicate" in message
        )

    async def create_top_up_intent(
        self,
        wallet_id: str,
        amount_fiat: Decimal,
        currency: str = "usd",
    ) -> dict:
        """
        Create a Stripe PaymentIntent for fiat top-up.

        Args:
            wallet_id: The wallet to credit after successful payment
            amount_fiat: Amount in fiat currency (e.g., 50.00 for $50)
            currency: ISO 4217 currency code (default: usd)

        Returns:
            {
                "client_secret": str,
                "payment_intent_id": str,
                "amount_credits": int,
                "amount_fiat": float,
                "currency": str,
            }
        """
        normalized_currency = currency.lower()
        if normalized_currency != SUPPORTED_TOP_UP_CURRENCY:
            raise ValueError("unsupported_top_up_currency: only USD is supported")
        if not amount_fiat.is_finite() or amount_fiat <= 0:
            raise ValueError("invalid_top_up_amount")

        amount_cents_decimal = amount_fiat * Decimal("100")
        if amount_cents_decimal != amount_cents_decimal.to_integral_value():
            raise ValueError("invalid_top_up_amount: use no more than two decimals")
        amount_cents = int(amount_cents_decimal)

        async with self._session_factory()() as session:
            result = await session.execute(
                # SQLModel fields are plain Python types (not Mapped[...]), so mypy
                # sees this comparison as bool rather than a ColumnElement[bool].
                # Root cause lives in app/db/models.py (out of scope here).
                select(WalletModel).where(WalletModel.wallet_id == wallet_id)  # type: ignore[arg-type]
            )
            wallet = result.scalar_one_or_none()
            if not wallet:
                raise WalletNotFoundError(wallet_id)
            if wallet.wallet_type != "sponsor":
                raise ValueError("top_up_wallet_must_be_sponsor")

        credits = Decimal(amount_cents) * settings.EXCHANGE_RATE / Decimal("100")
        if not credits.is_finite() or credits <= 0:
            raise ValueError("invalid_top_up_exchange_rate")
        credits_metadata = (
            str(int(credits))
            if credits == credits.to_integral_value()
            else format(credits.normalize(), "f")
        )
        credits_response: int | float = (
            int(credits) if credits == credits.to_integral_value() else float(credits)
        )

        def _create_top_up_payment_intent() -> Any:
            return stripe.PaymentIntent.create(
                amount=amount_cents,
                currency=SUPPORTED_TOP_UP_CURRENCY,
                metadata={
                    "wallet_id": wallet_id,
                    "credits": credits_metadata,
                    "idempotency_key": str(uuid4()),
                },
            )

        # The Stripe SDK call is synchronous network I/O; run it on a worker
        # thread so it never blocks the event loop (same offload pattern as
        # charge_shared_payment_token).
        intent = await to_thread.run_sync(_create_top_up_payment_intent)

        logger.info(
            f"Created PaymentIntent {intent.id} for wallet {wallet_id}: "
            f"${amount_fiat} -> {credits} credits"
        )

        return {
            "client_secret": intent.client_secret,
            "payment_intent_id": intent.id,
            "amount_credits": credits_response,
            "amount_fiat": float(amount_fiat),
            "currency": SUPPORTED_TOP_UP_CURRENCY.upper(),
        }

    async def charge_shared_payment_token(
        self,
        *,
        spt_token: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
    ) -> dict:
        """Create and confirm a PaymentIntent from a Shared Payment Token.

        Pure outbound-charge helper for the ACP commerce bridge: it never
        touches wallets, the ledger, or ``_mint_credits`` — settlement
        evidence is recorded by the caller (permit reservation, signed
        receipt, audit chain), and no credits are ever minted from this path.
        ``idempotency_key`` is passed through to Stripe so a retried checkout
        with the same key cannot charge twice. The token itself is never
        logged.
        """
        normalized_currency = currency.lower()
        if normalized_currency != SUPPORTED_TOP_UP_CURRENCY:
            raise ValueError("unsupported_acp_currency: only USD is supported")
        if (
            isinstance(amount_minor, bool)
            or not isinstance(amount_minor, int)
            or amount_minor <= 0
        ):
            raise ValueError("invalid_acp_charge_amount")
        if not spt_token:
            raise ValueError("missing_shared_payment_token")

        def _create_payment_intent() -> Any:
            return stripe.PaymentIntent.create(
                amount=amount_minor,
                currency=normalized_currency,
                payment_method=spt_token,
                confirm=True,
                idempotency_key=idempotency_key,
            )

        # The Stripe SDK call is synchronous network I/O; run it on a worker
        # thread so it never blocks the event loop mid-checkout.
        intent = await to_thread.run_sync(_create_payment_intent)

        intent_id = self._stripe_value(intent, "id")
        logger.info(
            "Confirmed shared-payment-token PaymentIntent %s for %s minor units",
            intent_id,
            amount_minor,
        )

        return {
            "payment_intent_id": intent_id,
            "status": self._stripe_value(intent, "status"),
            "amount": self._stripe_value(intent, "amount"),
            "currency": self._stripe_value(intent, "currency"),
        }

    async def cancel_payment_intent(
        self,
        payment_intent_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """Cancel a not-yet-settled PaymentIntent.

        Used by the ACP commerce bridge when a Shared Payment Token charge
        confirms but does not settle (e.g. "requires_action"): the refused
        checkout is rolled back on our side, so the still-live PaymentIntent
        must not be left able to settle later with no governance record.
        Only cancelable statuses should reach this call (Stripe refuses to
        cancel e.g. a "processing" intent); the caller treats failure as
        best-effort evidence, never as a masking error. No wallet, ledger,
        or minting logic is touched here.
        """
        if not payment_intent_id:
            raise ValueError("missing_payment_intent_id")

        def _cancel_payment_intent() -> Any:
            kwargs: dict[str, Any] = {}
            if idempotency_key is not None:
                kwargs["idempotency_key"] = idempotency_key
            return stripe.PaymentIntent.cancel(payment_intent_id, **kwargs)

        # The Stripe SDK call is synchronous network I/O; run it on a worker
        # thread so it never blocks the event loop (same offload pattern as
        # charge_shared_payment_token).
        intent = await to_thread.run_sync(_cancel_payment_intent)

        logger.info(
            "Canceled PaymentIntent %s (status=%s)",
            self._stripe_value(intent, "id"),
            self._stripe_value(intent, "status"),
        )

        return {
            "payment_intent_id": self._stripe_value(intent, "id"),
            "status": self._stripe_value(intent, "status"),
        }

    async def handle_webhook(
        self,
        payload: bytes,
        sig_header: str,
    ) -> bool:
        """
        Process Stripe webhook events.

        Args:
            payload: Raw request body bytes
            sig_header: Stripe-Signature header value

        Returns:
            True if processed successfully, False on signature failure
        Raises:
            ValueError: For unhandled event types
        """
        try:
            event = stripe.Webhook.construct_event(
                payload,
                sig_header,
                settings.STRIPE_WEBHOOK_SECRET,
            )
        except (ValueError, stripe.SignatureVerificationError) as e:
            logger.error(f"Invalid Stripe signature: {e}")
            return False

        # The refund handler is dispatched separately because it also needs the
        # event id for idempotent debiting; the rest take just the event object.
        handler_map = {
            "payment_intent.succeeded": self._handle_payment_success,
            "payment_intent.payment_failed": self._handle_payment_failed,
        }

        event_type = self._stripe_value(event, "type")
        event_data = self._stripe_value(event, "data")
        event_object = self._stripe_value(event_data, "object")
        if event_type == "charge.refunded":
            await self._handle_refund(
                event_object,
                self._stripe_value(event, "id"),
            )
        elif event_type in handler_map:
            await handler_map[event_type](event_object)
        else:
            logger.debug(f"Ignoring unhandled event type: {event_type}")

        return True

    async def _handle_payment_success(self, payment_intent: Any) -> None:
        """Mint credits only from a fully settled USD PaymentIntent."""
        payment_intent_id, wallet_id, credits = self._validate_succeeded_payment_intent(
            payment_intent
        )

        await self._mint_credits(
            wallet_id=wallet_id,
            amount=credits,
            payment_intent_id=payment_intent_id,
            description=f"Fiat top-up via Stripe ({payment_intent_id})",
        )

        logger.info(
            f"Minted {credits} credits to wallet {wallet_id} "
            f"from PaymentIntent {payment_intent_id}"
        )

    @staticmethod
    def _validate_succeeded_payment_intent(
        payment_intent: Any,
    ) -> tuple[str, str, Decimal]:
        """Derive credits from Stripe settlement fields and verify metadata."""
        payment_intent_id = StripeIntegration._stripe_value(payment_intent, "id")
        if not isinstance(payment_intent_id, str) or not payment_intent_id:
            raise StripeSettlementError("missing_payment_intent_id")
        if StripeIntegration._stripe_value(payment_intent, "status") != "succeeded":
            raise StripeSettlementError("payment_intent_not_succeeded")

        currency = StripeIntegration._stripe_value(payment_intent, "currency")
        if (
            not isinstance(currency, str)
            or currency.lower() != SUPPORTED_TOP_UP_CURRENCY
        ):
            raise StripeSettlementError("unsupported_top_up_currency")

        amount = StripeIntegration._stripe_value(payment_intent, "amount")
        amount_received = StripeIntegration._stripe_value(
            payment_intent, "amount_received"
        )
        if (
            isinstance(amount, bool)
            or not isinstance(amount, int)
            or isinstance(amount_received, bool)
            or not isinstance(amount_received, int)
            or amount <= 0
            or amount_received != amount
        ):
            raise StripeSettlementError("payment_intent_amount_mismatch")

        credits = Decimal(amount_received) * settings.EXCHANGE_RATE / Decimal("100")
        if not credits.is_finite() or credits <= 0:
            raise StripeSettlementError("invalid_settled_credit_amount")

        metadata = StripeIntegration._stripe_value(payment_intent, "metadata")
        if metadata is None:
            raise StripeSettlementError("missing_payment_intent_metadata")
        wallet_id = StripeIntegration._stripe_value(metadata, "wallet_id")
        if not isinstance(wallet_id, str) or not wallet_id.strip():
            raise StripeSettlementError("missing_wallet_metadata")

        try:
            metadata_credits = Decimal(
                str(StripeIntegration._stripe_value(metadata, "credits"))
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise StripeSettlementError("invalid_credit_metadata") from exc
        if (
            not metadata_credits.is_finite()
            or metadata_credits <= 0
            or metadata_credits != credits
        ):
            raise StripeSettlementError("credit_metadata_mismatch")

        return payment_intent_id, wallet_id, credits

    async def _handle_payment_failed(self, payment_intent: dict) -> None:
        """Log payment failure and notify via Slack."""
        from ..services.notifications import get_notification_service

        wallet_id = payment_intent["metadata"].get("wallet_id")
        error_msg = payment_intent.get("last_payment_error", {}).get(
            "message", "Unknown error"
        )

        logger.warning(f"Payment failed for wallet {wallet_id}: {error_msg}")

        if wallet_id:
            notifications = get_notification_service()
            await notifications.send_payment_failed_alert(
                wallet_id=wallet_id,
                error_message=error_msg,
                payment_intent_id=payment_intent["id"],
            )

    @staticmethod
    def _validate_refund_charge(charge: Any) -> tuple[str, int, int, str | None]:
        """Validate authoritative cumulative refund fields from Stripe."""
        payment_intent_id = StripeIntegration._stripe_value(charge, "payment_intent")
        if not isinstance(payment_intent_id, str) or not payment_intent_id:
            raise StripeSettlementError("missing_refund_payment_intent_id")

        currency = StripeIntegration._stripe_value(charge, "currency")
        if (
            not isinstance(currency, str)
            or currency.lower() != SUPPORTED_TOP_UP_CURRENCY
        ):
            raise StripeSettlementError("unsupported_refund_currency")

        charge_amount = StripeIntegration._stripe_value(charge, "amount")
        if (
            isinstance(charge_amount, bool)
            or not isinstance(charge_amount, int)
            or charge_amount <= 0
        ):
            raise StripeSettlementError("invalid_charge_amount")

        amount_refunded = StripeIntegration._stripe_value(charge, "amount_refunded")
        if (
            isinstance(amount_refunded, bool)
            or not isinstance(amount_refunded, int)
            or amount_refunded <= 0
        ):
            raise StripeSettlementError("invalid_refund_amount")
        if amount_refunded > charge_amount:
            raise StripeSettlementError("refund_exceeds_charge_amount")

        charge_id = StripeIntegration._stripe_value(charge, "id")
        normalized_charge_id = (
            charge_id if isinstance(charge_id, str) and charge_id else None
        )
        return (
            payment_intent_id,
            charge_amount,
            amount_refunded,
            normalized_charge_id,
        )

    async def _handle_refund(self, charge: Any, event_id: str | None = None) -> None:
        await run_with_write_conflict_retry(
            lambda: self._handle_refund_once(charge, event_id),
            restart_on=lambda exc: isinstance(exc, _RefundSnapshotChanged),
            on_exhausted=lambda exc: RuntimeError("stripe_refund_write_contended"),
        )

    async def _handle_refund_once(self, charge: Any, event_id: str | None) -> None:
        """Apply only the new delta from Stripe's cumulative refund amount."""
        (
            payment_intent_id,
            charge_amount,
            amount_refunded,
            charge_id,
        ) = self._validate_refund_charge(charge)
        description = f"Refund for PaymentIntent {payment_intent_id}"
        stripe_event_id = event_id or charge_id

        try:
            async with self._session_factory()() as session:
                async with session.begin():
                    credit_result = await session.execute(
                        select(LedgerEntryModel)
                        .where(
                            LedgerEntryModel.payment_intent_id == payment_intent_id  # type: ignore[arg-type]
                        )
                        .with_for_update()
                    )
                    credit_entry = credit_result.scalar_one_or_none()
                    if not credit_entry:
                        raise StripeSettlementError("refund_payment_intent_not_found")
                    if credit_entry.amount <= 0:
                        raise StripeSettlementError("invalid_original_credit_amount")

                    wallet_result = await session.execute(
                        select(WalletModel)
                        .where(
                            WalletModel.wallet_id == credit_entry.wallet_id  # type: ignore[arg-type]
                        )
                        .with_for_update()
                    )
                    wallet = wallet_result.scalar_one_or_none()
                    if not wallet:
                        raise StripeSettlementError("refund_wallet_not_found")
                    if wallet.wallet_type != "sponsor":
                        raise StripeSettlementError("refund_wallet_must_be_sponsor")

                    prior_result = await session.execute(
                        select(LedgerEntryModel).where(
                            LedgerEntryModel.wallet_id == credit_entry.wallet_id,  # type: ignore[arg-type]
                            LedgerEntryModel.action == "refund",  # type: ignore[arg-type]
                            LedgerEntryModel.description == description,  # type: ignore[arg-type]
                        )
                    )
                    prior_refunds = prior_result.scalars().all()
                    already_refunded = sum(
                        (-entry.amount for entry in prior_refunds),
                        Decimal("0"),
                    )
                    cumulative_refund = (
                        credit_entry.amount
                        * Decimal(amount_refunded)
                        / Decimal(charge_amount)
                    )
                    refund_delta = cumulative_refund - already_refunded

                    # Stripe's amount_refunded is cumulative. An older event can
                    # arrive after a newer one; it must never reverse or repeat a
                    # debit already reflected in the ledger.
                    if refund_delta <= 0:
                        logger.info(
                            "Ignoring stale or duplicate refund event %s for %s",
                            stripe_event_id,
                            payment_intent_id,
                        )
                        return
                    # Apply the clawback relatively, in one statement. The
                    # cumulative arithmetic above already makes *this* event
                    # idempotent, but the write itself was still a
                    # read-modify-write: a charge landing on the same sponsor
                    # wallet between the read and the write is silently erased,
                    # and here that discrepancy is against real fiat Stripe has
                    # already returned.
                    # FOR UPDATE does not serialize readers on SQLite. Admit
                    # this delta only while the refund ledger still matches the
                    # snapshot used to calculate it; a loser restarts its whole
                    # transaction. Count rows rather than summing SQL floats.
                    refund_count = (
                        select(func.count())
                        .select_from(LedgerEntryModel)
                        .where(
                            cast(
                                ColumnElement[bool],
                                LedgerEntryModel.wallet_id == credit_entry.wallet_id,
                            ),
                            cast(
                                ColumnElement[bool], LedgerEntryModel.action == "refund"
                            ),
                            cast(
                                ColumnElement[bool],
                                LedgerEntryModel.description == description,
                            ),
                        )
                        .scalar_subquery()
                    )
                    clawback = await session.execute(
                        sa_update(WalletModel)
                        .where(
                            cast(
                                ColumnElement[bool],
                                WalletModel.wallet_id == credit_entry.wallet_id,
                            ),
                            refund_count == len(prior_refunds),
                        )
                        .values(
                            balance=WalletModel.balance - refund_delta,
                            lifetime_debits=WalletModel.lifetime_debits + refund_delta,
                            # WalletModel.updated_at has a default but no
                            # onupdate, and a Core UPDATE bypasses the ORM
                            # attribute write, so nothing advances it here
                            # unless it is set. A fiat clawback is the most
                            # consequential balance move the wallet sees; it
                            # must not be the one that leaves the row looking
                            # untouched. Every other balance-moving wallet
                            # UPDATE in this codebase sets it.
                            updated_at=utc_now(),
                        )
                        .returning(cast(ColumnElement[str], WalletModel.wallet_id))
                        .execution_options(synchronize_session=False)
                    )
                    if clawback.scalar_one_or_none() is None:
                        raise _RefundSnapshotChanged()
                    # The liability and freeze decision below turn on the
                    # balance this clawback produced, so read it back first.
                    await session.refresh(wallet)
                    refund_liability = max(Decimal("0"), -wallet.balance)
                    wallet_status_before_refund = wallet.status
                    wallet_frozen_for_liability = False
                    if refund_liability > 0:
                        # Stripe has already returned the fiat, so rejecting the
                        # event because credits were spent would leave the books
                        # wrong forever. Preserve the negative balance as the
                        # sponsor's durable liability and contain further spend.
                        # Do not weaken a pre-existing closed/suspended state.
                        if wallet.status not in {
                            WalletStatus.FROZEN.value,
                            WalletStatus.SUSPENDED.value,
                            WalletStatus.CLOSED.value,
                        }:
                            wallet.status = WalletStatus.FROZEN.value
                        wallet_frozen_for_liability = (
                            wallet.status == WalletStatus.FROZEN.value
                        )
                        session.add(
                            BillingAlertModel(
                                alert_id=str(uuid4())[:12],
                                wallet_id=wallet.wallet_id,
                                alert_type=AlertType.SUSPICIOUS_ACTIVITY.value,
                                current_balance=wallet.balance,
                                message=(
                                    f"Stripe refund created a {refund_liability} "
                                    "credit liability. Wallet contained with status "
                                    f"{wallet.status} (previously "
                                    f"{wallet_status_before_refund}) pending review."
                                ),
                                severity=AlertSeverity.CRITICAL.value,
                            )
                        )
                        logger.critical(
                            "Stripe refund created a %s credit liability for "
                            "wallet %s; wallet status=%s pending review.",
                            refund_liability,
                            wallet.wallet_id,
                            wallet.status,
                        )
                    session.add(wallet)
                    session.add(
                        LedgerEntryModel(
                            entry_id=str(uuid4()),
                            wallet_id=wallet.wallet_id,
                            action="refund",
                            amount=-refund_delta,
                            balance_after=wallet.balance,
                            description=description,
                            stripe_event_id=stripe_event_id,
                            metadata_json=json.dumps(
                                {
                                    "payment_intent_id": payment_intent_id,
                                    "stripe_charge_amount": charge_amount,
                                    "stripe_amount_refunded": amount_refunded,
                                    "refund_delta_credits": str(refund_delta),
                                    "refund_liability_credits": str(refund_liability),
                                    "wallet_frozen_for_refund_liability": (
                                        wallet_frozen_for_liability
                                    ),
                                    "wallet_status_before_refund": (
                                        wallet_status_before_refund
                                    ),
                                    "wallet_status_after_refund": wallet.status,
                                },
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                    )
        except IntegrityError as exc:
            if not self._is_duplicate_stripe_event_error(exc):
                logger.exception(
                    "Stripe refund debit failed with non-idempotency integrity "
                    "error for event %s",
                    stripe_event_id,
                )
                raise
            logger.info(
                "Idempotency catch: refund event %s already processed. "
                "Swallowing duplicate to avoid a second debit.",
                stripe_event_id,
            )

    async def _mint_credits(
        self,
        wallet_id: str,
        amount: Decimal,
        payment_intent_id: str,
        description: str,
    ) -> None:
        """
        Mint credits to a wallet with idempotency.

        The UNIQUE constraint on payment_intent_id ensures we never double-mint.
        Only that duplicate-key failure is swallowed to stop Stripe retries; other
        integrity errors must surface so a real payment is not silently dropped.
        """
        try:
            async with self._session_factory()() as session:
                async with session.begin():
                    result = await session.execute(
                        select(WalletModel)
                        .where(WalletModel.wallet_id == wallet_id)  # type: ignore[arg-type]  # see app/db/models.py
                        .with_for_update()
                    )
                    wallet = result.scalar_one_or_none()

                    if not wallet:
                        raise StripeSettlementError("top_up_wallet_not_found")
                    if wallet.wallet_type != "sponsor":
                        raise StripeSettlementError("top_up_wallet_must_be_sponsor")

                    await session.execute(
                        sa_update(WalletModel)
                        .where(
                            cast(
                                ColumnElement[bool], WalletModel.wallet_id == wallet_id
                            )
                        )
                        .values(
                            balance=WalletModel.balance + amount,
                            lifetime_credits=WalletModel.lifetime_credits + amount,
                            updated_at=utc_now(),
                        )
                        .execution_options(synchronize_session=False)
                    )
                    await session.refresh(wallet)

                    entry = LedgerEntryModel(
                        entry_id=str(uuid4()),
                        wallet_id=wallet_id,
                        action="credit",
                        amount=amount,
                        balance_after=wallet.balance,
                        payment_intent_id=payment_intent_id,
                        description=description,
                    )
                    session.add(entry)

                await session.commit()

        except IntegrityError as exc:
            if not self._is_duplicate_payment_intent_error(exc):
                logger.exception(
                    "Stripe credit mint failed with non-idempotency integrity error "
                    "for PaymentIntent %s",
                    payment_intent_id,
                )
                raise

            logger.info(
                f"Idempotency catch: PaymentIntent {payment_intent_id} "
                f"already processed. Swallowing error to return 200 OK."
            )
            return

    @staticmethod
    def _is_duplicate_stripe_event_error(exc: IntegrityError) -> bool:
        """Return true only for the refund-idempotency unique constraint."""
        message = str(getattr(exc, "orig", exc)).lower()
        return "stripe_event_id" in message and (
            "unique" in message or "duplicate" in message
        )


_stripe_integration: Optional[StripeIntegration] = None


def get_stripe_integration() -> StripeIntegration:
    """Get or create the StripeIntegration singleton."""
    global _stripe_integration
    if _stripe_integration is None:
        _stripe_integration = StripeIntegration()
    return _stripe_integration
