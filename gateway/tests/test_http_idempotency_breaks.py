"""Adversarial checks for opt-in HTTP Idempotency-Key routes.

Covers billing charge, billing transfer, agent-wallet provisioning, and API
key issuance. A missing header stays opt-in. A header that is present but
unusable must not run the side effect. A reused key with a different request
must conflict. A retry after success must return the original JSON and must
not repeat the effect.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, LedgerEntryModel
from app.main import app
from app.services.idempotency import MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH
from app.services.signing_keys import sha256_hex
from sqlalchemy import func, select

pytestmark = pytest.mark.dormant


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


def _lines(*keys: str) -> list[tuple[str, str]]:
    """Repeat Idempotency-Key as separate header lines."""
    return [("X-API-Key", "test-key"), *[("Idempotency-Key", key) for key in keys]]


async def _sponsor(client, api_headers, name: str, *, credits: float = 10000):
    response = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": name,
            "email": f"{name}@t.com",
            "initial_credits": credits,
        },
        headers=api_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["wallet_id"]


async def _agent(client, api_headers, sponsor_id: str, agent_id: str, budget: float):
    response = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": agent_id,
            "budget_credits": budget,
        },
        headers=api_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["wallet_id"]


async def _debit_count(wallet_id: str) -> int:
    async with get_session_factory()() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(
                    LedgerEntryModel.wallet_id == wallet_id,
                    LedgerEntryModel.action == "debit",
                )
            )
        ).scalar_one()
    return int(count)


async def _balance(client, api_headers, wallet_id: str) -> float:
    response = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
    assert response.status_code == 200, response.text
    return response.json()["balance"]


def _assert_invalid_key(response, reason_code: str) -> None:
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_idempotency_key"
    assert detail["reason_code"] == reason_code
    assert detail["remediation"]["type"] == "retry_with_valid_idempotency_key"


# --- Header missing vs header present but unusable ---


async def test_charge_without_header_is_a_separate_operation(
    client, api_headers, clean_database
):
    """No Idempotency-Key means each call is its own charge."""
    sponsor = await _sponsor(client, api_headers, "no-header")
    wallet_id = await _agent(client, api_headers, sponsor, "no-header-bot", 5000)
    url = f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=10"

    first = await client.post(url, headers=api_headers)
    second = await client.post(url, headers=api_headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["entry_id"] != second.json()["entry_id"]
    assert await _debit_count(wallet_id) == 2


async def test_blank_idempotency_key_does_not_charge(
    client, api_headers, clean_database
):
    sponsor = await _sponsor(client, api_headers, "blank-key")
    wallet_id = await _agent(client, api_headers, sponsor, "blank-bot", 5000)
    url = f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=10"

    for key, reason in (
        ("", "idempotency_key_blank"),
        ("   ", "idempotency_key_blank"),
    ):
        first = await client.post(url, headers={**api_headers, "Idempotency-Key": key})
        second = await client.post(url, headers={**api_headers, "Idempotency-Key": key})
        _assert_invalid_key(first, reason)
        _assert_invalid_key(second, reason)

    assert await _debit_count(wallet_id) == 0


async def test_overlong_and_control_keys_do_not_charge(
    client, api_headers, clean_database
):
    sponsor = await _sponsor(client, api_headers, "bad-key")
    wallet_id = await _agent(client, api_headers, sponsor, "bad-bot", 5000)
    url = f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=10"
    cases = (
        ("k" * (MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH + 1), "idempotency_key_too_long"),
        ("ab\x01c", "idempotency_key_control_characters"),
        ("ab\x7fc", "idempotency_key_control_characters"),
    )

    for key, reason in cases:
        response = await client.post(
            url, headers={**api_headers, "Idempotency-Key": key}
        )
        _assert_invalid_key(response, reason)

    assert await _debit_count(wallet_id) == 0


async def test_disagreeing_idempotency_key_headers_do_not_charge(
    client, api_headers, clean_database
):
    sponsor = await _sponsor(client, api_headers, "two-keys")
    wallet_id = await _agent(client, api_headers, sponsor, "two-bot", 5000)
    url = f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=10"

    first = await client.post(url, headers=_lines("key-a", "key-b"))
    second = await client.post(url, headers=_lines("key-b", "key-a"))
    _assert_invalid_key(first, "idempotency_key_conflict")
    _assert_invalid_key(second, "idempotency_key_conflict")
    assert await _debit_count(wallet_id) == 0


async def test_repeated_identical_header_replays_one_charge(
    client, api_headers, clean_database
):
    sponsor = await _sponsor(client, api_headers, "same-lines")
    wallet_id = await _agent(client, api_headers, sponsor, "same-bot", 5000)
    url = f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=10"

    first = await client.post(url, headers=_lines("same-key", "same-key"))
    assert first.status_code == 200, first.text
    second = await client.post(
        url, headers={**api_headers, "Idempotency-Key": "same-key"}
    )
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert await _debit_count(wallet_id) == 1


async def test_blank_key_does_not_mint_api_key_or_agent_wallet(
    client, api_headers, clean_database
):
    sponsor = await _sponsor(client, api_headers, "blank-mint", credits=1000)
    payload = {"wallet_id": sponsor, "key_name": "once"}
    headers = {**api_headers, "Idempotency-Key": ""}

    first = await client.post("/v1/api-keys", json=payload, headers=headers)
    second = await client.post("/v1/api-keys", json=payload, headers=headers)
    _assert_invalid_key(first, "idempotency_key_blank")
    _assert_invalid_key(second, "idempotency_key_blank")
    listed = await client.get(f"/v1/api-keys/{sponsor}", headers=api_headers)
    assert listed.status_code == 200
    assert listed.json()["keys"] == []

    agent_body = {
        "sponsor_wallet_id": sponsor,
        "agent_id": "blank-agent",
        "budget_credits": 100,
    }
    agent_headers = {**api_headers, "Idempotency-Key": "   "}
    created = await client.post(
        "/v1/billing/wallets/agent", json=agent_body, headers=agent_headers
    )
    again = await client.post(
        "/v1/billing/wallets/agent", json=agent_body, headers=agent_headers
    )
    _assert_invalid_key(created, "idempotency_key_blank")
    _assert_invalid_key(again, "idempotency_key_blank")
    assert await _balance(client, api_headers, sponsor) == 1000


# --- Same key, different request ---


async def test_transfer_reused_key_with_different_correlation_conflicts(
    client, api_headers, clean_database
):
    sender = await _sponsor(client, api_headers, "corr-src", credits=5000)
    receiver = await _sponsor(client, api_headers, "corr-dst", credits=0)
    headers = {**api_headers, "Idempotency-Key": "xfer-corr-1"}
    base = {
        "from_wallet_id": sender,
        "to_wallet_id": receiver,
        "amount": 100,
        "description": "handoff",
    }

    first = await client.post(
        "/v1/billing/transfer",
        params={**base, "correlation_id": "job-1"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        "/v1/billing/transfer",
        params={**base, "correlation_id": "job-2"},
        headers=headers,
    )

    assert second.status_code == 409, second.text
    assert second.json()["detail"]["error"] == "idempotency_key_reused"
    assert await _balance(client, api_headers, sender) == 4900
    assert await _balance(client, api_headers, receiver) == 100


async def test_transfer_recorded_before_correlation_is_part_of_the_hash_still_replays(
    client, api_headers, clean_database
):
    """Rows written before correlation_id joined the hash must still replay.

    A retry that now hashes differently must not look like a new transfer.
    """
    sender = await _sponsor(client, api_headers, "old-src", credits=5000)
    receiver = await _sponsor(client, api_headers, "old-dst", credits=0)
    old_payload = {
        "from_wallet_id": sender,
        "to_wallet_id": receiver,
        "amount": "25.0",
        "description": None,
    }
    stored = {
        "transfer_id": "old-transfer",
        "from_wallet_id": sender,
        "to_wallet_id": receiver,
        "amount": 25.0,
        "from_balance_after": 4975.0,
        "to_balance_after": 25.0,
        "status": "completed",
    }
    async with get_session_factory()() as session:
        session.add(
            IdempotencyRecordModel(
                record_id="idm-oldtransfer0001",
                wallet_id=sender,
                endpoint="/v1/billing/transfer",
                idempotency_key="xfer-old-hash",
                request_hash=sha256_hex(old_payload),
                response_json=json.dumps(stored),
                status_code=200,
            )
        )
        await session.commit()

    response = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": sender,
            "to_wallet_id": receiver,
            "amount": 25,
            "correlation_id": "added-later",
        },
        headers={**api_headers, "Idempotency-Key": "xfer-old-hash"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["transfer_id"] == "old-transfer"
    assert await _balance(client, api_headers, sender) == 5000
    assert await _balance(client, api_headers, receiver) == 0


# --- Replay after a terminal outcome ---


async def test_charge_replay_returns_the_original_json(
    client, api_headers, clean_database
):
    sponsor = await _sponsor(client, api_headers, "replay-charge")
    wallet_id = await _agent(client, api_headers, sponsor, "replay-bot", 5000)
    url = f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=10"
    headers = {**api_headers, "Idempotency-Key": "charge-replay-body"}

    first = await client.post(url, headers=headers)
    second = await client.post(url, headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert await _debit_count(wallet_id) == 1


async def test_transfer_replay_returns_the_original_json(
    client, api_headers, clean_database
):
    sender = await _sponsor(client, api_headers, "replay-src", credits=5000)
    receiver = await _sponsor(client, api_headers, "replay-dst", credits=0)
    headers = {**api_headers, "Idempotency-Key": "xfer-replay-body"}
    params = {
        "from_wallet_id": sender,
        "to_wallet_id": receiver,
        "amount": 1000,
        "description": "once",
        "correlation_id": "job-replay",
    }

    first = await client.post("/v1/billing/transfer", params=params, headers=headers)
    second = await client.post("/v1/billing/transfer", params=params, headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert isinstance(second.json()["amount"], (int, float))
    assert await _balance(client, api_headers, sender) == 4000
    assert await _balance(client, api_headers, receiver) == 1000


async def test_transfer_error_replays_keep_the_original_body(
    client, api_headers, clean_database
):
    sender = await _sponsor(client, api_headers, "err-src", credits=50)
    receiver = await _sponsor(client, api_headers, "err-dst", credits=0)
    headers = {**api_headers, "Idempotency-Key": "xfer-err-402"}
    params = {
        "from_wallet_id": sender,
        "to_wallet_id": receiver,
        "amount": 1000,
    }

    first = await client.post("/v1/billing/transfer", params=params, headers=headers)
    second = await client.post("/v1/billing/transfer", params=params, headers=headers)
    assert first.status_code == 402, first.text
    assert second.status_code == 402, second.text
    assert second.json() == first.json()
    assert set(first.json()) == {"detail"}
    assert await _balance(client, api_headers, sender) == 50

    missing_headers = {**api_headers, "Idempotency-Key": "xfer-err-404"}
    missing = {
        "from_wallet_id": sender,
        "to_wallet_id": "wlt-missing-dest",
        "amount": 1,
    }
    first_missing = await client.post(
        "/v1/billing/transfer", params=missing, headers=missing_headers
    )
    second_missing = await client.post(
        "/v1/billing/transfer", params=missing, headers=missing_headers
    )
    assert first_missing.status_code == 404, first_missing.text
    assert second_missing.status_code == 404, second_missing.text
    assert second_missing.json() == first_missing.json()
    assert set(first_missing.json()) == {"detail"}

    same_headers = {**api_headers, "Idempotency-Key": "xfer-err-400"}
    same = {"from_wallet_id": sender, "to_wallet_id": sender, "amount": 1}
    first_same = await client.post(
        "/v1/billing/transfer", params=same, headers=same_headers
    )
    second_same = await client.post(
        "/v1/billing/transfer", params=same, headers=same_headers
    )
    assert first_same.status_code == 400, first_same.text
    assert second_same.status_code == 400, second_same.text
    assert second_same.json() == first_same.json()
    assert set(first_same.json()) == {"detail"}
    assert await _balance(client, api_headers, sender) == 50


async def test_transfer_unknown_source_with_key_is_404(
    client, api_headers, clean_database
):
    headers = {**api_headers, "Idempotency-Key": "xfer-missing-source"}
    params = {
        "from_wallet_id": "wlt-missing-source",
        "to_wallet_id": "wlt-missing-dest",
        "amount": 10,
    }

    first = await client.post("/v1/billing/transfer", params=params, headers=headers)
    second = await client.post("/v1/billing/transfer", params=params, headers=headers)

    assert first.status_code == 404, first.text
    assert second.status_code == 404, second.text
    assert second.json() == first.json()
    assert "wlt-missing-source" in first.json()["detail"]


async def test_agent_wallet_replay_returns_the_original_json(
    client, api_headers, clean_database
):
    sponsor = await _sponsor(client, api_headers, "agent-replay", credits=1000)
    payload = {
        "sponsor_wallet_id": sponsor,
        "agent_id": "replay-agent",
        "budget_credits": 100,
    }
    headers = {**api_headers, "Idempotency-Key": "agent-replay-body"}

    first = await client.post(
        "/v1/billing/wallets/agent", json=payload, headers=headers
    )
    second = await client.post(
        "/v1/billing/wallets/agent", json=payload, headers=headers
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json() == first.json()
    assert await _balance(client, api_headers, sponsor) == 900
