from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import (
    ControlPlaneAuditEventModel,
    McpDispatchAttemptModel,
    ReceiptModel,
)
from app.main import app
from app.schemas.billing import ServiceCategory
from app.schemas.trust import ReceiptResponse
from app.services.agent_money import get_agent_money
from app.services.audit_log import record_audit_event
from app.services.idempotency import get_idempotency_service
from app.services.mcp_dispatch_attempts import get_mcp_dispatch_attempt_service
from app.services.receipts import get_receipt_service
from app.services.signing_keys import sha256_hex
from app.trust.evidence import _dispatch_evidence, _dispatch_linkage_failures
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def _seed_dispatch_receipt(
    client: AsyncClient,
    *,
    suffix: str,
) -> tuple[dict, ReceiptResponse, McpDispatchAttemptModel, dict]:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"dispatch-evidence-{suffix}"
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        idem_key=f"dispatch-evidence-permit-{suffix}",
    )
    request_payload = {"tool": tool_name, "arguments": {"value": suffix}}
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint="/mcp/messages",
        idempotency_key=f"dispatch-evidence-invoke-{suffix}",
        request_payload=request_payload,
    )
    assert begun.replay is None

    dispatch_service = get_mcp_dispatch_attempt_service()
    attempt = await dispatch_service.prepare(
        idempotency_record_id=begun.record_id,
        wallet_id=provisioned["agent_wallet_id"],
        permit_id=permit["permit_id"],
        key_id=provisioned["key_id"],
        public_tool_id=tool_name,
        upstream_tool_name="partner_lookup",
        upstream_origin="https://partner.example:8443",
        request_hash=begun.request_hash,
        credits_authorized=Decimal("1.5"),
    )
    charge = await get_agent_money().charge(
        wallet_id=provisioned["agent_wallet_id"],
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/mcp/messages",
        operation_key=begun.record_id,
    )
    assert charge is not None
    await dispatch_service.attach_charge(
        attempt_id=attempt.attempt_id,
        ledger_entry_id=charge.entry_id,
        credits_charged=Decimal("1.5"),
    )
    await dispatch_service.claim_dispatch(attempt.attempt_id)
    response_payload = {
        "content": [{"type": "text", "text": "TOP_SECRET_RESULT"}],
        "structuredContent": {"password": "do-not-expose"},
        "isError": False,
    }
    attempt = await dispatch_service.complete(
        attempt_id=attempt.attempt_id,
        state="succeeded",
        result_payload=response_payload,
        error_code=None,
        max_result_bytes=4096,
    )
    audit_event = await record_audit_event(
        event="mcp.invoke",
        wallet_id=provisioned["agent_wallet_id"],
        tool=tool_name,
        endpoint="/mcp/messages",
        auth_source="db",
        key_id=provisioned["key_id"],
        ok=True,
        metadata={
            "permit_id": permit["permit_id"],
            "request_hash": begun.request_hash,
            "ledger_entry_id": charge.entry_id,
            "dispatch_attempt_id": attempt.attempt_id,
            "dispatch_state": attempt.state,
            "upstream_tool_name": attempt.upstream_tool_name,
            "upstream_origin": attempt.upstream_origin,
            "dispatch_response_hash": attempt.response_hash,
        },
    )
    receipt = await get_receipt_service().create_receipt(
        idempotency_record_id=begun.record_id,
        dispatch_attempt_id=attempt.attempt_id,
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool=tool_name,
        request_payload=None,
        request_hash=begun.request_hash,
        response_payload=response_payload,
        ledger_entry_id=charge.entry_id,
        credits_authorized=Decimal("1.5"),
        credits_charged=Decimal("1.5"),
        outcome="success",
        audit_event_id=audit_event.event_id,
    )
    return provisioned, receipt, attempt, response_payload


