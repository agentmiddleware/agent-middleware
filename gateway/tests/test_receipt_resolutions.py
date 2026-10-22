"""Signed resolutions bind to the original receipt and remain publicly verifiable."""

from __future__ import annotations

import asyncio
import hashlib
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from app.db.database import get_session_factory
from app.db.models import ReceiptResolutionModel, ResolutionChainHeadModel
from app.main import app
from app.services.receipt_resolutions import ReceiptResolutionError, append_resolution
from app.services.receipts import get_receipt_service
from b2a_sdk.receipt_verifier import VerificationStatus, key_set_from_document
from b2a_sdk.resolution_verifier import verify_resolution, verify_resolution_chain
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client


@pytest.fixture
async def resolution_database(clean_database):
    """Delete the new FK children before a later test's shared cleanup runs.

    The SEL-352 file allowlist excludes tests/conftest.py, whose cleanup list
    predates these tables. Keep this test module isolated until that list can
    be updated in a separate change.
    """
    yield
    async with get_session_factory()() as session:
        await session.execute(text("DELETE FROM receipt_resolutions"))
        await session.execute(text("DELETE FROM resolution_chain_heads"))
        await session.commit()


async def _uncertain_receipt(
    provisioned, permit, attempt: str, *, outcome: str = "delivery_uncertain"
):
    return await get_receipt_service().create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="resolution-test-tool",
        request_payload={"attempt": attempt},
        response_payload=None,
        ledger_entry_id=None,
        credits_authorized=Decimal("1"),
        credits_charged=Decimal("1"),
        outcome=outcome,
        audit_event_id=None,
    )


async def _setup(client):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="resolution-test-tool",
        idem_key="resolution-test-permit",
    )
    return provisioned, permit


async def _portable_receipt(client, receipt_id, provisioned):
    response = await client.get(
        f"/v1/receipts/{receipt_id}/portable",
        headers=provisioned["agent_headers"],
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.anyio
async def test_append_resolution_replays_one_signed_record(client, resolution_database):
    provisioned, permit = await _setup(client)
    receipt = await _uncertain_receipt(provisioned, permit, "replay")

    first = await append_resolution(receipt.receipt_id, "confirmed_success")
    replay = await append_resolution(receipt.receipt_id, "confirmed_success")

    assert first.resolution_id == replay.resolution_id
    assert first.signature == replay.signature
    assert first.resolution_hash == replay.resolution_hash
    assert first.sequence == replay.sequence == 1
    assert first.prev_hash is None
    async with get_session_factory()() as session:
        count = await session.scalar(
            select(func.count()).select_from(ReceiptResolutionModel)
        )
        assert count == 1
        head = await session.get(
            ResolutionChainHeadModel, provisioned["agent_wallet_id"]
        )
        assert head is not None
        assert head.last_sequence == 1
        assert head.last_hash == first.resolution_hash

    with pytest.raises(ReceiptResolutionError, match="conflict"):
        await append_resolution(receipt.receipt_id, "confirmed_not_delivered")


@pytest.mark.anyio
async def test_append_rejects_non_uncertain_receipt(client, resolution_database):
    provisioned, permit = await _setup(client)
    receipt = await _uncertain_receipt(
        provisioned, permit, "already-final", outcome="success"
    )

    with pytest.raises(ReceiptResolutionError, match="not_delivery_uncertain"):
        await append_resolution(receipt.receipt_id, "confirmed_success")


@pytest.mark.anyio
async def test_concurrent_wallet_appends_preserve_one_chain(
    client, resolution_database
):
    provisioned, permit = await _setup(client)
    receipts = [
        await _uncertain_receipt(provisioned, permit, "race-a"),
        await _uncertain_receipt(provisioned, permit, "race-b"),
    ]

    records = await asyncio.gather(
        *(
            append_resolution(receipt.receipt_id, "confirmed_success")
            for receipt in receipts
        )
    )

    first, second = sorted(records, key=lambda record: record.sequence)
    assert [first.sequence, second.sequence] == [1, 2]
    assert first.prev_hash is None
    assert second.prev_hash == first.resolution_hash


@pytest.mark.anyio
async def test_corrupt_wallet_head_refuses_next_append(client, resolution_database):
    provisioned, permit = await _setup(client)
    first_receipt = await _uncertain_receipt(provisioned, permit, "head-a")
    second_receipt = await _uncertain_receipt(provisioned, permit, "head-b")
    await append_resolution(first_receipt.receipt_id, "confirmed_success")
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE resolution_chain_heads SET last_hash = :bad_hash "
                "WHERE wallet_id = :wallet_id"
            ),
            {"bad_hash": "f" * 64, "wallet_id": provisioned["agent_wallet_id"]},
        )
        await session.commit()

    with pytest.raises(ReceiptResolutionError, match="resolution_chain_invalid"):
        await append_resolution(second_receipt.receipt_id, "confirmed_success")
    async with get_session_factory()() as session:
        count = await session.scalar(
            select(func.count()).select_from(ReceiptResolutionModel)
        )
        assert count == 1


