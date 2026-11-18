"""Authority and evidence attacks against runtime-issued demo authority."""

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from b2a_sdk.jcs import dumps_text
from b2a_sdk.receipt_verifier import (
    VerificationStatus,
    key_set_from_document,
    verify_bundle,
)


def test_conflicting_payload_cannot_reuse_a_completed_key(judge):
    """Verify conflicting input cannot reuse a completed idempotency key."""
    judge.issue()
    first = judge.call()
    assert first["status"] == 200
    first_receipt_id = first["body"]["result"]["receipt"]["receipt_id"]
    first_snapshot = judge.snapshot()
    conflict = judge.call(message="conflicting input")
    assert conflict["status"] == 200
    assert conflict["body"]["error"]["message"] == "idempotency_key_reused"
    after_conflict = judge.snapshot()
    assert after_conflict["dispatches"] == after_conflict["debits"] == 1
    assert after_conflict["balance"] == first_snapshot["balance"]
    assert after_conflict["spent"] == first_snapshot["spent"]
    replay = judge.call()
    assert replay["status"] == 200
    assert replay["body"]["result"]["receipt"]["receipt_id"] == first_receipt_id
    after_replay = judge.snapshot()
    assert after_replay["spent"] == first_snapshot["spent"]
    assert after_replay["debits"] == first_snapshot["debits"]
    assert replay["body"]["result"]["receipt"]["receipt_id"] == first_receipt_id


@pytest.mark.parametrize("attack", ["anonymous", "tool_scope", "budget"])
def test_guardrails_deny_before_dispatch_or_money(judge, attack):
    """Verify authority and budget denials leave execution state unchanged."""
    judge.issue()
    if attack == "budget":
        # Spend the issued cap using genuine product-generated operations.
        # Stop at the first denial rather than inventing a balance or permit.
        succeeded = 0
        for index in range(20):
            before = judge.snapshot()
            response = judge.call(key=f"budget-{index}")
            if "error" in response["body"]:
                break
            succeeded += 1
        else:
            pytest.fail("demo permit failed to bound spend within 20 calls")
        assert response["body"]["error"]["message"] == "permit_budget_exceeded"
        assert succeeded == 5, f"expected 5 successful calls, got {succeeded}"
        from decimal import Decimal

        assert Decimal(before["spent"]) == Decimal("5"), (
            f"expected 5 credits spent, got {before['spent']}"
        )
    else:
        before = judge.snapshot()
        response = judge.call(
            authenticated=attack != "anonymous",
            tool="partner.other" if attack == "tool_scope" else "partner.echo",
        )
        if attack == "anonymous":
            assert response["status"] in (401, 403)
        else:
            assert response["body"]["error"]["message"] in (
                "key_tool_not_allowed",
                "demo_tool_not_allowed",
            )
    after = judge.snapshot()
    for field in ("balance", "spent", "dispatches", "debits", "refunds"):
        assert after[field] == before[field], field


def test_offline_tamper_and_unknown_key_fail_closed(judge):
    """Verify offline validation rejects tampering and unknown signing keys."""
    judge.issue()
    first = judge.call()
    receipt_id = first["body"]["result"]["receipt"]["receipt_id"]
    bundle, document = judge.export(receipt_id)
    judge.close()
    keys = key_set_from_document(document)
    original_result = verify_bundle(bundle, keys)
    assert original_result.ok
    signed = json.loads(bundle["signing_input"])
    signed["outcome"] = "denied"
    tampered = {**bundle, "signing_input": dumps_text(signed)}
    result = verify_bundle(tampered, keys)
    assert result.status is VerificationStatus.INVALID
    assert result.is_tampered
    other_private = Ed25519PrivateKey.generate()
    other_public = other_private.public_key()
    other_key_b64 = base64.b64encode(
        other_public.public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    other_keys = {"other-key-id": other_key_b64}
    unknown = verify_bundle(bundle, other_keys)
    assert unknown.status is VerificationStatus.UNKNOWN_KEY
    assert not unknown.ok


def test_permit_replay_from_another_demo_wallet_is_denied(judge):
    """Verify a permit cannot authorize another demo wallet."""
    first_permit = judge.issue()
    first_demo = judge._all_demos[0]
    first_wallet_before = judge.snapshot(first_demo)
    judge.issue()
    second_demo = judge._all_demos[1]
    second_wallet_before = judge.snapshot(second_demo)
    response = judge.call(permit_id=first_permit)
    assert response["status"] == 200
    assert response["body"]["error"]["message"] == "permit_wallet_mismatch"
    second_wallet_after = judge.snapshot(second_demo)
    for field in ("balance", "spent", "dispatches", "debits", "refunds"):
        assert second_wallet_after[field] == second_wallet_before[field], field
    first_wallet_after = judge.snapshot(first_demo)
    for field in ("balance", "spent", "dispatches", "debits", "refunds"):
        assert first_wallet_after[field] == first_wallet_before[field], field
