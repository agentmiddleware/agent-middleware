"""QA hardening for the Python SDK: money, auth, idempotency, error handling.

Every test here runs against httpx.MockTransport (or plain unit calls) and
asserts on what the SDK sent or how it typed the failure. No network access,
no sleeps, no wall-clock dependence.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from b2a_sdk import (
    AgentMiddlewareClient,
    AuthorizationError,
    DeliveryUncertainError,
    IdempotencyConflictError,
    InsufficientFundsError,
    PermitDeniedError,
    TransportError,
    verify_cli,
)
from b2a_sdk import mcp as mcp_module
from b2a_sdk.client import DryRunSimulation
from b2a_sdk.decorators import (
    billable,
    combined,
    mcp_tool,
    monitored,
    register_mcp_tool_callback,
)
from b2a_sdk.edge_client import (
    B2AEdgeClient,
    GovernedEdgeSession,
    LocalPermitValidator,
)
from b2a_sdk.errors import APIError
from b2a_sdk.models import ACPCheckoutRequest, ACPLineItem, Permit
from b2a_sdk.receipt_verifier import VerificationError

TOOL = "partner.search"


def _recording(responses: dict) -> tuple[AgentMiddlewareClient, list]:
    """A real client whose transport records each request it is sent."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.method, request.url.path)
        if key not in responses:
            raise AssertionError(f"unexpected request: {request.method} {request.url.path}")
        result = responses[key]
        return result(request) if callable(result) else result

    client = AgentMiddlewareClient(
        api_key="test-key",
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )
    return client, seen


def _edge_client(responses: dict) -> tuple[B2AEdgeClient, list]:
    """A B2AEdgeClient rewired to a recording mock transport (no network)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.method, request.url.path)
        if key not in responses:
            raise AssertionError(f"unexpected request: {request.method} {request.url.path}")
        return responses[key]

    client = B2AEdgeClient(api_url="http://test", api_key="edge-key", wallet_id="wallet-1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, seen


def _receipt(**overrides) -> dict:
    payload = {
        "receipt_id": "receipt-1",
        "idempotency_record_id": "idem-1",
        "permit_id": "permit-1",
        "wallet_id": "wallet-1",
        "key_id": "key-1",
        "tool": TOOL,
        "request_hash": "a" * 64,
        "response_hash": "b" * 64,
        "ledger_entry_id": "ledger-1",
        "dispatch_attempt_id": "dispatch-1",
        "credits_authorized": "2",
        "credits_charged": "2",
        "outcome": "success",
        "audit_event_id": "audit-1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "signature": "receipt-signature",
        "signature_key_id": "kid-1",
    }
    payload.update(overrides)
    return payload


def _permit_dict(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    payload = {
        "permit_id": "permit-1",
        "issuer_wallet_id": "wallet-1",
        "subject_wallet_id": "wallet-1",
        "subject_key_id": "key-1",
        "scopes": [f"tool:{TOOL}:invoke", "billing:charge"],
        "allowed_tools": [TOOL],
        "max_credits": "50",
        "spent_credits": "0",
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "nonce": "nonce-1",
        "status": "active",
        "signature": base64.b64encode(b"\x00" * 64).decode(),
        "key_id": "kid-1",
        "issued_at": now.isoformat(),
        "revoked_at": None,
    }
    payload.update(overrides)
    return payload


def _ok_envelope(**result_overrides) -> httpx.Response:
    result = {
        "content": [{"type": "text", "text": '{"ok": true}'}],
        "isError": False,
        "receipt": _receipt(),
    }
    result.update(result_overrides)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": "k", "result": result})


def _error_envelope(
    message: str, receipt: dict | None = None, code: int = -32603
) -> httpx.Response:
    error: dict = {"code": code, "message": message}
    if receipt is not None:
        error["data"] = {"receipt": receipt}
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": "k", "error": error})


class TestCharge402Robustness:
    """A 402 response preserves the SDK's typed body contract."""

    @staticmethod
    def _402_client(body) -> tuple[AgentMiddlewareClient, list]:
        if isinstance(body, bytes):
            response = httpx.Response(402, content=body)
        else:
            response = httpx.Response(402, json=body)
        return _recording({("POST", "/v1/billing/charge"): response})

    @pytest.mark.asyncio
    async def test_402_string_detail_still_insufficient_funds(self):
        client, _ = self._402_client({"detail": "wallet_empty"})
        async with client:
            with pytest.raises(InsufficientFundsError) as exc_info:
                await client.charge("wallet-1", "iot_bridge", units=5)
        assert exc_info.value.wallet_id == "wallet-1"
        assert exc_info.value.shortfall is None

    @pytest.mark.asyncio
    async def test_402_invalid_json_body_is_api_error(self):
        client, _ = self._402_client(b"<html>not json</html>")
        async with client:
            with pytest.raises(APIError, match="invalid_json_response") as exc_info:
                await client.charge("wallet-1", "iot_bridge", units=5)
        assert type(exc_info.value) is APIError
        assert exc_info.value.status_code == 402

    @pytest.mark.asyncio
    async def test_402_list_body_is_api_error(self):
        client, _ = self._402_client([1, 2, 3])
        async with client:
            with pytest.raises(APIError, match="invalid_object_response") as exc_info:
                await client.charge("wallet-1", "iot_bridge", units=5)
        assert type(exc_info.value) is APIError
        assert exc_info.value.status_code == 402

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True, reason="bug: charge leaks raw httpx.ConnectError instead of TransportError"
    )
    async def test_charge_transport_error_is_typed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = AgentMiddlewareClient(
            api_key="test-key",
            base_url="http://test",
            transport=httpx.MockTransport(handler),
        )
        async with client:
            with pytest.raises(TransportError):
                await client.charge("wallet-1", "iot_bridge", units=1)


