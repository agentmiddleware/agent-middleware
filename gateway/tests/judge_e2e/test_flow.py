"""Demo issuance, bounded authority, dispatch, replay, and offline evidence."""

from decimal import Decimal

from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle


def test_demo_permit_dispatch_replay_and_offline_verification(judge):
    permit_id = judge.issue()
    initial = judge.snapshot()
    assert initial["permit_status"] == "active"
    assert Decimal(initial["spent"]) == 0
    assert initial["attempts"] == initial["receipts"] == initial["completed"] == []
    assert initial["dispatches"] == initial["debits"] == initial["refunds"] == 0

    first = judge.call()
    assert first["status"] == 200
    assert first["body"]["result"]["content"] == [{"type": "text", "text": "hello"}]
    receipt = first["body"]["result"]["receipt"]
    assert receipt["outcome"] == "success"
    assert receipt["permit_id"] == permit_id
    completed = judge.snapshot()
    assert completed["attempts"] == ["succeeded"]
    assert completed["receipts"] == [receipt["receipt_id"]]
    assert completed["outcomes"] == ["success"]
    assert completed["completed"] == [True]
    assert completed["dispatches"] == completed["debits"] == 1
    assert completed["refunds"] == 0
    charge = Decimal(str(receipt["credits_charged"]))
    assert charge > 0
    assert Decimal(completed["spent"]) == charge
    assert Decimal(initial["balance"]) - Decimal(completed["balance"]) == charge

    replay = judge.call()
    assert replay["status"] == 200
    assert replay["body"]["result"]["receipt"] == receipt
    assert judge.snapshot() == completed

    bundle, document = judge.export(receipt["receipt_id"])
    # Close the gateway before invoking the standalone SDK verifier. This
    # verification cannot rely on the server or its private signing material.
    judge.close()
    verified = verify_bundle(bundle, key_set_from_document(document))
    assert verified.ok
    assert verified.receipt_id == receipt["receipt_id"]
    assert verified.claims["outcome"] == "success"
