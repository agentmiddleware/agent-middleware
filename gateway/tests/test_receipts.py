from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, ReceiptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.idempotency import get_idempotency_service
from app.services.receipts import ReceiptError, get_receipt_service
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_receipt_signature_verifies_and_detects_tampering(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="receipt-tool",
    )
    receipt = await get_receipt_service().create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="receipt-tool",
        request_payload={"message": "before"},
        response_payload={"message": "after"},
        ledger_entry_id=None,
        credits_authorized=Decimal("2"),
        credits_charged=Decimal("0"),
        outcome="denied",
        audit_event_id=None,
        reason_code="permit_forbidden_field:secret_token",
    )
    assert receipt.reason_code == "permit_forbidden_field:secret_token"
    signing_input = await get_receipt_service().signing_input(receipt.receipt_id)
    assert signing_input is not None
    assert json.loads(signing_input)["reason_code"] == (
        "permit_forbidden_field:secret_token"
    )

    verify_resp = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt.receipt_id},
        headers=provisioned["agent_headers"],
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["valid"] is True

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ReceiptModel).where(ReceiptModel.receipt_id == receipt.receipt_id)
        )
        model = result.scalar_one()
        model.reason_code = "permit_forbidden_field:different_field"
        session.add(model)
        await session.commit()

    tampered_resp = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt.receipt_id},
        headers=provisioned["agent_headers"],
    )
    assert tampered_resp.status_code == 200
    assert tampered_resp.json()["valid"] is False
    assert tampered_resp.json()["reason"] == "receipt_signature_invalid"


@pytest.mark.anyio
async def test_reason_code_is_optional_for_success_and_legacy_receipts(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="receipt-optional-reason-tool",
        idem_key="receipt-optional-reason-permit",
    )
    service = get_receipt_service()

    success = await service.create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="receipt-optional-reason-tool",
        request_payload={"attempt": "success"},
        response_payload={"ok": True},
        ledger_entry_id=None,
        credits_authorized=Decimal("1"),
        credits_charged=Decimal("1"),
        outcome="success",
        audit_event_id=None,
    )
    legacy_denial = await service.create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="receipt-optional-reason-tool",
        request_payload={"attempt": "legacy-denial"},
        response_payload={"error": "denied"},
        ledger_entry_id=None,
        credits_authorized=Decimal("1"),
        credits_charged=Decimal("0"),
        outcome="denied",
        audit_event_id=None,
    )

    for receipt in (success, legacy_denial):
        assert receipt.reason_code is None
        signing_input = await service.signing_input(receipt.receipt_id)
        assert signing_input is not None
        assert "reason_code" not in json.loads(signing_input)
        assert (await service.verify_receipt(receipt.receipt_id))[0] is True

    with pytest.raises(ReceiptError, match="receipt_reason_code_invalid"):
        await service.create_receipt(
            permit_id=permit["permit_id"],
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool="receipt-optional-reason-tool",
            request_payload={"attempt": "invalid-success"},
            response_payload={"ok": True},
            ledger_entry_id=None,
            credits_authorized=Decimal("1"),
            credits_charged=Decimal("1"),
            outcome="success",
            audit_event_id=None,
            reason_code="should_not_exist",
        )


@pytest.mark.anyio
async def test_receipt_idempotency_matches_reason_code(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="receipt-reason-idempotency-tool",
        idem_key="receipt-reason-idempotency-permit",
    )
    request_payload = {"attempt": "reason-idempotency"}
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint="/mcp/messages",
        idempotency_key="receipt-reason-idempotency-invoke",
        request_payload=request_payload,
    )
    assert begun.replay is None
    service = get_receipt_service()
    kwargs = {
        "permit_id": permit["permit_id"],
        "wallet_id": provisioned["agent_wallet_id"],
        "key_id": provisioned["key_id"],
        "tool": "receipt-reason-idempotency-tool",
        "request_payload": request_payload,
        "response_payload": {"error": "denied"},
        "ledger_entry_id": None,
        "credits_authorized": Decimal("1"),
        "credits_charged": Decimal("0"),
        "outcome": "denied",
        "audit_event_id": None,
        "idempotency_record_id": begun.record_id,
    }

    receipt = await service.create_receipt(
        **kwargs,
        reason_code="permit_budget_exceeded",
    )
    replay = await service.create_receipt(
        **kwargs,
        reason_code="permit_budget_exceeded",
    )
    assert replay.receipt_id == receipt.receipt_id

    with pytest.raises(ReceiptError, match="receipt_idempotency_conflict"):
        await service.create_receipt(
            **kwargs,
            reason_code="permit_expired",
        )


