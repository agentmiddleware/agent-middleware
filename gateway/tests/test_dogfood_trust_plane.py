"""Dogfood playground: partner.notes.write behind the trust plane."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dogfood_trust_plane_writes_one_note_and_holds_loop():
    result = subprocess.run(
        [sys.executable, "scripts/dogfood_trust_plane.py", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["allowed_tool"] == "partner.notes.write"
    assert proof["note_count"] == 1
    assert proof["notes_on_disk"][0]["text"] == "dogfood: first governed note"
    assert proof["success_receipt_id"].startswith("rcpt-")
    assert proof["replay_receipt_id"] == proof["success_receipt_id"]
    assert proof["denial_reason"] == "permit_tool_not_allowed"
    assert proof["ungoverned_denial_reason"] == "permit_required"
