"""
Tests for KYC Verification (Stripe Identity).
Validates the KYC verification flow for sponsor wallets.
"""

import hashlib
import hmac
import json
import time

import pytest
import stripe
from unittest.mock import patch, MagicMock
from uuid import uuid4
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.services import kyc_service as kyc_service_module


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
            "sponsor_name": "KYC Test Corp",
            "email": "kyc@test.com",
            "initial_credits": 10000.0,
            "require_kyc": True,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.anyio
async def test_create_sponsor_wallet_with_kyc_required(client, api_headers):
    """Test that creating a sponsor wallet with require_kyc=True sets kyc_status to pending."""
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "KYC Required Corp",
            "email": "kyc-required@test.com",
            "initial_credits": 10000.0,
            "require_kyc": True,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["wallet_type"] == "sponsor"
    assert data["kyc_status"] == "pending"
    assert data["status"] == "pending_kyc"


@pytest.mark.anyio
async def test_create_sponsor_wallet_without_kyc(client, api_headers):
    """Test that creating a sponsor wallet without require_kyc sets kyc_status to not_required."""
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "No KYC Corp",
            "email": "no-kyc@test.com",
            "initial_credits": 10000.0,
            "require_kyc": False,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["kyc_status"] == "not_required"


@pytest.mark.anyio
async def test_get_kyc_status_pending(client, api_headers, sponsor_wallet):
    """Test getting KYC status for a pending verification."""
    resp = await client.get(
        f"/v1/kyc/status/{sponsor_wallet['wallet_id']}",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["wallet_id"] == sponsor_wallet["wallet_id"]
    assert data["kyc_status"] == "pending"
    assert data["requires_verification"] is True
    assert "verification" in data["message"].lower()


@pytest.mark.anyio
async def test_get_kyc_status_wallet_not_found(client, api_headers):
    """Test getting KYC status for a non-existent wallet."""
    resp = await client.get(
        "/v1/kyc/status/nonexistent-wallet",
        headers=api_headers,
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_get_kyc_status_verified(client, api_headers):
    """Test getting KYC status for a verified wallet."""
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Verified Corp",
            "email": "verified@test.com",
            "initial_credits": 10000.0,
            "require_kyc": False,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    wallet_id = resp.json()["wallet_id"]

    resp = await client.get(
        f"/v1/kyc/status/{wallet_id}",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["kyc_status"] == "not_required"
    assert data["requires_verification"] is False


@pytest.mark.anyio
async def test_create_kyc_session_not_required(client, api_headers):
    """Test creating KYC session for a wallet that doesn't need it."""
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "No KYC Needed",
            "email": "no-kyc@test.com",
            "initial_credits": 10000.0,
            "require_kyc": False,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    wallet_id = resp.json()["wallet_id"]
    assert resp.json()["kyc_status"] == "not_required"

    resp = await client.post(
        "/v1/kyc/sessions",
        json={
            "wallet_id": wallet_id,
            "return_url": "https://example.com/callback",
        },
        headers=api_headers,
    )
    assert resp.status_code == 400
    assert "not required" in resp.json()["detail"]["message"].lower()


@pytest.mark.anyio
async def test_create_kyc_session_wallet_not_found(client, api_headers):
    """Test creating KYC session for a non-existent wallet."""
    resp = await client.post(
        "/v1/kyc/sessions",
        json={
            "wallet_id": "nonexistent-wallet",
            "return_url": "https://example.com/callback",
        },
        headers=api_headers,
    )
    assert resp.status_code == 404


@pytest.mark.anyio
@patch("app.services.kyc_service.stripe.identity.VerificationSession.create")
async def test_create_kyc_session_success(
    mock_stripe_create, client, api_headers, sponsor_wallet
):
    """Test successful KYC session creation."""
    mock_session = MagicMock()
    mock_session.id = "vs_test123"
    mock_session.url = "https://verify.stripe.com/test_session"
    mock_stripe_create.return_value = mock_session

    resp = await client.post(
        "/v1/kyc/sessions",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "return_url": "https://example.com/callback",
            "document_type": "passport",
        },
        headers=api_headers,
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["wallet_id"] == sponsor_wallet["wallet_id"]
    assert data["session_id"] == "vs_test123"
    assert data["session_url"] == "https://verify.stripe.com/test_session"
    assert data["status"] == "pending"


@pytest.mark.anyio
async def test_get_verification_details_not_found(client, api_headers):
    """Test getting verification details for a non-existent verification."""
    resp = await client.get(
        "/v1/kyc/verifications/nonexistent-verification",
        headers=api_headers,
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_kyc_status_includes_all_fields(client, api_headers, sponsor_wallet):
    """Test that KYC status response includes all expected fields."""
    resp = await client.get(
        f"/v1/kyc/status/{sponsor_wallet['wallet_id']}",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    required_fields = [
        "wallet_id",
        "kyc_status",
        "requires_verification",
        "message",
    ]
    for field in required_fields:
        assert field in data, f"Missing field: {field}"


@pytest.mark.anyio
async def test_db_key_cannot_read_other_wallet_kyc_status(client, api_headers):
    """A DB-backed key scoped to wallet A must not read wallet B's KYC status."""
    wallet_a_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "KYC Tenant A",
            "email": "kyc-a@test.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_b_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "KYC Tenant B",
            "email": "kyc-b@test.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_a = wallet_a_resp.json()["wallet_id"]
    wallet_b = wallet_b_resp.json()["wallet_id"]

    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_a},
        headers=api_headers,
    )
    db_headers = {"X-API-Key": key_resp.json()["api_key"]}

    resp = await client.get(f"/v1/kyc/status/{wallet_b}", headers=db_headers)
    assert resp.status_code == 403

    own_resp = await client.get(f"/v1/kyc/status/{wallet_a}", headers=db_headers)
    assert own_resp.status_code == 200


@pytest.mark.anyio
async def test_db_key_cannot_create_kyc_session_for_other_wallet(client, api_headers):
    """A DB-backed key scoped to wallet A must not start a KYC session for wallet B."""
    wallet_a_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "KYC Session A",
            "email": "kyc-session-a@test.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_b_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "KYC Session B",
            "email": "kyc-session-b@test.com",
            "initial_credits": 1000,
            "require_kyc": True,
        },
        headers=api_headers,
    )
    wallet_a = wallet_a_resp.json()["wallet_id"]
    wallet_b = wallet_b_resp.json()["wallet_id"]

    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_a},
        headers=api_headers,
    )
    db_headers = {"X-API-Key": key_resp.json()["api_key"]}

    resp = await client.post(
        "/v1/kyc/sessions",
        json={"wallet_id": wallet_b, "return_url": "https://example.com/callback"},
        headers=db_headers,
    )
    assert resp.status_code == 403


