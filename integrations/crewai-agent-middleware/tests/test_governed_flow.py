"""Tests for governed permit→invoke→receipt flow in CrewAI wrapper."""

import ast
import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from b2a_sdk.errors import (
    AuthenticationError,
    AuthorizationError,
    DeliveryUncertainError,
    IdempotencyConflictError,
    InsufficientFundsError,
    PermitDeniedError,
)
from crewai.agents.parser import AgentAction
from crewai.tools.tool_failure import ToolExecutionFailedError
from crewai.utilities.tool_utils import (
    aexecute_tool_and_check_finality,
    execute_tool_and_check_finality,
)

from crewai_b2a import B2AClient, CrewAIB2ATool


def _permit_payload() -> dict:
    return {
        "permit_id": "permit-1",
        "issuer_wallet_id": "wallet-1",
        "subject_wallet_id": "wallet-1",
        "subject_key_id": "key-1",
        "scopes": ["tool:partner.search:invoke", "billing:charge"],
        "allowed_tools": ["partner.search"],
        "max_credits": "100",
        "spent_credits": "0",
        "expires_at": datetime.now(UTC).isoformat(),
        "nonce": "nonce-1",
        "status": "active",
        "signature": "sig-permit-1",
        "key_id": "key-1",
        "issued_at": datetime.now(UTC).isoformat(),
        "revoked_at": None,
    }


def _receipt_payload(outcome: str = "success") -> dict:
    # Mirrors the receipt shape b2a_sdk.models.Receipt.from_dict requires
    # (see b2a_sdk/tests/test_trust_client.py).
    return {
        "receipt_id": f"receipt-{outcome}",
        "permit_id": "permit-1",
        "wallet_id": "wallet-1",
        "key_id": "key-1",
        "tool": "partner.search",
        "request_hash": "request-hash",
        "response_hash": "response-hash",
        "ledger_entry_id": "ledger-1",
        "dispatch_attempt_id": "dispatch-1",
        "credits_authorized": "2",
        "credits_charged": "2",
        "outcome": outcome,
        "audit_event_id": "audit-1",
        "created_at": datetime.now(UTC).isoformat(),
        "signature": f"sig-{outcome}",
        "signature_key_id": "signing-key-1",
    }


@pytest.mark.asyncio
async def test_call_tool_requires_idempotency_key():
    """Test that idempotency_key is required and must not be blank."""
    permit_called = False
    invoke_called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal permit_called, invoke_called

        if request.url.path == "/v1/permits":
            permit_called = True
            assert request.headers["idempotency-key"] == "permit-invoke-key-1"
            return httpx.Response(201, json=_permit_payload())

        if request.url.path == "/mcp/messages":
            invoke_called = True
            assert request.headers["idempotency-key"] == "invoke-key-1"
            body = json.loads(request.content)
            assert body["params"]["mcpContext"]["idempotency_key"] == "invoke-key-1"
            assert body["params"]["mcpContext"]["permit_id"] == "permit-1"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "invoke-key-1",
                    "result": {
                        "content": [{"type": "text", "text": '{"ok": true}'}],
                        "structuredContent": {"ok": True},
                        "isError": False,
                        "receipt": _receipt_payload(),
                    },
                },
            )

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    base_client = B2AClient(api_key="test-key", transport=transport)
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    tool.client = base_client

    result = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="invoke-key-1",
        permit_idempotency_key="permit-invoke-key-1",
        arguments={"query": "test"},
    )

    assert permit_called
    assert invoke_called
    assert "receipt-success" in result

    result_blank = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="   ",
        permit_idempotency_key="permit-key",
        arguments={},
    )
    assert "Error: idempotency_key is required" in result_blank

    await base_client.close()


