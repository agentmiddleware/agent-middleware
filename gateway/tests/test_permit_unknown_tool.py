"""A permit's allowlist is checked before the tool registry.

When a governed call carries an explicit permit and names a tool the permit
does not allow, the refusal is ``permit_tool_not_allowed`` (-32003 / HTTP 403)
with a signed denial receipt, whether or not the tool is registered. Calls
without a permit, and tools the permit does name, keep "Tool not found".
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, PermitModel, ReceiptModel
from app.main import app
from app.routers import mcp as mcp_router
from app.services.dogfood_tool import (
    DOGFOOD_SECOND_TOOL_ID,
    DOGFOOD_TOOL_ID,
    sync_dogfood_tool_registration,
)
from app.services.receipts import get_receipt_service
from app.services.service_registry import get_service_registry
from tests.test_mcp_trust_mode import (
    _assert_denial_audited,
    _assert_no_tool_debits,
    _jsonrpc_body,
    strict_trust_mode,  # noqa: F401 - fixture
)
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}

# Real product tool ids from app/services/dogfood_tool.py. The fixture below
# registers partner.notes.write (ENABLE_DOGFOOD_TOOL) and keeps
# partner.notes.count unregistered (ENABLE_DOGFOOD_SECOND_TOOL off).
ALLOWED = DOGFOOD_TOOL_ID
UNKNOWN = DOGFOOD_SECOND_TOOL_ID


@pytest.fixture(autouse=True)
def real_tools(monkeypatch):
    monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "true")
    monkeypatch.setenv("ENABLE_DOGFOOD_SECOND_TOOL", "false")
    get_settings.cache_clear()
    sync_dogfood_tool_registration()
    registry = get_service_registry()
    assert registry.get_local(ALLOWED) is not None
    assert registry.get_local(UNKNOWN) is None
    yield
    monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "false")
    get_settings.cache_clear()
    sync_dogfood_tool_registration()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def standard_mcp_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


async def _permit_for_allowed_tool(client: AsyncClient) -> tuple[dict, dict]:
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=ALLOWED,
    )
    return provisioned, permit


async def _assert_denial_receipt(receipt: dict, *, permit_id: str, wallet_id: str):
    assert receipt["outcome"] == "denied"
    assert receipt["reason_code"] == "permit_tool_not_allowed"
    assert receipt["permit_id"] == permit_id
    assert receipt["wallet_id"] == wallet_id
    assert receipt["tool"] == UNKNOWN
    assert receipt["ledger_entry_id"] is None
    assert str(receipt["credits_charged"]) in {"0", "0.0", "0.00"}
    assert (await get_receipt_service().verify_receipt(receipt["receipt_id"]))[0]


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
async def test_jsonrpc_unregistered_tool_outside_permit_is_permit_denial(
    client, clean_database
):
    provisioned, permit = await _permit_for_allowed_tool(client)
    resp = await client.post(
        "/mcp/messages",
        json=_jsonrpc_body(
            tool_name=UNKNOWN,
            wallet_id=provisioned["agent_wallet_id"],
            request_id=f"unknown-tool-permit-call-{uuid4()}",
            permit_id=permit["permit_id"],
            idempotency_key=f"unknown-tool-permit-idem-{uuid4()}",
        ),
        headers=provisioned["agent_headers"],
    )

    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "permit_tool_not_allowed"
    assert error["data"]["details"]["requested_tool"] == UNKNOWN
    assert error["data"]["details"]["allowed_tools"] == [ALLOWED]
    await _assert_denial_receipt(
        error["data"]["receipt"],
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
    )
    await _assert_denial_audited(
        wallet_id=provisioned["agent_wallet_id"],
        tool_name=UNKNOWN,
        reason="permit_tool_not_allowed",
    )
    await _assert_no_tool_debits(
        client=client,
        wallet_id=provisioned["agent_wallet_id"],
        headers=provisioned["agent_headers"],
        tool_name=UNKNOWN,
    )


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
async def test_jsonrpc_permit_denial_for_unregistered_tool_replays(
    client, clean_database
):
    provisioned, permit = await _permit_for_allowed_tool(client)
    body = _jsonrpc_body(
        tool_name=UNKNOWN,
        wallet_id=provisioned["agent_wallet_id"],
        request_id=f"unknown-tool-permit-replay-{uuid4()}",
        permit_id=permit["permit_id"],
        idempotency_key=f"unknown-tool-permit-replay-idem-{uuid4()}",
    )
    first = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    replay = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    first_error = first.json()["error"]
    replay_error = replay.json()["error"]
    assert first_error["code"] == replay_error["code"] == -32003
    assert first_error["message"] == "permit_tool_not_allowed"
    assert replay_error["message"] == first_error["message"]
    expected_details = {
        "requested_tool": UNKNOWN,
        "allowed_tools": [ALLOWED],
    }
    assert first_error["data"]["details"] == expected_details
    assert replay_error["data"]["details"] == expected_details
    assert first_error["data"]["receipt"] == replay_error["data"]["receipt"]
    await _assert_denial_receipt(
        replay_error["data"]["receipt"],
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
    )
    async with get_session_factory()() as session:
        receipts = await session.scalar(
            select(func.count())
            .select_from(ReceiptModel)
            .where(
                ReceiptModel.wallet_id == provisioned["agent_wallet_id"],
                ReceiptModel.tool == UNKNOWN,
            )
        )
        debits = await session.scalar(
            select(func.count())
            .select_from(LedgerEntryModel)
            .where(
                LedgerEntryModel.wallet_id == provisioned["agent_wallet_id"],
                LedgerEntryModel.action == "debit",
            )
        )
    assert receipts == 1
    assert debits == 0


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
async def test_rest_unregistered_tool_outside_permit_is_403_with_receipt(
    client, clean_database
):
    provisioned, permit = await _permit_for_allowed_tool(client)
    resp = await client.post(
        f"/mcp/tools/{UNKNOWN}/invoke",
        json={
            "name": UNKNOWN,
            "arguments": {"message": "hello"},
            "mcp_context": {
                "wallet_id": provisioned["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": f"unknown-tool-permit-rest-idem-{uuid4()}",
            },
        },
        headers=provisioned["agent_headers"],
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "permit_tool_not_allowed"
    await _assert_denial_receipt(
        detail["receipt"],
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
    )


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
async def test_unregistered_tool_named_by_permit_is_still_not_found(
    client, clean_database
):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=UNKNOWN,
    )
    body = {
        "name": UNKNOWN,
        "arguments": {"message": "hello"},
        "mcp_context": {
            "wallet_id": provisioned["agent_wallet_id"],
            "permit_id": permit["permit_id"],
            "idempotency_key": "named-unknown-rest-idem",
        },
    }
    rest = await client.post(
        f"/mcp/tools/{UNKNOWN}/invoke", json=body, headers=provisioned["agent_headers"]
    )
    assert rest.status_code == 404
    rpc = await client.post(
        "/mcp/messages",
        json=_jsonrpc_body(
            tool_name=UNKNOWN,
            wallet_id=provisioned["agent_wallet_id"],
            request_id=f"named-unknown-rpc-{uuid4()}",
            permit_id=permit["permit_id"],
            idempotency_key=f"named-unknown-rpc-idem-{uuid4()}",
        ),
        headers=provisioned["agent_headers"],
    )
    assert rpc.json()["error"]["code"] == -32001


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
async def test_unregistered_tool_without_permit_is_unchanged(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    rpc = await client.post(
        "/mcp/messages",
        json=_jsonrpc_body(
            tool_name=UNKNOWN,
            wallet_id=provisioned["agent_wallet_id"],
            request_id=f"no-permit-unknown-{uuid4()}",
            idempotency_key=f"no-permit-unknown-idem-{uuid4()}",
        ),
        headers=provisioned["agent_headers"],
    )
    assert rpc.json()["error"]["code"] == -32001
    assert rpc.json()["error"]["message"] == f"Tool not found: {UNKNOWN}"
    assert mcp_router.settings.TRUST_MODE_ENABLED is True


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
async def test_standard_mcp_explicit_permit_unregistered_tool_is_permit_denial(
    client, clean_database, standard_mcp_enabled
):
    provisioned, permit = await _permit_for_allowed_tool(client)
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": UNKNOWN,
                "arguments": {},
                "_meta": {"io.agentmiddleware/permit_id": permit["permit_id"]},
            },
        },
        headers={**provisioned["agent_headers"], **MCP_HEADERS},
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "permit_tool_not_allowed"
    await _assert_denial_receipt(
        error["data"]["receipt"],
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
    )


@pytest.mark.anyio
async def test_standard_mcp_without_permit_unknown_tool_still_not_found(
    client, clean_database, standard_mcp_enabled
):
    """No permit is auto-minted for a tool that does not exist."""
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": UNKNOWN, "arguments": {}},
        },
        headers={**provisioned["agent_headers"], **MCP_HEADERS},
    )
    assert resp.json()["error"]["code"] == -32001


async def _receipt_count(wallet_id: str) -> int:
    async with get_session_factory()() as session:
        return await session.scalar(
            select(func.count())
            .select_from(ReceiptModel)
            .where(ReceiptModel.wallet_id == wallet_id)
        )


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
async def test_foreign_wallet_permit_does_not_leak_allowlist(client, clean_database):
    """Another wallet's permit is validated before its allowlist is read.

    The caller gets the same wallet-mismatch denial whether or not the foreign
    permit names the tool, and never sees that permit's allowed_tools.
    """
    victim = await provision_agent_wallet(client)
    names_unknown = await create_tool_permit(
        client,
        wallet_id=victim["agent_wallet_id"],
        key_id=victim["key_id"],
        tool_name=UNKNOWN,
        idem_key="victim-permit-names-unknown",
    )
    names_other = await create_tool_permit(
        client,
        wallet_id=victim["agent_wallet_id"],
        key_id=victim["key_id"],
        tool_name=ALLOWED,
        idem_key="victim-permit-names-other",
    )
    attacker = await provision_agent_wallet(client)
    errors = []
    for permit in (names_unknown, names_other):
        resp = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=UNKNOWN,
                wallet_id=attacker["agent_wallet_id"],
                request_id=f"foreign-permit-{uuid4()}",
                permit_id=permit["permit_id"],
                idempotency_key=f"foreign-permit-idem-{uuid4()}",
            ),
            headers=attacker["agent_headers"],
        )
        errors.append(resp.json()["error"])
    for error in errors:
        assert error["code"] == -32003
        assert error["message"] == "permit_wallet_mismatch"
        assert "allowed_tools" not in str(error.get("data", {}).get("details"))
        assert ALLOWED not in str(error)
    assert errors[0]["message"] == errors[1]["message"]


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
async def test_permit_denial_without_idempotency_key_mints_no_receipt(
    client, clean_database
):
    provisioned, permit = await _permit_for_allowed_tool(client)
    before = await _receipt_count(provisioned["agent_wallet_id"])
    resp = await client.post(
        "/mcp/messages",
        json=_jsonrpc_body(
            tool_name=UNKNOWN,
            wallet_id=provisioned["agent_wallet_id"],
            request_id=f"no-idem-unknown-{uuid4()}",
            permit_id=permit["permit_id"],
        ),
        headers=provisioned["agent_headers"],
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "permit_tool_not_allowed"
    assert not error.get("data", {}).get("receipt")
    assert await _receipt_count(provisioned["agent_wallet_id"]) == before


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
async def test_action_permit_unregistered_tool_gets_permit_decision(
    client, clean_database
):
    """Action permits are no longer excluded from the permit-scope check."""
    provisioned, permit = await _permit_for_allowed_tool(client)
    async with get_session_factory()() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        model.action_contract_version = 1
        await session.commit()
    before = await _receipt_count(provisioned["agent_wallet_id"])
    resp = await client.post(
        "/mcp/messages",
        json=_jsonrpc_body(
            tool_name=UNKNOWN,
            wallet_id=provisioned["agent_wallet_id"],
            request_id=f"action-permit-unknown-{uuid4()}",
            permit_id=permit["permit_id"],
            idempotency_key=f"action-permit-unknown-idem-{uuid4()}",
        ),
        headers=provisioned["agent_headers"],
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] != "action_tool_binding_required"
    assert error["message"].startswith("permit_")
    # No idempotency record exists on the action path, so no receipt.
    assert await _receipt_count(provisioned["agent_wallet_id"]) == before


async def _forge_signature(permit_id: str) -> None:
    async with get_session_factory()() as session:
        model = await session.get(PermitModel, permit_id)
        sig = model.signature
        model.signature = ("A" if sig[0] != "A" else "B") + sig[1:]
        await session.commit()


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
@pytest.mark.parametrize("action", [False, True], ids=["tool", "action"])
async def test_bad_signature_rejected_before_allowlist_is_read(
    client, clean_database, action
):
    """A tampered permit is refused on its signature, never on its allowlist.

    Before the fix the allowlist was read (and echoed) before the signature
    was checked, so a forged row produced permit_tool_not_allowed with the
    row's allowed_tools in the details.
    """
    provisioned, permit = await _permit_for_allowed_tool(client)
    await _forge_signature(permit["permit_id"])
    if action:
        async with get_session_factory()() as session:
            model = await session.get(PermitModel, permit["permit_id"])
            model.action_contract_version = 1
            await session.commit()
    before = await _receipt_count(provisioned["agent_wallet_id"])
    resp = await client.post(
        "/mcp/messages",
        json=_jsonrpc_body(
            tool_name=UNKNOWN,
            wallet_id=provisioned["agent_wallet_id"],
            request_id=f"forged-permit-{uuid4()}",
            permit_id=permit["permit_id"],
            idempotency_key=f"forged-permit-idem-{uuid4()}",
        ),
        headers=provisioned["agent_headers"],
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "permit_signature_invalid"
    assert "allowed_tools" not in str(error)
    assert ALLOWED not in str(error)
    if action:
        assert await _receipt_count(provisioned["agent_wallet_id"]) == before


@pytest.mark.anyio
@pytest.mark.usefixtures("strict_trust_mode")
async def test_bad_signature_rejected_before_any_permit_field(client, clean_database):
    """The signature is checked before status, expiry or binding are read.

    A forged row that is also marked revoked must still be refused as
    permit_signature_invalid, not answered from its unsigned status.
    """
    provisioned, permit = await _permit_for_allowed_tool(client)
    await _forge_signature(permit["permit_id"])
    async with get_session_factory()() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        model.status = "revoked"
        await session.commit()
    resp = await client.post(
        "/mcp/messages",
        json=_jsonrpc_body(
            tool_name=UNKNOWN,
            wallet_id=provisioned["agent_wallet_id"],
            request_id=f"forged-revoked-{uuid4()}",
            permit_id=permit["permit_id"],
            idempotency_key=f"forged-revoked-idem-{uuid4()}",
        ),
        headers=provisioned["agent_headers"],
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "permit_signature_invalid"
    assert "allowed_tools" not in str(error)
