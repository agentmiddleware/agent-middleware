"""
KYC Verification Service using Stripe Identity.

Handles sponsor identity verification required for fiat top-ups.

Architecture:
1. Client calls /v1/kyc/sessions → creates Stripe Identity verification session
2. Client redirects user to Stripe Identity hosted UI
3. User completes verification via Stripe
4. Stripe sends webhook to /v1/webhooks/stripe with verification status
5. Webhook updates wallet kyc_status based on verification result
"""

import logging
from datetime import timedelta
from typing import Any, Literal, Optional, cast
from uuid import uuid4

import stripe
from sqlalchemy import and_, or_, select, update as sa_update

from ..core.time import utc_now
from ..db.database import get_session_factory
from ..db.models import KYCVerificationModel, WalletModel
from ..core.config import get_settings
from ..schemas.billing import KYCDocumentType, KYCStatus, WalletStatus
from .agent_money import WalletNotFoundError
from .wallet_status import SPENDABLE_WALLET_STATUSES

logger = logging.getLogger(__name__)

# KYC may reclaim a suspension it imposed, in addition to already-spendable
# statuses. handle_rejected and handle_expired below suspend the wallet, so
# re-verifying is the documented way out and must stay reachable.
_KYC_OWNED_SUSPENSION_REASONS = ("rejected", "expired")
settings = get_settings()

stripe.api_key = settings.STRIPE_SECRET_KEY


class KYCNotRequiredError(Exception):
    """Raised when KYC is not required for a wallet."""

    pass


class KYCVerificationError(Exception):
    """Raised when KYC verification fails."""

    pass