class TestRequestJsonMapping:
    """Governed-path failures arrive as typed errors, never raw crashes."""

    @pytest.mark.asyncio
    async def test_invalid_json_body_is_api_error(self):
        client, _ = _recording(
            {("GET", "/mcp/tools.json"): httpx.Response(200, content=b"not json")}
        )
        async with client:
            with pytest.raises(APIError, match="invalid_json_response"):
                await client.discover_tools()

    @pytest.mark.asyncio
    async def test_non_dict_json_body_is_api_error(self):
        client, _ = _recording({("GET", "/mcp/tools.json"): httpx.Response(200, json=[1, 2])})
        async with client:
            with pytest.raises(APIError, match="invalid_object_response"):
                await client.discover_tools()

    @pytest.mark.asyncio
    async def test_403_permit_detail_is_permit_denied(self):
        client, _ = _recording(
            {("GET", "/mcp/tools.json"): httpx.Response(403, json={"detail": "permit_revoked"})}
        )
        async with client:
            with pytest.raises(PermitDeniedError):
                await client.discover_tools()

    @pytest.mark.asyncio
    async def test_403_plain_detail_is_authorization_error(self):
        client, _ = _recording(
            {
                ("GET", "/mcp/tools.json"): httpx.Response(
                    403, json={"detail": "wallet_access_denied"}
                )
            }
        )
        async with client:
            with pytest.raises(AuthorizationError):
                await client.discover_tools()

    @pytest.mark.asyncio
    async def test_409_is_idempotency_conflict(self):
        client, _ = _recording(
            {("GET", "/mcp/tools.json"): httpx.Response(409, json={"detail": "key_in_use"})}
        )
        async with client:
            with pytest.raises(IdempotencyConflictError):
                await client.discover_tools()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "detail"),
        [
            ({"detail": {"error": "boom"}}, "boom"),
            ({"detail": {"detail": "nested"}}, "nested"),
            ({"error": "bam"}, "bam"),
            ({}, "HTTP 500"),
        ],
    )
    async def test_error_detail_extraction_prefers_server_message(self, body, detail):
        client, _ = _recording({("GET", "/mcp/tools.json"): httpx.Response(500, json=body)})
        async with client:
            with pytest.raises(APIError) as exc_info:
                await client.discover_tools()
        assert exc_info.value.detail == detail
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_402_without_wallet_names_unknown_wallet(self):
        client, _ = _recording(
            {("GET", "/mcp/tools.json"): httpx.Response(402, json={"detail": {}})}
        )
        async with client:
            with pytest.raises(InsufficientFundsError) as exc_info:
                await client.discover_tools()
        assert exc_info.value.wallet_id == "unknown"


