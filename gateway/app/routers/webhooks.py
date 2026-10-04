"""
Stripe Webhook Router
Receives and processes Stripe webhook events.

Webhook URL for production: https://api.yourdomain.com/v1/webhooks/stripe
Local development: stripe listen --forward-to localhost:8000/v1/webhooks/stripe
"""

import logging
from fastapi import APIRouter, Request, HTTPException, status

from ..services.stripe_integration import StripeSettlementError, get_stripe_integration
from ..services.kyc_service import get_kyc_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/webhooks",
    tags=["Webhooks"],
)


@router.post("/stripe")
async def handle_stripe_webhook(request: Request):
    """
    Receive and process Stripe webhook events.

    Stripe will retry this endpoint if we return non-200.
    Our service layer handles idempotency internally via
    UNIQUE constraint on payment_intent_id + IntegrityError catch.

    Events handled:
    - payment_intent.succeeded → Mint credits to wallet
    - payment_intent.payment_failed → Log and notify
    - charge.refunded → Debit wallet
    - identity.verification_session.verified → Approve KYC
    - identity.verification_session.requires_input → Log
    - identity.verification_session.redacted → Expire verification
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not sig_header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe signature",
        )

    stripe_integration = get_stripe_integration()
    try:
        success = await stripe_integration.handle_webhook(payload, sig_header)
    except StripeSettlementError as exc:
        logger.warning("Rejected invalid Stripe settlement: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_stripe_settlement",
                "message": str(exc),
            },
        ) from exc

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe signature",
        )

    return {"status": "received"}


@router.post("/stripe/identity")
async def handle_stripe_identity_webhook(request: Request):
    """
    Receive and process Stripe Identity webhook events.

    These events are for KYC verification status updates.
    Note: Stripe often sends these to the same /webhooks/stripe endpoint,
    but we handle them separately here for clarity.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not sig_header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe signature",
        )

    kyc_service = get_kyc_service()
    success = await kyc_service.handle_webhook(payload, sig_header)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe signature",
        )

    return {"status": "received"}
