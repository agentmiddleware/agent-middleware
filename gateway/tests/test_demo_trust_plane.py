from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_demo_trust_plane_script_proves_core_loop():
    result = subprocess.run(
        [sys.executable, "scripts/demo_trust_plane.py", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        # The demo now also times a hundred fresh governed calls, each one a
        # real reservation, dispatch, debit and receipt. That is a few
        # seconds of headroom on a warm machine and rather more on a cold CI
        # runner, so the ceiling is generous on purpose: a slow box should
        # not read as a broken proof.
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["permit_id"].startswith("permit-")
    assert proof["success_receipt_id"].startswith("rcpt-")
    assert proof["signing_key_id"] == "demo-ed25519"
    assert proof["inspected_receipts"] == 1
    assert proof["inspected_audit_events"] >= 1
    assert proof["replay_receipt_id"] == proof["success_receipt_id"]
    assert proof["denial_receipt_id"].startswith("rcpt-")
    assert proof["denial_replay_receipt_id"] == proof["denial_receipt_id"]
    assert proof["denial_reason"] == "permit_tool_not_allowed"
    assert proof["ungoverned_denial_reason"] == "permit_required"
    assert proof["cross_wallet_status"] == 403
    assert proof["audit_chain_checked_events"] >= 1

    # The public site publishes what the boundary costs in time, and it may
    # only publish a number this demo actually measured. A sample of one is
    # not a p95, so the count is pinned too.
    latency = proof["gateway_latency"]
    assert latency["samples"] == 100
    assert latency["path"] == "POST /mcp/messages"
    assert latency["tool"] == "trust-plane-echo"
    assert latency["transport"]
    assert 0 < latency["p50_ms"] <= latency["p95_ms"]
    assert latency["min_ms"] <= latency["p50_ms"]
    assert latency["p95_ms"] <= latency["max_ms"]
