"""Tests for the shared Stripe webhook signature helper.

The settlement handler and the KYC handler verify events against the same
Stripe signing secret. Both must route through
``app.services.stripe_webhook_auth.construct_verified_event`` so a bad
signature fails identically (False at the service, 400 at the router) no
matter which webhook receives it.
"""

import pytest
import stripe
from unittest.mock import MagicMock, patch

from app.services import kyc_service as kyc_service_module
from app.services import stripe_integration as stripe_integration_module
from app.services.kyc_service import KYCService
from app.services.stripe_integration import StripeIntegration
from app.services.stripe_webhook_auth import (
    StripeWebhookAuthError,
    construct_verified_event,
)


def test_helper_returns_verified_event():
    """A valid signature yields the Stripe event object."""
    sentinel = {"id": "evt_ok", "type": "charge.refunded"}
    with patch(
        "app.services.stripe_webhook_auth.stripe.Webhook.construct_event",
        return_value=sentinel,
    ) as mock_construct:
        event = construct_verified_event(b"payload", "t=1,v1=abc", secret="whsec_test")
    assert event == sentinel
    mock_construct.assert_called_once_with(b"payload", "t=1,v1=abc", "whsec_test")


def test_helper_raises_auth_error_on_signature_failure():
    """A Stripe signature failure raises, and stays catchable as ValueError."""
    assert issubclass(StripeWebhookAuthError, ValueError)
    with patch(
        "app.services.stripe_webhook_auth.stripe.Webhook.construct_event",
        side_effect=stripe.SignatureVerificationError("bad sig", "sig"),
    ):
        with pytest.raises(StripeWebhookAuthError):
            construct_verified_event(b"payload", "sig", secret="whsec_test")


def test_helper_raises_auth_error_on_malformed_payload():
    """A malformed payload raises the same shared error type."""
    with patch(
        "app.services.stripe_webhook_auth.stripe.Webhook.construct_event",
        side_effect=ValueError("malformed"),
    ):
        with pytest.raises(StripeWebhookAuthError):
            construct_verified_event(b"payload", "sig", secret="whsec_test")


def test_helper_rejects_missing_header_without_verifying():
    """An unsigned delivery is refused before any crypto runs."""
    with patch(
        "app.services.stripe_webhook_auth.stripe.Webhook.construct_event",
    ) as mock_construct:
        with pytest.raises(StripeWebhookAuthError):
            construct_verified_event(b"payload", "", secret="whsec_test")
    mock_construct.assert_not_called()


@pytest.mark.anyio
async def test_settlement_handler_uses_shared_helper():
    """The settlement webhook verifies through the shared helper, same secret."""
    service = StripeIntegration()
    payload, sig = b"payload", "sig"
    sentinel = {"id": "evt_x", "type": "unhandled.test", "data": {"object": {}}}
    with patch.object(
        stripe_integration_module,
        "construct_verified_event",
        return_value=sentinel,
    ) as mock_verify:
        assert await service.handle_webhook(payload, sig) is True
    mock_verify.assert_called_once_with(
        payload, sig, secret=stripe_integration_module.settings.STRIPE_WEBHOOK_SECRET
    )


@pytest.mark.anyio
async def test_settlement_handler_rejects_when_helper_rejects():
    """A helper rejection means no mint and no dispatch in settlement."""
    service = StripeIntegration()
    with (
        patch.object(
            stripe_integration_module,
            "construct_verified_event",
            side_effect=StripeWebhookAuthError("invalid_stripe_signature"),
        ),
        patch.object(
            StripeIntegration, "_mint_credits", new_callable=MagicMock
        ) as mock_mint,
    ):
        assert await service.handle_webhook(b"payload", "bad") is False
    mock_mint.assert_not_called()


@pytest.mark.anyio
async def test_kyc_handler_uses_shared_helper():
    """The KYC webhook verifies through the shared helper, same secret."""
    service = KYCService()
    payload, sig = b"payload", "sig"
    sentinel = {"id": "evt_y", "type": "unhandled.test", "data": {"object": {}}}
    with patch.object(
        kyc_service_module,
        "construct_verified_event",
        return_value=sentinel,
    ) as mock_verify:
        assert await service.handle_webhook(payload, sig) is True
    mock_verify.assert_called_once_with(
        payload, sig, secret=kyc_service_module.settings.STRIPE_WEBHOOK_SECRET
    )


@pytest.mark.anyio
async def test_kyc_handler_rejects_when_helper_rejects():
    """A helper rejection means no verification state change in KYC."""
    service = KYCService()
    with (
        patch.object(
            kyc_service_module,
            "construct_verified_event",
            side_effect=StripeWebhookAuthError("invalid_stripe_signature"),
        ),
        patch.object(KYCService, "_handle_verification_verified") as mock_verified,
    ):
        assert await service.handle_webhook(b"payload", "bad") is False
    mock_verified.assert_not_called()