@pytest.mark.anyio
async def test_dispatch_evidence_is_linked_sanitized_and_in_both_shapes(
    client: AsyncClient,
    clean_database,
) -> None:
    provisioned, receipt, attempt, _response = await _seed_dispatch_receipt(
        client,
        suffix="success",
    )

    detailed_response = await client.get(
        f"/v1/receipts/{receipt.receipt_id}/evidence",
        headers=provisioned["agent_headers"],
    )
    flat_response = await client.get(
        f"/v1/evidence/{receipt.receipt_id}",
        headers=provisioned["agent_headers"],
    )

    assert detailed_response.status_code == 200
    assert flat_response.status_code == 200
    detailed = detailed_response.json()
    flat = flat_response.json()
    checks = {check["name"]: check for check in detailed["checks"]}
    assert detailed["valid"] is True
    assert checks["dispatch_linkage"]["status"] == "passed"
    assert flat["verification"]["dispatch_linkage"] == "ok"
    assert detailed["dispatch"] == flat["dispatch"]
    assert detailed["dispatch"] == {
        "attempt_id": attempt.attempt_id,
        "state": "succeeded",
        "public_tool_id": attempt.public_tool_id,
        "upstream_tool_name": "partner_lookup",
        "upstream_origin": "https://partner.example:8443",
        "request_hash": attempt.request_hash,
        "response_hash": attempt.response_hash,
        "ledger_entry_id": attempt.ledger_entry_id,
        "credits_authorized": "1.50000000",
        "credits_charged": "1.50000000",
        "error_code": None,
        "created_at": detailed["dispatch"]["created_at"],
        "dispatched_at": detailed["dispatch"]["dispatched_at"],
        "completed_at": detailed["dispatch"]["completed_at"],
    }
    serialized = detailed_response.text + flat_response.text
    assert "TOP_SECRET_RESULT" not in serialized
    assert "do-not-expose" not in serialized
    assert "dispatch-evidence-invoke-success" not in serialized


