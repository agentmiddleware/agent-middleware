"""Signed resolutions bind to the original receipt and remain publicly verifiable."""

from __future__ import annotations

import asyncio
import hashlib
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from app.db.database import get_session_factory
from app.db.models import (
    ReceiptResolutionModel,
    ResolutionChainHeadModel,
    SigningKeyModel,
)
from app.main import app
from app.services.receipt_resolutions import ReceiptResolutionError, append_resolution
from app.services.receipts import get_receipt_service
from app.services.signing_keys import get_signing_key_service
from b2a_sdk.receipt_verifier import VerificationStatus, key_set_from_document
from b2a_sdk.resolution_verifier import verify_resolution, verify_resolution_chain
from tests.support.db_gate import collection_mark, require_live_postgres
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

POSTGRES_REASON = "requires an opted-in disposable PostgreSQL test database"


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client


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
async def test_append_resolution_replays_one_signed_record(client, clean_database):
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
async def test_append_rejects_non_uncertain_receipt(client, clean_database):
    provisioned, permit = await _setup(client)
    receipt = await _uncertain_receipt(
        provisioned, permit, "already-final", outcome="success"
    )

    with pytest.raises(ReceiptResolutionError, match="not_delivery_uncertain"):
        await append_resolution(receipt.receipt_id, "confirmed_success")


@pytest.mark.anyio
async def test_concurrent_wallet_appends_preserve_one_chain(client, clean_database):
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
@collection_mark(flag="RUN_POSTGRES_CONCURRENCY_TESTS", reason=POSTGRES_REASON)
async def test_postgres_parallel_wallet_appends_preserve_one_chain(
    client, clean_database
):
    require_live_postgres(flag="RUN_POSTGRES_CONCURRENCY_TESTS", reason=POSTGRES_REASON)
    provisioned, permit = await _setup(client)
    receipts = [
        await _uncertain_receipt(provisioned, permit, f"postgres-{index}")
        for index in range(10)
    ]

    records = await asyncio.gather(
        *(
            append_resolution(receipt.receipt_id, "confirmed_success")
            for receipt in receipts
        )
    )
    ordered = sorted(records, key=lambda record: record.sequence)
    assert [record.sequence for record in ordered] == list(range(1, 11))
    assert ordered[0].prev_hash is None
    assert all(
        current.prev_hash == previous.resolution_hash
        for previous, current in zip(ordered, ordered[1:], strict=False)
    )

    async with get_session_factory()() as session:
        stored = (
            (
                await session.execute(
                    select(ReceiptResolutionModel)
                    .where(
                        ReceiptResolutionModel.wallet_id
                        == provisioned["agent_wallet_id"]
                    )
                    .order_by(ReceiptResolutionModel.sequence)
                )
            )
            .scalars()
            .all()
        )
        head = await session.get(
            ResolutionChainHeadModel, provisioned["agent_wallet_id"]
        )
    assert [record.resolution_id for record in stored] == [
        record.resolution_id for record in ordered
    ]
    assert {record.receipt_id for record in stored} == {
        receipt.receipt_id for receipt in receipts
    }
    assert head is not None
    assert head.last_sequence == 10
    assert head.last_hash == ordered[-1].resolution_hash


@pytest.mark.anyio
async def test_disabled_tail_key_does_not_block_next_append(
    client, clean_database, monkeypatch
):
    provisioned, permit = await _setup(client)
    first_receipt = await _uncertain_receipt(provisioned, permit, "disabled-tail-a")
    first = await append_resolution(first_receipt.receipt_id, "confirmed_success")
    signing_keys = get_signing_key_service()
    monkeypatch.setattr(signing_keys, "_private_key", Ed25519PrivateKey.generate())
    monkeypatch.setattr(signing_keys, "_key_id", "test-resolution-tail-rotation")
    rotated = await signing_keys.ensure_active_key()
    second_receipt = await _uncertain_receipt(provisioned, permit, "disabled-tail-b")
    async with get_session_factory()() as session:
        old_key = await session.get(SigningKeyModel, first.signature_key_id)
        assert old_key is not None
        old_key.status = "disabled"
        await session.commit()

    second = await append_resolution(second_receipt.receipt_id, "confirmed_success")
    assert second.sequence == 2
    assert second.prev_hash == first.resolution_hash
    assert second.signature_key_id == rotated.key_id