@pytest.mark.anyio
@patch("app.services.kyc_service.stripe.identity.VerificationSession.create")
async def test_db_key_cannot_read_other_wallet_verification_details(
    mock_stripe_create, client, api_headers
):
    """A DB-backed key scoped to wallet A must not read wallet B's verification details."""
    wallet_a_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "KYC Verify A",
            "email": "kyc-verify-a@test.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_b_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "KYC Verify B",
            "email": "kyc-verify-b@test.com",
            "initial_credits": 1000,
            "require_kyc": True,
        },
        headers=api_headers,
    )
    wallet_a = wallet_a_resp.json()["wallet_id"]
    wallet_b = wallet_b_resp.json()["wallet_id"]

    mock_session = MagicMock()
    mock_session.id = "vs_test_other_wallet"
    mock_session.url = "https://verify.stripe.com/test_session"
    mock_stripe_create.return_value = mock_session

    session_resp = await client.post(
        "/v1/kyc/sessions",
        json={"wallet_id": wallet_b, "return_url": "https://example.com/callback"},
        headers=api_headers,
    )
    assert session_resp.status_code == 201
    verification_id = session_resp.json()["verification_id"]

    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_a},
        headers=api_headers,
    )
    db_headers = {"X-API-Key": key_resp.json()["api_key"]}

    resp = await client.get(
        f"/v1/kyc/verifications/{verification_id}",
        headers=db_headers,
    )
    # A foreign verification answers exactly like a missing one: a 403 would
    # confirm the id exists, and its detail echoed the owner's wallet_id.
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "verification_not_found"
    assert wallet_b not in resp.text