class _IdempotentGateway:
    """Mock gateway that enforces idempotency the way the real server does.

    The first request under a key does the work (creates the permit, or charges
    the call) and stores its response with a hash of the request. An identical
    replay gets the stored response without new work; the same key with a
    changed request gets the server's conflict (HTTP 409 for permits, a
    JSON-RPC ``idempotency_key_reused`` error for MCP calls).
    """

    def __init__(self) -> None:
        self.permit_requests = 0
        self.charges = 0
        # (Idempotency-Key header, mcpContext.idempotency_key) per MCP request.
        self.invoke_keys: list[tuple[str, str]] = []
        self._permits: dict[str, tuple[str, dict]] = {}
        self._invocations: dict[str, tuple[str, dict]] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        key = request.headers["idempotency-key"]

        if request.url.path == "/v1/permits":
            self.permit_requests += 1
            request_hash = json.dumps(body, sort_keys=True)
            stored = self._permits.setdefault(key, (request_hash, _permit_payload()))
            if stored[0] != request_hash:
                return httpx.Response(409, json={"detail": "idempotency_key_reused"})
            return httpx.Response(201, json=stored[1])

        if request.url.path == "/mcp/messages":
            params = body["params"]
            self.invoke_keys.append((key, params["mcpContext"]["idempotency_key"]))
            request_hash = json.dumps(
                {"name": params["name"], "arguments": params["arguments"]},
                sort_keys=True,
            )
            if key not in self._invocations:
                self.charges += 1
                receipt = _receipt_payload()
                receipt["receipt_id"] = f"receipt-charge-{self.charges}"
                self._invocations[key] = (
                    request_hash,
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "content": [{"type": "text", "text": '{"ok": true}'}],
                            "structuredContent": {"ok": True},
                            "isError": False,
                            "receipt": receipt,
                        },
                    },
                )
            stored_hash, stored_response = self._invocations[key]
            if stored_hash != request_hash:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32603, "message": "idempotency_key_reused"},
                    },
                )
            return httpx.Response(200, json=stored_response)

        return httpx.Response(404)


@pytest.mark.asyncio
async def test_identical_replay_forwards_same_key_and_charges_once():
    """An identical replay reuses the permit and key, so the call is charged once."""
    gateway = _IdempotentGateway()
    base_client = B2AClient(api_key="test-key", transport=httpx.MockTransport(gateway))
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    tool.client = base_client

    call = {
        "operation": "call_tool",
        "tool_name": "partner.search",
        "idempotency_key": "replay-key",
        "permit_idempotency_key": "permit-replay-key",
        "arguments": {"query": "same"},
    }
    first = await tool._arun(**call)
    replay = await tool._arun(**call)

    assert gateway.invoke_keys == [("replay-key", "replay-key")] * 2
    assert gateway.permit_requests == 1
    assert gateway.charges == 1
    assert ast.literal_eval(first)["receipt_id"] == "receipt-charge-1"
    assert ast.literal_eval(replay)["receipt_id"] == "receipt-charge-1"

    await base_client.close()


@pytest.mark.asyncio
async def test_replay_with_changed_arguments_returns_idempotency_conflict():
    """Reusing an invocation key with changed arguments is a conflict, not a replay."""
    gateway = _IdempotentGateway()
    base_client = B2AClient(api_key="test-key", transport=httpx.MockTransport(gateway))
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    tool.client = base_client

    first = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="replay-key",
        permit_idempotency_key="permit-replay-key",
        arguments={"query": "first"},
    )
    with pytest.raises(IdempotencyConflictError) as exc_info:
        await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="replay-key",
            permit_idempotency_key="permit-replay-key",
            arguments={"query": "second"},
        )

    assert gateway.invoke_keys == [("replay-key", "replay-key")] * 2
    assert gateway.permit_requests == 1
    assert gateway.charges == 1
    assert ast.literal_eval(first)["receipt_id"] == "receipt-charge-1"
    assert "idempotency_key_reused" in str(exc_info.value)

    await base_client.close()


@pytest.mark.asyncio
async def test_signed_receipt_returned():
    """Test that signed receipts are returned with signature and credits charged."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())

        if request.url.path == "/mcp/messages":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "signed-key",
                    "result": {
                        "content": [{"type": "text", "text": '{"status": "ok"}'}],
                        "structuredContent": {"status": "ok"},
                        "isError": False,
                        "receipt": _receipt_payload(),
                    },
                },
            )

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    base_client = B2AClient(api_key="test-key", transport=transport)
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    tool.client = base_client

    result = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="signed-key",
        permit_idempotency_key="permit-signed-key",
        arguments={},
    )

    assert "receipt_id" in result
    assert "signature" in result
    assert "credits_charged" in result
    assert "sig-success" in result
    assert "'2'" in result

    await base_client.close()


def _governed_tool(handler) -> tuple[CrewAIB2ATool, B2AClient]:
    client = B2AClient(api_key="test-key", transport=httpx.MockTransport(handler))
    tool = CrewAIB2ATool(
        api_key="test-key",
        wallet_id="wallet-1",
        sync_client_factory=lambda: B2AClient(
            api_key="test-key", transport=httpx.MockTransport(handler)
        ),
    )
    tool.client = client
    return tool, client


def _mcp_error_handler(reason: str, outcome: str):
    """Permit succeeds, then the invoke answers with a typed gateway error."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "billing-key",
                    "error": {
                        "code": -32003,
                        "message": reason,
                        "data": {"receipt": _receipt_payload(outcome)},
                    },
                },
            )
        return httpx.Response(404)

    return handler


