from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_REASONS = [
    "permit_required",
    "permit_not_found",
    "permit_tool_not_allowed",
    "permit_scope_missing",
    "permit_budget_exceeded",
    "permit_wallet_mismatch",
    "permit_key_mismatch",
    "permit_expired",
    "permit_revoked",
    "permit_signature_invalid",
]


def test_red_team_trust_plane_denies_every_attack():
    result = subprocess.run(
        [sys.executable, "scripts/red_team_trust_plane.py", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)

    # Every attack is denied with a concrete, specific reason.
    assert proof["attacks_denied"] == 10
    assert proof["denial_reasons"] == EXPECTED_REASONS

    # Eight of the ten attacks reach the permit-validation path that emits a
    # signed denial receipt (no_permit and unknown_permit do not — there is no
    # permit model to bind a receipt to). Every emitted receipt must already
    # have been asserted to have outcome=denied and ledger_entry_id=None by the
    # script; this fixes the count so a regression that stopped emitting them
    # would break the test.
    assert proof["attacks_with_signed_denial_receipts"] == 8

    # No denied attack ever touches the ledger on either wallet, for either
    # the allowed or the blocked demo tool.
    assert proof["victim_debits_after_attacks"] == 0
    assert proof["attacker_debits_after_attacks"] == 0

    # The one valid call still works and charges exactly once.
    assert proof["success_receipt_id"].startswith("rcpt-")
    assert proof["ledger_debits_total"] == 1

    # The audit chain survives the siege.
    assert proof["audit_chain_valid"] is True
    assert proof["audit_chain_checked_events"] >= 1
