"""Auth boundaries and idempotency-key identity for the LangChain wrapper.

The shared client strips surrounding whitespace before it puts an idempotency
key on the wire, and the server keeps that text as the key. The permit cache
has to use the same text. These tests also check that a bad call is refused
before a permit or a wallet read is sent, and that a denied call is not
reported as a successful receipt.
"""

import ast
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from b2a_sdk import (
    AuthenticationError,
    AuthorizationError,
    DeliveryUncertainError,
    IdempotencyConflictError,
    InsufficientFundsError,
    PermitDeniedError,
)

from langchain_b2a import B2AClient, get_langgraph_tools
from langchain_b2a.tools import get_mcp_tools

API_KEY = "test-key-do-not-leak"


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


def _invoke_response(request: httpx.Request, outcome: str = "success") -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "structuredContent": {"ok": True},
                "isError": False,
                "receipt": _receipt_payload(outcome),
            },
        },
    )


def _call(**overrides: object) -> dict:
    payload = {
        "tool_name": "partner.search",
        "idempotency_key": "invoke-key",
        "permit_idempotency_key": "permit-key",
        "arguments": {"query": "test"},
    }
    payload.update(overrides)
    return payload


class _Gateway:
    """Records permit bodies and treats a changed body under one key as a conflict."""

    def __init__(self) -> None:
        self.permit_keys: list[str] = []
        self.permit_bodies: list[dict] = []
        self.invoke_keys: list[str] = []
        self.api_keys: list[str] = []
        self._permits: dict[str, str] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.api_keys.append(request.headers["x-api-key"])
        if request.url.path == "/v1/permits":
            body = json.loads(request.content)
            key = request.headers["idempotency-key"]
            self.permit_keys.append(key)
            self.permit_bodies.append(body)
            digest = json.dumps(body, sort_keys=True)
            stored = self._permits.setdefault(key, digest)
            if stored != digest:
                return httpx.Response(409, json={"detail": "idempotency_key_reused"})
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            self.invoke_keys.append(request.headers["idempotency-key"])
            assert API_KEY not in request.content.decode()
            return _invoke_response(request)
        return httpx.Response(404, json={"detail": "not_found"})


def _client(handler) -> B2AClient:
    return B2AClient(api_key=API_KEY, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_padded_permit_key_reuses_one_permit_body():
    """Surrounding whitespace is not a second permit.

    The client sends the stripped key. Caching the raw string builds a new
    expiry for the same wire key, and the server then rejects the replay.
    """
    gateway = _Gateway()
    client = _client(gateway)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]

    first = ast.literal_eval(
        await tool.ainvoke(
            _call(
                idempotency_key="invoke-1",
                permit_idempotency_key="  permit-key\n",
            )
        )
    )
    second = ast.literal_eval(
        await tool.ainvoke(
            _call(
                idempotency_key="invoke-2",
                permit_idempotency_key="permit-key",
            )
        )
    )

    assert gateway.permit_keys == ["permit-key"]
    assert len(gateway.permit_bodies) == 1
    assert first["receipt_id"] == second["receipt_id"]
    assert API_KEY not in str(first)
    await client.close()


@pytest.mark.asyncio
async def test_newline_inside_invoke_key_is_refused_before_any_request():
    gateway = _Gateway()
    client = _client(gateway)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]

    with pytest.raises(ValueError, match="control characters"):
        await tool.ainvoke(_call(idempotency_key="invoke\nkey"))

    assert gateway.permit_keys == []
    assert gateway.invoke_keys == []
    await client.close()


@pytest.mark.asyncio
async def test_overlong_permit_key_is_refused_before_any_request():
    gateway = _Gateway()
    client = _client(gateway)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]

    with pytest.raises(ValueError, match="128"):
        await tool.ainvoke(_call(permit_idempotency_key="p" * 129))

    assert gateway.permit_keys == []
    await client.close()


@pytest.mark.asyncio
async def test_blank_tool_name_is_refused_before_any_request():
    gateway = _Gateway()
    client = _client(gateway)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]

    with pytest.raises(ValueError, match="tool_name"):
        await tool.ainvoke(_call(tool_name="   "))

    assert gateway.permit_keys == []
    assert gateway.invoke_keys == []
    await client.close()


@pytest.mark.asyncio
async def test_blank_wallet_is_refused_before_any_request():
    gateway = _Gateway()
    client = _client(gateway)

    with pytest.raises(ValueError, match="wallet_id"):
        get_mcp_tools(client, wallet_id="  ")

    assert gateway.permit_keys == []
    await client.close()


@pytest.mark.asyncio
async def test_arguments_must_be_an_object_before_any_request():
    gateway = _Gateway()
    client = _client(gateway)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]

    with pytest.raises(ValueError, match="arguments"):
        await tool.coroutine(**_call(arguments=["not-an-object"]))

    assert gateway.permit_keys == []
    await client.close()