def _stub_stripe_session(mock_stripe_create) -> str:
    """Make the patched Stripe create return a session with a unique id."""
    session_id = f"vs_test_{uuid4().hex}"
    mock_session = MagicMock()
    mock_session.id = session_id
    mock_session.url = "https://verify.stripe.com/test_session"
    mock_stripe_create.return_value = mock_session
    return session_id


async def _sponsor_with_key(client, api_headers, name: str, *, require_kyc: bool):
    wallet_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": name,
            "email": f"{name.lower().replace(' ', '-')}@test.com",
            "initial_credits": 1000,
            "require_kyc": require_kyc,
        },
        headers=api_headers,
    )
    assert wallet_resp.status_code == 201
    wallet_id = wallet_resp.json()["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id},
        headers=api_headers,
    )
    assert key_resp.status_code == 201
    return wallet_id, {"X-API-Key": key_resp.json()["api_key"]}


@pytest.mark.dormant
@pytest.mark.anyio
@patch("app.services.kyc_service.stripe.identity.VerificationSession.create")
async def test_foreign_verification_is_indistinguishable_from_missing(
    mock_stripe_create, client, api_headers
):
    """Wallet-scoped keys: deny cross-tenant reads without an existence oracle.

    Wallet A's key asking for wallet B's verification must get the same 404 a
    nonexistent id gets, never wallet B's id or details; wallet B's own key and
    the bootstrap admin must still read it.
    """
    wallet_a, headers_a = await _sponsor_with_key(
        client, api_headers, "KYC Oracle A", require_kyc=True
    )
    wallet_b, headers_b = await _sponsor_with_key(
        client, api_headers, "KYC Oracle B", require_kyc=True
    )
    assert wallet_a != wallet_b
    session_id = _stub_stripe_session(mock_stripe_create)

    created = await client.post(
        "/v1/kyc/sessions",
        json={"wallet_id": wallet_b, "return_url": "https://example.com/callback"},
        headers=headers_b,
    )
    assert created.status_code == 201, created.text
    verification_id = created.json()["verification_id"]

    foreign = await client.get(
        f"/v1/kyc/verifications/{verification_id}", headers=headers_a
    )
    missing = await client.get(
        "/v1/kyc/verifications/nonexistent-verification", headers=headers_a
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["detail"]["error"] == missing.json()["detail"]["error"]
    assert wallet_b not in foreign.text
    assert session_id not in foreign.text
    assert "wallet_access_denied" not in foreign.text

    owner = await client.get(
        f"/v1/kyc/verifications/{verification_id}", headers=headers_b
    )
    assert owner.status_code == 200, owner.text
    assert owner.json()["wallet_id"] == wallet_b
    assert owner.json()["stripe_session_id"] == session_id

    admin = await client.get(
        f"/v1/kyc/verifications/{verification_id}", headers=api_headers
    )
    assert admin.status_code == 200, admin.text
    assert admin.json()["wallet_id"] == wallet_b


@pytest.mark.dormant
@pytest.mark.anyio
@patch("app.services.kyc_service.stripe.identity.VerificationSession.create")
async def test_default_session_sends_no_invalid_document_type_to_stripe(
    mock_stripe_create, client, api_headers, sponsor_wallet
):
    """Omitting document_type must not send Stripe a value it rejects.

    The old default "document" is not a Stripe Identity allowed_types value,
    so every default request would have been refused by Stripe.
    """
    _stub_stripe_session(mock_stripe_create)

    resp = await client.post(
        "/v1/kyc/sessions",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "return_url": "https://example.com/callback",
        },
        headers=api_headers,
    )
    assert resp.status_code == 201, resp.text

    kwargs = mock_stripe_create.call_args.kwargs
    # No restriction means Stripe accepts every document type it supports.
    assert "allowed_types" not in kwargs["options"]["document"]
    assert "document" not in kwargs["metadata"].values()