_BILLING_CALL = {
    "operation": "call_tool",
    "tool_name": "partner.search",
    "idempotency_key": "billing-key",
    "permit_idempotency_key": "permit-billing-key",
    "arguments": {"query": "test"},
}


def _assert_terminal_billing_error(exc: Exception, error_type: type[Exception]) -> None:
    """The raised object keeps the fields a caller needs to decide not to retry."""
    if error_type is InsufficientFundsError:
        assert isinstance(exc, InsufficientFundsError)
        assert exc.wallet_id == "wallet-1"
    elif error_type is PermitDeniedError:
        assert isinstance(exc, PermitDeniedError)
        assert exc.reason == "permit_tool_not_allowed"
    elif error_type is DeliveryUncertainError:
        assert isinstance(exc, DeliveryUncertainError)
        assert exc.receipt_id == "receipt-delivery_uncertain"
    elif error_type is IdempotencyConflictError:
        assert isinstance(exc, IdempotencyConflictError)
        assert "idempotency_key_reused" in str(exc)
    else:
        raise AssertionError(f"unexpected error type {error_type}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "outcome", "error_type"),
    [
        ("insufficient_funds", "insufficient_funds", InsufficientFundsError),
        ("permit_tool_not_allowed", "denied", PermitDeniedError),
        ("delivery_uncertain", "delivery_uncertain", DeliveryUncertainError),
        ("idempotency_key_reused", "denied", IdempotencyConflictError),
    ],
    ids=[
        "insufficient_funds",
        "permit_denied",
        "delivery_uncertain",
        "idempotency_conflict",
    ],
)
async def test_arun_raises_terminal_billing_errors(reason, outcome, error_type):
    """Invoke failures that must not be retried stay typed exceptions."""
    tool, client = _governed_tool(_mcp_error_handler(reason, outcome))
    with pytest.raises(error_type) as exc_info:
        await tool._arun(**_BILLING_CALL)
    _assert_terminal_billing_error(exc_info.value, error_type)
    await client.close()


def _run_on_fresh_loop(tool: CrewAIB2ATool, client: B2AClient, **kwargs):
    """Call the sync wrapper on its private loop, then close both clients."""
    try:
        return tool._run(**kwargs)
    finally:
        tool.close()
        asyncio.run(client.close())


@pytest.mark.parametrize(
    ("reason", "outcome", "error_type"),
    [
        ("insufficient_funds", "insufficient_funds", InsufficientFundsError),
        ("permit_tool_not_allowed", "denied", PermitDeniedError),
        ("delivery_uncertain", "delivery_uncertain", DeliveryUncertainError),
        ("idempotency_key_reused", "denied", IdempotencyConflictError),
    ],
    ids=[
        "insufficient_funds",
        "permit_denied",
        "delivery_uncertain",
        "idempotency_conflict",
    ],
)
def test_run_raises_terminal_billing_errors(reason, outcome, error_type):
    """The sync wrapper raises the same terminal billing errors as the async one."""
    tool, client = _governed_tool(_mcp_error_handler(reason, outcome))
    with pytest.raises(error_type) as exc_info:
        _run_on_fresh_loop(tool, client, **_BILLING_CALL)
    _assert_terminal_billing_error(exc_info.value, error_type)


def _permit_insufficient_funds_handler():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            return httpx.Response(
                402,
                json={
                    "detail": {
                        "shortfall": 3,
                        "top_up_url": "https://billing.example/top-up",
                    }
                },
            )
        return httpx.Response(404)

    return handler


def _assert_permit_insufficient_funds(exc: InsufficientFundsError) -> None:
    assert exc.wallet_id == "wallet-1"
    assert exc.shortfall == 3.0
    assert exc.top_up_url == "https://billing.example/top-up"
    assert exc.status_code == 402


@pytest.mark.asyncio
async def test_arun_raises_insufficient_funds_when_permit_create_is_rejected():
    """A 402 while creating the permit is an insufficient-funds error, not text."""
    tool, client = _governed_tool(_permit_insufficient_funds_handler())
    with pytest.raises(InsufficientFundsError) as exc_info:
        await tool._arun(**_BILLING_CALL)
    _assert_permit_insufficient_funds(exc_info.value)
    await client.close()


