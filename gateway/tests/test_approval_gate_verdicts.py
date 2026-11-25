"""Pinned external verdict shape and existing AMW receipt compatibility."""

import hashlib
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import ReceiptModel
from app.main import app
from app.services.receipts import get_receipt_service
from tests.fixtures.approval_gate_verdicts.check import canonical_call, check
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

FIXTURES = Path(__file__).parent / "fixtures" / "approval_gate_verdicts"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("name", "blob_id", "decision", "verdict"),
    [
        (
            "approve.json",
            "5818baee45ca810b8a406211f570ce6b46989d47",
            "ALLOW",
            "approve",
        ),
        ("reject.json", "abee997212b3a660975afd3acf7b600b82eacde8", "BLOCK", "reject"),
    ],
)
def test_pinned_verdict_pair(name, blob_id, decision, verdict):
    # Upstream omits the final newline; only that formatting byte is added here.
    raw = (FIXTURES / name).read_bytes().removesuffix(b"\n")
    assert (
        hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        == blob_id
    )
    fixture = load_fixture(name)
    call = fixture["artifact_canonical"]
    assert canonical_call(json.loads(call)) == call
    assert hashlib.sha256(call.encode()).hexdigest() == fixture["artifact_sha256"]
    result = check(fixture, call)
    assert result.decision == decision
    assert result.reasons == (f"verdict={verdict}",)
    assert result.receipt_digest == fixture["proof"]["event"]["id"]
    retry = check(fixture, call)
    assert retry.decision == result.decision
    assert retry.receipt_digest == result.receipt_digest


@pytest.mark.parametrize("field", ["amount", "idempotency_key", "tool"])
def test_approve_is_bound_to_exact_call(field):
    fixture = load_fixture("approve.json")
    call = json.loads(fixture["artifact_canonical"])
    call[field] = call[field] + 1 if field == "amount" else call[field] + ":changed"
    result = check(fixture, canonical_call(call))
    assert result.decision == "BLOCK"
    assert result.reasons == (
        "verdict is not bound to this call (artifact_hash mismatch)",
    )
    assert result.receipt_digest is None


def test_approve_cannot_authorize_retry_with_prior_refund():
    approve = load_fixture("approve.json")
    reject = load_fixture("reject.json")
    assert check(approve, reject["artifact_canonical"]).decision == "BLOCK"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("sig", "00" * 64, "BIP-340 signature does not verify"),
        ("id", "00" * 32, "event id does not recompute"),
        ("pubkey", "00" * 32, "signed by a key the checker does not pin"),
    ],
)
def test_tampered_event_blocks(field, value, reason):
    fixture = load_fixture("approve.json")
    fixture["proof"]["event"][field] = value
    result = check(fixture, fixture["artifact_canonical"])
    assert result.decision == "BLOCK"
    assert reason in result.reasons
    assert result.receipt_digest is None


def test_signed_content_cannot_be_changed_to_approve():
    fixture = load_fixture("reject.json")
    event = fixture["proof"]["event"]
    event["content"] = event["content"].replace(
        '"verdict":"reject"', '"verdict":"approve"'
    )
    result = check(fixture, fixture["artifact_canonical"])
    assert result.decision == "BLOCK"
    assert "event id does not recompute" in result.reasons
    assert result.receipt_digest is None


def test_unsigned_metadata_cannot_override_signed_reject():
    fixture = load_fixture("reject.json")
    fixture["proof"]["proof_payload"]["verdict"] = "approve"
    fixture["artifact_sha256"] = load_fixture("approve.json")["artifact_sha256"]
    result = check(fixture, fixture["artifact_canonical"])
    assert result.decision == "BLOCK"
    assert result.reasons == ("verdict=reject",)
    assert result.receipt_digest == fixture["proof"]["event"]["id"]