@pytest.mark.dormant
@pytest.mark.anyio
@patch("app.services.kyc_service.stripe.identity.VerificationSession.create")
async def test_explicit_document_type_is_forwarded_to_stripe(
    mock_stripe_create, client, api_headers, sponsor_wallet
):
    """A Stripe-vocabulary document_type is passed through as allowed_types."""
    _stub_stripe_session(mock_stripe_create)

    resp = await client.post(
        "/v1/kyc/sessions",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "return_url": "https://example.com/callback",
            "document_type": "driving_license",
        },
        headers=api_headers,
    )
    assert resp.status_code == 201, resp.text
    kwargs = mock_stripe_create.call_args.kwargs
    assert kwargs["options"]["document"]["allowed_types"] == ["driving_license"]


@pytest.mark.dormant
@pytest.mark.anyio
@pytest.mark.parametrize("document_type", ["driver_license", "document", "selfie"])
@patch("app.services.kyc_service.stripe.identity.VerificationSession.create")
async def test_document_type_outside_stripe_vocabulary_is_rejected(
    mock_stripe_create, document_type, client, api_headers, sponsor_wallet
):
    """Values Stripe rejects fail locally with 422, before any Stripe call."""
    _stub_stripe_session(mock_stripe_create)
    resp = await client.post(
        "/v1/kyc/sessions",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "return_url": "https://example.com/callback",
            "document_type": document_type,
        },
        headers=api_headers,
    )
    assert resp.status_code == 422, resp.text
    mock_stripe_create.assert_not_called()


@pytest.mark.dormant
@pytest.mark.anyio
@pytest.mark.parametrize(
    "return_url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "http://attacker.example.com/kyc-callback",
        "ftp://example.com/kyc-callback",
        "https://",
        "/relative/kyc-callback",
        "https://example.com/" + "a" * 2048,
    ],
    ids=[
        "javascript",
        "data",
        "http-non-loopback",
        "ftp",
        "no-host",
        "relative",
        "too-long",
    ],
)
@patch("app.services.kyc_service.stripe.identity.VerificationSession.create")
async def test_unsafe_return_url_is_rejected(
    mock_stripe_create, return_url, client, api_headers, sponsor_wallet
):
    """return_url must be an absolute https URL (http only for loopback)."""
    _stub_stripe_session(mock_stripe_create)
    resp = await client.post(
        "/v1/kyc/sessions",
        json={"wallet_id": sponsor_wallet["wallet_id"], "return_url": return_url},
        headers=api_headers,
    )
    assert resp.status_code == 422, resp.text
    mock_stripe_create.assert_not_called()


@pytest.mark.dormant
@pytest.mark.anyio
@pytest.mark.parametrize(
    "return_url",
    ["http://localhost:3000/kyc-callback", "http://127.0.0.1:8080/cb"],
)
@patch("app.services.kyc_service.stripe.identity.VerificationSession.create")
async def test_loopback_http_return_url_is_allowed(
    mock_stripe_create, return_url, client, api_headers, sponsor_wallet
):
    """Local development callbacks over plain http keep working."""
    _stub_stripe_session(mock_stripe_create)

    resp = await client.post(
        "/v1/kyc/sessions",
        json={"wallet_id": sponsor_wallet["wallet_id"], "return_url": return_url},
        headers=api_headers,
    )
    assert resp.status_code == 201, resp.text
    assert mock_stripe_create.call_args.kwargs["return_url"] == return_url


@pytest.mark.anyio
async def test_wallet_response_includes_kyc_status(client, api_headers):
    """Test that wallet response includes kyc_status field."""
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "KYC Status Check",
            "email": "kyc-status@test.com",
            "initial_credits": 5000.0,
            "require_kyc": True,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "kyc_status" in data
    assert data["kyc_status"] == "pending"


IDENTITY_WEBHOOK_SECRET = "whsec_identity_test"


def _stripe_signature_header(payload: bytes, secret: str) -> str:
    """A Stripe-Signature header computed the way Stripe signs webhooks."""
    timestamp = int(time.time())
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


