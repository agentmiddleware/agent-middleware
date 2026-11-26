"""Pin the exact bytes covered by an Ed25519 receipt resolution signature."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.db.models import ReceiptResolutionModel
from app.services.receipt_resolutions import resolution_signing_input


GOLDEN = (
    Path(__file__).parent / "fixtures" / "resolution_signing_golden" / "genesis.json"
)


def test_resolution_signing_input_is_byte_stable() -> None:
    model = ReceiptResolutionModel(
        resolution_id="rsl_golden_0001",
        receipt_id="rcpt_golden_0001",
        wallet_id="agt-golden-0001",
        sequence=1,
        prev_hash=None,
        receipt_hash="a" * 64,
        outcome="confirmed_success",
        created_at=datetime(2026, 10, 22, 12, 34, 56, tzinfo=timezone.utc),
        payload_hash="",
        resolution_hash="",
        signing_input="",
        signature="",
        signature_key_id="sk_golden_0001",
    )

    signing_input, payload_hash = resolution_signing_input(
        model, model.signature_key_id
    )
    actual = {
        "payload_hash": payload_hash,
        "resolution_hash": hashlib.sha256(signing_input.encode("utf-8")).hexdigest(),
        "signing_input": signing_input,
    }
    expected = json.loads(GOLDEN.read_text())

    assert actual == expected, (
        "resolution signing bytes changed; existing offline resolution proofs "
        "would no longer verify"
    )