@pytest.mark.anyio
async def test_dynamic_forbidden_field_denial_signs_stable_reason_without_charge(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    tool_name = "receipt-dynamic-forbidden-field"
    unsafe_field_name = "Secret Token/Raw"
    _register_echo_tool(tool_name)
    try:
        permit_resp = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": provisioned["key_id"],
                "allowed_tools": [tool_name],
                "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                "max_credits": 50,
                "forbidden_fields": [unsafe_field_name],
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=30)
                ).isoformat(),
            },
            headers={
                **BOOTSTRAP_HEADERS,
                "Idempotency-Key": "receipt-dynamic-forbidden-permit",
            },
        )
        assert permit_resp.status_code == 201, permit_resp.text
        permit = permit_resp.json()
        response = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "receipt-dynamic-forbidden-call",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": {unsafe_field_name: "must-not-be-logged"},
                    "mcpContext": {
                        "wallet_id": wallet_id,
                        "permit_id": permit["permit_id"],
                        "idempotency_key": "receipt-dynamic-forbidden-invoke",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )
    finally:
        get_service_registry().unregister_local(tool_name)

    assert response.status_code == 200, response.text
    error = response.json()["error"]
    assert error["message"] == "permit_forbidden_field"
    assert unsafe_field_name not in error["message"]
    receipt = error["data"]["receipt"]
    assert receipt["reason_code"] == "permit_forbidden_field"
    assert receipt["ledger_entry_id"] is None
    assert receipt["credits_charged"] == "0"

    portable = await client.get(
        f"/v1/receipts/{receipt['receipt_id']}/portable",
        headers=provisioned["agent_headers"],
    )
    assert portable.status_code == 200, portable.text
    signed_claims = json.loads(portable.json()["signing_input"])
    assert signed_claims["reason_code"] == "permit_forbidden_field"

    ledger = await client.get(
        f"/v1/billing/ledger/{wallet_id}",
        headers=provisioned["agent_headers"],
    )
    assert ledger.status_code == 200, ledger.text
    assert all(
        tool_name not in entry["description"] for entry in ledger.json()["entries"]
    )


def _register_echo_tool(tool_name: str) -> None:
    def echo(message: str = "ok") -> dict:
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name=f"Evidence test {tool_name}",
        description="Receipt evidence test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=2.0,
        unit_name="call",
    )


async def _create_governed_receipt(
    client: AsyncClient,
    *,
    provisioned: dict,
    tool_name: str,
    idem_key: str,
) -> tuple[dict, dict]:
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        idem_key=f"{idem_key}-permit",
    )
    response = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": f"{idem_key}-call",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"message": "hello"},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": idem_key,
                },
            },
        },
        headers=provisioned["agent_headers"],
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["isError"] is False
    return permit, payload["result"]["receipt"]


