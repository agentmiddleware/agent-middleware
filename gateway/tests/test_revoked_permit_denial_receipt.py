"""Test revoked permit denial behavior and receipt generation.

This test suite verifies the design partner's claim that a revoked permit:
1. Is denied before any charge or budget movement
2. Does not dispatch to the tool
3. Returns a specific denial error code
4. Produces a signed receipt
5. The receipt is offline-verifiable with the Python SDK
6. A tampered receipt is rejected by the SDK verifier
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import PermitModel, ReceiptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    create_tool_permit,
    provision_agent_wallet,
    resign_permit_model,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _register_test_tool(tool_name: str) -> None:
    """Register a simple test tool for permit validation."""
    get_service_registry().register_local(
        service_id=tool_name,
        name=tool_name,
        description="test tool for revoked permit checks",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": {"message": message, "dispatched": True},
        credits_per_unit=2.0,
        unit_name="call",
    )


def _mcp_call(wallet_id: str, permit_id: str, tool_name: str, idem: str) -> dict:
    """Create an MCP tools/call request payload."""
    return {
        "jsonrpc": "2.0",
        "id": idem,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"message": "test"},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idem,
            },
        },
    }


@pytest.mark.anyio
async def test_revoked_permit_denied_without_budget_movement(client, clean_database):
    """A revoked permit is denied without reserving or charging credits.

    G1: Verifies wallet balance unchanged and total ledger count unchanged across all categories,
    not just agent_comms entries with the tool name.
    """
    from unittest.mock import patch

    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-no-charge-tool"
    _register_test_tool(tool_name)

    try:
        # Get initial wallet balance and ledger count
        wallet_resp = await client.get(
            f"/v1/billing/wallets/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        assert wallet_resp.status_code == 200
        initial_balance = wallet_resp.json()["balance"]

        ledger_resp = await client.get(
            f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        assert ledger_resp.status_code == 200
        initial_ledger_count = len(ledger_resp.json()["entries"])

        # Create and then revoke a permit
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_revoked_permit_denied_without_budget_movement-1",
        )

        revoke_resp = await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        assert revoke_resp.status_code == 200

        # Verify the permit is revoked
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(PermitModel).where(PermitModel.permit_id == permit["permit_id"])
            )
            model = result.scalar_one()
            assert model.status == "revoked"
            spent_before = model.spent_credits

        # Mock charge paths to ensure they are never called
        with (
            patch("app.services.agent_money.AgentMoney.charge") as mock_charge,
            patch("stripe.Charge.create") as mock_stripe,
        ):
            # Try to invoke with the revoked permit
            call_resp = await client.post(
                "/mcp/messages",
                json=_mcp_call(
                    provisioned["agent_wallet_id"],
                    permit["permit_id"],
                    tool_name,
                    "revoked-call-1",
                ),
                headers=provisioned["agent_headers"],
            )
            assert call_resp.status_code == 200
            error = call_resp.json()["error"]
            assert error["message"] == "permit_revoked"

            # Assert charge paths were never called
            mock_charge.assert_not_called()
            mock_stripe.assert_not_called()

        # Verify no budget movement occurred
        async with factory() as session:
            result = await session.execute(
                select(PermitModel).where(PermitModel.permit_id == permit["permit_id"])
            )
            model = result.scalar_one()
            assert model.spent_credits == spent_before

        # G1: Verify wallet balance is unchanged
        wallet_resp = await client.get(
            f"/v1/billing/wallets/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        assert wallet_resp.status_code == 200
        final_balance = wallet_resp.json()["balance"]
        assert final_balance == initial_balance, (
            f"Wallet balance changed: {initial_balance} -> {final_balance}"
        )

        # G1: Verify total ledger count is unchanged (no PLATFORM_FEE or other charges)
        ledger_resp = await client.get(
            f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        assert ledger_resp.status_code == 200
        final_ledger_count = len(ledger_resp.json()["entries"])
        assert final_ledger_count == initial_ledger_count, (
            f"Ledger entry count changed: {initial_ledger_count} -> {final_ledger_count}"
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_revoked_permit_not_dispatched(client, clean_database):
    """A revoked permit denial does not dispatch to the upstream tool.

    QA gap 4: Includes positive control (active permit dispatches once) and
    asserts no dispatch_attempt_id and no mcp_dispatch_attempts row.
    """
    from app.db.models import McpDispatchAttemptModel

    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-no-dispatch-tool"

    dispatched = []

    def tracking_tool(message="ok"):
        dispatched.append(message)
        return {"message": message, "dispatched": True}

    get_service_registry().register_local(
        service_id=tool_name,
        name=tool_name,
        description="tool that tracks dispatch calls",
        category=ServiceCategory.AGENT_COMMS,
        func=tracking_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )

    try:
        # QA gap 4: Positive control - active permit dispatches once
        active_permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="active-permit-positive-control",
        )

        call_resp_active = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                active_permit["permit_id"],
                tool_name,
                "active-call-positive-control",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp_active.status_code == 200
        assert "result" in call_resp_active.json()
        assert len(dispatched) == 1, "Active permit should dispatch once"
        dispatched.clear()

        # Now test revoked permit
        revoked_permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="revoked-permit-no-dispatch",
        )

        # Revoke the permit
        revoke_resp = await client.post(
            f"/v1/permits/{revoked_permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        assert revoke_resp.status_code == 200

        # Try to invoke with revoked permit
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                revoked_permit["permit_id"],
                tool_name,
                "revoked-call-2",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        error = call_resp.json()["error"]
        assert error["message"] == "permit_revoked"

        # QA gap 4: Assert no dispatch_attempt_id in response
        assert "dispatch_attempt_id" not in error.get("data", {})

        # QA gap 4: Verify the tool was never invoked via tracking_tool callback
        assert len(dispatched) == 0

        # QA gap 4: Verify no mcp_dispatch_attempts row
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(McpDispatchAttemptModel).where(
                    McpDispatchAttemptModel.permit_id == revoked_permit["permit_id"]
                )
            )
            attempts = result.scalars().all()
            assert len(attempts) == 0, (
                "Revoked permit should create no dispatch attempts"
            )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_revoked_permit_returns_denial_code(client, clean_database):
    """A revoked permit returns the specific 'permit_revoked' error code."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-error-code-tool"
    _register_test_tool(tool_name)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_revoked_permit_returns_denial_code-1",
        )

        # Revoke the permit
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )

        # Invoke and check error code
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-3",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        error = call_resp.json()["error"]
        assert error["message"] == "permit_revoked"
        assert error["code"] == -32003  # ToolPermissionDenied error code
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_revoked_permit_produces_receipt(client, clean_database):
    """A revoked permit denial produces a signed receipt.

    QA gap 1: Verifies credits_charged=='0', ledger_entry_id None, permit_id, wallet_id,
    and tool from the SIGNED claims in the portable bundle, not just the inline copy.
    """
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-receipt-tool"
    _register_test_tool(tool_name)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_revoked_permit_produces_receipt-1",
        )

        # Revoke the permit
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )

        # Invoke with revoked permit
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-4",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        error = call_resp.json()["error"]
        assert error["message"] == "permit_revoked"

        # Verify a receipt exists in the inline response
        assert "receipt" in error["data"]
        receipt = error["data"]["receipt"]
        assert receipt["outcome"] == "denied"
        assert receipt["reason_code"] == "permit_revoked"
        # Note: inline receipt may not have signing_input, see t:297 xfail
        receipt_id = receipt["receipt_id"]

        # QA gap 1: Fetch portable bundle and verify SIGNED claims
        portable_resp = await client.get(
            f"/v1/receipts/{receipt_id}/portable",
            headers=provisioned["agent_headers"],
        )
        assert portable_resp.status_code == 200
        portable_bundle = portable_resp.json()

        # Verify with SDK to get signed claims
        from b2a_sdk.receipt_verifier import verify_bundle, key_set_from_document

        keys_resp = await client.get("/.well-known/trust-keys.json")
        assert keys_resp.status_code == 200
        keys_doc = keys_resp.json()
        key_set = key_set_from_document(keys_doc)

        result = verify_bundle(portable_bundle, key_set)
        assert result.ok, f"Verification failed: {result.status} - {result.reason}"

        # QA gap 1: Assert signed claims (from signing_input, not just inline copy)
        assert result.claims["credits_charged"] == "0"
        assert result.claims["ledger_entry_id"] is None
        assert result.claims["permit_id"] == permit["permit_id"]
        assert result.claims["wallet_id"] == provisioned["agent_wallet_id"]
        assert result.claims["tool"] == tool_name

        # Verify the receipt is persisted
        factory = get_session_factory()
        async with factory() as session:
            result_db = await session.execute(
                select(ReceiptModel).where(
                    ReceiptModel.permit_id == permit["permit_id"]
                )
            )
            stored_receipt = result_db.scalar_one()
            assert stored_receipt.outcome == "denied"
            assert stored_receipt.signature is not None
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Receipt in MCP error response is not in portable bundle format with signing_input",
)
@pytest.mark.anyio
async def test_revoked_permit_inline_receipt_lacks_signing_input(
    client, clean_database
):
    """Receipt in MCP error response lacks signing_input for offline verification.

    This test documents that the inline receipt (returned in the MCP error payload)
    lacks the signing_input field required by verify_bundle. The portable endpoint
    GET /v1/receipts/{id}/portable must be used for offline verification.
    """
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-inline-verify-tool"
    _register_test_tool(tool_name)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_revoked_permit_inline_receipt_lacks_signing_input-1",
        )

        # Revoke and invoke
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )

        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-5",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        receipt = call_resp.json()["error"]["data"]["receipt"]

        # Get the public keys for verification
        keys_resp = await client.get("/.well-known/trust-keys.json")
        assert keys_resp.status_code == 200
        keys_doc = keys_resp.json()

        # Try to verify with the Python SDK
        # Note: The inline receipt cannot be verified because it lacks signing_input.
        # For offline verification, use GET /v1/receipts/{id}/portable instead.
        # See test_portable_denial_receipt_offline_verifiable for the working path.
        from b2a_sdk.receipt_verifier import verify_bundle, key_set_from_document

        key_set = key_set_from_document(keys_doc)
        result = verify_bundle(receipt, key_set)

        # The inline receipt lacks signing_input so verification fails with MALFORMED
        assert result.ok, f"Verification failed: {result.status} - {result.reason}"
        assert result.claims["outcome"] == "denied"
        assert result.claims["reason_code"] == "permit_revoked"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_portable_denial_receipt_offline_verifiable(client, clean_database):
    """Portable bundle from GET /v1/receipts/{id}/portable is offline-verifiable."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-portable-verify-tool"
    _register_test_tool(tool_name)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_portable_denial_receipt_offline_verifiable-1",
        )

        # Revoke and invoke to get a denial receipt
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )

        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-portable-1",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        receipt_id = call_resp.json()["error"]["data"]["receipt"]["receipt_id"]

        # Fetch the portable bundle
        portable_resp = await client.get(
            f"/v1/receipts/{receipt_id}/portable",
            headers=provisioned["agent_headers"],
        )
        assert portable_resp.status_code == 200
        portable_bundle = portable_resp.json()

        # Get the public keys for verification
        keys_resp = await client.get("/.well-known/trust-keys.json")
        assert keys_resp.status_code == 200
        keys_doc = keys_resp.json()

        # Verify with the Python SDK
        from b2a_sdk.receipt_verifier import verify_bundle, key_set_from_document

        key_set = key_set_from_document(keys_doc)
        result = verify_bundle(portable_bundle, key_set)

        # The portable bundle should verify offline
        assert result.ok, f"Verification failed: {result.status} - {result.reason}"
        assert result.claims["outcome"] == "denied"
        assert result.claims["reason_code"] == "permit_revoked"
        assert result.receipt_id == receipt_id
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_tampered_portable_denial_receipt_rejected(client, clean_database):
    """A tampered portable denial receipt bundle is rejected by offline verification."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-portable-tamper-tool"
    _register_test_tool(tool_name)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_tampered_portable_denial_receipt_rejected-1",
        )

        # Revoke and invoke
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )

        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-portable-2",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        receipt_id = call_resp.json()["error"]["data"]["receipt"]["receipt_id"]

        # Fetch the portable bundle
        portable_resp = await client.get(
            f"/v1/receipts/{receipt_id}/portable",
            headers=provisioned["agent_headers"],
        )
        assert portable_resp.status_code == 200
        portable_bundle = portable_resp.json()

        # Get the public keys
        keys_resp = await client.get("/.well-known/trust-keys.json")
        assert keys_resp.status_code == 200
        keys_doc = keys_resp.json()

        # Tamper with the portable bundle
        from b2a_sdk.receipt_verifier import verify_bundle, key_set_from_document
        import copy

        key_set = key_set_from_document(keys_doc)

        # First verify the untampered bundle works
        result = verify_bundle(portable_bundle, key_set)
        assert result.ok, "Untampered bundle should verify"

        # Now tamper with fields in the signing_input (the canonical signed payload)
        tampered = copy.deepcopy(portable_bundle)

        # Tamper with a field in signing_input - change the receipt_id
        import json

        signing_input = json.loads(tampered["signing_input"])
        signing_input["receipt_id"] = "rcpt-tampered-000000000000"
        tampered["signing_input"] = json.dumps(
            signing_input, separators=(",", ":"), sort_keys=True
        )

        tampered_result = verify_bundle(tampered, key_set)
        # Verification should fail because signature doesn't match tampered signing_input
        assert tampered_result.is_rejected or tampered_result.is_tampered, (
            f"Tampered bundle should be rejected, got: {tampered_result.status} - {tampered_result.reason}"
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("outcome", "success"),
        ("reason_code", "permit_exhausted"),
        ("credits_charged", "100"),
        ("permit_id", "prmt-tampered-000000000000"),
        ("tool", "tampered_tool_name"),
        ("wallet_id", "wlt-tampered-000000000000"),
    ],
)
@pytest.mark.anyio
async def test_tampered_portable_signed_field_rejected(
    client, clean_database, field, tampered_value
):
    """Tampering any signed field in the portable bundle signing_input is rejected.

    G2: Parametrized test that tampers each signed field inside signing_input,
    not the top-level envelope. Each tamper should cause verification to fail.
    """
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-tamper-field-tool"
    _register_test_tool(tool_name)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_tampered_portable_signed_field_rejected-1",
        )

        # Revoke and invoke
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )

        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                f"revoked-tamper-{field}",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        receipt_id = call_resp.json()["error"]["data"]["receipt"]["receipt_id"]

        # Fetch the portable bundle
        portable_resp = await client.get(
            f"/v1/receipts/{receipt_id}/portable",
            headers=provisioned["agent_headers"],
        )
        assert portable_resp.status_code == 200
        portable_bundle = portable_resp.json()

        # Get the public keys
        keys_resp = await client.get("/.well-known/trust-keys.json")
        assert keys_resp.status_code == 200
        keys_doc = keys_resp.json()

        from b2a_sdk.receipt_verifier import verify_bundle, key_set_from_document
        import copy
        import json

        key_set = key_set_from_document(keys_doc)

        # First verify the untampered bundle works
        result = verify_bundle(portable_bundle, key_set)
        assert result.ok, "Untampered bundle should verify"

        # Tamper with the specific field in signing_input
        tampered = copy.deepcopy(portable_bundle)
        signing_input = json.loads(tampered["signing_input"])
        signing_input[field] = tampered_value
        tampered["signing_input"] = json.dumps(
            signing_input, separators=(",", ":"), sort_keys=True
        )

        tampered_result = verify_bundle(tampered, key_set)
        # G2: Verification must fail (INVALID) because signature doesn't match tampered signing_input
        assert (
            tampered_result.status == "INVALID"
            or tampered_result.is_rejected
            or tampered_result.is_tampered
        ), (
            f"Tampered {field} should be rejected, got: {tampered_result.status} - {tampered_result.reason}"
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Gateway bug: revoked permit not enforced on POST /mcp/tools/{service_id}/invoke; permit validated at mcp.py:1427 but code path unclear; root cause not yet isolated",
)
@pytest.mark.anyio
async def test_revoked_permit_via_rest_invoke(client, clean_database):
    """QA gap 5: Revoked permit denial through POST /mcp/tools/{service_id}/invoke."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-rest-invoke-tool"
    _register_test_tool(tool_name)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_revoked_permit_via_rest_invoke-1",
        )

        # Revoke the permit
        revoke_resp = await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        assert revoke_resp.status_code == 200, f"Revoke failed: {revoke_resp.json()}"

        # Verify permit is actually revoked in DB
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(PermitModel).where(PermitModel.permit_id == permit["permit_id"])
            )
            model = result.scalar_one()
            assert model.status == "revoked", f"Permit not revoked: {model.status}"

        # Try to invoke via REST endpoint
        call_resp = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json={
                "name": tool_name,
                "arguments": {"message": "test"},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "revoked-rest-invoke-1",
                },
            },
            headers=provisioned["agent_headers"],
        )
        # QA gap 5: Expect 403 with permit_revoked
        assert call_resp.status_code == 403
        error = call_resp.json()
        assert "permit_revoked" in str(error).lower()
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Gateway bug: revoked permit not enforced on POST /mcp; permit validated at mcp.py:1427 but code path unclear; root cause not yet isolated",
)
@pytest.mark.anyio
async def test_revoked_permit_via_standard_mcp_endpoint(client, clean_database):
    """QA gap 6: Revoked permit denial through standard POST /mcp endpoint."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-standard-mcp-tool"
    _register_test_tool(tool_name)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_revoked_permit_via_standard_mcp_endpoint-1",
        )

        # Revoke the permit
        revoke_resp = await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        assert revoke_resp.status_code == 200, f"Revoke failed: {revoke_resp.json()}"

        # Verify permit is actually revoked in DB
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(PermitModel).where(PermitModel.permit_id == permit["permit_id"])
            )
            model = result.scalar_one()
            assert model.status == "revoked", f"Permit not revoked: {model.status}"

        # Try to invoke via standard /mcp endpoint
        call_resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "standard-mcp-1",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": {"message": "test"},
                },
            },
            headers={
                **provisioned["agent_headers"],
                "X-Wallet-ID": provisioned["agent_wallet_id"],
                "X-Permit-ID": permit["permit_id"],
                "Accept": "application/json",
            },
        )
        assert call_resp.status_code == 200
        resp_json = call_resp.json()
        assert "error" in resp_json
        assert resp_json["error"]["message"] == "permit_revoked"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_revoked_permit_details_includes_revoked_at(client, clean_database):
    """QA gap 7: Assert error.data.details.revoked_at equals DB permits.revoked_at."""
    from datetime import datetime

    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-at-field-tool"
    _register_test_tool(tool_name)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_revoked_permit_details_includes_revoked_at-1",
        )

        # Revoke the permit
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )

        # Get the DB revoked_at timestamp
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(PermitModel).where(PermitModel.permit_id == permit["permit_id"])
            )
            model = result.scalar_one()
            assert model.status == "revoked"
            db_revoked_at = model.revoked_at
            assert db_revoked_at is not None
            # Normalize to UTC timezone-aware for comparison
            from datetime import timezone

            db_revoked_at = db_revoked_at.replace(tzinfo=timezone.utc)
        # Invoke with revoked permit
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-at-call-1",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        error = call_resp.json()["error"]
        assert error["message"] == "permit_revoked"

        # QA gap 7: Assert error.data.details.revoked_at matches DB
        details = error["data"].get("details", {})
        response_revoked_at_str = details.get("revoked_at")
        assert response_revoked_at_str is not None, (
            "details.revoked_at missing from error response"
        )

        # Parse and compare timestamps
        response_revoked_at = datetime.fromisoformat(
            response_revoked_at_str.replace("Z", "+00:00")
        )
        # Allow 1 second tolerance for any rounding/formatting differences
        time_diff = abs((response_revoked_at - db_revoked_at).total_seconds())
        assert time_diff < 1.0, (
            f"revoked_at mismatch: DB={db_revoked_at}, response={response_revoked_at}"
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.parametrize(
    "terminal_status",
    [
        "revoked",
        pytest.param(
            "exhausted",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="Gateway bug: exhausted permit during reserve raises internal_error instead of denying with permit_exhausted. See gateway/app/services/permits.py:867",
            ),
        ),
    ],
)
@pytest.mark.anyio
async def test_permit_revoked_during_reserve_is_denied(
    client, clean_database, terminal_status
):
    """QA gap 7: Concurrency test - permit revoked/exhausted between precheck and reserve.

    References test_trust_negative_security.py:test_terminal_permit_between_precheck_and_reservation_is_denied
    for the race condition pattern at permits.py:815 and :867.
    """
    from unittest.mock import patch
    from app.routers.mcp import evaluate_wallet_policy
    from app.core.time import utc_now

    provisioned = await provision_agent_wallet(client)
    tool_name = f"race-{terminal_status}-tool"
    _register_test_tool(tool_name)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="test_permit_revoked_during_reserve_is_denied-1",
        )

        async def transition_permit_during_policy(**kwargs):
            # Transition the permit to terminal state before policy evaluation
            factory = get_session_factory()
            async with factory() as session:
                model = await session.get(PermitModel, permit["permit_id"])
                assert model is not None
                if terminal_status == "revoked":
                    model.status = "revoked"
                    model.revoked_at = utc_now()
                elif terminal_status == "exhausted":
                    model.status = "exhausted"
                    model.spent_credits = model.total_credits
                await resign_permit_model(model)
                session.add(model)
                await session.commit()
            return await evaluate_wallet_policy(**kwargs)

        with patch(
            "app.routers.mcp.evaluate_wallet_policy",
            new=transition_permit_during_policy,
        ):
            call_resp = await client.post(
                "/mcp/messages",
                json=_mcp_call(
                    provisioned["agent_wallet_id"],
                    permit["permit_id"],
                    tool_name,
                    f"racing-{terminal_status}-1",
                ),
                headers=provisioned["agent_headers"],
            )
            assert call_resp.status_code == 200
            error = call_resp.json()["error"]
            # The second validation at permits.py:867 should catch the race
            expected_message = (
                "permit_revoked" if terminal_status == "revoked" else "permit_exhausted"
            )
            assert error["message"] == expected_message
    finally:
        get_service_registry().unregister_local(tool_name)