@pytest.mark.anyio
async def test_legacy_receipt_skips_dispatch_linkage_without_invalidating_evidence(
    client: AsyncClient,
    clean_database,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "legacy-dispatch-evidence"
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        idem_key="legacy-dispatch-evidence-permit",
    )
    request_payload = {"tool": tool_name, "arguments": {}}
    charge = await get_agent_money().charge(
        wallet_id=provisioned["agent_wallet_id"],
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/mcp/messages",
    )
    assert charge is not None
    audit_event = await record_audit_event(
        event="mcp.invoke",
        wallet_id=provisioned["agent_wallet_id"],
        tool=tool_name,
        endpoint="/mcp/messages",
        auth_source="db",
        key_id=provisioned["key_id"],
        ok=True,
        metadata={
            "permit_id": permit["permit_id"],
            "request_hash": sha256_hex(request_payload),
            "ledger_entry_id": charge.entry_id,
        },
    )
    receipt = await get_receipt_service().create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool=tool_name,
        request_payload=request_payload,
        response_payload={"ok": True},
        ledger_entry_id=charge.entry_id,
        credits_authorized=Decimal("1.5"),
        credits_charged=Decimal("1.5"),
        outcome="success",
        audit_event_id=audit_event.event_id,
    )

    response = await client.get(
        f"/v1/evidence/{receipt.receipt_id}",
        headers=provisioned["agent_headers"],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is True
    assert payload["dispatch"] is None
    assert payload["verification"]["dispatch_linkage"] == "skipped"


@pytest.mark.anyio
async def test_dispatch_evidence_does_not_expose_cross_wallet_attempt(
    client: AsyncClient,
    clean_database,
) -> None:
    owner, receipt, _owner_attempt, _response = await _seed_dispatch_receipt(
        client,
        suffix="owner",
    )
    other = await provision_agent_wallet(client)
    other_tool = "dispatch-evidence-other"
    other_permit = await create_tool_permit(
        client,
        wallet_id=other["agent_wallet_id"],
        key_id=other["key_id"],
        tool_name=other_tool,
        idem_key="dispatch-evidence-permit-other",
    )
    other_request = {"tool": other_tool, "arguments": {}}
    other_begun = await get_idempotency_service().begin_with_record(
        wallet_id=other["agent_wallet_id"],
        endpoint="/mcp/messages",
        idempotency_key="other-wallet-secret-idempotency-key",
        request_payload=other_request,
    )
    dispatch_service = get_mcp_dispatch_attempt_service()
    other_attempt = await dispatch_service.prepare(
        idempotency_record_id=other_begun.record_id,
        wallet_id=other["agent_wallet_id"],
        permit_id=other_permit["permit_id"],
        key_id=other["key_id"],
        public_tool_id=other_tool,
        upstream_tool_name="secret_partner_tool",
        upstream_origin="https://other-partner.example",
        request_hash=other_begun.request_hash,
        credits_authorized=Decimal("1.5"),
    )
    other_attempt = await dispatch_service.complete_pre_dispatch_failure(
        attempt_id=other_attempt.attempt_id,
        expected_updated_at=other_attempt.updated_at,
        result_payload={"content": [{"text": "OTHER_WALLET_SECRET"}]},
        error_code="connect_failed",
        max_result_bytes=4096,
    )

    factory = get_session_factory()
    async with factory() as session:
        receipt_model = await session.get(ReceiptModel, receipt.receipt_id)
        assert receipt_model is not None
        receipt_model.dispatch_attempt_id = other_attempt.attempt_id
        session.add(receipt_model)
        await session.commit()

    response = await client.get(
        f"/v1/receipts/{receipt.receipt_id}/evidence",
        headers=owner["agent_headers"],
    )

    assert response.status_code == 200
    payload = response.json()
    checks = {check["name"]: check for check in payload["checks"]}
    assert payload["valid"] is False
    assert payload["dispatch"] is None
    assert checks["dispatch_linkage"]["reason"] == "dispatch_attempt_not_found"
    assert "audit_dispatch_attempt_mismatch" in checks["audit_event_linkage"][
        "reason"
    ].split(";")
    assert "secret_partner_tool" not in response.text
    assert "OTHER_WALLET_SECRET" not in response.text
    assert "other-wallet-secret-idempotency-key" not in response.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("metadata_field", "tampered_value", "expected_failure"),
    [
        (
            "upstream_origin",
            "https://tampered.example",
            "audit_upstream_origin_mismatch",
        ),
        (
            "upstream_tool_name",
            "tampered_partner_tool",
            "audit_upstream_tool_mismatch",
        ),
        (
            "dispatch_state",
            "delivery_uncertain",
            "audit_dispatch_state_mismatch",
        ),
        (
            "dispatch_response_hash",
            "f" * 64,
            "audit_dispatch_response_hash_mismatch",
        ),
    ],
)
async def test_dispatch_evidence_rejects_tampered_signed_audit_metadata(
    client: AsyncClient,
    clean_database,
    metadata_field: str,
    tampered_value: str,
    expected_failure: str,
) -> None:
    provisioned, receipt, _attempt, _response = await _seed_dispatch_receipt(
        client,
        suffix=f"audit-tamper-{metadata_field}",
    )
    assert receipt.audit_event_id is not None

    factory = get_session_factory()
    async with factory() as session:
        audit_event = await session.get(
            ControlPlaneAuditEventModel,
            receipt.audit_event_id,
        )
        assert audit_event is not None
        metadata = json.loads(audit_event.metadata_json or "{}")
        metadata[metadata_field] = tampered_value
        audit_event.metadata_json = json.dumps(metadata, sort_keys=True)
        session.add(audit_event)
        await session.commit()

    response = await client.get(
        f"/v1/receipts/{receipt.receipt_id}/evidence",
        headers=provisioned["agent_headers"],
    )

    assert response.status_code == 200
    payload = response.json()
    checks = {check["name"]: check for check in payload["checks"]}
    assert payload["valid"] is False
    assert expected_failure in checks["audit_event_linkage"]["reason"].split(";")


