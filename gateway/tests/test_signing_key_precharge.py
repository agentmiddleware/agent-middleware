"""A replica whose signing key cannot sign refuses before it charges.

Every governed outcome past the charge needs a signed audit event and a signed
receipt. When the configured key cannot sign (its row is disabled, retired or
bound to other key material) the old order was: reserve, charge, run the tool,
then fail to sign. The wallet was debited and the tool had run, but nothing
recorded either, and the idempotency record stayed in progress for manual
review (issue #280).

``SigningKeyService.assert_ready_to_sign`` runs the same resolution every
signature runs, before anything is begun, reserved or charged, and the routers
answer its ``SigningKeyUnavailableError`` as retryable (``-32005`` / HTTP 409):
nothing was recorded, so the same key may be retried on a replica whose key
can sign. These tests pin that ordering on each governed transport, and that
the health checks report an unusable key row without changing it.
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.health import gather_dependency_report
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, ReceiptModel, SigningKeyModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from app.services.signing_keys import (
    SigningKeyService,
    SigningKeyUnavailableError,
    get_signing_key_service,
)
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

TOOL_COST = 2.0
TOOL_NAME = "signing-precharge-echo"


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def echo_tool():
    runs = {"count": 0}

    def echo(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        return {"message": message}

    get_service_registry().register_local(
        service_id=TOOL_NAME,
        name="Signing precharge echo",
        description="Signing-key pre-charge surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        yield runs
    finally:
        get_service_registry().unregister_local(TOOL_NAME)


async def _set_key_status(key_id: str, status: str) -> None:
    async with get_session_factory()() as session:
        row = await session.get(SigningKeyModel, key_id)
        assert row is not None
        row.status = status
        session.add(row)
        await session.commit()


async def _key_row(key_id: str) -> SigningKeyModel:
    async with get_session_factory()() as session:
        row = await session.get(SigningKeyModel, key_id)
        assert row is not None
        return row


async def _count(model: Any, *conditions: Any) -> int:
    async with get_session_factory()() as session:
        query = select(func.count()).select_from(model)
        for condition in conditions:
            query = query.where(condition)
        return int(await session.scalar(query) or 0)


async def _debits(wallet_id: str) -> int:
    return await _count(
        LedgerEntryModel,
        LedgerEntryModel.wallet_id == wallet_id,
        LedgerEntryModel.action == "debit",
    )


async def _setup(client: AsyncClient, *, suffix: str) -> dict[str, Any]:
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=TOOL_NAME,
        idem_key=f"signing-precharge-permit-{suffix}",
    )
    key = await get_signing_key_service().ensure_active_key()
    return {"ctx": ctx, "permit": permit, "key_id": key.key_id}


def _jsonrpc_body(*, wallet_id: str, permit_id: str, key: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": f"call-{key}",
        "method": "tools/call",
        "params": {
            "name": TOOL_NAME,
            "arguments": {"message": "hello"},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": key,
            },
        },
    }


def _rest_body(*, wallet_id: str, permit_id: str, key: str) -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "arguments": {"message": "hello"},
        "mcp_context": {
            "wallet_id": wallet_id,
            "permit_id": permit_id,
            "idempotency_key": key,
        },
    }


@pytest.mark.anyio
async def test_assert_ready_to_sign_refuses_a_disabled_key_without_touching_it(
    clean_database: None,
) -> None:
    service = get_signing_key_service()
    key = await service.ensure_active_key()
    await _set_key_status(key.key_id, "disabled")

    with pytest.raises(SigningKeyUnavailableError) as raised:
        await service.assert_ready_to_sign()

    assert str(raised.value) == "signing_key_unavailable"
    assert raised.value.cause == "signing_key_disabled"
    assert (await _key_row(key.key_id)).status == "disabled"


@pytest.mark.anyio
async def test_assert_ready_to_sign_refuses_mismatched_key_material(
    clean_database: None,
) -> None:
    original = await get_signing_key_service().ensure_active_key()
    misconfigured = SigningKeyService()
    misconfigured._private_key = Ed25519PrivateKey.generate()
    misconfigured._key_id = original.key_id

    with pytest.raises(SigningKeyUnavailableError) as raised:
        await misconfigured.assert_ready_to_sign()

    assert raised.value.cause == "signing_key_id_public_key_mismatch"
    row = await _key_row(original.key_id)
    assert row.public_key_b64 == original.public_key_b64
    assert row.status == "active"


@pytest.mark.anyio
async def test_assert_ready_to_sign_passes_for_a_usable_key(
    clean_database: None,
) -> None:
    await get_signing_key_service().assert_ready_to_sign()


@pytest.mark.anyio
async def test_jsonrpc_refuses_before_the_charge_and_the_key_stays_retryable(
    client: AsyncClient, clean_database: None, echo_tool
) -> None:
    case = await _setup(client, suffix="jsonrpc")
    ctx = case["ctx"]
    wallet_id = ctx["agent_wallet_id"]
    body = _jsonrpc_body(
        wallet_id=wallet_id,
        permit_id=case["permit"]["permit_id"],
        key="signing-precharge-jsonrpc",
    )
    debits_before = await _debits(wallet_id)
    receipts_before = await _count(ReceiptModel)
    await _set_key_status(case["key_id"], "disabled")

    resp = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32005, error
    assert error["message"] == "signing_key_unavailable", error
    # Nothing moved: no debit, no receipt, the tool never ran.
    assert await _debits(wallet_id) == debits_before
    assert await _count(ReceiptModel) == receipts_before
    assert echo_tool["count"] == 0

    # The retry the -32005 promised lands once the key can sign again.
    await _set_key_status(case["key_id"], "active")
    again = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert again.status_code == 200, again.text
    assert "error" not in again.json(), again.text
    assert echo_tool["count"] == 1
    assert await _debits(wallet_id) == debits_before + 1


@pytest.mark.anyio
async def test_rest_invoke_refuses_before_the_charge_with_409(
    client: AsyncClient, clean_database: None, echo_tool
) -> None:
    case = await _setup(client, suffix="rest")
    ctx = case["ctx"]
    wallet_id = ctx["agent_wallet_id"]
    body = _rest_body(
        wallet_id=wallet_id,
        permit_id=case["permit"]["permit_id"],
        key="signing-precharge-rest",
    )
    debits_before = await _debits(wallet_id)
    await _set_key_status(case["key_id"], "disabled")

    resp = await client.post(
        f"/mcp/tools/{TOOL_NAME}/invoke", json=body, headers=ctx["agent_headers"]
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "signing_key_unavailable"
    assert await _debits(wallet_id) == debits_before
    assert echo_tool["count"] == 0

    await _set_key_status(case["key_id"], "active")
    again = await client.post(
        f"/mcp/tools/{TOOL_NAME}/invoke", json=body, headers=ctx["agent_headers"]
    )
    assert again.status_code == 200, again.text
    assert again.json()["isError"] is False, again.text
    assert echo_tool["count"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error"),
    [("retired", "signing_key_retired"), ("disabled", "signing_key_disabled")],
)
async def test_health_reports_an_unusable_key_row_without_changing_it(
    client: AsyncClient, clean_database: None, status: str, error: str
) -> None:
    key = await get_signing_key_service().ensure_active_key()
    await _set_key_status(key.key_id, status)
    before = await _key_row(key.key_id)

    report = await gather_dependency_report()
    signing_key = report["dependencies"]["signing_key"]
    assert signing_key["status"] == "down", signing_key
    assert signing_key["error"] == error, signing_key
    assert "signing_key" in report["unhealthy"]

    ready = await client.get("/health/ready")
    assert ready.status_code == 503, ready.text
    assert ready.json()["checks"]["signing_key"]["error"] == error

    # A health probe never reactivates or otherwise rewrites the row.
    after = await _key_row(key.key_id)
    assert after.status == status
    assert after.activated_at == before.activated_at
    assert after.retired_at == before.retired_at


@pytest.mark.anyio
async def test_health_reports_a_usable_key_as_up(
    client: AsyncClient, clean_database: None
) -> None:
    await get_signing_key_service().ensure_active_key()

    ready = await client.get("/health/ready")
    assert ready.json()["checks"]["signing_key"]["status"] == "up", ready.text


@pytest.fixture
def standard_mcp_enabled(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


@pytest.mark.anyio
async def test_standard_mcp_refuses_before_the_mint_and_the_charge(
    client: AsyncClient, clean_database: None, standard_mcp_enabled, echo_tool
) -> None:
    ctx = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    key = await get_signing_key_service().ensure_active_key()
    request = {
        "json": {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": {"message": "hello"}},
        },
        "headers": {
            **ctx["agent_headers"],
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Idempotency-Key": "signing-precharge-standard-1",
        },
    }
    debits_before = await _debits(wallet_id)
    await _set_key_status(key.key_id, "disabled")

    resp = await client.post("/mcp", **request)

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32005, error
    assert error["message"] == "signing_key_unavailable", error
    assert await _debits(wallet_id) == debits_before
    assert echo_tool["count"] == 0

    await _set_key_status(key.key_id, "active")
    again = await client.post("/mcp", **request)
    assert again.status_code == 200, again.text
    assert "error" not in again.json(), again.text
    assert echo_tool["count"] == 1


@pytest.mark.anyio
async def test_billing_charge_refuses_before_the_debit_and_the_key_stays_retryable(
    client: AsyncClient, clean_database: None
) -> None:
    """``POST /v1/billing/charge`` debits, then signs its governance audit event.

    With a key that cannot sign it used to debit and then fail: HTTP 500, one
    debit, no audit event, and a retry of the same key answered
    ``idempotency_in_progress``. It now refuses first.
    """
    ctx = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    key = await get_signing_key_service().ensure_active_key()
    url = (
        f"/v1/billing/charge?wallet_id={wallet_id}"
        "&service=agent_comms&units=2&request_path=POST+/v1/agent-comms/send"
    )
    headers = {**ctx["agent_headers"], "Idempotency-Key": "signing-precharge-billing"}
    debits_before = await _debits(wallet_id)
    await _set_key_status(key.key_id, "disabled")

    refused = await client.post(url, headers=headers)

    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"]["error"] == "signing_key_unavailable"
    assert await _debits(wallet_id) == debits_before

    await _set_key_status(key.key_id, "active")
    again = await client.post(url, headers=headers)
    assert again.status_code == 200, again.text
    assert await _debits(wallet_id) == debits_before + 1


@pytest.mark.anyio
async def test_the_refusal_logs_its_underlying_reason_once(
    clean_database: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Callers answer with the canned reason; the cause is in the server log."""
    service = get_signing_key_service()
    key = await service.ensure_active_key()
    await _set_key_status(key.key_id, "disabled")

    with caplog.at_level("WARNING", logger="app.services.signing_keys"):
        with pytest.raises(SigningKeyUnavailableError):
            await service.assert_ready_to_sign()

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.services.signing_keys"
    ]
    assert len(lines) == 1, lines
    assert "cause=signing_key_disabled" in lines[0]
    assert key.key_id in lines[0]
