"""Boundaries the AutoGen wrapper must enforce before it mints a permit.

The trust plane stores an idempotency key verbatim and hashes the whole permit
body, including the expiry. These tests cover the cases where a retry would
either conflict with a key the SDK already collapsed, or mint a permit for a
call that cannot succeed.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from b2a_sdk import IdempotencyConflictError, TransportError

from autogen_b2a import B2AClient, B2AFunctionTool


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


def _receipt_payload() -> dict:
    return {
        "receipt_id": "receipt-success",
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
        "outcome": "success",
        "audit_event_id": "audit-1",
        "created_at": datetime.now(UTC).isoformat(),
        "signature": "sig-success",
        "signature_key_id": "signing-key-1",
    }


def _mcp_ok(call_id: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "result": {
            "content": [{"type": "text", "text": '{"ok": true}'}],
            "structuredContent": {"ok": True},
            "isError": False,
            "receipt": _receipt_payload(),
        },
    }


class _BoundTool:
    def __init__(
        self, handler, *, api_key: str = "tenant-a-key", wallet_id: str = "wallet-1"
    ):
        self.client = B2AClient(api_key=api_key, transport=httpx.MockTransport(handler))
        self.tool = B2AFunctionTool(api_key=api_key, wallet_id=wallet_id)
        self._opened = self.tool.client
        self.tool.client = self.client

    async def close(self) -> None:
        await self.client.close()
        await self._opened.close()


def _valid_call(**overrides) -> dict:
    call = {
        "tool_name": "partner.search",
        "idempotency_key": "invoke-1",
        "permit_idempotency_key": "permit-1",
        "arguments": {"query": "test"},
    }
    call.update(overrides)
    return call


@pytest.mark.asyncio
async def test_padded_permit_key_reuses_one_permit_body():
    """A padded permit key and its stripped form are one server key.

    b2a_sdk strips the key before it is sent. Caching the raw text misses on
    the retry, builds a new expiry, and the trust plane rejects the second
    body as a conflict.
    """
    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            body = json.loads(request.content)
            if bodies and body["expires_at"] != bodies[0]["expires_at"]:
                return httpx.Response(409, json={"detail": "idempotency_key_reused"})
            assert request.headers["idempotency-key"] == "permit-pad"
            bodies.append(body)
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_mcp_ok("invoke"))
        return httpx.Response(404, json={"detail": "missing"})

    bound = _BoundTool(handler)
    try:
        first = await bound.tool.call_mcp_tool(
            **_valid_call(
                idempotency_key="invoke-1",
                permit_idempotency_key="  permit-pad  ",
            )
        )
        second = await bound.tool.call_mcp_tool(
            **_valid_call(
                idempotency_key="invoke-2",
                permit_idempotency_key="permit-pad",
            )
        )
    except IdempotencyConflictError as exc:  # the old cache miss surfaces here
        pytest.fail(f"padded permit key conflicted with its stripped form: {exc}")
    else:
        assert len(bodies) == 1
        assert first["receipt_id"] == second["receipt_id"] == "receipt-success"
    finally:
        await bound.close()


@pytest.mark.asyncio
async def test_invalid_invoke_key_does_not_mint_a_permit():
    """A key the trust plane will refuse must fail before permit creation."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_mcp_ok("invoke"))
        return httpx.Response(404, json={"detail": "missing"})

    bound = _BoundTool(handler)
    try:
        with pytest.raises(ValueError, match="at most 128"):
            await bound.tool.call_mcp_tool(**_valid_call(idempotency_key="k" * 129))
        with pytest.raises(ValueError, match="control characters"):
            await bound.tool.call_mcp_tool(**_valid_call(idempotency_key="bad\nkey"))
        with pytest.raises(TypeError, match="must be a string"):
            await bound.tool.call_mcp_tool(**_valid_call(idempotency_key=15))
        assert calls == []
    finally:
        await bound.close()


@pytest.mark.asyncio
async def test_invalid_permit_key_does_not_reach_the_api():
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(201, json=_permit_payload())

    bound = _BoundTool(handler)
    try:
        with pytest.raises(ValueError, match="control characters"):
            await bound.tool.call_mcp_tool(
                **_valid_call(permit_idempotency_key="permit\nbad")
            )
        with pytest.raises(TypeError, match="must be a string"):
            await bound.tool.call_mcp_tool(**_valid_call(permit_idempotency_key=9))
        assert calls == []
    finally:
        await bound.close()


@pytest.mark.asyncio
async def test_blank_wallet_does_not_pin_a_permit_key():
    """A blank wallet must not consume the permit key.

    The first attempt used to store a permit body for that key. Correcting
    the wallet and retrying the same key then failed locally, even though
    the server had never accepted a permit.
    """
    permit_bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            permit_bodies.append(json.loads(request.content))
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_mcp_ok("invoke"))
        return httpx.Response(404, json={"detail": "missing"})

    bound = _BoundTool(handler)
    try:
        bound.tool.wallet_id = "   "
        with pytest.raises(ValueError, match="wallet_id"):
            await bound.tool.call_mcp_tool(**_valid_call())
        bound.tool.wallet_id = "wallet-1"
        result = await bound.tool.call_mcp_tool(**_valid_call())
        assert result["receipt_id"] == "receipt-success"
        assert len(permit_bodies) == 1
        assert permit_bodies[0]["subject_wallet_id"] == "wallet-1"
    finally:
        await bound.close()