@pytest.mark.anyio
async def test_public_resolution_and_wallet_chain_verify_without_credentials(
    client, resolution_database
):
    provisioned, permit = await _setup(client)
    first_receipt = await _uncertain_receipt(provisioned, permit, "first")
    second_receipt = await _uncertain_receipt(provisioned, permit, "second")
    first = await append_resolution(first_receipt.receipt_id, "confirmed_success")
    second = await append_resolution(
        second_receipt.receipt_id, "confirmed_not_delivered"
    )
    assert second.sequence == 2
    assert second.prev_hash == first.resolution_hash

    record_response = await client.get(
        f"/v1/receipts/{first_receipt.receipt_id}/resolution"
    )
    assert record_response.status_code == 200, record_response.text
    record = record_response.json()
    assert record["resolution_id"] == first.resolution_id

    chain_response = await client.get(
        f"/v1/receipts/resolutions/wallet/{provisioned['agent_wallet_id']}"
    )
    assert chain_response.status_code == 200, chain_response.text
    chain = chain_response.json()
    assert chain["total"] == 2
    assert [item["sequence"] for item in chain["records"]] == [1, 2]
    first_page = await client.get(
        f"/v1/receipts/resolutions/wallet/{provisioned['agent_wallet_id']}?limit=1"
    )
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["next_offset"] == 1
    second_page = await client.get(
        f"/v1/receipts/resolutions/wallet/{provisioned['agent_wallet_id']}?limit=1&offset=1"
    )
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["next_offset"] is None
    assert (
        first_page.json()["records"] + second_page.json()["records"] == chain["records"]
    )

    keys_response = await client.get("/.well-known/trust-keys.json")
    assert keys_response.status_code == 200, keys_response.text
    keys = key_set_from_document(keys_response.json())
    receipt_bundle = await _portable_receipt(
        client, first_receipt.receipt_id, provisioned
    )
    expected_hash = hashlib.sha256(
        receipt_bundle["signing_input"].encode("utf-8")
    ).hexdigest()
    assert first.receipt_hash == expected_hash
    assert record["receipt_hash"] == expected_hash
    assert verify_resolution(record, receipt_bundle, keys).ok
    verified_chain = verify_resolution_chain(chain["records"], receipt_bundle, keys)
    assert verified_chain.ok, verified_chain.reason
    assert verified_chain.records_verified == 2

    reordered = list(reversed(chain["records"]))
    assert (
        verify_resolution_chain(reordered, receipt_bundle, keys).status
        is VerificationStatus.MISMATCH
    )


@pytest.mark.anyio
async def test_resolution_for_a_different_valid_receipt_is_rejected(
    client, resolution_database
):
    provisioned, permit = await _setup(client)
    first_receipt = await _uncertain_receipt(provisioned, permit, "bound")
    wrong_receipt = await _uncertain_receipt(provisioned, permit, "other")
    await append_resolution(first_receipt.receipt_id, "confirmed_success")

    record_response = await client.get(
        f"/v1/receipts/{first_receipt.receipt_id}/resolution"
    )
    assert record_response.status_code == 200, record_response.text
    keys_response = await client.get("/.well-known/trust-keys.json")
    assert keys_response.status_code == 200, keys_response.text
    keys = key_set_from_document(keys_response.json())
    wrong_bundle = await _portable_receipt(
        client, wrong_receipt.receipt_id, provisioned
    )

    verdict = verify_resolution(record_response.json(), wrong_bundle, keys)
    assert verdict.status is VerificationStatus.MISMATCH
    assert verdict.is_rejected


@pytest.mark.anyio
async def test_tampered_resolution_signing_input_is_rejected(
    client, resolution_database
):
    provisioned, permit = await _setup(client)
    receipt = await _uncertain_receipt(provisioned, permit, "tamper")
    await append_resolution(receipt.receipt_id, "confirmed_success")

    record_response = await client.get(f"/v1/receipts/{receipt.receipt_id}/resolution")
    assert record_response.status_code == 200, record_response.text
    keys_response = await client.get("/.well-known/trust-keys.json")
    assert keys_response.status_code == 200, keys_response.text
    receipt_bundle = await _portable_receipt(client, receipt.receipt_id, provisioned)
    tampered = dict(record_response.json())
    tampered["signing_input"] = tampered["signing_input"].replace(
        '"outcome":"confirmed_success"',
        '"outcome":"confirmed_not_delivered"',
        1,
    )

    verdict = verify_resolution(
        tampered, receipt_bundle, key_set_from_document(keys_response.json())
    )
    assert verdict.status is VerificationStatus.INVALID
    assert verdict.is_rejected
