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


async def _wallet_balance(wallet_id: str):
    from app.db.database import get_session_factory
    from app.db.models import WalletModel

    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        return wallet.balance


async def _transfer_debit_count(wallet_id: str) -> int:
    from sqlalchemy import select

    from app.db.database import get_session_factory
    from app.db.models import LedgerEntryModel

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.wallet_id == wallet_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return sum(1 for row in rows if row.action == "transfer" and row.amount < 0)


@pytest.mark.anyio
async def test_same_correlation_id_does_not_move_credits_twice(clean_database):
    """A retry that repeats correlation_id must not pay the same transfer again.

    The id is the transfer's identity. The HTTP Idempotency-Key header is a
    separate, optional guard, so a client that retries with only this id used
    to move the credits a second time.
    """
    from decimal import Decimal

    from app.services.agent_money import get_agent_money

    money = get_agent_money()
    sender = await money.create_sponsor_wallet(
        sponsor_name="Corr Sender",
        email="corr-sender@t.com",
        initial_credits=Decimal("10000"),
    )
    receiver = await money.create_sponsor_wallet(
        sponsor_name="Corr Receiver",
        email="corr-receiver@t.com",
        initial_credits=Decimal("0"),
    )
    first = await money.transfer(
        from_wallet_id=sender.wallet_id,
        to_wallet_id=receiver.wallet_id,
        amount=Decimal("1000"),
        correlation_id="retry-corr-1",
    )
    sender_after = await _wallet_balance(sender.wallet_id)
    receiver_after = await _wallet_balance(receiver.wallet_id)

    second = await money.transfer(
        from_wallet_id=sender.wallet_id,
        to_wallet_id=receiver.wallet_id,
        amount=Decimal("1000"),
        correlation_id="retry-corr-1",
    )

    assert await _wallet_balance(sender.wallet_id) == sender_after
    assert await _wallet_balance(receiver.wallet_id) == receiver_after
    assert second["from_balance_after"] == first["from_balance_after"]
    assert second["to_balance_after"] == first["to_balance_after"]
    assert second["transfer_id"] == "retry-corr-1"
    assert await _transfer_debit_count(sender.wallet_id) == 1


@pytest.mark.anyio
async def test_reused_correlation_id_with_different_amount_is_rejected(
    client, two_wallets, api_headers
):
    """The same id with a different amount is a conflict, not a second payment."""
    sender_id = two_wallets["sender_id"]
    receiver_id = two_wallets["receiver_id"]
    params = {
        "from_wallet_id": sender_id,
        "to_wallet_id": receiver_id,
        "correlation_id": "retry-corr-conflict",
    }

    first = await client.post(
        "/v1/billing/transfer",
        params={**params, "amount": 1000.0},
        headers=api_headers,
    )
    assert first.status_code == 200
    sender_after = await _wallet_balance(sender_id)

    conflict = await client.post(
        "/v1/billing/transfer",
        params={**params, "amount": 50.0},
        headers=api_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "idempotency_key_reused"
    assert await _wallet_balance(sender_id) == sender_after
    assert await _transfer_debit_count(sender_id) == 1


@pytest.mark.anyio
async def test_correlation_id_retry_on_the_http_route_moves_credits_once(
    client, two_wallets, api_headers
):
    """The public route honors correlation_id even with no Idempotency-Key."""
    sender_id = two_wallets["sender_id"]
    receiver_id = two_wallets["receiver_id"]
    params = {
        "from_wallet_id": sender_id,
        "to_wallet_id": receiver_id,
        "amount": 1000.0,
        "correlation_id": "retry-corr-http",
    }

    first = await client.post(
        "/v1/billing/transfer", params=params, headers=api_headers
    )
    assert first.status_code == 200
    second = await client.post(
        "/v1/billing/transfer", params=params, headers=api_headers
    )
    assert second.status_code == 200
    assert second.json()["transfer_id"] == first.json()["transfer_id"]
    assert "idempotent_replay" not in second.json()

    sender = await client.get(f"/v1/billing/wallets/{sender_id}", headers=api_headers)
    receiver = await client.get(
        f"/v1/billing/wallets/{receiver_id}", headers=api_headers
    )
    assert sender.json()["balance"] == 9000.0
    assert receiver.json()["balance"] == 1000.0
    assert await _transfer_debit_count(sender_id) == 1


@pytest.mark.anyio
async def test_idempotency_key_dedupes_after_the_http_record_is_gone(
    client, two_wallets, api_headers
):
    """Losing the HTTP replay row must not let the same key move credits again.

    The header guard lives outside the ledger. A crash after the transfer
    commits, or a deleted replay row, used to run the movement a second time.
    """
    from sqlalchemy import delete as sa_delete

    from app.db.database import get_session_factory
    from app.db.models import IdempotencyRecordModel

    sender_id = two_wallets["sender_id"]
    receiver_id = two_wallets["receiver_id"]
    headers = {**api_headers, "Idempotency-Key": "xfer-engine-key-1"}
    params = {
        "from_wallet_id": sender_id,
        "to_wallet_id": receiver_id,
        "amount": 1000.0,
    }

    first = await client.post("/v1/billing/transfer", params=params, headers=headers)
    assert first.status_code == 200

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            sa_delete(IdempotencyRecordModel).where(
                IdempotencyRecordModel.idempotency_key == "xfer-engine-key-1"
            )
        )
        await session.commit()

    second = await client.post("/v1/billing/transfer", params=params, headers=headers)
    assert second.status_code == 200
    assert "idempotent_replay" not in second.json()

    sender = await client.get(f"/v1/billing/wallets/{sender_id}", headers=api_headers)
    receiver = await client.get(
        f"/v1/billing/wallets/{receiver_id}", headers=api_headers
    )
    assert sender.json()["balance"] == 9000.0
    assert receiver.json()["balance"] == 1000.0
    assert await _transfer_debit_count(sender_id) == 1