@pytest.mark.asyncio
async def test_permit_is_bound_to_the_given_wallet_and_tool():
    gateway = _Gateway()
    client = _client(gateway)
    tool = get_mcp_tools(client, wallet_id="wallet-1", permit_budget=Decimal("40"))[0]

    result = await tool.ainvoke(_call())
    body = gateway.permit_bodies[0]

    assert body["issuer_wallet_id"] == "wallet-1"
    assert body["subject_wallet_id"] == "wallet-1"
    assert body["allowed_tools"] == ["partner.search"]
    assert body["scopes"] == ["tool:partner.search:invoke", "billing:charge"]
    assert body["max_credits"] == "40"
    assert gateway.api_keys == [API_KEY, API_KEY]
    assert API_KEY not in json.dumps(body)
    assert API_KEY not in result
    parsed = ast.literal_eval(result)
    assert parsed["signature"] == "sig-success"
    await client.close()


@pytest.mark.asyncio
async def test_same_permit_key_cannot_change_tools_without_a_new_request():
    gateway = _Gateway()
    client = _client(gateway)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]
    await tool.ainvoke(_call())
    before = len(gateway.permit_keys)

    with pytest.raises(ValueError, match="different permit terms"):
        await tool.ainvoke(_call(tool_name="other.tool", idempotency_key="invoke-2"))

    assert len(gateway.permit_keys) == before
    assert gateway.invoke_keys == ["invoke-key"]
    await client.close()


@pytest.mark.asyncio
async def test_invalid_api_key_does_not_invoke_or_echo_the_key():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == API_KEY
        if request.url.path == "/v1/permits":
            return httpx.Response(401, json={"detail": "unauthorized"})
        raise AssertionError(f"unexpected {request.url.path}")

    client = _client(handler)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]

    with pytest.raises(AuthenticationError, match="unauthorized") as caught:
        await tool.ainvoke(_call())

    assert API_KEY not in str(caught.value)
    await client.close()


@pytest.mark.asyncio
async def test_foreign_wallet_is_denied_before_invoke():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/permits":
            return httpx.Response(
                403,
                json={"detail": "subject_wallet_access_denied"},
            )
        raise AssertionError("invoke must not run after a refused permit")

    client = _client(handler)
    tool = get_mcp_tools(client, wallet_id="victim-wallet")[0]

    with pytest.raises(AuthorizationError, match="subject_wallet_access_denied") as caught:
        await tool.ainvoke(_call())

    assert seen == ["/v1/permits"]
    assert API_KEY not in str(caught.value)
    await client.close()


@pytest.mark.asyncio
async def test_auth_failure_retries_the_same_permit_body():
    """A refused create must not pin a new expiry onto the caller's key."""
    bodies: list[dict] = []
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            bodies.append(json.loads(request.content))
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(401, json={"detail": "unauthorized"})
            return httpx.Response(201, json=_permit_payload())
        return _invoke_response(request)

    client = _client(handler)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]
    with pytest.raises(AuthenticationError):
        await tool.ainvoke(_call())
    await tool.ainvoke(_call())

    assert bodies[0] == bodies[1]
    await client.close()


@pytest.mark.asyncio
async def test_permit_denial_is_not_a_successful_receipt():
    permits = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            permits["n"] += 1
            return httpx.Response(201, json=_permit_payload())
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "invoke-key",
                "error": {"code": -32000, "message": "permit_tool_not_allowed"},
            },
        )

    client = _client(handler)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]
    with pytest.raises(PermitDeniedError, match="permit_tool_not_allowed"):
        await tool.ainvoke(_call())
    with pytest.raises(PermitDeniedError):
        await tool.ainvoke(_call(idempotency_key="invoke-2"))

    assert permits["n"] == 1
    await client.close()


@pytest.mark.asyncio
async def test_insufficient_funds_is_not_a_successful_receipt():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "invoke-key",
                "error": {"code": -32000, "message": "insufficient_funds"},
            },
        )

    client = _client(handler)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]
    with pytest.raises(InsufficientFundsError):
        await tool.ainvoke(_call())
    await client.close()


@pytest.mark.asyncio
async def test_delivery_uncertain_raises_and_reuses_the_permit():
    permits = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            permits["n"] += 1
            return httpx.Response(201, json=_permit_payload())
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": json.loads(request.content)["id"],
                "error": {
                    "code": -32603,
                    "message": "delivery_uncertain",
                    "data": {"receipt": _receipt_payload("delivery_uncertain")},
                },
            },
        )

    client = _client(handler)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]
    with pytest.raises(DeliveryUncertainError) as first:
        await tool.ainvoke(_call())
    with pytest.raises(DeliveryUncertainError) as second:
        await tool.ainvoke(_call())

    assert first.value.receipt_id == "receipt-delivery_uncertain"
    assert second.value.receipt_id == first.value.receipt_id
    assert permits["n"] == 1
    await client.close()


