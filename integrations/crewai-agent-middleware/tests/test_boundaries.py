"""Edge cases for the CrewAI governed tool: sync calls, auth, and permit terms."""

import asyncio
import json
import threading
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
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


def _invoke_response(outcome: str = "success") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "invoke",
        "result": {
            "content": [{"type": "text", "text": '{"ok": true}'}],
            "structuredContent": {"ok": True},
            "isError": False,
            "receipt": _receipt_payload(outcome),
        },
    }


def _tool(transport: httpx.MockTransport, **kwargs) -> tuple[CrewAIB2ATool, B2AClient]:
    api_key = kwargs.pop("api_key", "test-key")
    wallet_id = kwargs.pop("wallet_id", "wallet-1")
    tool = CrewAIB2ATool(
        api_key=api_key,
        wallet_id=wallet_id,
        base_url="https://middleware.test",
        **kwargs,
    )
    client = B2AClient(
        api_key=api_key,
        base_url="https://middleware.test",
        transport=transport,
    )
    tool.client = client
    return tool, client


def _shutdown(tool: CrewAIB2ATool, client: B2AClient) -> None:
    """Close the HTTP client on the loop that used it, then stop that loop."""
    loop = getattr(tool, "_sync_loop", None)
    if loop is not None and not loop.is_closed():
        try:
            loop.run_until_complete(client.close())
        except RuntimeError:
            pass
    closer = getattr(tool, "close", None)
    if callable(closer):
        closer()
        return
    try:
        asyncio.run(client.close())
    except RuntimeError:
        pass


def test_api_key_is_not_visible_in_repr_or_dump():
    tool = CrewAIB2ATool(api_key="super-secret-key", wallet_id="wallet-1")
    assert "super-secret-key" not in repr(tool)
    assert "super-secret-key" not in str(tool.model_dump())
    copied = tool.model_copy()
    assert copied.api_key == "super-secret-key"
    assert "super-secret-key" not in repr(copied)


def test_blank_api_key_and_wallet_are_rejected():
    with pytest.raises(ValueError, match="api_key"):
        CrewAIB2ATool(api_key="   ", wallet_id="wallet-1")
    with pytest.raises(ValueError, match="wallet_id"):
        CrewAIB2ATool(api_key="test-key", wallet_id="")


def test_agent_schema_includes_call_fields():
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    assert tool.args_schema is not None
    fields = set(tool.args_schema.model_fields)
    assert {
        "operation",
        "tool_name",
        "idempotency_key",
        "permit_idempotency_key",
        "arguments",
    } <= fields