@pytest.mark.anyio
async def test_receipt_evidence_links_governed_mcp_artifacts(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    tool_name = "receipt-evidence-tool"
    registry = get_service_registry()
    _register_echo_tool(tool_name)
    try:
        permit, receipt = await _create_governed_receipt(
            client,
            provisioned=provisioned,
            tool_name=tool_name,
            idem_key="receipt-evidence-success",
        )
        evidence_resp = await client.get(
            f"/v1/receipts/{receipt['receipt_id']}/evidence",
            headers=provisioned["agent_headers"],
        )
    finally:
        registry.unregister_local(tool_name)

    assert evidence_resp.status_code == 200
    evidence = evidence_resp.json()
    assert evidence["valid"] is True
    assert evidence["permit"]["permit_id"] == permit["permit_id"]
    assert evidence["receipt"]["receipt_id"] == receipt["receipt_id"]
    assert evidence["audit_event"]["event"] == "mcp.invoke"
    assert evidence["ledger_entry"]["entry_id"] == receipt["ledger_entry_id"]

    checks = {check["name"]: check for check in evidence["checks"]}
    for check_name in (
        "wallet_access",
        "receipt_signature",
        "permit_signature",
        "permit_wallet_binding",
        "permit_tool_scope",
        "audit_event_linkage",
        "audit_chain",
        "ledger_linkage",
    ):
        assert checks[check_name]["status"] == "passed"


@pytest.mark.anyio
async def test_receipt_evidence_denies_cross_wallet_access(
    client,
    clean_database,
):
    wallet_a = await provision_agent_wallet(client)
    wallet_b = await provision_agent_wallet(client)
    tool_name = "receipt-evidence-cross-wallet"
    registry = get_service_registry()
    _register_echo_tool(tool_name)
    try:
        _, receipt = await _create_governed_receipt(
            client,
            provisioned=wallet_a,
            tool_name=tool_name,
            idem_key="receipt-evidence-cross-wallet",
        )
        response = await client.get(
            f"/v1/receipts/{receipt['receipt_id']}/evidence",
            headers=wallet_b["agent_headers"],
        )
    finally:
        registry.unregister_local(tool_name)

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "wallet_access_denied"


@pytest.mark.anyio
async def test_receipt_evidence_detects_ledger_tampering(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    tool_name = "receipt-evidence-ledger-tamper"
    registry = get_service_registry()
    _register_echo_tool(tool_name)
    try:
        _, receipt = await _create_governed_receipt(
            client,
            provisioned=provisioned,
            tool_name=tool_name,
            idem_key="receipt-evidence-ledger-tamper",
        )
    finally:
        registry.unregister_local(tool_name)

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(LedgerEntryModel).where(
                LedgerEntryModel.entry_id == receipt["ledger_entry_id"]
            )
        )
        entry = result.scalar_one()
        entry.amount = Decimal("-999")
        session.add(entry)
        await session.commit()

    evidence_resp = await client.get(
        f"/v1/receipts/{receipt['receipt_id']}/evidence",
        headers=provisioned["agent_headers"],
    )

    assert evidence_resp.status_code == 200
    evidence = evidence_resp.json()
    assert evidence["valid"] is False
    checks = {check["name"]: check for check in evidence["checks"]}
    assert checks["ledger_linkage"]["status"] == "failed"
    assert checks["ledger_linkage"]["reason"] == "ledger_amount_mismatch"


@pytest.mark.anyio
async def test_receipt_evidence_does_not_expose_cross_wallet_artifacts(
    client,
    clean_database,
):
    wallet_a = await provision_agent_wallet(client)
    wallet_b = await provision_agent_wallet(client)
    tool_a = "receipt-evidence-wallet-a"
    tool_b = "receipt-evidence-wallet-b"
    registry = get_service_registry()
    _register_echo_tool(tool_a)
    _register_echo_tool(tool_b)
    try:
        _, receipt_a = await _create_governed_receipt(
            client,
            provisioned=wallet_a,
            tool_name=tool_a,
            idem_key="receipt-evidence-wallet-a",
        )
        _, receipt_b = await _create_governed_receipt(
            client,
            provisioned=wallet_b,
            tool_name=tool_b,
            idem_key="receipt-evidence-wallet-b",
        )
    finally:
        registry.unregister_local(tool_a)
        registry.unregister_local(tool_b)

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ReceiptModel).where(
                ReceiptModel.receipt_id == receipt_a["receipt_id"]
            )
        )
        model = result.scalar_one()
        model.permit_id = receipt_b["permit_id"]
        model.audit_event_id = receipt_b["audit_event_id"]
        model.ledger_entry_id = receipt_b["ledger_entry_id"]
        session.add(model)
        await session.commit()

    evidence_resp = await client.get(
        f"/v1/receipts/{receipt_a['receipt_id']}/evidence",
        headers=wallet_a["agent_headers"],
    )

    assert evidence_resp.status_code == 200
    evidence = evidence_resp.json()
    assert evidence["valid"] is False
    assert evidence["permit"] is None
    assert evidence["audit_event"] is None
    assert evidence["ledger_entry"] is None

    checks = {check["name"]: check for check in evidence["checks"]}
    assert checks["permit_exists"]["status"] == "failed"
    assert checks["audit_event_linkage"]["status"] == "failed"
    assert checks["audit_event_linkage"]["reason"] == "audit_event_not_found"
    assert checks["ledger_linkage"]["status"] == "failed"
    assert checks["ledger_linkage"]["reason"] == "ledger_not_found"


