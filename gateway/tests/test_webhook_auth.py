"""Tests for the shared webhook signature helper (app.core.webhook_auth).

Pattern 4 (unsigned webhooks): every inbound webhook in this gateway verifies
through one helper that fails closed with constant-time compares. These tests
pin that contract: no secret means refusal even for correctly signed traffic,
missing and bad signatures are denied alike, and the live Stripe receivers
answer 401 while changing nothing.
"""

import hashlib
import hmac
import json
import time

import pytest
from httpx import AsyncClient, ASGITransport

from app.core.config import get_settings
from app.core.webhook_auth import (
    WebhookVerificationError,
    require_webhook_secret,
    secure_compare,
    verify_stripe_event,
)
from app.main import app


@pytest.fixture(autouse=True)
async def _clean_shared_db(clean_database):
    """Webhook tests post through the shared app; start from an empty DB."""
    return


# The Stripe webhook receivers are dormant trust surfaces: production mounts
# them only with Stripe configured, so the suite mounts them for this module
# (same mechanism as test_kyc / test_stripe_integration).
pytestmark = pytest.mark.dormant

SHARED_SECRET = "whsec_shared_helper_test"


def _sign(payload: bytes, secret: str) -> str:
    """A Stripe-Signature header computed the way Stripe signs webhooks."""
    timestamp = int(time.time())
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _event_payload() -> bytes:
    return json.dumps({"id": "evt_helper_test", "object": "event"}).encode()


def test_require_webhook_secret_accepts_configured_secret():
    assert require_webhook_secret(SHARED_SECRET, name="TEST") == SHARED_SECRET


@pytest.mark.parametrize("secret", [None, "", "   "])
def test_require_webhook_secret_refuses_missing_secret(secret):
    """No secret means the receiver cannot verify, so it must refuse."""
    with pytest.raises(WebhookVerificationError):
        require_webhook_secret(secret, name="TEST")


def test_secure_compare_matches_constant_time_semantics():
    assert secure_compare("abc123", "abc123") is True
    assert secure_compare("abc123", "abc124") is False
    assert secure_compare("abc123", "short") is False
    assert secure_compare(b"abc123", b"abc123") is True
    assert secure_compare(b"abc123", b"abc124") is False


def test_verify_stripe_event_accepts_valid_signature():
    payload = _event_payload()
    event = verify_stripe_event(payload, _sign(payload, SHARED_SECRET), SHARED_SECRET)
    assert event["id"] == "evt_helper_test"


def test_verify_stripe_event_refuses_missing_secret():
    """Fail closed: a correctly signed event is still refused with no secret."""
    payload = _event_payload()
    with pytest.raises(WebhookVerificationError):
        verify_stripe_event(payload, _sign(payload, SHARED_SECRET), "")


@pytest.mark.parametrize("header", [None, "", "   "])
def test_verify_stripe_event_refuses_missing_header(header):
    with pytest.raises(WebhookVerificationError):
        verify_stripe_event(_event_payload(), header, SHARED_SECRET)


def test_verify_stripe_event_refuses_wrong_secret():
    payload = _event_payload()
    with pytest.raises(WebhookVerificationError):
        verify_stripe_event(payload, _sign(payload, "whsec_attacker"), SHARED_SECRET)


def test_verify_stripe_event_refuses_non_ascii_signature():
    payload = _event_payload()
    header = _sign(payload, SHARED_SECRET).replace("v1=", "v1=é")
    with pytest.raises(WebhookVerificationError):
        verify_stripe_event(payload, header, SHARED_SECRET)


def test_verify_stripe_event_refuses_tampered_payload():
    signed = _sign(b'{"id":"evt_helper_test","object":"event"}', SHARED_SECRET)
    with pytest.raises(WebhookVerificationError):
        verify_stripe_event(
            b'{"id":"evt_forged","object":"event"}', signed, SHARED_SECRET
        )


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
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Webhook Helper Sponsor",
            "email": "webhook-helper@test.com",
            "initial_credits": 0,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _succeeded_top_up_payload(wallet_id: str) -> bytes:
    return json.dumps(
        {
            "id": "evt_helper_top_up",
            "object": "event",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_helper_top_up",
                    "status": "succeeded",
                    "amount": 5000,
                    "amount_received": 5000,
                    "currency": "usd",
                    "metadata": {"wallet_id": wallet_id, "credits": "50000"},
                }
            },
        }
    ).encode()


@pytest.mark.anyio
async def test_payment_webhook_denies_everything_when_secret_unconfigured(
    client, monkeypatch
):
    """With no secret configured, even well-formed traffic is refused with 401."""
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", "")
    payload = b'{"id":"evt_x","object":"event"}'

    for headers in (
        {},
        {"stripe-signature": "anything"},
        {"stripe-signature": _sign(payload, SHARED_SECRET)},
    ):
        resp = await client.post(
            "/v1/webhooks/stripe", content=payload, headers=headers
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid Stripe signature"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path", ["/v1/webhooks/stripe", "/v1/webhooks/stripe/identity"]
)
async def test_stripe_receivers_refuse_non_ascii_signature(client, monkeypatch, path):
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", SHARED_SECRET)
    resp = await client.post(
        path,
        content=_event_payload(),
        headers={b"stripe-signature": b"t=1,v1=\xe9"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid Stripe signature"


@pytest.mark.anyio
async def test_payment_webhook_denies_forged_and_accepts_valid(
    client, api_headers, sponsor_wallet, monkeypatch
):
    """End to end with real signatures: forged is 401 with no mint, valid mints."""
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", SHARED_SECRET)
    wallet_id = sponsor_wallet["wallet_id"]
    payload = _succeeded_top_up_payload(wallet_id)

    forged = await client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": _sign(payload, "whsec_attacker")},
    )
    assert forged.status_code == 401

    wallet = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
    assert wallet.json()["balance"] == 0.0

    valid = await client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": _sign(payload, SHARED_SECRET)},
    )
    assert valid.status_code == 200, valid.text
    assert valid.json() == {"status": "received"}

    wallet = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
    assert wallet.json()["balance"] == 50000.0


@pytest.mark.anyio
async def test_identity_webhook_denies_everything_when_secret_unconfigured(
    client, monkeypatch
):
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", "")
    payload = b'{"id":"evt_y","object":"event"}'
    resp = await client.post(
        "/v1/webhooks/stripe/identity",
        content=payload,
        headers={"stripe-signature": _sign(payload, SHARED_SECRET)},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid Stripe signature"
