"""Authority and evidence attacks against runtime-issued demo authority."""

import json

import pytest

from b2a_sdk.receipt_verifier import (
    VerificationStatus,
    key_set_from_document,
    verify_bundle,
)


def test_conflicting_payload_cannot_reuse_a_completed_key(judge):
    judge.issue()
    first = judge.call()
    assert first["status"] == 200
    receipt_id = first["body"]["result"]["receipt"]["receipt_id"]
    before = judge.snapshot()
    conflict = judge.call(message="conflicting input")
    assert conflict["status"] == 200
    assert conflict["body"]["error"]["message"] == "idempotency_key_reused"
    after = judge.snapshot()
    assert after["dispatches"] == after["debits"] == 1
    assert after["balance"] == before["balance"]
    assert after["spent"] == before["spent"]
    assert judge.call()["body"]["result"]["receipt"]["receipt_id"] == receipt_id


@pytest.mark.parametrize("attack", ["anonymous", "tool_scope", "budget"])
def test_guardrails_deny_before_dispatch_or_money(judge, attack):
    judge.issue()
    if attack == "budget":
        # Spend the issued cap using genuine product-generated operations.
        # Stop at the first denial rather than inventing a balance or permit.
        for index in range(20):
            before = judge.snapshot()
            response = judge.call(key=f"budget-{index}")
            if "error" in response["body"]:
                break
        else:
            pytest.fail("demo permit failed to bound spend within 20 calls")
        assert response["body"]["error"]["message"] == "permit_budget_exceeded"
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
    judge.issue()
    first = judge.call()
    receipt_id = first["body"]["result"]["receipt"]["receipt_id"]
    bundle, document = judge.export(receipt_id)
    judge.close()
    keys = key_set_from_document(document)
    assert verify_bundle(bundle, keys).ok
    signed = json.loads(bundle["signing_input"])
    signed["outcome"] = "denied"
    tampered = {**bundle, "signing_input": json.dumps(signed, sort_keys=True)}
    result = verify_bundle(tampered, keys)
    assert result.status is VerificationStatus.INVALID
    assert result.is_tampered
    unknown = verify_bundle(bundle, {})
    assert unknown.status is VerificationStatus.UNKNOWN_KEY
    assert not unknown.ok


def test_permit_replay_from_another_demo_wallet_is_denied(judge):
    first_permit = judge.issue()
    judge.issue()
    before = judge.snapshot()
    response = judge.call(permit_id=first_permit)
    assert response["status"] == 200
    assert response["body"]["error"]["message"] == "permit_wallet_mismatch"
    after = judge.snapshot()
    for field in ("balance", "spent", "dispatches", "debits", "refunds"):
        assert after[field] == before[field], field