def test_sync_run_calls_the_api_twice_without_a_running_loop():
    """CrewAI calls run() from a worker thread. It must work twice on one client."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_invoke_response())
        return httpx.Response(404, json={"detail": "missing"})

    tool, client = _tool(httpx.MockTransport(handler))
    call = {
        "operation": "call_tool",
        "tool_name": "partner.search",
        "idempotency_key": "invoke-sync",
        "permit_idempotency_key": "permit-sync",
        "arguments": {"query": "same"},
    }
    box: dict[str, str] = {}

    def work() -> None:
        try:
            box["first"] = tool.run(**call)
            box["second"] = tool.run(**call)
        finally:
            _shutdown(tool, client)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()

    first = box["first"]
    second = box["second"]
    assert "receipt-success" in first
    assert first == second
    assert calls.count("/v1/permits") == 1
    assert calls.count("/mcp/messages") == 2


@pytest.mark.asyncio
async def test_missing_tool_name_does_not_create_a_permit():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500, json={"detail": "should not be called"})

    tool, client = _tool(httpx.MockTransport(handler))
    try:
        missing = await tool._arun(
            operation="call_tool",
            idempotency_key="invoke-1",
            permit_idempotency_key="permit-1",
        )
        blank = await tool._arun(
            operation="call_tool",
            tool_name="   ",
            idempotency_key="invoke-1",
            permit_idempotency_key="permit-1",
        )
    finally:
        await client.close()

    assert seen == []
    assert missing == "Error: tool_name is required and must not be blank"
    assert blank == "Error: tool_name is required and must not be blank"


@pytest.mark.asyncio
async def test_overlong_invoke_key_does_not_create_a_permit():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(201, json=_permit_payload())

    tool, client = _tool(httpx.MockTransport(handler))
    try:
        result = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="k" * 129,
            permit_idempotency_key="permit-1",
            arguments={},
        )
    finally:
        await client.close()

    assert seen == []
    assert "128" in result
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_non_object_arguments_do_not_create_a_permit():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            body = json.loads(request.content)
            if body["params"]["arguments"] != {}:
                return httpx.Response(400, json={"detail": "arguments must be an object"})
            return httpx.Response(200, json=_invoke_response())
        return httpx.Response(404, json={"detail": "missing"})

    tool, client = _tool(httpx.MockTransport(handler))
    try:
        result = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-1",
            permit_idempotency_key="permit-1",
            arguments=["not", "a", "dict"],
        )
        none_result = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-2",
            permit_idempotency_key="permit-2",
            arguments=None,
        )
    finally:
        await client.close()

    assert result == "Error: arguments must be an object"
    assert seen == ["/v1/permits", "/mcp/messages"]
    assert "receipt-success" in none_result


@pytest.mark.asyncio
async def test_shorter_permit_ttl_is_not_silently_ignored():
    created = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            created["n"] += 1
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_invoke_response())
        return httpx.Response(404, json={"detail": "missing"})

    tool, client = _tool(httpx.MockTransport(handler), permit_ttl_minutes=30)
    try:
        first = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-1",
            permit_idempotency_key="permit-ttl",
            arguments={},
        )
        tool.permit_ttl_minutes = 5
        second = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-2",
            permit_idempotency_key="permit-ttl",
            arguments={},
        )
    finally:
        await client.close()

    assert "receipt-success" in first
    assert second == "Error: permit_idempotency_key reused with different permit terms"
    assert created["n"] == 1


@pytest.mark.asyncio
async def test_delivery_uncertain_keeps_the_receipt_id():
    uncertain = _receipt_payload("delivery_uncertain")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "invoke-uncertain",
                    "error": {
                        "code": -32603,
                        "message": "delivery_uncertain",
                        "data": {"receipt": uncertain},
                    },
                },
            )
        return httpx.Response(404, json={"detail": "missing"})

    tool, client = _tool(httpx.MockTransport(handler))
    try:
        result = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-uncertain",
            permit_idempotency_key="permit-uncertain",
            arguments={},
        )
    finally:
        await client.close()

    assert result.startswith("Error:")
    assert "delivery_uncertain" in result
    assert "receipt-delivery_uncertain" in result


@pytest.mark.asyncio
async def test_auth_failure_does_not_invoke_or_stick():
    permits = {"n": 0}
    invokes = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            permits["n"] += 1
            if permits["n"] == 1:
                return httpx.Response(401, json={"detail": "invalid api key"})
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            invokes["n"] += 1
            return httpx.Response(200, json=_invoke_response())
        return httpx.Response(404, json={"detail": "missing"})

    tool, client = _tool(httpx.MockTransport(handler))
    try:
        denied = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-auth",
            permit_idempotency_key="permit-auth",
            arguments={},
        )
        retried = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-auth",
            permit_idempotency_key="permit-auth",
            arguments={},
        )
    finally:
        await client.close()

    assert denied == "Error: invalid api key"
    assert invokes["n"] == 1
    assert permits["n"] == 2
    assert "receipt-success" in retried


@pytest.mark.asyncio
async def test_two_wallets_do_not_share_a_permit():
    wallets: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            body = json.loads(request.content)
            wallets.append(body["subject_wallet_id"])
            payload = _permit_payload()
            payload["issuer_wallet_id"] = body["issuer_wallet_id"]
            payload["subject_wallet_id"] = body["subject_wallet_id"]
            payload["allowed_tools"] = body["allowed_tools"]
            return httpx.Response(201, json=payload)
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_invoke_response())
        if request.url.path == "/mcp/tools.json":
            return httpx.Response(
                200,
                json={"tools": [{"name": "partner.search", "description": "Search"}]},
            )
        if request.url.path.startswith("/v1/billing/wallets/"):
            return httpx.Response(200, json={"balance": 9})
        return httpx.Response(404, json={"detail": "missing"})

    transport = httpx.MockTransport(handler)
    tool_a, client_a = _tool(transport, api_key="key-a", wallet_id="wallet-a")
    tool_b, client_b = _tool(transport, api_key="key-b", wallet_id="wallet-b")
    try:
        result_a = await tool_a._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-a",
            permit_idempotency_key="shared-permit-key",
            arguments={"query": "a"},
        )
        result_b = await tool_b._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-b",
            permit_idempotency_key="shared-permit-key",
            arguments={"query": "b"},
        )
        balance = await tool_b._arun(operation="balance")
        tools = await tool_a._arun(operation="discover_tools")
    finally:
        await client_a.close()
        await client_b.close()

    assert wallets == ["wallet-a", "wallet-b"]
    assert "receipt-success" in result_a
    assert "receipt-success" in result_b
    assert balance == "Balance: 9 credits"
    assert "partner.search" in tools


@pytest.mark.asyncio
async def test_balance_and_discovery_use_this_tools_identity(monkeypatch):
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers["x-api-key"]
        captured["path"] = request.url.path
        if request.url.path == "/mcp/tools.json":
            return httpx.Response(
                200,
                json={"tools": [{"name": "partner.search", "description": "Search"}]},
            )
        return httpx.Response(200, json={"balance": 4})

    transport = httpx.MockTransport(handler)
    real_client = B2AClient

    class _CaptureClient(real_client):
        def __init__(self, api_key: str, base_url: str = "https://api.thisisatest.tech", **kwargs):
            captured["constructed_key"] = api_key
            captured["constructed_url"] = base_url
            super().__init__(
                api_key=api_key,
                base_url=base_url,
                transport=transport,
                **kwargs,
            )

    monkeypatch.setattr("crewai_b2a.tool.B2AClient", _CaptureClient)
    tool = CrewAIB2ATool(
        api_key="key-from-tool",
        wallet_id="wallet-from-tool",
        base_url="https://middleware.test",
    )
    try:
        balance = await tool._arun(operation="balance")
    finally:
        assert tool.client is not None
        await tool.client.close()

    assert balance == "Balance: 4 credits"
    assert captured["constructed_key"] == "key-from-tool"
    assert captured["constructed_url"] == "https://middleware.test"
    assert captured["key"] == "key-from-tool"
    assert captured["path"] == "/v1/billing/wallets/wallet-from-tool"


@pytest.mark.asyncio
async def test_permit_key_reuse_rejects_changed_terms_without_a_second_invoke():
    invokes = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            invokes["n"] += 1
            return httpx.Response(200, json=_invoke_response())
        return httpx.Response(404, json={"detail": "missing"})

    tool, client = _tool(httpx.MockTransport(handler))
    try:
        first = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-1",
            permit_idempotency_key="permit-terms",
            arguments={"query": "one"},
        )
        changed_tool = await tool._arun(
            operation="call_tool",
            tool_name="other.tool",
            idempotency_key="invoke-2",
            permit_idempotency_key="permit-terms",
            arguments={},
        )
        tool.permit_budget = Decimal(5)
        changed_budget = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-3",
            permit_idempotency_key="permit-terms",
            arguments={},
        )
        tool.permit_budget = Decimal(100)
        tool.wallet_id = "wallet-2"
        changed_wallet = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-4",
            permit_idempotency_key="permit-terms",
            arguments={},
        )
        unknown = await tool._arun(operation="nope")
    finally:
        await client.close()

    assert "receipt-success" in first
    assert changed_tool == "Error: permit_idempotency_key reused with different permit terms"
    assert changed_budget == "Error: permit_idempotency_key reused with different permit terms"
    assert changed_wallet == "Error: permit_idempotency_key reused with different permit terms"
    assert unknown == "Unknown operation: nope"
    assert invokes["n"] == 1


@pytest.mark.asyncio
async def test_failed_permit_retry_keeps_the_original_expiry():
    expiries: list[str] = []
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            attempts["n"] += 1
            expiries.append(json.loads(request.content)["expires_at"])
            if attempts["n"] == 1:
                return httpx.Response(503, json={"detail": "unavailable"})
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_invoke_response())
        return httpx.Response(404, json={"detail": "missing"})

    tool, client = _tool(httpx.MockTransport(handler))
    try:
        failed = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-retry",
            permit_idempotency_key="permit-retry",
            arguments={"query": "same"},
        )
        retried = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-retry",
            permit_idempotency_key="permit-retry",
            arguments={"query": "same"},
        )
    finally:
        await client.close()

    assert failed == "Error: unavailable"
    assert expiries[0] == expiries[1]
    assert "receipt-success" in retried


@pytest.mark.asyncio
async def test_tool_error_receipt_is_not_dropped():
    receipt = _receipt_payload("error")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "invoke-err",
                    "result": {
                        "content": [{"type": "text", "text": "nope"}],
                        "isError": True,
                        "error": "upstream failed",
                        "receipt": receipt,
                    },
                },
            )
        return httpx.Response(404, json={"detail": "missing"})

    tool, client = _tool(httpx.MockTransport(handler))
    try:
        result = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-err",
            permit_idempotency_key="permit-err",
            arguments={},
        )
    finally:
        await client.close()

    assert result.startswith("Error:")
    assert "receipt-error" in result


@pytest.mark.asyncio
async def test_copied_tool_does_not_spend_the_original_permit_on_another_wallet():
    invokes: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            body = json.loads(request.content)
            payload = _permit_payload()
            payload["permit_id"] = f"permit-{body['subject_wallet_id']}"
            payload["issuer_wallet_id"] = body["issuer_wallet_id"]
            payload["subject_wallet_id"] = body["subject_wallet_id"]
            payload["allowed_tools"] = body["allowed_tools"]
            return httpx.Response(201, json=payload)
        if request.url.path == "/mcp/messages":
            body = json.loads(request.content)
            context = body["params"]["mcpContext"]
            invokes.append((context["wallet_id"], context["permit_id"]))
            return httpx.Response(200, json=_invoke_response())
        return httpx.Response(404, json={"detail": "missing"})

    tool, client = _tool(httpx.MockTransport(handler))
    try:
        original = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-original",
            permit_idempotency_key="permit-copy",
            arguments={},
        )
        copied = tool.model_copy()
        copied.wallet_id = "wallet-2"
        moved = await copied._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-copy",
            permit_idempotency_key="permit-copy",
            arguments={},
        )
    finally:
        await client.close()

    assert "receipt-success" in original
    assert ("wallet-2", "permit-wallet-1") not in invokes
    assert "permit_idempotency_key reused" in moved or ("wallet-2", "permit-wallet-2") in invokes
