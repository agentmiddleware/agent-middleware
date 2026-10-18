"""Shared Stripe webhook signature verification.

Both the settlement handler (``StripeIntegration.handle_webhook``) and the
KYC handler (``KYCService.handle_webhook``) verify events against the same
Stripe signing secret. This module holds that one verification step so both
catch signature-verification failures identically (see the rail conformance
checklist in ``docs/settlement-rails.md``).

Security notes: the secret is always passed in by the caller, never read
here, so this module cannot pick the wrong secret. Failures raise; they
never return an event. Callers must log only the reason, never the payload
or the signature header.
"""

from __future__ import annotations

from typing import Any

import stripe


class StripeWebhookAuthError(ValueError):
    """A Stripe webhook payload failed signature verification.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers keep
    rejecting bad signatures exactly as before.
    """


def construct_verified_event(
    payload: bytes,
    sig_header: str,
    *,
    secret: str,
) -> Any:
    """Verify a Stripe webhook payload and return the event.

    Raises:
        StripeWebhookAuthError: when the signature header is missing or the
            payload fails verification. No event is returned on failure.
    """
    if not sig_header:
        raise StripeWebhookAuthError("missing_stripe_signature")
    try:
        return stripe.Webhook.construct_event(payload, sig_header, secret)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise StripeWebhookAuthError(f"invalid_stripe_signature: {exc}") from exc
