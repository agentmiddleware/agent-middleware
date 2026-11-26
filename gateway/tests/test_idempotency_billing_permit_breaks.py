"""Break tests for billing, permit, and settlement idempotency callers.

These cover replay keys the billing and permit routes used to accept without
the checks the governed MCP and AWI routes already apply: a second
Idempotency-Key line, a blank or overlong key, a control character, a
transfer correlation id left out of the replay hash, and a stored transfer
body that does not match the body the caller already received.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.audit_log import list_audit_events
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

EVM_PAY_TO = "0x1111111111111111111111111111111111111111"
EVM_PAYER = "0x2222222222222222222222222222222222222222"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


def _assert_invalid_key(resp, reason_code: str) -> None:
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_idempotency_key"
    assert detail["reason_code"] == reason_code
    assert "Idempotency-Key header" in detail["source"]
    assert detail["message"].startswith("invalid_idempotency_key")
    assert detail["remediation"]["type"] == "retry_with_valid_idempotency_key"


async def _debit_count(client, wallet_id: str, api_headers: dict) -> int:
    ledger = await client.get(f"/v1/billing/ledger/{wallet_id}", headers=api_headers)
    assert ledger.status_code == 200, ledger.text
    return sum(1 for entry in ledger.json()["entries"] if entry["action"] == "debit")


async def _balance(client, wallet_id: str, api_headers: dict) -> float:
    wallet = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
    assert wallet.status_code == 200, wallet.text
    return wallet.json()["balance"]


async def _funded_agent(client, api_headers: dict, *, tag: str) -> str:
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": tag,
            "email": f"{tag}@example.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    assert sponsor.status_code == 201, sponsor.text
    agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor.json()["wallet_id"],
            "agent_id": tag,
            "budget_credits": 5000,
        },
        headers=api_headers,
    )
    assert agent.status_code == 201, agent.text
    return agent.json()["wallet_id"]


async def _transfer_pair(client, api_headers: dict, *, tag: str) -> tuple[str, str]:
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": tag,
            "email": f"{tag}@example.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    assert sponsor.status_code == 201, sponsor.text
    src = sponsor.json()["wallet_id"]
    source = await client.post(
        "/v1/billing/wallets/agent",
        json={"sponsor_wallet_id": src, "agent_id": f"{tag}-a", "budget_credits": 5000},
        headers=api_headers,
    )
    dest = await client.post(
        "/v1/billing/wallets/agent",
        json={"sponsor_wallet_id": src, "agent_id": f"{tag}-b", "budget_credits": 100},
        headers=api_headers,
    )
    assert source.status_code == 201, source.text
    assert dest.status_code == 201, dest.text
    return source.json()["wallet_id"], dest.json()["wallet_id"]


def _permit_body(wallet_id: str) -> dict:
    return {
        "issuer_wallet_id": wallet_id,
        "subject_wallet_id": wallet_id,
        "allowed_tools": ["break-tool"],
        "scopes": ["tool:break-tool:invoke", "billing:charge"],
        "max_credits": 5,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    }


def _permit_request_body(agent: dict) -> dict:
    return {
        "issuer_wallet_id": agent["sponsor_wallet_id"],
        "subject_wallet_id": agent["agent_wallet_id"],
        "allowed_tools": ["break-tool"],
        "max_credits": 5,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "justification": "Break test ask.",
    }


@pytest.mark.anyio
async def test_charge_refuses_two_different_idempotency_key_lines(
    client, api_headers, clean_database
):
    """A second Idempotency-Key line must not be dropped and then charged."""
    wallet_id = await _funded_agent(client, api_headers, tag="charge-two-keys")
    url = f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=10"
    conflicting = [
        ("X-API-Key", "test-key"),
        ("Idempotency-Key", "charge-line-a"),
        ("Idempotency-Key", "charge-line-b"),
    ]
    refused = await client.post(url, headers=conflicting)
    _assert_invalid_key(refused, "idempotency_key_conflict")
    assert await _debit_count(client, wallet_id, api_headers) == 0

    # The dropped line must not have become a live replay slot either.
    only_b = await client.post(
        url, headers={**api_headers, "Idempotency-Key": "charge-line-b"}
    )
    assert only_b.status_code == 200, only_b.text
    assert await _debit_count(client, wallet_id, api_headers) == 1


@pytest.mark.anyio
async def test_charge_collapses_identical_idempotency_key_lines(
    client, api_headers, clean_database
):
    wallet_id = await _funded_agent(client, api_headers, tag="charge-same-lines")
    url = f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=10"
    repeated = [
        ("X-API-Key", "test-key"),
        ("Idempotency-Key", "charge-same"),
        ("Idempotency-Key", "charge-same"),
    ]
    first = await client.post(url, headers=repeated)
    assert first.status_code == 200, first.text
    replay = await client.post(
        url, headers={**api_headers, "Idempotency-Key": "charge-same"}
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["entry_id"] == first.json()["entry_id"]
    assert await _debit_count(client, wallet_id, api_headers) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("key", "reason_code"),
    [
        ("   ", "idempotency_key_blank"),
        ("k" * 129, "idempotency_key_too_long"),
        ("bad\nkey", "idempotency_key_control_characters"),
        ("bad\x7fkey", "idempotency_key_control_characters"),
    ],
)
async def test_charge_refuses_unusable_idempotency_key_before_debit(
    client, api_headers, clean_database, key, reason_code
):
    wallet_id = await _funded_agent(
        client, api_headers, tag=f"charge-bad-{reason_code}"
    )
    url = f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=10"
    refused = await client.post(url, headers={**api_headers, "Idempotency-Key": key})
    _assert_invalid_key(refused, reason_code)
    assert await _debit_count(client, wallet_id, api_headers) == 0


@pytest.mark.anyio
async def test_agent_wallet_refuses_two_different_idempotency_key_lines(
    client, api_headers, clean_database
):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Two Key Sponsor",
            "email": "two-key-sponsor@example.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    assert sponsor.status_code == 201, sponsor.text
    sponsor_id = sponsor.json()["wallet_id"]
    refused = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "two-key-agent",
            "budget_credits": 1000,
        },
        headers=[
            ("X-API-Key", "test-key"),
            ("Idempotency-Key", "provision-line-a"),
            ("Idempotency-Key", "provision-line-b"),
        ],
    )
    _assert_invalid_key(refused, "idempotency_key_conflict")
    assert await _balance(client, sponsor_id, api_headers) == 10000.0


@pytest.mark.anyio
async def test_transfer_refuses_two_different_idempotency_key_lines(
    client, api_headers, clean_database
):
    source, dest = await _transfer_pair(client, api_headers, tag="xfer-two-keys")
    url = (
        f"/v1/billing/transfer?from_wallet_id={source}&to_wallet_id={dest}&amount=1000"
    )
    refused = await client.post(
        url,
        headers=[
            ("X-API-Key", "test-key"),
            ("Idempotency-Key", "xfer-line-a"),
            ("Idempotency-Key", "xfer-line-b"),
        ],
    )
    _assert_invalid_key(refused, "idempotency_key_conflict")
    assert await _balance(client, source, api_headers) == 5000.0
    assert await _balance(client, dest, api_headers) == 100.0


@pytest.mark.anyio
async def test_transfer_correlation_id_is_part_of_the_replay_identity(
    client, api_headers, clean_database
):
    """Same wallets and amount with a different correlation id is a new ask."""
    source, dest = await _transfer_pair(client, api_headers, tag="xfer-corr")
    headers = {**api_headers, "Idempotency-Key": "xfer-corr-1"}
    base = (
        f"/v1/billing/transfer?from_wallet_id={source}&to_wallet_id={dest}&amount=1000"
    )
    first = await client.post(f"{base}&correlation_id=corr-a", headers=headers)
    assert first.status_code == 200, first.text
    assert isinstance(first.json()["amount"], (int, float))

    conflict = await client.post(f"{base}&correlation_id=corr-b", headers=headers)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["error"] == "idempotency_key_reused"

    same = await client.post(f"{base}&correlation_id=corr-a", headers=headers)
    assert same.status_code == 200, same.text
    assert same.json() == first.json()
    assert await _balance(client, source, api_headers) == 4000.0
    assert await _balance(client, dest, api_headers) == 1100.0


@pytest.mark.anyio
async def test_transfer_success_replay_matches_the_first_body(
    client, api_headers, clean_database
):
    source, dest = await _transfer_pair(client, api_headers, tag="xfer-body")
    headers = {**api_headers, "Idempotency-Key": "xfer-body-1"}
    url = (
        f"/v1/billing/transfer?from_wallet_id={source}&to_wallet_id={dest}&amount=1000"
    )
    first = await client.post(url, headers=headers)
    assert first.status_code == 200, first.text
    replay = await client.post(url, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert isinstance(replay.json()["amount"], (int, float))
    assert isinstance(replay.json()["from_balance_after"], (int, float))
    assert await _balance(client, source, api_headers) == 4000.0
    assert await _balance(client, dest, api_headers) == 1100.0


@pytest.mark.anyio
async def test_transfer_error_replay_matches_the_first_body(
    client, api_headers, clean_database
):
    source, dest = await _transfer_pair(client, api_headers, tag="xfer-err")
    missing = (
        f"/v1/billing/transfer?from_wallet_id={source}"
        "&to_wallet_id=wal-not-a-destination&amount=10"
    )
    headers = {**api_headers, "Idempotency-Key": "xfer-missing-dest"}
    first_missing = await client.post(missing, headers=headers)
    assert first_missing.status_code == 404, first_missing.text
    replay_missing = await client.post(missing, headers=headers)
    assert replay_missing.status_code == 404, replay_missing.text
    assert replay_missing.json() == first_missing.json()

    too_much = (
        f"/v1/billing/transfer?from_wallet_id={source}&to_wallet_id={dest}&amount=9000"
    )
    poor = {**api_headers, "Idempotency-Key": "xfer-poor"}
    first_poor = await client.post(too_much, headers=poor)
    assert first_poor.status_code == 402, first_poor.text
    replay_poor = await client.post(too_much, headers=poor)
    assert replay_poor.status_code == 402, replay_poor.text
    assert replay_poor.json() == first_poor.json()
    assert await _balance(client, source, api_headers) == 5000.0
    assert await _balance(client, dest, api_headers) == 100.0


@pytest.mark.anyio
async def test_keyed_transfer_from_unknown_wallet_is_not_a_server_error(
    client, api_headers, clean_database
):
    resp = await client.post(
        "/v1/billing/transfer?from_wallet_id=wal-missing-source"
        "&to_wallet_id=wal-missing-dest&amount=1",
        headers={**api_headers, "Idempotency-Key": "xfer-missing-source"},
    )
    assert resp.status_code == 404, resp.text
    again = await client.post(
        "/v1/billing/transfer?from_wallet_id=wal-missing-source"
        "&to_wallet_id=wal-missing-dest&amount=1",
        headers={**api_headers, "Idempotency-Key": "xfer-missing-source"},
    )
    assert again.status_code == 404, again.text


@pytest.mark.anyio
async def test_unkeyed_transfer_from_unknown_wallet_records_governance_denial(
    client, api_headers, clean_database
):
    source = "wal-missing-source"
    request_id = "xfer-missing-source-unkeyed"
    resp = await client.post(
        f"/v1/billing/transfer?from_wallet_id={source}"
        f"&to_wallet_id=wal-missing-dest&amount=1&correlation_id={request_id}",
        headers=api_headers,
    )
    assert resp.status_code == 404, resp.text
    events = await list_audit_events(wallet_id=source, request_id=request_id)
    assert len(events) == 1
    assert events[0].event == "billing.transfer"
    assert events[0].ok is False
    assert events[0].error == "wallet_not_found"


@pytest.mark.anyio
async def test_permit_refuses_two_different_idempotency_key_lines(
    client, clean_database
):
    agent = await provision_agent_wallet(client)
    body = _permit_body(agent["agent_wallet_id"])
    refused = await client.post(
        "/v1/permits",
        json=body,
        headers=[
            *BOOTSTRAP_HEADERS.items(),
            ("Idempotency-Key", "permit-line-a"),
            ("Idempotency-Key", "permit-line-b"),
        ],
    )
    _assert_invalid_key(refused, "idempotency_key_conflict")

    created = await client.post(
        "/v1/permits",
        json=body,
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "permit-line-b"},
    )
    assert created.status_code == 201, created.text


@pytest.mark.anyio
async def test_permit_refuses_blank_idempotency_key(client, clean_database):
    agent = await provision_agent_wallet(client)
    refused = await client.post(
        "/v1/permits",
        json=_permit_body(agent["agent_wallet_id"]),
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "   "},
    )
    _assert_invalid_key(refused, "idempotency_key_blank")


@pytest.mark.anyio
async def test_permit_still_requires_an_idempotency_key_header(client, clean_database):
    agent = await provision_agent_wallet(client)
    missing = await client.post(
        "/v1/permits",
        json=_permit_body(agent["agent_wallet_id"]),
        headers=BOOTSTRAP_HEADERS,
    )
    assert missing.status_code == 422, missing.text


@pytest.mark.anyio
async def test_permit_request_refuses_two_different_idempotency_key_lines(
    client, clean_database
):
    agent = await provision_agent_wallet(client)
    refused = await client.post(
        "/v1/permit-requests",
        json=_permit_request_body(agent),
        headers=[
            *agent["agent_headers"].items(),
            ("Idempotency-Key", "preq-line-a"),
            ("Idempotency-Key", "preq-line-b"),
        ],
    )
    _assert_invalid_key(refused, "idempotency_key_conflict")


@pytest.mark.anyio
async def test_x402_settle_refuses_two_different_idempotency_key_lines(
    client, clean_database
):
    agent = await provision_agent_wallet(client)
    permit = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": agent["agent_wallet_id"],
            "subject_wallet_id": agent["agent_wallet_id"],
            "subject_key_id": agent["key_id"],
            "allowed_tools": ["x402.payment"],
            "scopes": ["tool:x402.payment:invoke", "billing:charge"],
            "max_credits": 100,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "x402-break-permit"},
    )
    assert permit.status_code == 201, permit.text
    permit_id = permit.json()["permit_id"]
    refused = await client.post(
        "/v1/x402/settle",
        json={
            "permit_id": permit_id,
            "wallet_id": agent["agent_wallet_id"],
            "amount": "0.03",
            "pay_to": EVM_PAY_TO,
            "network": "base",
            "payer": EVM_PAYER,
        },
        headers=[
            *agent["agent_headers"].items(),
            ("Idempotency-Key", "x402-line-a"),
            ("Idempotency-Key", "x402-line-b"),
        ],
    )
    _assert_invalid_key(refused, "idempotency_key_conflict")
    stored = await client.get(f"/v1/permits/{permit_id}", headers=BOOTSTRAP_HEADERS)
    assert stored.status_code == 200, stored.text
    assert Decimal(str(stored.json()["spent_credits"])) == 0
