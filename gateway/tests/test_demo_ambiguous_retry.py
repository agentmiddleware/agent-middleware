"""The ambiguous-retry demo is the repository's headline claim, so CI runs it.

A demo that drifts from the code is worse than no demo: it is a claim nobody
re-checked. These assertions pin the numbers the transcript prints, not the
prose around them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_demo() -> dict:
    result = subprocess.run(
        [sys.executable, "scripts/demo_ambiguous_retry.py", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_retry_without_the_boundary_pays_twice():
    """The problem the product exists for has to stay real in the demo.

    If this ever drops to one payout, the contrast the transcript draws is
    fiction and the demo is lying about the baseline.
    """
    proof = _run_demo()

    assert proof["ungoverned_payouts"] == 2
    assert proof["ungoverned_total_usd"] == "500.00"


def test_retry_through_the_boundary_pays_once_and_debits_once():
    proof = _run_demo()

    # The tool body ran exactly once: this counts executions, not intentions.
    assert proof["governed_payouts"] == 1
    assert proof["governed_total_usd"] == "250.00"
    assert proof["governed_debits"] == 1

    # The retry returned the stored outcome rather than making a new one.
    assert proof["replay_receipt_id"] == proof["receipt_id"]
    assert proof["replay_confirmation"] == proof["payout_confirmation"]
    assert proof["payout_confirmation"] == "PAY-0001"


def test_spent_key_refuses_a_different_payout():
    proof = _run_demo()

    assert proof["conflict_reason"] == "idempotency_key_reused"
    # The refusal must not have moved money or credits either.
    assert proof["governed_payouts"] == 1
    assert proof["governed_debits"] == 1


def test_receipt_verifies_offline_and_rejects_an_edited_copy():
    proof = _run_demo()

    assert proof["offline_verified"] is True
    assert proof["offline_signing_key_id"] == "demo-ed25519"
    assert proof["offline_forgery_detected"] is True
