"""
Tests for Agent-to-Agent Wallet Transfers.
Validates the transfer endpoint and service.
"""

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


@pytest.fixture
async def two_wallets(client, api_headers):
    """Create two wallets for transfer testing."""
    resp1 = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Sender",
            "email": "sender@t.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    sender_id = resp1.json()["wallet_id"]

    resp2 = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Receiver",
            "email": "receiver@t.com",
            "initial_credits": 0,
        },
        headers=api_headers,
    )
    receiver_id = resp2.json()["wallet_id"]

    return {"sender_id": sender_id, "receiver_id": receiver_id}


@pytest.mark.anyio
async def test_transfer_success(client, two_wallets, api_headers):
    """Test successful credit transfer between wallets."""
    sender_id = two_wallets["sender_id"]
    receiver_id = two_wallets["receiver_id"]

    resp = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": sender_id,
            "to_wallet_id": receiver_id,
            "amount": 1000.0,
            "description": "Payment for services",
        },
        headers=api_headers,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["from_wallet_id"] == sender_id
    assert data["to_wallet_id"] == receiver_id
    assert data["amount"] == 1000.0
    assert data["status"] == "completed"

    sender_balance = await client.get(
        f"/v1/billing/wallets/{sender_id}", headers=api_headers
    )
    receiver_balance = await client.get(
        f"/v1/billing/wallets/{receiver_id}", headers=api_headers
    )

    assert sender_balance.json()["balance"] == 9000.0
    assert receiver_balance.json()["balance"] == 1000.0


@pytest.mark.anyio
async def test_transfer_insufficient_funds(client, two_wallets, api_headers):
    """Test transfer fails when sender has insufficient balance."""
    sender_id = two_wallets["sender_id"]
    receiver_id = two_wallets["receiver_id"]

    resp = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": sender_id,
            "to_wallet_id": receiver_id,
            "amount": 50000.0,  # More than sender has
        },
        headers=api_headers,
    )

    assert resp.status_code == 402
    data = resp.json()
    assert "insufficient_funds" in data["detail"]["error"]


@pytest.mark.anyio
async def test_transfer_same_wallet_fails(client, two_wallets, api_headers):
    """Test transfer to same wallet is rejected."""
    sender_id = two_wallets["sender_id"]

    resp = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": sender_id,
            "to_wallet_id": sender_id,
            "amount": 100.0,
        },
        headers=api_headers,
    )

    assert resp.status_code == 400
    assert "same wallet" in resp.json()["detail"]["message"].lower()


@pytest.mark.anyio
async def test_transfer_zero_amount_fails(client, two_wallets, api_headers):
    """Test transfer with zero amount is rejected."""
    sender_id = two_wallets["sender_id"]
    receiver_id = two_wallets["receiver_id"]

    resp = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": sender_id,
            "to_wallet_id": receiver_id,
            "amount": 0.0,
        },
        headers=api_headers,
    )

    assert resp.status_code == 422  # FastAPI validation error


@pytest.mark.anyio
async def test_transfer_wallet_not_found(client, api_headers):
    """Test transfer with non-existent wallet returns 404."""
    resp = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": "nonexistent",
            "to_wallet_id": "also-nonexistent",
            "amount": 100.0,
        },
        headers=api_headers,
    )

    assert resp.status_code == 404


@pytest.mark.anyio
async def test_transfer_records_ledger(client, two_wallets, api_headers):
    """Test that transfer creates ledger entries on both wallets."""
    sender_id = two_wallets["sender_id"]
    receiver_id = two_wallets["receiver_id"]

    await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": sender_id,
            "to_wallet_id": receiver_id,
            "amount": 500.0,
            "correlation_id": "test-correlation-123",
        },
        headers=api_headers,
    )

    sender_ledger = await client.get(
        f"/v1/billing/ledger/{sender_id}", headers=api_headers
    )
    receiver_ledger = await client.get(
        f"/v1/billing/ledger/{receiver_id}", headers=api_headers
    )

    sender_entries = sender_ledger.json()["entries"]
    receiver_entries = receiver_ledger.json()["entries"]

    assert len(sender_entries) >= 2  # Initial credit + transfer out
    assert len(receiver_entries) >= 1  # Transfer in

    transfer_out = next(e for e in sender_entries if e["amount"] < 0)
    transfer_in = next(e for e in receiver_entries if e["amount"] > 0)

    assert transfer_out["amount"] == -500.0
    assert transfer_in["amount"] == 500.0
