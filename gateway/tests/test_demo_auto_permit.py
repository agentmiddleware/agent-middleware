"""Demo calls without a permit use bounded authority and preserve replay."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import PermitModel
from app.main import app
from app.routers import mcp as mcp_router
from app.schemas.billing import ServiceCategory
from app.services.demo_tenant import _memory_counters
from app.services.service_registry import get_service_registry
from app.services.signing_keys import get_signing_key_service
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

STANDARD_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, client=("203.0.113.247", 443))
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def demo_settings(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    raw_private_key = Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    signing_key = base64.b64encode(raw_private_key).decode()
    _memory_counters.clear()
    for name, value in {
        "ENVIRONMENT": "local",
        "REDIS_URL": "",
        "ENABLE_DEMO_TENANT": True,
        "DEMO_ALLOWED_TOOLS": "partner.echo",
        "DEMO_KEY_MAX_USES": None,
        "DEMO_WALLET_CREDITS": Decimal("10"),
        "DEMO_WALLET_DAILY_LIMIT": None,
        "DEMO_MAX_PERMIT_CREDITS": Decimal("3"),
        "DEMO_MAX_PERMIT_TTL_MINUTES": 3,
        "STANDARD_MCP_PERMIT_TTL_SECONDS": 120,
        "DEMO_ISSUE_PER_IP_PER_DAY": 10,
        "DEMO_ISSUE_GLOBAL_PER_HOUR": 100,
        "DEMO_ISSUE_GLOBAL_PER_DAY": 100,
        "DEMO_MAX_LIVE_KEYS": 100,
        "TRUST_SIGNING_PRIVATE_KEY_B64": signing_key,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(mcp_router.settings, "TRUST_MODE_ENABLED", True)
    monkeypatch.setattr(mcp_router.settings, "ALLOW_LEGACY_UNPERMITTED_MCP", False)
    monkeypatch.setattr(
        mcp_router.settings, "TRUST_SIGNING_PRIVATE_KEY_B64", signing_key
    )
    monkeypatch.setattr(get_signing_key_service(), "_private_key", None)
    try:
        yield settings
    finally:
        _memory_counters.clear()


@pytest.fixture
def echo_tools():
    calls: list[str] = []
    registry = get_service_registry()

    def echo(message: str = "hello") -> dict[str, str]:
        calls.append("partner.echo")
        return {"message": message}

    def other(message: str = "hello") -> dict[str, str]:
        calls.append("partner.other")
        return {"message": message}

    for tool_name, handler in (("partner.echo", echo), ("partner.other", other)):
        registry.register_local(
            service_id=tool_name,
            name=tool_name,
            description="Demo permit test tool",
            category=ServiceCategory.AGENT_COMMS,
            func=handler,
            credits_per_unit=2.0,
            unit_name="call",
        )
    try:
        yield calls
    finally:
        registry.unregister_local("partner.echo")
        registry.unregister_local("partner.other")


async def _demo_key(client: AsyncClient) -> dict:
    response = await client.post("/v1/demo/keys", json={})
    assert response.status_code == 201, response.text
    return response.json()


def _request_body(
    transport: str, tool: str, wallet_id: str, permit_id: str | None = None
) -> dict:
    context = {"wallet_id": wallet_id}
    if permit_id is not None:
        context["permit_id"] = permit_id
    if transport == "rest":
        return {
            "name": tool,
            "arguments": {"message": "hello"},
            "mcp_context": context,
        }
    return {
        "jsonrpc": "2.0",
        "id": "demo-call",
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": {"message": "hello"},
            "mcpContext": context,
        },
    }


async def _call(
    client: AsyncClient,
    *,
    transport: str,
    tool: str,
    wallet_id: str,
    api_key: str,
    request_key: str | None,
    permit_id: str | None = None,
):
    path = f"/mcp/tools/{tool}/invoke" if transport == "rest" else "/mcp/messages"
    headers = {"X-API-Key": api_key}
    if request_key is not None:
        headers["Idempotency-Key"] = request_key
    return await client.post(
        path,
        json=_request_body(transport, tool, wallet_id, permit_id),
        headers=headers,
    )


async def _standard_call(
    client: AsyncClient, *, tool: str, api_key: str, request_key: str
):
    return await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "demo-standard-call",
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"message": "hello"}},
        },
        headers={
            "X-API-Key": api_key,
            "Idempotency-Key": request_key,
            **STANDARD_MCP_HEADERS,
        },
    )


def _result(response, transport: str) -> dict:
    assert response.status_code == 200, response.text
    body = response.json()
    return body if transport == "rest" else body["result"]


def _error(response, transport: str, expected_code: str) -> None:
    body = response.json()
    if transport == "rest":
        assert response.status_code == 403, response.text
        envelope = body
    else:
        assert response.status_code == 200, response.text
        envelope = body["error"]["data"]
    assert envelope["code"] == expected_code
    assert envelope["message"]
    assert envelope["fix"]
    assert envelope["docs_url"].endswith(f"#{expected_code}")


async def _permits_for(wallet_id: str) -> list[PermitModel]:
    async with get_session_factory()() as session:
        rows = await session.execute(
            select(PermitModel).where(PermitModel.subject_wallet_id == wallet_id)
        )
        return list(rows.scalars())


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "jsonrpc"])
async def test_demo_call_auto_mints_bounded_permit_and_replays_once(
    client: AsyncClient, clean_database, demo_settings, echo_tools, transport: str
) -> None:
    demo = await _demo_key(client)
    call = dict(
        transport=transport,
        tool="partner.echo",
        wallet_id=demo["wallet_id"],
        api_key=demo["api_key"],
        request_key="demo-auto-echo-1",
    )
    first = _result(await _call(client, **call), transport)
    assert first["isError"] is False
    receipt = first["receipt"]
    permit_id = receipt["permit_id"]
    assert first["permit_id"] == permit_id
    permits = await _permits_for(demo["wallet_id"])
    assert len(permits) == 1
    permit = permits[0]
    assert permit.permit_id == permit_id
    assert permit.issuer_wallet_id == demo["wallet_id"]
    assert permit.subject_key_id == demo["key_id"]
    assert json.loads(permit.allowed_tools_json) == ["partner.echo"]
    assert Decimal("0") < permit.max_credits <= demo_settings.DEMO_MAX_PERMIT_CREDITS
    assert (
        timedelta(0)
        < permit.expires_at - permit.issued_at
        <= timedelta(minutes=demo_settings.DEMO_MAX_PERMIT_TTL_MINUTES)
    )

    replay = _result(await _call(client, **call), transport)
    assert replay["receipt"]["receipt_id"] == receipt["receipt_id"]
    assert replay["permit_id"] == permit_id
    assert len(await _permits_for(demo["wallet_id"])) == 1
    assert echo_tools == ["partner.echo"]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "jsonrpc"])
async def test_demo_call_without_request_key_mints_permit_and_receipt(
    client: AsyncClient, clean_database, demo_settings, echo_tools, transport: str
) -> None:
    demo = await _demo_key(client)
    response = await _call(
        client,
        transport=transport,
        tool="partner.echo",
        wallet_id=demo["wallet_id"],
        api_key=demo["api_key"],
        request_key=None,
    )
    result = _result(response, transport)
    assert result["isError"] is False
    assert result["permit_id"] == result["receipt"]["permit_id"]
    assert len(await _permits_for(demo["wallet_id"])) == 1
    assert echo_tools == ["partner.echo"]


@pytest.mark.anyio
async def test_standard_mcp_demo_permit_uses_configured_caps_and_denies_other_tool(
    client: AsyncClient,
    clean_database,
    demo_settings,
    echo_tools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo_settings, "ENABLE_STANDARD_MCP_ENDPOINT", True)
    monkeypatch.setattr(demo_settings, "DEMO_AUTO_PERMIT_MAX_CREDITS", Decimal("2"))
    monkeypatch.setattr(demo_settings, "DEMO_AUTO_PERMIT_TTL_SECONDS", 60)
    demo = await _demo_key(client)

    before = utc_now()
    success = await _standard_call(
        client,
        tool="partner.echo",
        api_key=demo["api_key"],
        request_key="demo-standard-echo-1",
    )
    after = utc_now()
    assert success.status_code == 200, success.text
    result = success.json()["result"]
    assert result["isError"] is False
    receipt = result["receipt"]
    assert result["permit_id"] == receipt["permit_id"]
    permits = await _permits_for(demo["wallet_id"])
    assert len(permits) == 1
    permit = permits[0]
    assert permit.permit_id == receipt["permit_id"]
    assert permit.max_credits == demo_settings.DEMO_AUTO_PERMIT_MAX_CREDITS
    assert before + timedelta(seconds=60) <= permit.expires_at
    assert permit.expires_at <= after + timedelta(seconds=60)

    denied = await _standard_call(
        client,
        tool="partner.other",
        api_key=demo["api_key"],
        request_key="demo-standard-other-1",
    )
    assert denied.status_code == 200, denied.text
    envelope = denied.json()["error"]["data"]
    assert envelope["code"] == "demo_tool_not_allowed"
    assert envelope["message"]
    assert envelope["fix"]
    assert envelope["docs_url"].endswith("#demo_tool_not_allowed")
    assert len(await _permits_for(demo["wallet_id"])) == 1
    assert echo_tools == ["partner.echo"]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "jsonrpc"])
async def test_demo_key_cannot_auto_mint_for_other_tool(
    client: AsyncClient, clean_database, demo_settings, echo_tools, transport: str
) -> None:
    demo = await _demo_key(client)
    response = await _call(
        client,
        transport=transport,
        tool="partner.other",
        wallet_id=demo["wallet_id"],
        api_key=demo["api_key"],
        request_key="demo-auto-other-1",
    )
    body = response.json()
    code = body["code"] if transport == "rest" else body["error"]["data"]["code"]
    assert code in {"key_tool_not_allowed", "demo_tool_not_allowed"}
    _error(response, transport, code)
    assert await _permits_for(demo["wallet_id"]) == []
    assert echo_tools == []


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "jsonrpc"])
async def test_demo_auto_mint_refuses_tool_above_credit_cap(
    client: AsyncClient,
    clean_database,
    demo_settings,
    echo_tools,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    monkeypatch.setattr(demo_settings, "DEMO_AUTO_PERMIT_MAX_CREDITS", Decimal("1"))
    demo = await _demo_key(client)
    response = await _call(
        client,
        transport=transport,
        tool="partner.echo",
        wallet_id=demo["wallet_id"],
        api_key=demo["api_key"],
        request_key="demo-auto-over-cap-1",
    )
    _error(response, transport, "demo_auto_permit_budget_exceeded")
    assert await _permits_for(demo["wallet_id"]) == []
    assert echo_tools == []


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "jsonrpc"])
async def test_demo_auto_mint_preserves_insufficient_funds_error(
    client: AsyncClient,
    clean_database,
    demo_settings,
    echo_tools,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    monkeypatch.setattr(demo_settings, "DEMO_WALLET_CREDITS", Decimal("3"))
    demo = await _demo_key(client)
    call = dict(
        transport=transport,
        tool="partner.echo",
        wallet_id=demo["wallet_id"],
        api_key=demo["api_key"],
    )
    first = _result(
        await _call(client, **call, request_key="demo-funds-first"), transport
    )
    assert first["isError"] is False

    response = await _call(client, **call, request_key="demo-funds-second")
    body = response.json()
    if transport == "rest":
        assert response.status_code == 402, response.text
        assert body["detail"]["error"] == "authority_required"
        envelope = body
    else:
        assert response.status_code == 200, response.text
        assert body["error"]["code"] == -32004
        assert body["error"]["message"] == "permit_budget_exceeds_wallet_balance"
        envelope = body["error"]["data"]
    detail = body["detail"] if transport == "rest" else envelope
    assert detail["reason_code"] == "permit_budget_exceeds_wallet_balance"
    assert detail["requested"]["tool"] == "partner.echo"
    assert detail["requested"]["estimated_credits"] == "2.0"
    assert detail["remediation"]["type"] == "fund_wallet_or_request_authority"
    assert envelope["code"] == "permit_budget_exceeds_wallet_balance"
    assert envelope["message"]
    assert envelope["fix"]
    assert envelope["docs_url"].endswith("#permit_budget_exceeds_wallet_balance")
    assert len(await _permits_for(demo["wallet_id"])) == 1
    assert echo_tools == ["partner.echo"]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "jsonrpc"])
async def test_demo_auto_mint_refuses_empty_tool_allowlist(
    client: AsyncClient,
    clean_database,
    demo_settings,
    echo_tools,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    monkeypatch.setattr(demo_settings, "DEMO_ALLOWED_TOOLS", "")
    demo = await _demo_key(client)
    assert demo["allowed_tools"] is None
    response = await _call(
        client,
        transport=transport,
        tool="partner.echo",
        wallet_id=demo["wallet_id"],
        api_key=demo["api_key"],
        request_key="demo-auto-empty-tools-1",
    )
    _error(response, transport, "demo_tool_not_allowed")
    assert await _permits_for(demo["wallet_id"]) == []
    assert echo_tools == []


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "jsonrpc"])
async def test_demo_key_cannot_auto_mint_for_another_wallet(
    client: AsyncClient, clean_database, demo_settings, echo_tools, transport: str
) -> None:
    normal = await provision_agent_wallet(client)
    demo = await _demo_key(client)
    response = await _call(
        client,
        transport=transport,
        tool="partner.echo",
        wallet_id=normal["agent_wallet_id"],
        api_key=demo["api_key"],
        request_key="demo-auto-other-wallet-1",
    )
    _error(response, transport, "wallet_access_denied")
    assert await _permits_for(demo["wallet_id"]) == []
    assert await _permits_for(normal["agent_wallet_id"]) == []
    assert echo_tools == []


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "jsonrpc"])
async def test_normal_key_still_requires_permit_and_unknown_id_is_not_found(
    client: AsyncClient, clean_database, demo_settings, echo_tools, transport: str
) -> None:
    normal = await provision_agent_wallet(client)
    call = dict(
        transport=transport,
        tool="partner.echo",
        wallet_id=normal["agent_wallet_id"],
        api_key=normal["agent_headers"]["X-API-Key"],
    )
    missing = await _call(client, **call, request_key="normal-no-permit-1")
    _error(missing, transport, "permit_required")
    unknown = await _call(
        client, **call, request_key="normal-unknown-permit-1", permit_id="missing"
    )
    _error(unknown, transport, "permit_not_found")
    assert await _permits_for(normal["agent_wallet_id"]) == []
    assert echo_tools == []


@pytest.mark.anyio
async def test_normal_key_manual_rest_permit_omits_auto_permit_response_field(
    client: AsyncClient, clean_database, demo_settings, echo_tools
) -> None:
    normal = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=normal["agent_wallet_id"],
        key_id=normal["key_id"],
        tool_name="partner.echo",
        idem_key="normal-manual-permit-1",
    )
    response = await _call(
        client,
        transport="rest",
        tool="partner.echo",
        wallet_id=normal["agent_wallet_id"],
        api_key=normal["agent_headers"]["X-API-Key"],
        request_key="normal-manual-echo-1",
        permit_id=permit["permit_id"],
    )
    result = _result(response, "rest")
    assert result["receipt"]["permit_id"] == permit["permit_id"]
    assert "permit_id" not in result
    assert echo_tools == ["partner.echo"]


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["rest", "jsonrpc"])
@pytest.mark.parametrize("denial", ["budget", "expiry"])
async def test_demo_auto_permit_enforces_budget_and_expiry(
    client: AsyncClient,
    clean_database,
    demo_settings,
    echo_tools,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    denial: str,
) -> None:
    demo = await _demo_key(client)
    call = dict(
        transport=transport,
        tool="partner.echo",
        wallet_id=demo["wallet_id"],
        api_key=demo["api_key"],
    )
    first = _result(
        await _call(client, **call, request_key="demo-auto-first"), transport
    )
    permit_id = first["receipt"]["permit_id"]
    if denial == "expiry":
        permit = (await _permits_for(demo["wallet_id"]))[0]
        monkeypatch.setattr(
            "app.services.permits.utc_now",
            lambda: permit.expires_at + timedelta(seconds=1),
        )
    denied = await _call(
        client,
        **call,
        request_key="demo-auto-fresh",
        permit_id=permit_id,
    )
    expected = "permit_budget_exceeded" if denial == "budget" else "permit_expired"
    _error(denied, transport, expected)
    assert len(await _permits_for(demo["wallet_id"])) == 1
    assert echo_tools == ["partner.echo"]