@pytest.mark.parametrize("fixture", [None, [], {}, {"proof": {"event": None}}])
def test_missing_proof_blocks(fixture):
    result = check(fixture, load_fixture("approve.json")["artifact_canonical"])
    assert result.decision == "BLOCK"
    assert result.receipt_digest is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sig", "not-hex"),
        ("kind", "30078"),
        ("kind", 1),
        ("tags", None),
        ("content", "[]"),
        ("content", "{"),
    ],
)
def test_malformed_event_blocks(field, value):
    fixture = load_fixture("approve.json")
    fixture["proof"]["event"][field] = value
    result = check(fixture, fixture["artifact_canonical"])
    assert result.decision == "BLOCK"
    assert result.reasons == ("malformed fixture or call",)
    assert result.receipt_digest is None


def test_cli_offline_pair_and_call_override(tmp_path):
    command = [sys.executable, "-E", "-S", str(FIXTURES / "check.py")]
    # No site packages or Python env overrides; no gateway dependencies needed.
    pair = subprocess.run(
        [*command, str(FIXTURES / "approve.json"), str(FIXTURES / "reject.json")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert pair.returncode == 1
    assert "approve.json: ALLOW (verdict=approve)" in pair.stdout
    assert "reject.json: BLOCK (verdict=reject)" in pair.stdout
    call = json.loads(load_fixture("approve.json")["artifact_canonical"])
    path = tmp_path / "call.json"
    for amount, decision, exit_code in [
        (call["amount"], "ALLOW", 0),
        (call["amount"] + 1, "BLOCK", 1),
    ]:
        call["amount"] = amount
        path.write_text(json.dumps(call), encoding="utf-8")
        result = subprocess.run(
            [*command, str(FIXTURES / "approve.json"), "--call", str(path)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == exit_code
        assert f"approve.json: {decision}" in result.stdout
    path.write_text("{", encoding="utf-8")
    malformed = subprocess.run(
        [*command, str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed.returncode == 1
    assert (
        "BLOCK (cannot read fixture or call) receipt digest = unavailable"
        in malformed.stdout
    )


@pytest.mark.anyio
@pytest.mark.usefixtures("clean_database")
async def test_reject_digest_is_signed_in_real_denial_receipt():
    fixture = load_fixture("reject.json")
    call = json.loads(fixture["artifact_canonical"])
    result = check(fixture, fixture["artifact_canonical"])
    assert result.decision == "BLOCK"
    assert result.receipt_digest is not None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        provisioned = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=call["tool"],
        )
        service = get_receipt_service()
        receipt = await service.create_receipt(
            permit_id=permit["permit_id"],
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool=call["tool"],
            request_payload=call,
            response_payload=None,
            ledger_entry_id=None,
            credits_authorized=Decimal(str(permit["max_credits"])),
            credits_charged=Decimal("0"),
            outcome="denied",
            reason_code="human_approval_rejected",
            audit_event_id=None,
            approval_digest=result.receipt_digest,
        )
        assert receipt.approval_digest == result.receipt_digest
        assert receipt.credits_charged == Decimal("0")
        signing_input = await service.signing_input(receipt.receipt_id)
        assert signing_input is not None
        assert json.loads(signing_input)["approval_digest"] == result.receipt_digest
        response = await client.post(
            "/v1/receipts/verify",
            json={"receipt_id": receipt.receipt_id},
            headers=provisioned["agent_headers"],
        )
        assert response.status_code == 200
        assert response.json()["valid"] is True
        async with get_session_factory()() as session:
            model = await session.get(ReceiptModel, receipt.receipt_id)
            assert model is not None
            model.approval_digest = load_fixture("approve.json")["proof"]["event"]["id"]
            await session.commit()
        response = await client.post(
            "/v1/receipts/verify",
            json={"receipt_id": receipt.receipt_id},
            headers=provisioned["agent_headers"],
        )
        assert response.status_code == 200
        assert response.json()["valid"] is False
        assert response.json()["reason"] == "receipt_signature_invalid"