@pytest.mark.asyncio
async def test_unusable_budget_or_ttl_does_not_pin_a_permit_key():
    permit_calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            permit_calls["n"] += 1
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_mcp_ok("invoke"))
        return httpx.Response(404, json={"detail": "missing"})

    bound = _BoundTool(handler)
    try:
        bound.tool.permit_budget = Decimal(0)
        with pytest.raises(ValueError, match="permit_budget"):
            await bound.tool.call_mcp_tool(**_valid_call())
        bound.tool.permit_budget = Decimal(25)
        bound.tool.permit_ttl_minutes = 0
        with pytest.raises(ValueError, match="permit_ttl_minutes"):
            await bound.tool.call_mcp_tool(**_valid_call())
        bound.tool.permit_ttl_minutes = 15
        result = await bound.tool.call_mcp_tool(**_valid_call())
        assert result["receipt_id"] == "receipt-success"
        assert permit_calls["n"] == 1
    finally:
        await bound.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_budget",
    [Decimal("0.123456789"), Decimal(1000000000000), "0.000000001"],
)
async def test_unstorable_budget_does_not_pin_a_permit_key(invalid_budget):
    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            bodies.append(json.loads(request.content))
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_mcp_ok("invoke"))
        return httpx.Response(404, json={"detail": "missing"})

    bound = _BoundTool(handler)
    try:
        bound.tool.permit_budget = invalid_budget
        with pytest.raises(ValueError, match="permit_budget"):
            await bound.tool.call_mcp_tool(**_valid_call())
        assert bodies == []

        bound.tool.permit_budget = "25.5"
        await bound.tool.call_mcp_tool(**_valid_call())
        assert len(bodies) == 1
        assert bodies[0]["max_credits"] == "25.5"
    finally:
        await bound.close()


@pytest.mark.asyncio
async def test_blank_tool_name_and_non_object_arguments_do_not_mint():
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_mcp_ok("invoke"))
        return httpx.Response(404, json={"detail": "missing"})

    bound = _BoundTool(handler)
    try:
        with pytest.raises(ValueError, match="tool_name"):
            await bound.tool.call_mcp_tool(**_valid_call(tool_name="  "))
        with pytest.raises(ValueError, match="arguments must be an object"):
            await bound.tool.call_mcp_tool(**_valid_call(arguments=["nope"]))
        assert calls == []
    finally:
        await bound.close()


@pytest.mark.asyncio
async def test_arguments_are_fixed_before_the_permit_request():
    """Nested caller data is fixed before the permit request awaits."""
    seen: list[dict] = []
    arguments = {"filters": {"documents": ["first"]}}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            arguments["filters"]["documents"].append("changed-during-permit")
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            seen.append(json.loads(request.content)["params"]["arguments"])
            return httpx.Response(200, json=_mcp_ok("invoke-1"))
        return httpx.Response(404, json={"detail": "missing"})

    bound = _BoundTool(handler)
    try:
        await bound.tool.call_mcp_tool(**_valid_call(arguments=arguments))
        assert seen == [{"filters": {"documents": ["first"]}}]
    finally:
        await bound.close()


@pytest.mark.asyncio
async def test_invoke_uses_the_wallet_captured_for_the_permit():
    """A wallet change while the permit request is in flight stays off this call."""
    invoke_wallets: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            bound.tool.wallet_id = "wallet-other"
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            body = json.loads(request.content)
            invoke_wallets.append(body["params"]["mcpContext"]["wallet_id"])
            return httpx.Response(200, json=_mcp_ok("invoke-1"))
        return httpx.Response(404, json={"detail": "missing"})

    bound = _BoundTool(handler)
    try:
        await bound.tool.call_mcp_tool(**_valid_call())
        assert invoke_wallets == ["wallet-1"]
    finally:
        await bound.close()


@pytest.mark.asyncio
async def test_dropped_permit_response_retries_the_same_expiry():
    expiries: list[str] = []
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            expiries.append(json.loads(request.content)["expires_at"])
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("connection dropped")
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            return httpx.Response(200, json=_mcp_ok("invoke-1"))
        return httpx.Response(404, json={"detail": "missing"})

    bound = _BoundTool(handler)
    try:
        with pytest.raises(TransportError):
            await bound.tool.call_mcp_tool(**_valid_call())
        result = await bound.tool.call_mcp_tool(**_valid_call())
        assert result["receipt_id"] == "receipt-success"
        assert attempts["n"] == 2
        assert expiries[0] == expiries[1]
    finally:
        await bound.close()


@pytest.mark.asyncio
async def test_each_tool_sends_its_own_api_key():
    """Two tools must not share a credential. The key is the tenant boundary."""
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["x-api-key"], request.url.path))
        if request.url.path == "/v1/billing/wallets/wallet-1":
            return httpx.Response(200, json={"balance": 4})
        return httpx.Response(404, json={"detail": "missing"})

    first = _BoundTool(handler, api_key="tenant-a-key")
    second = _BoundTool(handler, api_key="tenant-b-key")
    try:
        assert await first.tool.get_wallet_balance() == 4
        assert await second.tool.get_wallet_balance() == 4
        assert seen == [
            ("tenant-a-key", "/v1/billing/wallets/wallet-1"),
            ("tenant-b-key", "/v1/billing/wallets/wallet-1"),
        ]
    finally:
        await first.close()
        await second.close()


def test_blank_api_key_and_wallet_are_rejected_at_construction():
    with pytest.raises(ValueError, match="api_key"):
        B2AFunctionTool(api_key="   ", wallet_id="wallet-1")
    with pytest.raises(ValueError, match="wallet_id"):
        B2AFunctionTool(api_key="tenant-a-key", wallet_id="")
    with pytest.raises(ValueError, match="api_key"):
        B2AFunctionTool(api_key=None, wallet_id="wallet-1")  # type: ignore[arg-type]