class TestDiscoveryAndPermitShapes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tools",
        [
            {"not": "a list"},
            ["not-a-dict"],
            [{"description": "missing name"}],
        ],
    )
    async def test_malformed_tools_response_is_api_error(self, tools):
        client, _ = _recording(
            {("GET", "/mcp/tools.json"): httpx.Response(200, json={"tools": tools})}
        )
        async with client:
            with pytest.raises(APIError, match="invalid_tools_response"):
                await client.discover_tools()

    @pytest.mark.asyncio
    async def test_create_permit_malformed_body_is_api_error(self):
        bad = _permit_dict()
        del bad["permit_id"]
        client, _ = _recording({("POST", "/v1/permits"): httpx.Response(201, json=bad)})
        from b2a_sdk.models import PermitRequest

        request = PermitRequest(
            issuer_wallet_id="wallet-1",
            subject_wallet_id="wallet-1",
            max_credits=Decimal("25"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        async with client:
            with pytest.raises(APIError, match="invalid_permit_response"):
                await client.create_permit(request, idempotency_key="k1")

    @pytest.mark.asyncio
    async def test_create_permit_overlong_key_rejected_before_send(self):
        client, seen = _recording({})
        from b2a_sdk.models import PermitRequest

        request = PermitRequest(
            issuer_wallet_id="wallet-1",
            subject_wallet_id="wallet-1",
            max_credits=Decimal("25"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        async with client:
            with pytest.raises(ValueError, match="at most 128"):
                await client.create_permit(request, idempotency_key="k" * 129)
        assert seen == []


class TestInvokeValidationAndErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_key", ["   ", "k" * 129])
    async def test_invoke_rejects_bad_idempotency_key_before_send(self, bad_key):
        client, seen = _recording({})
        async with client:
            with pytest.raises(ValueError):
                await client.invoke_tool(
                    TOOL, {}, wallet_id="wallet-1", permit_id="permit-1", idempotency_key=bad_key
                )
        assert seen == []

    @pytest.mark.asyncio
    async def test_uncertain_outcome_in_result_raises_with_receipt_id(self):
        client, _ = _recording(
            {
                ("POST", "/mcp/messages"): _ok_envelope(
                    receipt=_receipt(outcome="delivery_uncertain")
                )
            }
        )
        async with client:
            with pytest.raises(DeliveryUncertainError) as exc_info:
                await client.invoke_tool(
                    TOOL, {}, wallet_id="wallet-1", permit_id="permit-1", idempotency_key="k"
                )
        assert exc_info.value.receipt_id == "receipt-1"

    @pytest.mark.asyncio
    async def test_result_is_error_flag_is_api_error(self):
        client, _ = _recording(
            {("POST", "/mcp/messages"): _ok_envelope(isError=True, error="tool blew up")}
        )
        async with client:
            with pytest.raises(APIError, match="tool blew up"):
                await client.invoke_tool(
                    TOOL, {}, wallet_id="wallet-1", permit_id="permit-1", idempotency_key="k"
                )

    @pytest.mark.asyncio
    async def test_missing_receipt_is_api_error_not_crash(self):
        result = {"content": [{"type": "text", "text": "hi"}], "isError": False}
        client, _ = _recording(
            {("POST", "/mcp/messages"): httpx.Response(200, json={"result": result})}
        )
        async with client:
            with pytest.raises(APIError, match="mcp_missing_receipt"):
                await client.invoke_tool(
                    TOOL, {}, wallet_id="wallet-1", permit_id="permit-1", idempotency_key="k"
                )

    @pytest.mark.asyncio
    async def test_bad_content_shape_is_api_error(self):
        client, _ = _recording({("POST", "/mcp/messages"): _ok_envelope(content={"type": "text"})})
        async with client:
            with pytest.raises(APIError, match="invalid_mcp_content"):
                await client.invoke_tool(
                    TOOL, {}, wallet_id="wallet-1", permit_id="permit-1", idempotency_key="k"
                )

    @pytest.mark.asyncio
    async def test_bad_structured_content_is_api_error(self):
        client, _ = _recording(
            {("POST", "/mcp/messages"): _ok_envelope(structuredContent=["not", "a", "dict"])}
        )
        async with client:
            with pytest.raises(APIError, match="invalid_mcp_structured_content"):
                await client.invoke_tool(
                    TOOL, {}, wallet_id="wallet-1", permit_id="permit-1", idempotency_key="k"
                )

    @pytest.mark.asyncio
    async def test_uncertain_error_without_receipt_is_api_error(self):
        client, _ = _recording({("POST", "/mcp/messages"): _error_envelope("delivery_uncertain")})
        async with client:
            with pytest.raises(APIError, match="delivery_uncertain_without_receipt"):
                await client.invoke_tool(
                    TOOL, {}, wallet_id="wallet-1", permit_id="permit-1", idempotency_key="k"
                )

    @pytest.mark.asyncio
    async def test_jrpc_authorization_code_is_typed(self):
        client, _ = _recording(
            {("POST", "/mcp/messages"): _error_envelope("key_suspended", code=-32003)}
        )
        async with client:
            with pytest.raises(AuthorizationError):
                await client.invoke_tool(
                    TOOL, {}, wallet_id="wallet-1", permit_id="permit-1", idempotency_key="k"
                )

    @pytest.mark.asyncio
    async def test_unknown_jrpc_reason_is_plain_api_error(self):
        client, _ = _recording({("POST", "/mcp/messages"): _error_envelope("something_strange")})
        async with client:
            with pytest.raises(APIError, match="something_strange"):
                await client.invoke_tool(
                    TOOL, {}, wallet_id="wallet-1", permit_id="permit-1", idempotency_key="k"
                )

    @pytest.mark.asyncio
    async def test_insufficient_funds_without_receipt_has_no_receipt_id(self):
        client, _ = _recording({("POST", "/mcp/messages"): _error_envelope("insufficient_funds")})
        async with client:
            with pytest.raises(InsufficientFundsError) as exc_info:
                await client.invoke_tool(
                    TOOL, {}, wallet_id="wallet-1", permit_id="permit-1", idempotency_key="k"
                )
        assert exc_info.value.receipt_id is None
        assert exc_info.value.wallet_id == "wallet-1"


def _evidence_body(**overrides) -> dict:
    body = {
        "receipt_id": "receipt-1",
        "valid": True,
        "receipt": _receipt(),
        "permit": _permit_dict(),
        "ledger_entry": {"entry_id": "ledger-1"},
        "audit_event": {"event_id": "audit-1"},
        "verification": {"receipt_signature": "ok"},
        "dispatch": {"state": "succeeded"},
    }
    body.update(overrides)
    return body


class TestReceiptEvidenceEdges:
    @pytest.mark.asyncio
    async def test_verify_receipt_non_bool_valid_is_api_error(self):
        client, _ = _recording(
            {("POST", "/v1/receipts/verify"): httpx.Response(200, json={"valid": "yes"})}
        )
        async with client:
            with pytest.raises(APIError, match="invalid_receipt_verification"):
                await client.verify_receipt("receipt-1")

    @pytest.mark.asyncio
    async def test_verify_receipt_without_receipt_returns_none(self):
        client, _ = _recording(
            {
                ("POST", "/v1/receipts/verify"): httpx.Response(
                    200, json={"valid": False, "reason": "unknown_receipt"}
                )
            }
        )
        async with client:
            result = await client.verify_receipt("receipt-1")
        assert result.valid is False
        assert result.receipt is None

    @pytest.mark.asyncio
    async def test_get_receipt_malformed_body_is_api_error(self):
        bad = _receipt()
        del bad["signature"]
        client, _ = _recording({("GET", "/v1/receipts/receipt-1"): httpx.Response(200, json=bad)})
        async with client:
            with pytest.raises(APIError, match="invalid_get_receipt"):
                await client.get_receipt("receipt-1")

    @pytest.mark.asyncio
    async def test_get_evidence_without_permit_is_allowed(self):
        client, _ = _recording(
            {
                ("GET", "/v1/evidence/receipt-1"): httpx.Response(
                    200, json=_evidence_body(permit=None)
                )
            }
        )
        async with client:
            evidence = await client.get_evidence("receipt-1")
        assert evidence.permit is None
        assert evidence.valid is True

    @pytest.mark.asyncio
    async def test_get_evidence_missing_verification_is_api_error(self):
        body = _evidence_body()
        del body["verification"]
        client, _ = _recording({("GET", "/v1/evidence/receipt-1"): httpx.Response(200, json=body)})
        async with client:
            with pytest.raises(APIError, match="invalid_evidence_verification"):
                await client.get_evidence("receipt-1")

    @pytest.mark.asyncio
    async def test_get_evidence_non_bool_valid_is_api_error(self):
        client, _ = _recording(
            {
                ("GET", "/v1/evidence/receipt-1"): httpx.Response(
                    200, json=_evidence_body(valid="yes")
                )
            }
        )
        async with client:
            with pytest.raises(APIError, match="invalid_evidence_validity"):
                await client.get_evidence("receipt-1")

    @pytest.mark.asyncio
    async def test_get_evidence_malformed_permit_is_api_error(self):
        bad_permit = _permit_dict()
        del bad_permit["signature"]
        client, _ = _recording(
            {
                ("GET", "/v1/evidence/receipt-1"): httpx.Response(
                    200, json=_evidence_body(permit=bad_permit)
                )
            }
        )
        async with client:
            with pytest.raises(APIError, match="invalid_evidence_permit"):
                await client.get_evidence("receipt-1")

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True,
        reason="bug: get_evidence stringifies nested verification values, losing structure",
    )
    async def test_get_evidence_preserves_structured_verification(self):
        client, _ = _recording(
            {
                ("GET", "/v1/evidence/receipt-1"): httpx.Response(
                    200,
                    json=_evidence_body(
                        verification={"receipt_signature": {"sig": "abc", "kid": "k1"}}
                    ),
                )
            }
        )
        async with client:
            evidence = await client.get_evidence("receipt-1")
        assert evidence.verification["receipt_signature"] == {"sig": "abc", "kid": "k1"}


class TestModelsAndErrors:
    def test_permit_missing_field_raises_value_error(self):
        bad = _permit_dict()
        del bad["permit_id"]
        with pytest.raises(ValueError, match="permit_id"):
            Permit.from_dict(bad)

    def test_receipt_bad_datetime_raises_value_error(self):
        from b2a_sdk.models import Receipt

        with pytest.raises(ValueError, match="created_at") as exc_info:
            Receipt.from_dict(_receipt(created_at="not-a-time"))
        assert isinstance(exc_info.value.__cause__, ValueError)

    @pytest.mark.parametrize("field", ["expires_at", "issued_at", "revoked_at"])
    def test_permit_bad_datetime_names_field_and_preserves_cause(self, field):
        with pytest.raises(ValueError, match=field) as exc_info:
            Permit.from_dict(_permit_dict(**{field: "not-a-time"}))
        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_tool_definition_non_object_schema_raises(self):
        from b2a_sdk.models import ToolDefinition

        with pytest.raises(ValueError, match="inputSchema"):
            ToolDefinition.from_dict({"name": "t", "inputSchema": "nope"})

    def test_permit_request_nonce_round_trip(self):
        from b2a_sdk.models import PermitRequest

        base = {
            "issuer_wallet_id": "w1",
            "subject_wallet_id": "w2",
            "max_credits": Decimal("5"),
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        assert "nonce" not in PermitRequest(**base).to_payload()
        assert PermitRequest(**base, nonce="n1").to_payload()["nonce"] == "n1"

    def test_acp_checkout_request_payload_carries_spt(self):
        request = ACPCheckoutRequest(
            intent_id="int-1",
            line_items=[ACPLineItem(name="Widget", quantity=2, unit_amount=100, currency="usd")],
            spt_token="spt-secret",
            merchant_domain="example.com",
            client_total=200,
        )
        payload = request.to_payload()
        assert payload["spt_token"] == "spt-secret"
        assert payload["line_items"][0]["quantity"] == 2
        assert payload["client_total"] == 200

    @pytest.mark.asyncio
    async def test_acp_overlong_key_rejected_before_send(self):
        client, seen = _recording({})
        request = ACPCheckoutRequest(
            intent_id="int-1",
            line_items=[ACPLineItem(name="W", quantity=1, unit_amount=1, currency="usd")],
            spt_token="s",
            merchant_domain="example.com",
            client_total=1,
        )
        async with client:
            with pytest.raises(ValueError, match="at most 128"):
                await client.acp_checkout(
                    request,
                    sponsor_wallet_id="spn",
                    agent_wallet_id="agt",
                    idempotency_key="k" * 129,
                )
        assert seen == []

    def test_insufficient_funds_shortfall_forms(self):
        assert InsufficientFundsError("w", shortfall="12.5").shortfall == 12.5
        assert InsufficientFundsError("w", shortfall=None).shortfall is None
        assert InsufficientFundsError("w", shortfall="unknown").shortfall is None
        # A display field a server fills with garbage must not crash the client.
        assert InsufficientFundsError("w", shortfall="nonsense").shortfall is None
        error = InsufficientFundsError("w", shortfall=5.0, top_up_url="https://top/up")
        assert "https://top/up" in str(error)
        assert error.status_code == 402

    def test_permit_denied_and_uncertain_carry_ids_and_status(self):
        denied = PermitDeniedError("permit_revoked", receipt_id="r1")
        assert denied.status_code == 403
        assert denied.receipt_id == "r1"
        uncertain = DeliveryUncertainError(receipt_id="r2")
        assert uncertain.status_code == 504
        assert uncertain.receipt_id == "r2"


class TestBillableGuards:
    @pytest.mark.asyncio
    async def test_billable_sync_function_raises_without_charging(self):
        mock_client = MagicMock()
        mock_client.charge = AsyncMock()

        @billable(mock_client, wallet_id="w", service_category="s")
        def work():
            return "done"

        with pytest.raises(RuntimeError, match="@billable requires an async function"):
            await work()
        mock_client.charge.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("bad_key", "message"),
        [
            (None, "must return a string"),
            (123, "must return a string"),
            (True, "must return a string"),
            ("   ", "must not be blank"),
            ("k" * 129, "at most 128"),
            ("bad\nkey", "control characters"),
        ],
    )
    async def test_billable_invalid_factory_key_fails_before_charge(self, bad_key, message):
        """An invalid factory result must not send a charge or run the body."""
        client, seen = _recording({("POST", "/v1/billing/charge"): httpx.Response(200, json={})})
        ran = False
        factory_calls = 0

        def factory(job_id: str, *, retry: bool):
            nonlocal factory_calls
            factory_calls += 1
            assert (job_id, retry) == ("job-1", True)
            return bad_key

        @billable(
            client,
            wallet_id="w",
            service_category="s",
            idempotency_key_factory=factory,
        )
        async def work(job_id: str, *, retry: bool):
            nonlocal ran
            ran = True

        async with client:
            with pytest.raises(ValueError, match=message):
                await work("job-1", retry=True)
        assert factory_calls == 1
        assert seen == []
        assert ran is False

    @pytest.mark.asyncio
    async def test_billable_factory_called_once_and_key_sent(self):
        client, seen = _recording(
            {("POST", "/v1/billing/charge"): httpx.Response(200, json={"charged": True})}
        )
        factory_calls = 0
        body_calls = 0

        def factory(job_id: str) -> str:
            nonlocal factory_calls
            factory_calls += 1
            return f"charge-{job_id}"

        @billable(
            client,
            wallet_id="w",
            service_category="s",
            idempotency_key_factory=factory,
        )
        async def work(job_id: str) -> str:
            nonlocal body_calls
            body_calls += 1
            return job_id

        async with client:
            assert await work("job-1") == "job-1"
        assert factory_calls == 1
        assert body_calls == 1
        assert len(seen) == 1
        assert seen[0].headers["idempotency-key"] == "charge-job-1"

    @pytest.mark.asyncio
    async def test_billable_without_factory_omits_new_charge_keyword(self):
        class LegacyClient:
            def __init__(self):
                self.calls = 0

            async def charge(self, *, wallet_id, service_category, units, request_path):
                self.calls += 1

        client = LegacyClient()

        @billable(client, wallet_id="w", service_category="s")
        async def work():
            return "done"

        assert await work() == "done"
        assert client.calls == 1

    @pytest.mark.asyncio
    async def test_combined_charges_and_emits_telemetry(self):
        mock_client = MagicMock()
        mock_client.telemetry = AsyncMock()
        mock_client.charge = AsyncMock()

        @combined(mock_client, wallet_id="w", service_category="s", service_name="svc")
        async def work():
            return "ok"

        assert await work() == "ok"
        mock_client.charge.assert_awaited_once()
        await asyncio.sleep(0)
        mock_client.telemetry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_monitored_truncates_captured_args(self):
        mock_client = MagicMock()
        mock_client.telemetry = AsyncMock()

        @monitored(mock_client, service_name="svc", capture_args=True)
        async def work(payload):
            return "ok"

        assert await work("x" * 500) == "ok"
        await asyncio.sleep(0)
        kwargs = mock_client.telemetry.call_args.kwargs
        assert len(kwargs["args"]) <= 200

    def test_mcp_tool_registers_schema_and_metadata(self):
        seen: list = []
        register_mcp_tool_callback(
            lambda service_id, func, input_schema, output_schema: seen.append(
                (service_id, input_schema, output_schema)
            )
        )

        @mcp_tool(service_id="adder", category="math")
        async def add(a: int, b: int) -> dict:
            """Add two numbers."""
            return {"sum": a + b}

        assert add._b2a_mcp_metadata["service_id"] == "adder"
        assert add._b2a_mcp_metadata["category"] == "math"
        service_id, input_schema, _ = seen[-1]
        assert service_id == "adder"
        assert set(input_schema["properties"]) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_mcp_tool_async_passthrough(self):
        @mcp_tool(service_id="echo")
        async def echo(text: str) -> str:
            return text

        assert await echo("hi") == "hi"

    def test_mcp_registration_callback_failure_does_not_break_decoration(self):
        def bad_callback(*args, **kwargs):
            raise RuntimeError("registry down")

        register_mcp_tool_callback(bad_callback)

        @mcp_tool(service_id="resilient")
        async def work() -> str:
            return "ok"

        assert work._b2a_mcp_metadata["service_id"] == "resilient"

    @pytest.mark.asyncio
    @pytest.mark.xfail(strict=True, reason="bug: mcp_tool awaits sync functions, raising TypeError")
    async def test_mcp_tool_supports_sync_functions(self):
        @mcp_tool(service_id="sync-adder")
        def add(a: int, b: int) -> int:
            return a + b

        assert await add(1, 2) == 3


class TestDryRunSimulation:
    def test_accounting_and_copy_semantics(self):
        sim = DryRunSimulation(client=MagicMock(), wallet_id="w")
        sim.add_charge_result({"would_succeed": True, "credits_would_charge": 3})
        sim.add_charge_result({"would_succeed": False})
        assert sim.total_cost == 3
        assert sim.charge_count == 1
        assert sim.would_succeed is False
        sim.charges.append({})
        assert sim.charge_count == 1

    @pytest.mark.asyncio
    async def test_end_without_session_returns_summary(self):
        sim = DryRunSimulation(client=MagicMock(), wallet_id="w")
        sim.add_charge_result({"would_succeed": True, "credits_would_charge": 2})
        summary = await sim.end()
        assert summary["total_simulated_credits"] == 2
        assert summary["charge_count"] == 1
        assert summary["would_succeed"] is True

    @pytest.mark.asyncio
    async def test_end_delete_success_returns_server_summary(self):
        client, _ = _recording(
            {
                ("DELETE", "/v1/billing/dry-run/session/s1"): httpx.Response(
                    200, json={"closed": True}
                )
            }
        )
        async with client:
            sim = DryRunSimulation(client=client, wallet_id="w", session_id="s1")
            assert await sim.end() == {"closed": True}

    @pytest.mark.asyncio
    async def test_end_delete_failure_falls_back_to_local_summary(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("gone", request=request)

        client = AgentMiddlewareClient(
            api_key="k", base_url="http://test", transport=httpx.MockTransport(handler)
        )
        async with client:
            sim = DryRunSimulation(client=client, wallet_id="w", session_id="s1")
            summary = await sim.end()
        assert summary["session_id"] == "s1"
        assert summary["would_succeed"] is True

    @pytest.mark.asyncio
    async def test_simulate_session_never_hits_real_charge(self):
        paths: list[str] = []
        factory_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/v1/billing/dry-run/session":
                return httpx.Response(200, json={"session_id": "sess-1"})
            if request.url.path == "/v1/billing/dry-run/charge":
                body = json.loads(request.content)
                assert body["dry_run_session_id"] == "sess-1"
                return httpx.Response(200, json={"would_succeed": True, "credits_would_charge": 5})
            if request.url.path == "/v1/billing/dry-run/session/sess-1":
                return httpx.Response(200, json={"ok": True})
            raise AssertionError(f"unexpected path: {request.url.path}")

        client = AgentMiddlewareClient(
            api_key="k", base_url="http://test", transport=httpx.MockTransport(handler)
        )

        def factory() -> str:
            nonlocal factory_calls
            factory_calls += 1
            return "charge-job-1"

        @billable(
            client,
            wallet_id="w",
            service_category="s",
            units=5.0,
            idempotency_key_factory=factory,
        )
        async def work():
            return "done"

        async with client:
            async with client.simulate_session(wallet_id="w") as sim:
                assert await work() == "done"
            assert sim.total_cost == 5
            assert sim.charge_count == 1
            assert sim.would_succeed is True
        assert "/v1/billing/charge" not in paths
        assert factory_calls == 0

    @pytest.mark.asyncio
    async def test_simulate_charge_request_shape(self):
        client, seen = _recording(
            {
                ("POST", "/v1/billing/dry-run/charge"): httpx.Response(
                    200, json={"would_succeed": True}
                )
            }
        )
        async with client:
            await client.simulate_charge(
                wallet_id="w",
                service_category="s",
                units=2.0,
                session_id="sess-9",
                description="price check",
            )
            await client.get_dry_run_estimate("w", "s")
        first, second = (json.loads(request.content) for request in seen)
        assert first["dry_run_session_id"] == "sess-9"
        assert first["description"] == "price check"
        assert "dry_run_session_id" not in second


class TestB2AEdgeClient:
    def test_auth_headers_with_and_without_key(self):
        assert B2AEdgeClient(api_key="k")._auth_headers() == {"X-API-Key": "k"}
        assert B2AEdgeClient(api_key=None)._auth_headers() == {}
        assert B2AEdgeClient(api_key="k")._headers() == {
            "Content-Type": "application/json",
            "X-API-Key": "k",
        }
        assert "X-API-Key" not in B2AEdgeClient(api_key=None)._headers()

    @pytest.mark.asyncio
    async def test_get_mcp_tools_sends_key_and_returns_tools(self):
        client, seen = _edge_client(
            {("GET", "/mcp/tools.json"): httpx.Response(200, json={"tools": [{"name": "t"}]})}
        )
        try:
            assert await client.get_mcp_tools() == [{"name": "t"}]
        finally:
            await client.close()
        assert seen[0].headers["x-api-key"] == "edge-key"

    @pytest.mark.asyncio
    async def test_call_mcp_tool_scopes_wallet_context(self):
        client, seen = _edge_client(
            {("POST", "/mcp/messages"): httpx.Response(200, json={"result": "ok"})}
        )
        try:
            await client.call_mcp_tool("search", {"q": "x"})
        finally:
            await client.close()
        body = json.loads(seen[0].content)
        assert body["method"] == "tools/call"
        assert body["params"]["mcpContext"] == {"wallet_id": "wallet-1"}
        assert body["id"] == 1

    @pytest.mark.asyncio
    async def test_call_mcp_tool_without_wallet_omits_context(self):
        client, _ = _edge_client(
            {("POST", "/mcp/messages"): httpx.Response(200, json={"result": "ok"})}
        )
        client.wallet_id = None
        try:
            result = await client.call_mcp_tool("search", {"q": "x"})
        finally:
            await client.close()
        assert result == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_create_awi_session_posts_target_and_steps(self):
        client, seen = _edge_client(
            {("POST", "/v1/awi/sessions"): httpx.Response(201, json={"session_id": "s"})}
        )
        try:
            await client.create_awi_session("https://example.com", max_steps=10)
        finally:
            await client.close()
        assert json.loads(seen[0].content) == {"target_url": "https://example.com", "max_steps": 10}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("permit_id", "key"),
        [("   ", "k1"), ("p1", "   "), ("p1", "k" * 129)],
    )
    async def test_execute_awi_action_validates_before_send(self, permit_id, key):
        client, seen = _edge_client({})
        try:
            with pytest.raises(ValueError):
                await client.execute_awi_action(
                    "s", "click", {}, permit_id=permit_id, idempotency_key=key
                )
        finally:
            await client.close()
        assert seen == []

    @pytest.mark.asyncio
    async def test_execute_awi_action_forwards_governance_headers(self):
        client, seen = _edge_client(
            {("POST", "/v1/awi/execute"): httpx.Response(200, json={"ok": True})}
        )
        try:
            result = await client.execute_awi_action(
                "s", "click", {"x": 1}, permit_id="p1", idempotency_key="k1"
            )
        finally:
            await client.close()
        assert result == {"ok": True}
        assert seen[0].headers["x-permit-id"] == "p1"
        assert seen[0].headers["idempotency-key"] == "k1"

    @pytest.mark.asyncio
    async def test_edge_balance_requires_wallet_and_reads_balance(self):
        client, _ = _edge_client(
            {("GET", "/v1/billing/wallets/wallet-1"): httpx.Response(200, json={"balance": 42.0})}
        )
        try:
            assert await client.get_balance() == 42.0
            client.wallet_id = None
            with pytest.raises(ValueError, match="wallet_id required"):
                await client.get_balance()
        finally:
            await client.close()

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True, reason="bug: edge get_balance reports 0.0 when the server omits balance"
    )
    async def test_edge_balance_missing_field_is_not_silent_zero(self):
        client, _ = _edge_client(
            {("GET", "/v1/billing/wallets/wallet-1"): httpx.Response(200, json={})}
        )
        try:
            assert await client.get_balance() is None
        finally:
            await client.close()


class TestLocalValidatorAndSession:
    @pytest.mark.xfail(
        strict=True,
        reason="bug: check raises TypeError on non-list scopes instead of fail-closed denial",
    )
    def test_check_malformed_scopes_fails_closed(self):
        validator = LocalPermitValidator(_permit_dict(scopes=5), {})
        decision = validator.check(TOOL, Decimal("1"))
        assert decision.allowed is False

    @pytest.mark.asyncio
    async def test_session_open_with_pinned_keys_skips_key_fetch(self):
        permit = _permit_dict()
        paths: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json=permit)

        client = AgentMiddlewareClient(
            api_key="k", base_url="http://test", transport=httpx.MockTransport(handler)
        )
        try:
            with patch.object(LocalPermitValidator, "verify_permit", return_value=True):
                session = await GovernedEdgeSession.open(
                    client,
                    permit_id="permit-1",
                    wallet_id="wallet-1",
                    trust_keys_document={"keys": []},
                )
            assert session.permit_id == "permit-1"
        finally:
            await client.close()
        assert paths == ["/v1/permits/permit-1"]

    @pytest.mark.asyncio
    async def test_session_open_empty_key_set_denies_signature(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/permits/permit-1":
                return httpx.Response(200, json=_permit_dict())
            return httpx.Response(200, json={"keys": []})

        client = AgentMiddlewareClient(
            api_key="k", base_url="http://test", transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(PermitDeniedError) as exc_info:
                await GovernedEdgeSession.open(client, permit_id="permit-1", wallet_id="wallet-1")
            assert exc_info.value.reason == "permit_signature_invalid"
        finally:
            await client.close()


def _signed_cli_bundle(tmp_path, *, kid="cli-key-1"):
    """A genuinely signed bundle plus its key document (needs cryptography)."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    payload = {"kid": kid, "receipt_id": "rcpt-cli-1", "outcome": "success"}
    signing_input = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = private.sign(signing_input.encode("utf-8"))
    bundle = {
        "receipt_id": "rcpt-cli-1",
        "kid": kid,
        "alg": "Ed25519",
        "canonicalization": "awi-canonical-json/1",
        "signing_input": signing_input,
        "signature": base64.b64encode(signature).decode(),
        "issuer": "https://api.example.com",
    }
    key_doc = {
        "keys": [
            {
                "kid": kid,
                "alg": "Ed25519",
                "status": "active",
                "public_key_b64": base64.b64encode(
                    private.public_key().public_bytes_raw()
                ).decode(),
            }
        ]
    }
    bundle_path = tmp_path / "bundle.json"
    keys_path = tmp_path / "keys.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    keys_path.write_text(json.dumps(key_doc), encoding="utf-8")
    return bundle_path, keys_path


class TestVerifyCli:
    def test_unreadable_bundle_path_is_undetermined(self, tmp_path):
        assert (
            verify_cli.main(
                ["--bundle", str(tmp_path / "missing.json"), "--keys", str(tmp_path / "k.json")]
            )
            == verify_cli.EXIT_UNDETERMINED
        )

    def test_malformed_keys_document_is_undetermined(self, tmp_path):
        bundle_path = tmp_path / "bundle.json"
        bundle_path.write_text("{}", encoding="utf-8")
        keys_path = tmp_path / "keys.json"
        keys_path.write_text('{"nope": []}', encoding="utf-8")
        assert (
            verify_cli.main(["--bundle", str(bundle_path), "--keys", str(keys_path)])
            == verify_cli.EXIT_UNDETERMINED
        )

    def test_verified_bundle_exits_zero(self, tmp_path, capsys):
        bundle_path, keys_path = _signed_cli_bundle(tmp_path)
        assert (
            verify_cli.main(["--bundle", str(bundle_path), "--keys", str(keys_path)])
            == verify_cli.EXIT_VERIFIED
        )
        assert "VERIFIED" in capsys.readouterr().out

    def test_verified_bundle_json_output_shape(self, tmp_path, capsys):
        bundle_path, keys_path = _signed_cli_bundle(tmp_path)
        assert (
            verify_cli.main(["--bundle", str(bundle_path), "--keys", str(keys_path), "--json"])
            == verify_cli.EXIT_VERIFIED
        )
        document = json.loads(capsys.readouterr().out)
        assert document["status"] == "verified"
        assert document["ok"] is True
        assert document["receipt_id"] == "rcpt-cli-1"

    def test_tampered_bundle_exits_invalid(self, tmp_path):
        bundle_path, keys_path = _signed_cli_bundle(tmp_path)
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle["signing_input"] = bundle["signing_input"].replace(
            '"outcome":"success"', '"outcome":"denied"'
        )
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        assert (
            verify_cli.main(["--bundle", str(bundle_path), "--keys", str(keys_path)])
            == verify_cli.EXIT_INVALID
        )

    def test_unknown_key_exits_undetermined(self, tmp_path):
        bundle_path, _ = _signed_cli_bundle(tmp_path)
        keys_path = tmp_path / "empty-keys.json"
        keys_path.write_text('{"keys": []}', encoding="utf-8")
        assert (
            verify_cli.main(["--bundle", str(bundle_path), "--keys", str(keys_path)])
            == verify_cli.EXIT_UNDETERMINED
        )

    def test_issuer_mismatch_exits_invalid(self, tmp_path):
        bundle_path, keys_path = _signed_cli_bundle(tmp_path)
        assert (
            verify_cli.main(
                [
                    "--bundle",
                    str(bundle_path),
                    "--keys",
                    str(keys_path),
                    "--expect-issuer",
                    "https://other.example.com",
                ]
            )
            == verify_cli.EXIT_INVALID
        )

    def test_verifier_failure_exits_undetermined(self, tmp_path, monkeypatch, capsys):
        bundle_path = tmp_path / "bundle.json"
        bundle_path.write_text("{}", encoding="utf-8")
        keys_path = tmp_path / "keys.json"
        keys_path.write_text('{"keys": []}', encoding="utf-8")

        def boom(bundle, key_set, expected_issuer=None):
            raise VerificationError("backend failed")

        monkeypatch.setattr(verify_cli, "verify_bundle", boom)
        assert (
            verify_cli.main(["--bundle", str(bundle_path), "--keys", str(keys_path)])
            == verify_cli.EXIT_UNDETERMINED
        )
        assert "could not run" in capsys.readouterr().err

    def test_issuer_fetch_uses_well_known_path(self, tmp_path, monkeypatch):
        bundle_path, keys_path = _signed_cli_bundle(tmp_path)
        key_document = json.loads(keys_path.read_text(encoding="utf-8"))
        fetched: list[str] = []

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return key_document

        def fake_get(url, *, timeout, follow_redirects):
            assert timeout > 0
            assert follow_redirects is False
            fetched.append(url)
            return FakeResponse()

        import httpx as httpx_module

        monkeypatch.setattr(httpx_module, "get", fake_get)
        assert (
            verify_cli.main(["--bundle", str(bundle_path), "--issuer", "https://api.example.com/"])
            == verify_cli.EXIT_VERIFIED
        )
        assert fetched == ["https://api.example.com/.well-known/trust-keys.json"]


class TestMcpManifest:
    def test_generate_manifest_builds_url_and_writes_file(self, tmp_path, monkeypatch):
        seen: list[str] = []

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"tools": [{"name": "t"}]}

        def fake_get(url, *, timeout, follow_redirects):
            assert timeout > 0
            assert follow_redirects is False
            seen.append(url)
            return FakeResponse()

        monkeypatch.setattr(mcp_module.httpx, "get", fake_get)
        output = tmp_path / "tools.json"
        manifest = mcp_module.generate_manifest(
            output_path=str(output), api_url="http://api", category="search"
        )
        assert manifest == {"tools": [{"name": "t"}]}
        assert seen == ["http://api/mcp/tools.json?category=search"]
        assert json.loads(output.read_text()) == {"tools": [{"name": "t"}]}