# Reading a receipt — and especially exporting its portable, offline-verifiable
# bundle — is tenant data behind authentication. These read surfaces must fail
# closed before any receipt lookup: the credential decides the status (missing
# or malformed -> 401, well-formed but unknown -> 403), and an unknown receipt
# id must never 404 for an anonymous caller, which would leak existence. See
# app/core/auth.get_auth_context and app/routers/receipts.py.
@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/v1/receipts/rcpt-does-not-exist",
        "/v1/receipts/rcpt-does-not-exist/portable",
        "/v1/receipts/rcpt-does-not-exist/evidence",
    ],
)
@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 401),
        ({"X-API-Key": "short"}, 401),
        ({"X-API-Key": "not-a-real-key"}, 403),
    ],
)
async def test_receipt_read_surfaces_require_authentication(
    client, clean_database, path, headers, expected_status
):
    resp = await client.get(path, headers=headers)
    assert resp.status_code == expected_status, resp.text
    # Auth is enforced before the lookup: no existence disclosure to anon callers.
    assert resp.status_code != 404, resp.text


@pytest.mark.anyio
async def test_receipt_digest_chain_validation_and_pre056_idempotent_replay(
    client,
    clean_database,
    monkeypatch,
):
    """Digest/chain gates: hex shape, sequence rules, pre-056 NULL replay.

    Real issues from bot review on #294: invalid digests must not pass a loose
    int() check; sequence stays unset until opted in; adopting a pre-056 row
    (NULL provenance, signature without those fields) must not conflict when
    the retry would freshly compute digests.
    """
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="receipt-digest-chain-tool",
        idem_key="receipt-digest-chain-permit",
    )
    service = get_receipt_service()
    base = {
        "permit_id": permit["permit_id"],
        "wallet_id": provisioned["agent_wallet_id"],
        "key_id": provisioned["key_id"],
        "tool": "receipt-digest-chain-tool",
        "request_payload": {"attempt": "digest-chain"},
        "response_payload": {"ok": True},
        "ledger_entry_id": None,
        "credits_authorized": Decimal("1"),
        "credits_charged": Decimal("0"),
        "outcome": "denied",
        "reason_code": "permit_budget_exceeded",
        "audit_event_id": None,
    }

    with pytest.raises(ReceiptError, match="receipt_permit_digest_invalid"):
        await service.create_receipt(**base, permit_digest="not-64-hex")
    with pytest.raises(ReceiptError, match="receipt_permit_digest_invalid"):
        await service.create_receipt(**base, permit_digest="g" * 64)
    with pytest.raises(ReceiptError, match="receipt_sequence_invalid"):
        await service.create_receipt(**base, sequence=-1)
    with pytest.raises(ReceiptError, match="receipt_prev_digest_required"):
        await service.create_receipt(**base, sequence=1)
    with pytest.raises(ReceiptError, match="receipt_prev_digest_invalid"):
        await service.create_receipt(**base, sequence=1, prev_receipt_digest="z" * 64)

    # First write mimics pre-056: no permit_digest available to bind.
    import app.services.receipts as receipts_mod

    async def _no_permit_digest(permit_id, *, session=None):
        return None

    monkeypatch.setattr(receipts_mod, "permit_digest_for_permit_id", _no_permit_digest)
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint="/mcp/messages",
        idempotency_key="receipt-digest-chain-invoke",
        request_payload=base["request_payload"],
    )
    assert begun.replay is None
    first = await service.create_receipt(
        **base,
        idempotency_record_id=begun.record_id,
    )
    assert first.sequence is None
    assert first.permit_digest is None
    assert first.prev_receipt_digest is None

    # Retry with live digest computation restored: historical NULL omission
    # must adopt, not raise receipt_idempotency_conflict.
    monkeypatch.undo()
    replay = await service.create_receipt(
        **base,
        idempotency_record_id=begun.record_id,
    )
    assert replay.receipt_id == first.receipt_id
    assert replay.permit_digest is None  # adopted row keeps historical omission

    # Explicit sequence 0 still signs as the first link and omits prev.
    chained = await service.create_receipt(**base, sequence=0)
    assert chained.sequence == 0
    assert chained.prev_receipt_digest is None
    signing_input = await service.signing_input(chained.receipt_id)
    assert signing_input is not None
    body = json.loads(signing_input)
    assert body["sequence"] == 0
    assert "prev_receipt_digest" not in body