@pytest.mark.anyio
async def test_corrupt_wallet_head_refuses_next_append(client, clean_database):
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
    client, clean_database
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
    assert record["issuer"] == receipt_bundle["issuer"]
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
async def test_resolution_get_returns_404_when_absent(client, clean_database):
    provisioned, permit = await _setup(client)
    receipt = await _uncertain_receipt(provisioned, permit, "unresolved")

    response = await client.get(f"/v1/receipts/{receipt.receipt_id}/resolution")
    assert response.status_code == 404
    assert response.json()["detail"] == "resolution_not_found"


@pytest.mark.anyio
async def test_wallet_chain_rejects_tampered_middle_record(client, clean_database):
    provisioned, permit = await _setup(client)
    receipts = [
        await _uncertain_receipt(provisioned, permit, f"middle-{index}")
        for index in range(3)
    ]
    records = [
        await append_resolution(receipt.receipt_id, "confirmed_success")
        for receipt in receipts
    ]
    async with get_session_factory()() as session:
        middle = await session.get(ReceiptResolutionModel, records[1].resolution_id)
        assert middle is not None
        middle.signature = "tampered"
        await session.commit()

    response = await client.get(
        f"/v1/receipts/resolutions/wallet/{provisioned['agent_wallet_id']}"
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "resolution_integrity_invalid"


@pytest.mark.anyio
async def test_wallet_chain_verifies_across_key_rotation(
    client, clean_database, monkeypatch
):
    provisioned, permit = await _setup(client)
    first_receipt = await _uncertain_receipt(provisioned, permit, "key-a")
    first = await append_resolution(first_receipt.receipt_id, "confirmed_success")
    signing_keys = get_signing_key_service()
    retired = await signing_keys.retire_key_metadata(first.signature_key_id)
    monkeypatch.setattr(signing_keys, "_private_key", Ed25519PrivateKey.generate())
    monkeypatch.setattr(signing_keys, "_key_id", "test-resolution-chain-rotation")
    rotated = await signing_keys.ensure_active_key()
    assert rotated.public_key_b64 != retired.public_key_b64
    second_receipt = await _uncertain_receipt(provisioned, permit, "key-b")
    second = await append_resolution(
        second_receipt.receipt_id, "confirmed_not_delivered"
    )
    assert first.signature_key_id != second.signature_key_id
    assert second.signature_key_id == rotated.key_id

    chain_response = await client.get(
        f"/v1/receipts/resolutions/wallet/{provisioned['agent_wallet_id']}"
    )
    assert chain_response.status_code == 200, chain_response.text
    keys_response = await client.get("/.well-known/trust-keys.json")
    assert keys_response.status_code == 200, keys_response.text
    first_bundle = await _portable_receipt(
        client, first_receipt.receipt_id, provisioned
    )
    verdict = verify_resolution_chain(
        chain_response.json()["records"],
        first_bundle,
        key_set_from_document(keys_response.json()),
    )
    assert verdict.ok, verdict.reason
    assert verdict.records_verified == 2


@pytest.mark.anyio
async def test_wallet_chain_offset_past_end_returns_empty_page(client, clean_database):
    provisioned, permit = await _setup(client)
    receipt = await _uncertain_receipt(provisioned, permit, "past-end")
    await append_resolution(receipt.receipt_id, "confirmed_success")

    response = await client.get(
        f"/v1/receipts/resolutions/wallet/{provisioned['agent_wallet_id']}?offset=5"
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["records"] == []
    assert page["total"] == 1
    assert page["offset"] == 5
    assert page["has_more"] is False
    assert page["next_offset"] is None


@pytest.mark.anyio
async def test_resolution_for_a_different_valid_receipt_is_rejected(
    client, clean_database
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
async def test_tampered_resolution_signing_input_is_rejected(client, clean_database):
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