@pytest.mark.asyncio
async def test_reused_invoke_key_with_a_new_permit_key_is_a_conflict():
    """The invocation key is bound to the first permit. A second permit must not replay it."""

    class Gateway(_Gateway):
        def __init__(self) -> None:
            super().__init__()
            self._invocations: dict[str, str] = {}
            self.permit_serial = 0

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            self.api_keys.append(request.headers["x-api-key"])
            if request.url.path == "/v1/permits":
                self.permit_serial += 1
                body = json.loads(request.content)
                key = request.headers["idempotency-key"]
                self.permit_keys.append(key)
                self.permit_bodies.append(body)
                payload = _permit_payload()
                payload["permit_id"] = f"permit-{self.permit_serial}"
                return httpx.Response(201, json=payload)
            if request.url.path == "/mcp/messages":
                body = json.loads(request.content)
                key = request.headers["idempotency-key"]
                self.invoke_keys.append(key)
                digest = json.dumps(
                    {
                        "name": body["params"]["name"],
                        "arguments": body["params"]["arguments"],
                        "permit_id": body["params"]["mcpContext"]["permit_id"],
                    },
                    sort_keys=True,
                )
                stored = self._invocations.setdefault(key, digest)
                if stored != digest:
                    return httpx.Response(
                        200,
                        json={
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "error": {"code": -32603, "message": "idempotency_key_reused"},
                        },
                    )
                return _invoke_response(request)
            return httpx.Response(404)

    gateway = Gateway()
    client = _client(gateway)
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]
    await tool.ainvoke(_call(permit_idempotency_key="permit-a"))

    with pytest.raises(IdempotencyConflictError, match="idempotency_key_reused"):
        await tool.ainvoke(_call(permit_idempotency_key="permit-b"))

    assert gateway.permit_keys == ["permit-a", "permit-b"]
    assert gateway.invoke_keys == ["invoke-key", "invoke-key"]
    await client.close()


@pytest.mark.asyncio
async def test_wallet_balance_uses_the_closed_wallet_and_hides_the_key():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["x-api-key"] == API_KEY
        assert request.url.path == "/v1/billing/wallets/wallet-1"
        return httpx.Response(200, json={"balance": 12.5, "wallet_id": "wallet-1"})

    client = _client(handler)
    tool = get_langgraph_tools(client, wallet_id="wallet-1")[1]
    result = await tool.ainvoke({})

    assert result == "Balance: 12.5 credits"
    assert API_KEY not in result
    assert seen == ["/v1/billing/wallets/wallet-1"]
    await client.close()


@pytest.mark.asyncio
async def test_blank_wallet_balance_tool_is_refused():
    client = _client(lambda request: httpx.Response(500))
    with pytest.raises(ValueError, match="wallet_id"):
        get_langgraph_tools(client, wallet_id=" ")
    await client.close()


@pytest.mark.asyncio
async def test_wallet_balance_denial_is_not_a_balance():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/billing/wallets/victim-wallet"
        return httpx.Response(403, json={"detail": "wallet_access_denied"})

    client = _client(handler)
    tool = get_langgraph_tools(client, wallet_id="victim-wallet")[1]
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await tool.ainvoke({})

    assert "Balance:" not in str(caught.value)
    assert API_KEY not in str(caught.value)
    await client.close()


@pytest.mark.asyncio
async def test_tool_schema_requires_caller_keys_and_hides_credentials():
    client = _client(lambda request: httpx.Response(404))
    try:
        tools = get_langgraph_tools(client, wallet_id="wallet-1")
        mcp_tool, wallet_tool = tools
        schema = mcp_tool.args_schema.model_json_schema()
        assert set(schema["required"]) >= {
            "tool_name",
            "idempotency_key",
            "permit_idempotency_key",
        }
        assert "wallet_id" not in schema["properties"]
        assert "api_key" not in schema["properties"]
        wallet_schema = wallet_tool.args_schema.model_json_schema()
        assert "wallet_id" not in wallet_schema.get("properties", {})
        assert wallet_tool.name == "wallet_balance"
        assert mcp_tool.name == "mcp_tool_call"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_zero_budget_and_ttl_are_refused_before_any_request():
    gateway = _Gateway()
    client = _client(gateway)
    with pytest.raises(ValueError, match="permit_budget"):
        get_mcp_tools(client, wallet_id="wallet-1", permit_budget=Decimal("0"))
    with pytest.raises(ValueError, match="permit_ttl"):
        get_mcp_tools(client, wallet_id="wallet-1", permit_ttl_minutes=0)

    assert gateway.permit_keys == []
    await client.close()