def test_assert_idempotent_match_tolerates_null_provenance_only():
    """Unit gate: stored NULL additive fields match; stored values still bind."""
    from app.services.receipts import ReceiptService

    model = ReceiptModel(
        receipt_id="rcpt_unit_1",
        permit_id="pmt_unit_1",
        wallet_id="w",
        key_id="k",
        tool="t",
        request_hash="a" * 64,
        response_hash=None,
        ledger_entry_id=None,
        credits_authorized=Decimal("1"),
        credits_charged=Decimal("0"),
        outcome="denied",
        reason_code="permit_budget_exceeded",
        audit_event_id=None,
        approval_id=None,
        permit_digest=None,
        approval_digest=None,
        counterparty=None,
        sequence=None,
        prev_receipt_digest=None,
        created_at=datetime.now(timezone.utc),
        signature="sig",
        signature_key_id="sk",
        idempotency_record_id="idem_1",
    )
    kwargs = dict(
        idempotency_record_id="idem_1",
        dispatch_attempt_id=None,
        permit_id="pmt_unit_1",
        wallet_id="w",
        key_id="k",
        tool="t",
        request_hash="a" * 64,
        response_hash=None,
        ledger_entry_id=None,
        credits_authorized=Decimal("1"),
        credits_charged=Decimal("0"),
        outcome="denied",
        reason_code="permit_budget_exceeded",
        audit_event_id=None,
        approval_id=None,
        permit_digest="b" * 64,
        sequence=0,
    )
    ReceiptService._assert_idempotent_match(model, **kwargs)

    model.permit_digest = "c" * 64
    with pytest.raises(ReceiptError, match="receipt_idempotency_conflict"):
        ReceiptService._assert_idempotent_match(model, **kwargs)


@pytest.mark.anyio
async def test_create_receipt_binds_counterparty_on_failed_unrefunded(
    client,
    clean_database,
):
    """Failure outcomes must still accept and sign the upstream counterparty."""
    service = get_receipt_service()
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="receipt-tool",
        idem_key="counterparty-unrefunded-permit",
    )
    receipt = await service.create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="receipt-tool",
        request_payload={"ping": "pong"},
        response_payload={"error": "refund_failed"},
        ledger_entry_id=None,
        credits_authorized=Decimal("1"),
        credits_charged=Decimal("1"),
        outcome="failed_unrefunded",
        audit_event_id=None,
        reason_code="refund_failed",
        counterparty="partner.example",
    )
    assert receipt.counterparty == "partner.example"
    signing_input = await service.signing_input(receipt.receipt_id)
    assert signing_input is not None
    body = json.loads(signing_input)
    assert body["counterparty"] == "partner.example"


@pytest.mark.anyio
async def test_receipt_chain_integrity_and_permit_digest_binding(
    client, clean_database
):
    """Codex #294: sequence/prev must bind to predecessor; permit_digest to permit."""
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="receipt-chain-integrity-tool",
        idem_key="receipt-chain-integrity-permit",
    )
    service = get_receipt_service()
    base = {
        "permit_id": permit["permit_id"],
        "wallet_id": provisioned["agent_wallet_id"],
        "key_id": provisioned["key_id"],
        "tool": "receipt-chain-integrity-tool",
        "request_payload": {"attempt": "chain-integrity"},
        "response_payload": {"ok": True},
        "ledger_entry_id": None,
        "credits_authorized": Decimal("1"),
        "credits_charged": Decimal("0"),
        "outcome": "denied",
        "reason_code": "permit_budget_exceeded",
        "audit_event_id": None,
    }

    from app.services import receipts as receipts_mod
    from app.services.signing_keys import sha256_hex

    live_digest = await receipts_mod.permit_digest_for_permit_id(permit["permit_id"])
    assert live_digest is not None and len(live_digest) == 64

    with pytest.raises(ReceiptError, match="receipt_permit_digest_mismatch"):
        await service.create_receipt(**base, permit_digest="a" * 64)

    bound = await service.create_receipt(**base, permit_digest=live_digest, sequence=0)
    assert bound.permit_digest == live_digest
    assert bound.sequence == 0

    with pytest.raises(ReceiptError, match="receipt_sequence_conflict"):
        await service.create_receipt(**base, sequence=0)

    first_input = await service.signing_input(bound.receipt_id)
    assert first_input is not None
    prev_digest = sha256_hex(first_input)

    with pytest.raises(ReceiptError, match="receipt_prev_digest_mismatch"):
        await service.create_receipt(**base, sequence=1, prev_receipt_digest="b" * 64)
    with pytest.raises(ReceiptError, match="receipt_prev_not_found"):
        await service.create_receipt(
            **base, sequence=2, prev_receipt_digest=prev_digest
        )

    second = await service.create_receipt(
        **base, sequence=1, prev_receipt_digest=prev_digest
    )
    assert second.sequence == 1
    assert second.prev_receipt_digest == prev_digest