@pytest.fixture
async def pending_identity_session(client, api_headers, sponsor_wallet, monkeypatch):
    """A sponsor wallet with an open Stripe Identity session and a known secret."""
    monkeypatch.setattr(
        kyc_service_module.settings, "STRIPE_WEBHOOK_SECRET", IDENTITY_WEBHOOK_SECRET
    )
    session_id = f"vs_sig_{sponsor_wallet['wallet_id']}"
    with patch(
        "app.services.kyc_service.stripe.identity.VerificationSession.create"
    ) as mock_create:
        mock_create.return_value = MagicMock(
            id=session_id, url="https://verify.stripe.com/sig_test"
        )
        resp = await client.post(
            "/v1/kyc/sessions",
            json={
                "wallet_id": sponsor_wallet["wallet_id"],
                "return_url": "https://example.com/callback",
            },
            headers=api_headers,
        )
    assert resp.status_code == 201, resp.text
    payload = json.dumps(
        {
            "id": "evt_identity_sig_test",
            "object": "event",
            "type": "identity.verification_session.verified",
            "data": {
                "object": {"id": session_id, "object": "identity.verification_session"}
            },
        }
    ).encode()
    return {"wallet_id": sponsor_wallet["wallet_id"], "payload": payload}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "signature",
    [
        pytest.param("forged", id="signed-with-another-secret"),
        pytest.param("t=1700000000,v1=deadbeef", id="stale-garbage-signature"),
        pytest.param("not-a-stripe-signature", id="unparseable-header"),
    ],
)
async def test_identity_webhook_rejects_bad_signature_with_400(
    client, api_headers, pending_identity_session, signature
):
    """A bad signature is a client error (400), not an unhandled 500, and changes nothing."""
    payload = pending_identity_session["payload"]
    if signature == "forged":
        signature = _stripe_signature_header(payload, "whsec_attacker")

    resp = await client.post(
        "/v1/webhooks/stripe/identity",
        content=payload,
        headers={"stripe-signature": signature},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid Stripe signature"
    status_resp = await client.get(
        f"/v1/kyc/status/{pending_identity_session['wallet_id']}",
        headers=api_headers,
    )
    assert status_resp.json()["kyc_status"] == "pending"


@pytest.mark.anyio
async def test_identity_webhook_rejects_signature_error_without_dispatch(
    client, pending_identity_session
):
    """stripe.SignatureVerificationError is not a ValueError; it must still map to 400."""
    assert not issubclass(stripe.SignatureVerificationError, ValueError)
    with (
        patch(
            "app.services.kyc_service.stripe.Webhook.construct_event",
            side_effect=stripe.SignatureVerificationError(
                "Invalid signature", "invalid_sig"
            ),
        ),
        patch.object(
            kyc_service_module.KYCService, "_handle_verification_verified"
        ) as mock_verified,
    ):
        resp = await client.post(
            "/v1/webhooks/stripe/identity",
            content=pending_identity_session["payload"],
            headers={"stripe-signature": "invalid_sig"},
        )

    assert resp.status_code == 400
    mock_verified.assert_not_called()


@pytest.mark.anyio
async def test_identity_webhook_missing_signature_is_400(client):
    resp = await client.post("/v1/webhooks/stripe/identity", content=b"{}")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Missing Stripe signature"


@pytest.mark.anyio
async def test_identity_webhook_accepts_correctly_signed_event(
    client, api_headers, pending_identity_session
):
    """Control for the rejections above: the same event, correctly signed, verifies."""
    payload = pending_identity_session["payload"]
    resp = await client.post(
        "/v1/webhooks/stripe/identity",
        content=payload,
        headers={
            "stripe-signature": _stripe_signature_header(
                payload, IDENTITY_WEBHOOK_SECRET
            )
        },
    )

    assert resp.status_code == 200, resp.text
    status_resp = await client.get(
        f"/v1/kyc/status/{pending_identity_session['wallet_id']}",
        headers=api_headers,
    )
    assert status_resp.json()["kyc_status"] == "verified"