def _linked_models() -> tuple[McpDispatchAttemptModel, ReceiptResponse]:
    now = datetime(2026, 1, 1)
    attempt = McpDispatchAttemptModel(
        attempt_id="dsp-test",
        idempotency_record_id="idm-test",
        wallet_id="wallet-test",
        permit_id="permit-test",
        key_id="key-test",
        public_tool_id="public-tool",
        upstream_tool_name="upstream_tool",
        upstream_origin="https://partner.example",
        request_hash="a" * 64,
        ledger_entry_id="txn-test",
        credits_authorized=Decimal("1.5"),
        credits_charged=Decimal("1.5"),
        state="succeeded",
        response_hash="b" * 64,
        created_at=now,
        updated_at=now,
        dispatched_at=now,
        completed_at=now,
    )
    receipt = ReceiptResponse(
        receipt_id="rcpt-test",
        idempotency_record_id="idm-test",
        dispatch_attempt_id="dsp-test",
        permit_id="permit-test",
        wallet_id="wallet-test",
        key_id="key-test",
        tool="public-tool",
        request_hash="a" * 64,
        response_hash="b" * 64,
        ledger_entry_id="txn-test",
        credits_authorized=Decimal("1.5"),
        credits_charged=Decimal("1.5"),
        outcome="success",
        audit_event_id="audit-test",
        created_at=now,
        signature="signature",
        signature_key_id="signing-key",
    )
    return attempt, receipt


@pytest.mark.parametrize(
    ("attempt_field", "changed_value", "expected_failure"),
    [
        ("wallet_id", "wallet-other", "dispatch_wallet_mismatch"),
        ("permit_id", "permit-other", "dispatch_permit_mismatch"),
        ("key_id", "key-other", "dispatch_key_mismatch"),
        ("public_tool_id", "other-tool", "dispatch_tool_mismatch"),
        ("request_hash", "c" * 64, "dispatch_request_hash_mismatch"),
        ("ledger_entry_id", "txn-other", "dispatch_ledger_entry_mismatch"),
        ("response_hash", "d" * 64, "dispatch_response_hash_mismatch"),
        ("idempotency_record_id", "idm-other", "dispatch_idempotency_mismatch"),
        (
            "credits_authorized",
            Decimal("2"),
            "dispatch_authorized_credits_mismatch",
        ),
        ("credits_charged", Decimal("2"), "dispatch_charged_credits_mismatch"),
        ("state", "prepared", "dispatch_state_not_terminal"),
        ("state", "delivery_uncertain", "dispatch_outcome_mismatch"),
    ],
)
def test_dispatch_linkage_rejects_mismatched_invariants(
    attempt_field: str,
    changed_value: object,
    expected_failure: str,
) -> None:
    attempt, receipt = _linked_models()
    setattr(attempt, attempt_field, changed_value)

    failures = _dispatch_linkage_failures(attempt=attempt, receipt=receipt)

    assert expected_failure in failures


def test_returned_error_accepts_refunded_and_exceptional_unrefunded_outcomes() -> None:
    attempt, receipt = _linked_models()
    attempt.state = "returned_error"
    receipt.outcome = "failed_refunded"
    receipt.credits_charged = Decimal("0")
    assert _dispatch_linkage_failures(attempt=attempt, receipt=receipt) == []

    receipt.outcome = "failed_unrefunded"
    receipt.credits_charged = attempt.credits_charged
    assert _dispatch_linkage_failures(attempt=attempt, receipt=receipt) == []


def test_dispatch_projection_strips_url_secrets_paths_and_unsafe_error_text() -> None:
    attempt, _receipt = _linked_models()
    attempt.upstream_origin = (
        "https://bearer-token:password@partner.example:8443/private?token=secret"
    )
    attempt.error_code = "Bearer super-secret-token"

    evidence = _dispatch_evidence(attempt).model_dump(mode="json")

    assert evidence["upstream_origin"] == "https://partner.example:8443"
    assert evidence["error_code"] == "upstream_error"
    assert "bearer-token" not in str(evidence)
    assert "super-secret-token" not in str(evidence)
    assert "/private" not in str(evidence)