class KYCService:
    """
    Handles KYC verification via Stripe Identity.

    Only sponsor wallets require KYC for fiat top-ups.
    Agent and child wallets inherit trust from their parent.
    """

    SESSION_EXPIRY_DAYS = 7
    VERIFICATION_TYPE: Literal["document", "id_number"] = "document"

    def __init__(self):
        self._session_factory = get_session_factory

    async def create_verification_session(
        self,
        wallet_id: str,
        return_url: str,
        document_type: Optional[KYCDocumentType] = None,
    ) -> dict:
        """
        Create a Stripe Identity verification session for a sponsor wallet.

        Args:
            wallet_id: The wallet requiring verification
            return_url: URL to redirect after verification completes
            document_type: Restrict verification to one Stripe Identity
                document type (driving_license, id_card, passport); None
                accepts any of them

        Returns:
            {
                "verification_id": str,
                "wallet_id": str,
                "session_id": str,
                "session_url": str,
                "status": str,
                "created_at": datetime,
                "expires_at": datetime,
            }
        """
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
                raise KYCNotRequiredError(
                    f"KYC is only required for sponsor wallets, "
                    f"not {wallet.wallet_type}"
                )

            if wallet.kyc_status == KYCStatus.VERIFIED.value:
                raise KYCNotRequiredError(
                    f"KYC already verified for wallet {wallet_id}"
                )

            if wallet.kyc_status == KYCStatus.NOT_REQUIRED.value:
                raise KYCNotRequiredError(
                    f"KYC verification is not required for wallet {wallet_id}"
                )

        metadata = {"wallet_id": wallet_id}
        document_options: dict[str, Any] = {
            "require_id_number": False,
            "require_live_capture": True,
            "require_matching_selfie": True,
        }
        if document_type is not None:
            # Already constrained to Stripe's vocabulary by the request schema.
            # Omitting allowed_types lets Stripe accept every document type.
            metadata["document_type"] = document_type
            document_options["allowed_types"] = [document_type]

        try:
            verification_session = stripe.identity.VerificationSession.create(
                type=self.VERIFICATION_TYPE,
                metadata=metadata,
                options=cast(Any, {"document": document_options}),
                return_url=return_url,
            )
        except stripe.error.StripeError as e:
            logger.error(f"Failed to create Stripe Identity session: {e}")
            raise KYCVerificationError(f"Failed to create verification session: {e}")

        verification_id = str(uuid4())
        expires_at = utc_now() + timedelta(days=self.SESSION_EXPIRY_DAYS)

        async with self._session_factory()() as session:
            verification = KYCVerificationModel(
                verification_id=verification_id,
                wallet_id=wallet_id,
                stripe_session_id=verification_session.id,
                status="pending",
                verification_type="identity",
                document_type=document_type,
                metadata_json=str(metadata),
            )
            session.add(verification)

            # Moving the wallet to pending_kyc must never WIDEN what it is
            # allowed to do. pending_kyc is a spendable status, so writing it
            # unconditionally turned a velocity auto-freeze, an operator
            # suspension, or a closed wallet back into a spending one -- and
            # the wallet was read before the Stripe network call above, so the
            # write also clobbered anything committed while that call was in
            # flight.
            #
            # A KYC session may only claim the status it is entitled to: one
            # that is already spendable, or a suspension KYC itself imposed
            # (a rejected or expired verification, set below). A freeze or a
            # closure is not KYC's to lift; those wallets still get their
            # session, and still get their kyc_status updated by the webhook,
            # but stay exactly as non-spendable as they were.
            claimed = await session.execute(
                sa_update(WalletModel)
                .where(
                    cast(Any, WalletModel.wallet_id) == wallet_id,
                    or_(
                        cast(Any, WalletModel.status).in_(
                            tuple(SPENDABLE_WALLET_STATUSES)
                        ),
                        and_(
                            cast(Any, WalletModel.status)
                            == WalletStatus.SUSPENDED.value,
                            cast(Any, WalletModel.kyc_status).in_(
                                _KYC_OWNED_SUSPENSION_REASONS
                            ),
                        ),
                    ),
                )
                .values(status="pending_kyc", updated_at=utc_now())
                .execution_options(synchronize_session=False)
            )
            if (cast(Any, claimed).rowcount or 0) != 1:
                logger.info(
                    "KYC session %s created for wallet %s without moving it to "
                    "pending_kyc: its status is not KYC's to change",
                    verification_id,
                    wallet_id,
                )

            await session.commit()

        logger.info(
            f"Created KYC verification {verification_id} for wallet {wallet_id}: "
            f"session={verification_session.id}"
        )

        return {
            "verification_id": verification_id,
            "wallet_id": wallet_id,
            "session_id": verification_session.id,
            "session_url": verification_session.url,
            "status": "pending",
            "created_at": utc_now(),
            "expires_at": expires_at,
        }

    async def get_verification_status(self, wallet_id: str) -> dict:
        """
        Get the current KYC verification status for a wallet.

        Args:
            wallet_id: The wallet to check

        Returns:
            {
                "wallet_id": str,
                "kyc_status": str,
                "verification_id": str | None,
                "stripe_session_id": str | None,
                "last_verified_at": datetime | None,
                "rejection_reason": str | None,
                "requires_verification": bool,
                "message": str,
            }
        """
        async with self._session_factory()() as session:
            wallet_result = await session.execute(
                # SQLModel fields are plain Python types (not Mapped[...]), so mypy
                # sees this comparison as bool rather than a ColumnElement[bool].
                # Root cause lives in app/db/models.py (out of scope here).
                select(WalletModel).where(WalletModel.wallet_id == wallet_id)  # type: ignore[arg-type]
            )
            wallet = wallet_result.scalar_one_or_none()

            if not wallet:
                raise WalletNotFoundError(wallet_id)

            kyc_status = wallet.kyc_status or "not_required"
            requires_verification = (
                wallet.wallet_type == "sponsor"
                and kyc_status not in ["verified", "not_required"]
            )

            verification_result = await session.execute(
                select(KYCVerificationModel)
                .where(KYCVerificationModel.wallet_id == wallet_id)  # type: ignore[arg-type]  # see app/db/models.py
                .order_by(KYCVerificationModel.created_at.desc())  # type: ignore[attr-defined]  # see app/db/models.py
            )
            verification = verification_result.scalars().first()

            message = self._get_status_message(kyc_status, requires_verification)

            return {
                "wallet_id": wallet_id,
                "kyc_status": kyc_status,
                "verification_id": (
                    verification.verification_id if verification else None
                ),
                "stripe_session_id": (
                    verification.stripe_session_id if verification else None
                ),
                "last_verified_at": (
                    verification.last_verified_at if verification else None
                ),
                "rejection_reason": (
                    verification.rejection_reason if verification else None
                ),
                "requires_verification": requires_verification,
                "message": message,
            }

    async def get_verification_details(self, verification_id: str) -> Optional[dict]:
        """
        Get detailed verification information by verification ID.

        Args:
            verification_id: The verification record ID

        Returns:
            Verification details dict or None if not found
        """
        async with self._session_factory()() as session:
            result = await session.execute(
                select(KYCVerificationModel).where(
                    KYCVerificationModel.verification_id == verification_id  # type: ignore[arg-type]
                )
            )
            verification = result.scalar_one_or_none()

            if not verification:
                return None

            return {
                "verification_id": verification.verification_id,
                "wallet_id": verification.wallet_id,
                "status": verification.status,
                "stripe_session_id": verification.stripe_session_id,
                "document_type": verification.document_type,
                "first_verified_at": verification.first_verified_at,
                "last_verified_at": verification.last_verified_at,
                "rejection_reason": verification.rejection_reason,
                "created_at": verification.created_at,
                "updated_at": verification.updated_at,
            }

    async def handle_webhook(self, payload: bytes, sig_header: str) -> bool:
        """
        Process Stripe Identity webhook events.

        Args:
            payload: Raw request body bytes
            sig_header: Stripe-Signature header value

        Returns:
            True if processed successfully, False on signature failure
        """
        try:
            event = stripe.Webhook.construct_event(
                payload,
                sig_header,
                settings.STRIPE_WEBHOOK_SECRET,
            )
        # SignatureVerificationError is a StripeError, not a ValueError, so it
        # must be named: otherwise a bad signature escapes as a 500.
        except (ValueError, stripe.SignatureVerificationError) as e:
            logger.error(f"Invalid Stripe signature: {e}")
            return False

        handler_map = {
            "identity.verification_session.verified": (
                self._handle_verification_verified
            ),
            "identity.verification_session.requires_input": (
                self._handle_requires_input
            ),
            "identity.verification_session.redacted": self._handle_redacted,
        }

        handler = handler_map.get(event["type"])
        if handler:
            await handler(event["data"]["object"])
        else:
            logger.debug(f"Ignoring unhandled event type: {event['type']}")

        return True

    async def _handle_verification_verified(self, verification_session: dict) -> None:
        """Handle successful KYC verification."""
        session_id = verification_session["id"]

        async with self._session_factory()() as session:
            verification_result = await session.execute(
                select(KYCVerificationModel).where(
                    KYCVerificationModel.stripe_session_id == session_id
                )  # type: ignore[arg-type]  # see app/db/models.py
            )
            verification = verification_result.scalar_one_or_none()

            if not verification:
                logger.error(f"Verification not found for session {session_id}")
                return

            verified_at = utc_now()
            verification.status = "verified"
            verification.last_verified_at = verified_at
            if not verification.first_verified_at:
                verification.first_verified_at = verified_at
            verification.rejection_reason = None

            wallet_result = await session.execute(
                select(WalletModel)
                .where(WalletModel.wallet_id == verification.wallet_id)  # type: ignore[arg-type]  # see app/db/models.py
                .with_for_update()
            )
            wallet = wallet_result.scalar_one_or_none()

            if wallet:
                wallet.kyc_status = "verified"
                wallet.kyc_verified_at = verified_at
                if wallet.status == "pending_kyc":
                    wallet.status = "active"
                session.add(wallet)

            await session.commit()

        logger.info(
            f"KYC verified for wallet {verification.wallet_id}, "
            f"verification {verification.verification_id}"
        )

        from .notifications import get_notification_service

        notifications = get_notification_service()
        await notifications.send_kyc_approved_alert(wallet_id=verification.wallet_id)

    async def _handle_requires_input(self, verification_session: dict) -> None:
        """Handle verification session requiring additional input."""
        session_id = verification_session["id"]

        async with self._session_factory()() as session:
            result = await session.execute(
                select(KYCVerificationModel).where(
                    KYCVerificationModel.stripe_session_id == session_id
                )  # type: ignore[arg-type]  # see app/db/models.py
            )
            verification = result.scalar_one_or_none()

            if not verification:
                return

            logger.info(
                f"KYC verification {verification.verification_id} "
                "requires additional input"
            )

    async def _handle_redacted(self, verification_session: dict) -> None:
        """Handle verification session redacted (expired/archived)."""
        session_id = verification_session["id"]

        async with self._session_factory()() as session:
            verification_result = await session.execute(
                select(KYCVerificationModel).where(
                    KYCVerificationModel.stripe_session_id == session_id
                )  # type: ignore[arg-type]  # see app/db/models.py
            )
            verification = verification_result.scalar_one_or_none()

            if not verification:
                return

            if verification.status == "pending":
                verification.status = "expired"
                verification.rejection_reason = "Verification session expired"

                wallet_result = await session.execute(
                    select(WalletModel)
                    .where(WalletModel.wallet_id == verification.wallet_id)  # type: ignore[arg-type]  # see app/db/models.py
                    .with_for_update()
                )
                wallet = wallet_result.scalar_one_or_none()

                if wallet and wallet.status == "pending_kyc":
                    wallet.kyc_status = "expired"
                    wallet.status = "suspended"
                    session.add(wallet)

            await session.commit()

            logger.warning(
                f"KYC verification {verification.verification_id} expired for "
                f"wallet {verification.wallet_id}"
            )

    async def reject_verification(
        self,
        wallet_id: str,
        reason: str,
    ) -> None:
        """
        Manually reject KYC verification for a wallet.

        Args:
            wallet_id: The wallet to reject
            reason: Reason for rejection
        """
        async with self._session_factory()() as session:
            verification_result = await session.execute(
                select(KYCVerificationModel)
                .where(KYCVerificationModel.wallet_id == wallet_id)  # type: ignore[arg-type]  # see app/db/models.py
                .order_by(KYCVerificationModel.created_at.desc())  # type: ignore[attr-defined]  # see app/db/models.py
            )
            verification = verification_result.scalars().first()

            if verification:
                verification.status = "rejected"
                verification.rejection_reason = reason
                verification.last_verified_at = utc_now()

            wallet_result = await session.execute(
                select(WalletModel)
                .where(WalletModel.wallet_id == wallet_id)  # type: ignore[arg-type]  # see app/db/models.py
                .with_for_update()
            )
            wallet = wallet_result.scalar_one_or_none()

            if wallet:
                wallet.kyc_status = "rejected"
                wallet.status = "suspended"
                session.add(wallet)

            await session.commit()

        logger.warning(f"KYC rejected for wallet {wallet_id}: {reason}")

        from .notifications import get_notification_service

        notifications = get_notification_service()
        await notifications.send_kyc_rejected_alert(wallet_id=wallet_id, reason=reason)

    def _get_status_message(self, kyc_status: str, requires_verification: bool) -> str:
        """Get human-readable status message."""
        messages = {
            "not_required": "KYC verification is not required for this wallet type.",
            "verified": "Identity verified. Fiat top-ups are enabled.",
            "pending": (
                "Verification in progress. Complete verification to enable top-ups."
            ),
            "rejected": "Verification rejected. Contact support for assistance.",
            "expired": "Verification expired. Please start a new verification.",
        }
        return messages.get(kyc_status, "Unknown KYC status.")


_kyc_service: Optional[KYCService] = None


def get_kyc_service() -> KYCService:
    """Get or create the KYCService singleton."""
    global _kyc_service
    if _kyc_service is None:
        _kyc_service = KYCService()
    return _kyc_service
