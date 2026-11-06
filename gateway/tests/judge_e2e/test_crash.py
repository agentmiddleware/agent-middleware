"""A real worker kill during dispatch retains charge and prevents resend."""

import signal
from decimal import Decimal

from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle


def test_kill_mid_dispatch_recovers_one_signed_uncertain_receipt(judge):
    judge.issue()
    initial = judge.snapshot()
    # call() returns the checkpoint event while the actual request is blocked
    # inside the executor, after the durable dispatch claim and debit commit.
    assert judge.call(kill=True) == {"event": "dispatch_claimed", "dispatches": 1}
    judge.kill()
    assert judge.process.exitcode == -signal.SIGKILL
    judge.start()
    orphan = judge.snapshot()
    assert orphan["attempts"] == ["dispatch_claimed"]
    assert orphan["debits"] == 1
    assert orphan["refunds"] == 0
    assert orphan["receipts"] == []
    assert orphan["completed"] == [False]

    # Zero idle time is deliberate fault injection after proven process death.
    recovered = judge.rpc("reconcile")
    assert recovered["dispatched_uncertain"] == 1
    assert recovered["failed_attempt_ids"] == ()
    terminal = judge.snapshot()
    assert terminal["attempts"] == ["delivery_uncertain"]
    assert terminal["outcomes"] == ["delivery_uncertain"]
    assert len(terminal["receipts"]) == 1
    assert terminal["completed"] == [True]
    assert terminal["debits"] == 1
    assert terminal["refunds"] == terminal["dispatches"] == 0
    assert terminal["balance"] == orphan["balance"]
    assert Decimal(initial["balance"]) > Decimal(terminal["balance"])

    for _ in range(2):
        replay = judge.call()
        assert replay["status"] == 200
        error = replay["body"]["error"]
        assert error["message"] == "delivery_uncertain"
        receipt = error["data"]["receipt"]
        assert receipt["receipt_id"] == terminal["receipts"][0]
        assert Decimal(str(receipt["credits_charged"])) == Decimal(terminal["spent"])
        assert judge.snapshot() == terminal
    again = judge.rpc("reconcile")
    assert again["dispatched_uncertain"] == 0
    assert again["failed_attempt_ids"] == ()
    assert judge.snapshot() == terminal
    bundle, document = judge.export(terminal["receipts"][0])
    judge.close()
    verified = verify_bundle(bundle, key_set_from_document(document))
    assert verified.ok
    assert verified.claims["outcome"] == "delivery_uncertain"
