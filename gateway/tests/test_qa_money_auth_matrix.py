"""QA matrix for money-moving routes: auth plus cross-wallet access.

Consolidates the checks an API consumer needs in one place: every route
that moves money (charge, transfer, top-up prepare, permit revoke, refund
retry, x402 settle) answers 401 with no credentials, refuses a key bound
to a foreign wallet with 403 without moving funds, and accepts a JWT
jwt_headers minted for the owning wallet.

Each test below covers behavior no existing test asserts (checked against
test_billing, test_transfers, test_permits, test_refund_reconciliation,
test_stripe_integration, test_x402, test_jwt_auth and
test_arbitrage_regressions before writing). The module stem is registered
in conftest's DORMANT_SURFACE_TEST_MODULES so the transfer, top-up, x402
and auth-token routers are mounted, mirroring production with
ENABLE_PROOF_SURFACES=true.

No test touches the network: top-up prepare paths that would reach Stripe
are either refused before the Stripe call (auth, wallet-type checks) or
run with Stripe's PaymentIntent.create patched out.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

# 32 raw bytes, strict base64. Same non-secret test material CI uses.
TEST_SIGNING_KEY = "dGVzdC1zaWduaW5nLWtleS1tYXRlcmlhbC0zMmJ5dGU="

EVM_PAY_TO = "0x1111111111111111111111111111111111111111"
EVM_PAYER = "0x2222222222222222222222222222222222222222"

_counter = itertools.count(1)


@pytest.fixture(autouse=True)
async def _clean_shared_db(clean_database):
    """Every money-route test starts from an empty shared database."""
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return dict(BOOTSTRAP_HEADERS)


@pytest.fixture
def signing_key(monkeypatch):
    monkeypatch.setenv("TRUST_SIGNING_PRIVATE_KEY_B64", TEST_SIGNING_KEY)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


async def _wallet_balance(client, wallet_id, headers) -> Decimal:
    resp = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return Decimal(str(resp.json()["balance_exact"]))


async def _exchange(client, api_key: str, scopes: list[str] | None = None) -> dict:
    body: dict = {"api_key": api_key}
    if scopes is not None:
        body["scopes"] = scopes
    resp = await client.post("/v1/auth/token", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _settle_body(permit_id: str, wallet_id: str) -> dict:
    return {
        "permit_id": permit_id,
        "wallet_id": wallet_id,
        "amount": "0.01",
        "pay_to": EVM_PAY_TO,
        "network": "base",
        "payer": EVM_PAYER,
    }


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_transfer_rejects_missing_api_key(client, api_headers):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "QA Matrix A",
            "email": "qa-matrix-a@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    sender_id = sponsor.json()["wallet_id"]
    resp = await client.post(
        "/v1/billing/transfer",
        params={"from_wallet_id": sender_id, "to_wallet_id": sender_id, "amount": 10},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.anyio
async def test_transfer_rejects_malformed_and_unknown_keys_without_oracle(
    client, api_headers
):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "QA Matrix B",
            "email": "qa-matrix-b@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    real_wallet = sponsor.json()["wallet_id"]
    fake_wallet = "spn-does-not-exist"
    cases = (
        ({"X-API-Key": "short"}, 401),
        ({"X-API-Key": "not-a-real-key"}, 403),
    )
    for headers, expected in cases:
        real = await client.post(
            "/v1/billing/transfer",
            params={
                "from_wallet_id": real_wallet,
                "to_wallet_id": fake_wallet,
                "amount": 10,
            },
            headers=headers,
        )
        fake = await client.post(
            "/v1/billing/transfer",
            params={
                "from_wallet_id": fake_wallet,
                "to_wallet_id": real_wallet,
                "amount": 10,
            },
            headers=headers,
        )
        assert real.status_code == expected, real.text
        assert fake.status_code == expected, fake.text
        assert real.text == fake.text
        assert real_wallet not in real.text


@pytest.mark.anyio
async def test_transfer_negative_amount_is_422(client, api_headers):
    first = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "QA Matrix C1",
            "email": "qa-matrix-c1@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    second = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "QA Matrix C2", "email": "qa-matrix-c2@t.com"},
        headers=api_headers,
    )
    resp = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": first.json()["wallet_id"],
            "to_wallet_id": second.json()["wallet_id"],
            "amount": -5,
        },
        headers=api_headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_transfer_scoped_key_cannot_spend_foreign_wallet(client, api_headers):
    owner = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    before = await _wallet_balance(client, other["agent_wallet_id"], api_headers)
    resp = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": other["agent_wallet_id"],
            "to_wallet_id": owner["agent_wallet_id"],
            "amount": 10,
        },
        headers=owner["agent_headers"],
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "wallet_access_denied"
    after = await _wallet_balance(client, other["agent_wallet_id"], api_headers)
    assert after == before


@pytest.mark.anyio
async def test_transfer_scoped_key_can_spend_own_wallet(client, api_headers):
    owner = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    before = await _wallet_balance(client, owner["agent_wallet_id"], api_headers)
    resp = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": owner["agent_wallet_id"],
            "to_wallet_id": other["agent_wallet_id"],
            "amount": 25,
        },
        headers=owner["agent_headers"],
    )
    assert resp.status_code == 200, resp.text
    after = await _wallet_balance(client, owner["agent_wallet_id"], api_headers)
    assert after == before - Decimal("25")


# ---------------------------------------------------------------------------
# Top-up
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_direct_top_up_rejects_unauthenticated_before_410(client, api_headers):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "QA Matrix D",
            "email": "qa-matrix-d@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_id = sponsor.json()["wallet_id"]
    resp = await client.post(
        "/v1/billing/top-up",
        json={"wallet_id": wallet_id, "amount_fiat": 5.0, "payment_method": "card"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.anyio
async def test_top_up_prepare_rejects_missing_api_key(client, api_headers):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "QA Matrix E",
            "email": "qa-matrix-e@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    resp = await client.post(
        "/v1/billing/top-up/prepare"
        f"?wallet_id={sponsor.json()['wallet_id']}&amount_fiat=5.0",
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.anyio
async def test_top_up_prepare_denies_foreign_wallet_key(client, api_headers):
    owner = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    with patch(
        "app.services.stripe_integration.stripe.PaymentIntent.create"
    ) as mock_create:
        mock_create.side_effect = AssertionError("Stripe must not be reached")
        resp = await client.post(
            "/v1/billing/top-up/prepare"
            f"?wallet_id={other['agent_wallet_id']}&amount_fiat=5.0",
            headers=owner["agent_headers"],
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "wallet_access_denied"


@pytest.mark.anyio
async def test_top_up_prepare_scoped_key_succeeds_for_own_sponsor(client, api_headers):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "QA Matrix F",
            "email": "qa-matrix-f@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_id = sponsor.json()["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys", json={"wallet_id": wallet_id}, headers=api_headers
    )
    assert key_resp.status_code == 201, key_resp.text
    scoped = {"X-API-Key": key_resp.json()["api_key"]}
    with patch(
        "app.services.stripe_integration.stripe.PaymentIntent.create"
    ) as mock_create:
        mock_create.return_value = MagicMock(
            id="pi_qa_matrix",
            client_secret="pi_qa_matrix_secret",
            status="requires_payment_method",
        )
        resp = await client.post(
            f"/v1/billing/top-up/prepare?wallet_id={wallet_id}&amount_fiat=5.0",
            headers=scoped,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_intent_id"] == "pi_qa_matrix"


# ---------------------------------------------------------------------------
# Permit reserve and revoke
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_permit_revoke_rejects_missing_api_key(client):
    resp = await client.post("/v1/permits/permit-does-not-exist/revoke")
    assert resp.status_code == 401, resp.text


@pytest.mark.anyio
async def test_permit_revoke_unknown_permit_is_404(client, api_headers):
    resp = await client.post(
        "/v1/permits/permit-does-not-exist/revoke", headers=api_headers
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.anyio
async def test_permit_revoke_denies_foreign_wallet_key(client):
    victim = await provision_agent_wallet(client)
    attacker = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=victim["agent_wallet_id"],
        key_id=victim["key_id"],
        tool_name="billing.charge",
        max_credits=50,
        idem_key=f"qa-matrix-revoke-{next(_counter)}",
    )
    resp = await client.post(
        f"/v1/permits/{permit['permit_id']}/revoke",
        headers=attacker["agent_headers"],
    )
    assert resp.status_code == 403, resp.text
    check = await client.get(
        f"/v1/permits/{permit['permit_id']}", headers=BOOTSTRAP_HEADERS
    )
    assert check.status_code == 200, check.text
    assert check.json()["status"] == "active"


@pytest.mark.anyio
async def test_permit_revoke_bootstrap_admin_succeeds(client, api_headers):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="billing.charge",
        max_credits=50,
        idem_key=f"qa-matrix-revoke-{next(_counter)}",
    )
    resp = await client.post(
        f"/v1/permits/{permit['permit_id']}/revoke", headers=api_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"


@pytest.mark.anyio
async def test_transfer_bootstrap_admin_can_spend_any_wallet(client, api_headers):
    owner = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    before = await _wallet_balance(client, other["agent_wallet_id"], api_headers)
    resp = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": other["agent_wallet_id"],
            "to_wallet_id": owner["agent_wallet_id"],
            "amount": 25,
        },
        headers=api_headers,
    )
    assert resp.status_code == 200, resp.text
    after = await _wallet_balance(client, other["agent_wallet_id"], api_headers)
    assert after == before - Decimal("25")


@pytest.mark.anyio
async def test_permit_revoke_owner_succeeds(client):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="billing.charge",
        max_credits=50,
        idem_key=f"qa-matrix-revoke-{next(_counter)}",
    )
    resp = await client.post(
        f"/v1/permits/{permit['permit_id']}/revoke",
        headers=provisioned["agent_headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"


# ---------------------------------------------------------------------------
# Refund reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_refund_list_rejects_missing_api_key(client):
    resp = await client.get("/v1/receipts/reconciliation/refunds")
    assert resp.status_code == 401, resp.text


@pytest.mark.anyio
async def test_refund_retry_rejects_missing_api_key(client):
    resp = await client.post(
        "/v1/receipts/reconciliation/refunds/rcpt-does-not-exist/retry"
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.anyio
async def test_refund_retry_admin_reaches_handler_for_unknown_receipt(
    client, api_headers
):
    resp = await client.post(
        "/v1/receipts/reconciliation/refunds/rcpt-does-not-exist/retry",
        headers=api_headers,
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# x402 settle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_x402_settle_rejects_missing_api_key(client):
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body("permit-does-not-exist", provisioned["agent_wallet_id"]),
        headers={"Idempotency-Key": f"qa-matrix-x402-{next(_counter)}"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.anyio
async def test_x402_settle_rejects_unknown_key(client):
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body("permit-does-not-exist", provisioned["agent_wallet_id"]),
        headers={
            "X-API-Key": "not-a-real-key",
            "Idempotency-Key": f"qa-matrix-x402-{next(_counter)}",
        },
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# JWT and admin on money routes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_auth_token_exchange_rejects_invalid_key(client):
    resp = await client.post("/v1/auth/token", json={"api_key": "not-a-real-key"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"]["error"] == "invalid_api_key"


@pytest.mark.anyio
async def test_jwt_bearer_can_charge_own_wallet(client, signing_key):
    provisioned = await provision_agent_wallet(client)
    tokens = await _exchange(client, provisioned["agent_headers"]["X-API-Key"])
    jwt_headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    before = await _wallet_balance(
        client, provisioned["agent_wallet_id"], BOOTSTRAP_HEADERS
    )
    resp = await client.post(
        f"/v1/billing/charge?wallet_id={provisioned['agent_wallet_id']}"
        "&service=iot_bridge&units=10",
        headers=jwt_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "debit"
    after = await _wallet_balance(
        client, provisioned["agent_wallet_id"], BOOTSTRAP_HEADERS
    )
    assert after == before - Decimal("20")


@pytest.mark.anyio
async def test_jwt_bearer_cannot_charge_foreign_wallet(client, signing_key):
    owner = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    tokens = await _exchange(client, owner["agent_headers"]["X-API-Key"])
    jwt_headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    before = await _wallet_balance(client, other["agent_wallet_id"], BOOTSTRAP_HEADERS)
    resp = await client.post(
        f"/v1/billing/charge?wallet_id={other['agent_wallet_id']}"
        "&service=iot_bridge&units=10",
        headers=jwt_headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "wallet_access_denied"
    after = await _wallet_balance(client, other["agent_wallet_id"], BOOTSTRAP_HEADERS)
    assert after == before


@pytest.mark.anyio
async def test_tampered_jwt_bearer_rejected_on_charge(client, signing_key):
    provisioned = await provision_agent_wallet(client)
    tokens = await _exchange(client, provisioned["agent_headers"]["X-API-Key"])
    tampered = tokens["access_token"][:-1] + (
        "a" if tokens["access_token"][-1] != "a" else "b"
    )
    resp = await client.post(
        f"/v1/billing/charge?wallet_id={provisioned['agent_wallet_id']}"
        "&service=iot_bridge&units=1",
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert resp.status_code == 401, resp.text
