"""Golden snapshot of the exact bytes a receipt signature covers.

Refactor gate. The receipt signing input is the product's audit contract:
any change to the field set, key ordering, Decimal/datetime normalization,
or the payload_hash derivation changes what an offline verifier must
reconstruct, and silently invalidates every receipt already issued.

This test freezes five representative receipts to byte-exact canonical
JSON, and freezes the Ed25519 signature over those bytes under a fixed
test seed (Ed25519 is deterministic, so the signature is a second pin on
the same bytes).

If this test fails, the change is either a bug or an intentional receipt
format change. An intentional change requires: (1) a receipt schema version
bump, (2) an entry in CHANGELOG.md, (3) regenerating the golden files with
``UPDATE_RECEIPT_GOLDEN=1 pytest tests/test_receipt_signing_input_snapshot.py``.
Never regenerate to make a refactor PR green.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, ReceiptModel
from app.main import app
from app.services.receipts import ReceiptService, get_receipt_service
from app.services.signing_keys import (
    canonical_json,
    get_signing_key_service,
    sha256_hex,
)
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "receipt_signing_golden"
# Fixed 32-byte seed; NOT a production key. Exists only to make the
# signature pin deterministic.
_TEST_SEED = bytes(range(32))
_FIXED_CREATED_AT = datetime(2026, 9, 14, 17, 0, 0, tzinfo=timezone.utc)


def _base_model(**overrides) -> ReceiptModel:
    fields = dict(
        receipt_id="rcpt_golden_0001",
        idempotency_record_id="idem_golden_0001",
        dispatch_attempt_id="disp_golden_0001",
        permit_id="pmt_golden_0001",
        wallet_id="agt-golden-0001",
        key_id="key_golden_0001",
        tool="golden-path-echo",
        request_hash="a" * 64,
        response_hash="b" * 64,
        ledger_entry_id="led_golden_0001",
        credits_authorized=Decimal("2.50000000"),
        credits_charged=Decimal("2.5"),
        outcome="success",
        reason_code=None,
        audit_event_id="aud_golden_0001",
        approval_id=None,
        constraints_evaluated_json=None,
        created_at=_FIXED_CREATED_AT,
        signature="",
        signature_key_id="sk_golden_0001",
    )
    fields.update(overrides)
    return ReceiptModel(**fields)


CASES = {
    # Additive action v1 fields pin new receipt bytes without regenerating any
    # historical fixture. The existing idempotency link signs the owner.
    "success_action_v1": (
        _base_model(
            action_contract_version=1,
            action_payload_hash="c" * 64,
            action_schema_id="fixture-payment",
            action_schema_version="1",
            action_public_tool_id="golden-path-echo",
            action_upstream_binding_hash="d" * 64,
        ),
        True,
    ),
    # Current-format success receipt with full governed-dispatch linkage.
    "success_linked": (_base_model(), True),
    # Legacy-format signature: linkage fields excluded from signing input.
    "success_legacy": (_base_model(), False),
    # Current-format success with a human-approval id. approval_id is signed
    # whenever set, on both current and legacy paths; omitting it from the
    # goldens would let a refactor drop the field while this suite stayed green.
    "success_approval": (
        _base_model(
            receipt_id="rcpt_golden_0003",
            approval_id="apr_golden_0001",
        ),
        True,
    ),
    # Same approval field on a linkage-stripped (legacy) signature. approval_id
    # is not gated on include_linkage; a backfilled 023 receipt that later
    # gained an idempotency row must still export the approval id.
    "success_legacy_approval": (
        _base_model(
            receipt_id="rcpt_golden_0004",
            approval_id="apr_golden_0001",
        ),
        False,
    ),
    # Signed denial with reason_code and permit-v2 constraints_evaluated.
    "denial_constraints": (
        _base_model(
            receipt_id="rcpt_golden_0002",
            response_hash=None,
            ledger_entry_id=None,
            credits_charged=Decimal("0"),
            outcome="denied",
            reason_code="permit_budget_exceeded",
            constraints_evaluated_json=json.dumps(
                {"max_credits": "2.5", "allowed_tools": ["golden-path-echo"]}
            ),
        ),
        True,
    ),
}


def _signing_input(name: str) -> str:
    model, include_linkage = CASES[name]
    payload = ReceiptService._verification_payload(
        model, include_linkage=include_linkage
    )
    return canonical_json(payload)


def _signature_b64(signing_input: str) -> str:
    key = Ed25519PrivateKey.from_private_bytes(_TEST_SEED)
    return base64.b64encode(key.sign(signing_input.encode())).decode()


@pytest.mark.parametrize("name", sorted(CASES))
def test_receipt_signing_input_is_byte_stable(name: str) -> None:
    signing_input = _signing_input(name)
    signature = _signature_b64(signing_input)
    golden_path = GOLDEN_DIR / f"{name}.json"
    actual = {
        "signing_input": signing_input,
        "signing_input_sha256": sha256_hex(signing_input),
        "signature_b64_over_test_seed": signature,
    }

    if os.environ.get("UPDATE_RECEIPT_GOLDEN") == "1":
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"golden regenerated: {golden_path}")

    assert golden_path.exists(), (
        f"missing golden {golden_path}; run with UPDATE_RECEIPT_GOLDEN=1 once"
    )
    expected = json.loads(golden_path.read_text())
    assert actual["signing_input"] == expected["signing_input"], (
        "receipt signing input changed — this invalidates offline verification "
        "of every issued receipt. See module docstring before regenerating."
    )
    assert actual == expected


def test_golden_fixtures_match_cases() -> None:
    """Every golden file must have a CASES entry, and every case a file.

    The #443 squash onto main kept the approval JSON files and dropped the
    test cases that read them. This assertion fails that class of merge.
    """
    fixture_names = {path.stem for path in GOLDEN_DIR.glob("*.json")}
    assert fixture_names == set(CASES)


def test_payload_hash_is_over_payload_without_itself() -> None:
    """payload_hash must be the hash of the payload *before* it was added."""
    model, _ = CASES["success_linked"]
    payload = ReceiptService._verification_payload(model, include_linkage=True)
    stripped = {k: v for k, v in payload.items() if k != "payload_hash"}
    assert payload["payload_hash"] == sha256_hex(stripped)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_signing_input_for_model_covers_approval_id(
    client: AsyncClient,
    clean_database,
) -> None:
    """Production export must include approval_id when the model has one."""
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="golden-approval-echo",
        idem_key="permit-golden-approval",
    )
    receipt = await get_receipt_service().create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="golden-approval-echo",
        request_payload={"value": "request"},
        response_payload={"value": "response"},
        ledger_entry_id=None,
        credits_authorized=Decimal("2"),
        credits_charged=Decimal("0"),
        outcome="denied",
        reason_code="human_approval_required",
        audit_event_id=None,
        approval_id="apr_golden_0001",
    )
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(ReceiptModel, receipt.receipt_id)
        assert model is not None
        signing_input = await ReceiptService().signing_input_for_model(
            model, session=session
        )
        assert signing_input is not None
        expected = canonical_json(
            ReceiptService._verification_payload(model, include_linkage=True)
        )
        assert signing_input == expected
        assert json.loads(signing_input)["approval_id"] == "apr_golden_0001"


@pytest.mark.anyio
async def test_signing_input_for_model_uses_legacy_payload_when_current_fails(
    client: AsyncClient,
    clean_database,
) -> None:
    """A pre-linkage signature must export the bytes that actually verify."""
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="golden-legacy-echo",
        idem_key="permit-golden-legacy",
    )
    request_hash = "a" * 64
    idempotency_record_id = "idm_golden_legacy_0001"
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            IdempotencyRecordModel(
                record_id=idempotency_record_id,
                wallet_id=provisioned["agent_wallet_id"],
                endpoint="/mcp/invoke",
                idempotency_key="golden-legacy-key",
                request_hash=request_hash,
            )
        )
        await session.commit()

    receipt = await get_receipt_service().create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="golden-legacy-echo",
        request_payload=None,
        response_payload={"value": "response"},
        ledger_entry_id=None,
        credits_authorized=Decimal("2"),
        credits_charged=Decimal("0"),
        outcome="denied",
        reason_code="permit_budget_exceeded",
        audit_event_id=None,
        idempotency_record_id=idempotency_record_id,
        dispatch_attempt_id=None,
        request_hash=request_hash,
        approval_id="apr_golden_legacy_0001",
    )

    signing_keys = get_signing_key_service()
    async with factory() as session:
        model = await session.get(ReceiptModel, receipt.receipt_id)
        assert model is not None
        idempotency = await session.get(IdempotencyRecordModel, idempotency_record_id)
        assert idempotency is not None
        idempotency.response_reference = model.receipt_id
        session.add(idempotency)

        unsigned_legacy = {
            k: v
            for k, v in ReceiptService._verification_payload(
                model, include_linkage=False
            ).items()
            if k not in {"alg", "kid", "payload_hash"}
        }
        signature, key_id, _payload_hash = await signing_keys.sign_payload(
            unsigned_legacy
        )
        model.signature = signature
        model.signature_key_id = key_id
        session.add(model)
        await session.commit()
        await session.refresh(model)

        current_payload = ReceiptService._verification_payload(
            model, include_linkage=True
        )
        assert not await signing_keys.verify_payload(
            current_payload,
            signature=model.signature,
            key_id=model.signature_key_id,
            session=session,
        )
        signing_input = await ReceiptService().signing_input_for_model(
            model, session=session
        )
        expected = canonical_json(
            ReceiptService._verification_payload(model, include_linkage=False)
        )
        assert signing_input == expected
        parsed = json.loads(signing_input)
        assert "idempotency_record_id" not in parsed
        assert parsed["approval_id"] == "apr_golden_legacy_0001"