def test_run_raises_insufficient_funds_when_permit_create_is_rejected():
    """The sync wrapper raises the same 402 instead of returning Error text."""
    tool, client = _governed_tool(_permit_insufficient_funds_handler())
    with pytest.raises(InsufficientFundsError) as exc_info:
        _run_on_fresh_loop(tool, client, **_BILLING_CALL)
    _assert_permit_insufficient_funds(exc_info.value)


def _connection_reset_handler():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    return handler


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, AuthenticationError), (403, AuthorizationError)],
)
@pytest.mark.asyncio
async def test_arun_propagates_gateway_auth_denial(status, error_type):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/permits"
        return httpx.Response(status, json={"detail": "access denied"})

    tool, client = _governed_tool(handler)
    try:
        with pytest.raises(error_type):
            await tool._arun(**_BILLING_CALL)
    finally:
        await client.close()
    assert calls == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, AuthenticationError), (403, AuthorizationError)],
)
def test_run_propagates_gateway_auth_denial(status, error_type):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/permits"
        return httpx.Response(status, json={"detail": "access denied"})

    tool, client = _governed_tool(handler)
    with pytest.raises(error_type):
        _run_on_fresh_loop(tool, client, **_BILLING_CALL)
    assert calls == 1


@pytest.mark.parametrize(
    ("reason", "outcome", "error_type"),
    [
        ("insufficient_funds", "insufficient_funds", InsufficientFundsError),
        ("permit_tool_not_allowed", "denied", PermitDeniedError),
        ("delivery_uncertain", "delivery_uncertain", DeliveryUncertainError),
        ("idempotency_key_reused", "denied", IdempotencyConflictError),
    ],
)
def test_crewai_dispatch_stops_on_terminal_gateway_error(reason, outcome, error_type):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/mcp/messages":
            calls += 1
        return await _mcp_error_handler(reason, outcome)(request)

    tool, client = _governed_tool(handler)
    structured_tool = tool.to_structured_tool()
    call_json = json.dumps(_BILLING_CALL)
    action = AgentAction(
        thought="",
        tool=tool.name,
        tool_input=call_json,
        text=f"Action: {tool.name}\nAction Input: {call_json}",
    )
    try:
        with pytest.raises(ToolExecutionFailedError) as exc_info:
            execute_tool_and_check_finality(action, [structured_tool])
    finally:
        tool.close()
        asyncio.run(client.close())
    assert calls == 1
    assert exc_info.value.record.failure.code == error_type.__name__
    assert not exc_info.value.record.failure.retryable


@pytest.mark.asyncio
async def test_crewai_async_dispatch_stops_on_delivery_uncertain():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/mcp/messages":
            calls += 1
        return await _mcp_error_handler("delivery_uncertain", "delivery_uncertain")(
            request
        )

    tool, client = _governed_tool(handler)
    call_json = json.dumps(_BILLING_CALL)
    action = AgentAction(
        thought="",
        tool=tool.name,
        tool_input=call_json,
        text=f"Action: {tool.name}\nAction Input: {call_json}",
    )
    try:
        with pytest.raises(ToolExecutionFailedError) as exc_info:
            await aexecute_tool_and_check_finality(action, [tool.to_structured_tool()])
    finally:
        tool.close()
        await client.close()
    assert calls == 1
    assert exc_info.value.record.failure.code == "DeliveryUncertainError"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, AuthenticationError), (403, AuthorizationError)],
)
def test_crewai_dispatch_stops_on_auth_denial(status, error_type):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/permits"
        return httpx.Response(status, json={"detail": "access denied"})

    tool, client = _governed_tool(handler)
    call_json = json.dumps(_BILLING_CALL)
    action = AgentAction(
        thought="",
        tool=tool.name,
        tool_input=call_json,
        text=f"Action: {tool.name}\nAction Input: {call_json}",
    )
    try:
        with pytest.raises(ToolExecutionFailedError) as exc_info:
            execute_tool_and_check_finality(action, [tool.to_structured_tool()])
    finally:
        tool.close()
        asyncio.run(client.close())
    assert calls == 1
    assert exc_info.value.record.failure.code == error_type.__name__


@pytest.mark.asyncio
async def test_arun_keeps_retryable_transport_failure_as_error_text():
    """A dropped connection is still text, so it does not look like a billing refusal."""
    tool, client = _governed_tool(_connection_reset_handler())
    result = await tool._arun(operation="discover_tools")
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "connection reset" in result
    await client.close()


def test_run_keeps_retryable_transport_failure_as_error_text():
    """The sync wrapper also leaves a dropped connection as retryable text."""
    tool, client = _governed_tool(_connection_reset_handler())
    result = _run_on_fresh_loop(tool, client, operation="discover_tools")
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "connection reset" in result
