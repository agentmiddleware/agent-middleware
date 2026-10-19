"""Shared inbound-webhook signature checks.

Every inbound third-party webhook in this gateway (currently the two Stripe
receivers in ``app/routers/webhooks.py``) verifies through this module, so
the fail-closed rules live in one place instead of being reimplemented per
receiver:

* the signing secret is required before any signature is trusted; a receiver
  with no secret configured refuses the request instead of skipping the
  check (the ``if secret and signature`` fail-open shape must never appear);
* a missing or bad signature is denied, and every denial looks identical to
  the caller (routers answer 401) so probes learn nothing about server
  configuration;
* signature comparison itself is constant-time (``hmac.compare_digest``),
  directly here for generic HMAC webhooks and inside the Stripe SDK's
  secure_compare for Stripe's v1 scheme.

Success responses stay minimal (an accepted marker, never tenant data).
Idempotent application of redelivered events stays with the services (UNIQUE
constraints on payment intent and event ids), not with this module.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

import stripe

logger = logging.getLogger(__name__)


class WebhookVerificationError(ValueError):
    """An inbound webhook cannot be trusted and must be refused.

    Covers a missing signing secret, a missing signature header, and a
    signature that does not verify. Routers map this to 401; services treat
    it as "drop the event, change nothing".
    """


def require_webhook_secret(secret: str | None, *, name: str) -> str:
    """Return the configured webhook secret, or refuse to verify without one.

    A receiver with no secret cannot tell a forged event from a real one, so
    it must not process anything. Call this before trusting any signature.
    """
    if not secret or not secret.strip():
        logger.error(
            "webhook receiver %s has no signing secret configured; "
            "refusing to process unsigned webhook traffic",
            name,
        )
        raise WebhookVerificationError(f"webhook receiver {name} is not configured")
    return secret


def secure_compare(first: str | bytes, second: str | bytes) -> bool:
    """Compare two signatures in constant time.

    The single choke point for signature equality in this gateway, so a new
    HMAC-signed webhook adopts constant-time comparison by calling this
    instead of ``==``. Accepts text or bytes; text is UTF-8 encoded first.
    """
    if isinstance(first, str):
        first = first.encode("utf-8")
    if isinstance(second, str):
        second = second.encode("utf-8")
    return hmac.compare_digest(first, second)


def verify_stripe_event(
    payload: bytes,
    sig_header: str | None,
    secret: str | None,
    *,
    secret_name: str = "STRIPE_WEBHOOK_SECRET",
) -> Any:
    """Verify a Stripe webhook payload and return the parsed event.

    Fails closed: raises :class:`WebhookVerificationError` when the secret
    is not configured, when the signature header is missing or blank, or
    when the Stripe SDK rejects the signature (which covers tampered
    payloads, wrong secrets, and timestamps older than the SDK's 300-second
    tolerance). The SDK compares the v1 HMAC with its own constant-time
    ``secure_compare``.

    Args:
        payload: Raw request body bytes, exactly as received.
        sig_header: Value of the ``Stripe-Signature`` request header.
        secret: The configured webhook signing secret (``whsec_...``).
        secret_name: Setting name used in logs and errors.

    Raises:
        WebhookVerificationError: The event must not be trusted.
    """
    require_webhook_secret(secret, name=secret_name)
    if not sig_header or not sig_header.strip():
        raise WebhookVerificationError("missing webhook signature")
    if not sig_header.isascii():
        # Stripe's constant-time comparison raises TypeError for non-ASCII text.
        raise WebhookVerificationError("invalid webhook signature")
    try:
        return stripe.Webhook.construct_event(
            payload,
            sig_header,
            secret,
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        # SignatureVerificationError is not a ValueError subclass, so it
        # needs naming explicitly; otherwise a bad signature escapes as a
        # 500 instead of a denial.
        logger.warning("Rejected webhook with invalid signature: %s", exc)
        raise WebhookVerificationError("invalid webhook signature") from exc
