from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, WalletModel
from app.main import app
from app.routers import mcp as mcp_router
from app.schemas.billing import ServiceCategory
from app.services.audit_log import list_audit_events
from app.services.idempotency import GOVERNED_MCP_IDEMPOTENCY_ENDPOINT
from app.services.signing_keys import get_signing_key_service
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


@pytest.fixture
def strict_trust_mode(monkeypatch):
    raw_private_key = Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw,
        PrivateFormat.Raw,
        NoEncryption(),
    )
    monkeypatch.setattr(mcp_router.settings, "TRUST_MODE_ENABLED", True)
    monkeypatch.setattr(mcp_router.settings, "ALLOW_LEGACY_UNPERMITTED_MCP", False)
    monkeypatch.setattr(
        mcp_router.settings,
        "TRUST_SIGNING_PRIVATE_KEY_B64",
        base64.b64encode(raw_private_key).decode(),
    )
    signing_keys = get_signing_key_service()
    signing_keys._private_key = None


@pytest.fixture
def legacy_trust_mode(monkeypatch):
    monkeypatch.setattr(mcp_router.settings, "TRUST_MODE_ENABLED", False)
    monkeypatch.setattr(mcp_router.settings, "ALLOW_LEGACY_UNPERMITTED_MCP", True)


def _register_echo_tool(tool_name: str) -> None:
    registry = get_service_registry()

    def trust_mode_echo(message: str = "ok") -> dict:
        return {"message": message}

    registry.register_local(
        service_id=tool_name,
        name=f"{tool_name} Echo",
        description="Trust mode enforcement test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=trust_mode_echo,
        credits_per_unit=2.0,
        unit_name="call",
    )


def _jsonrpc_body(
    *,
    tool_name: str,
    wallet_id: str,
    request_id: str,
    permit_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    mcp_context = {"wallet_id": wallet_id}
    if permit_id is not None:
        mcp_context["permit_id"] = permit_id
    if idempotency_key is not None:
        mcp_context["idempotency_key"] = idempotency_key
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"message": "hello"},
            "mcpContext": mcp_context,
        },
    }


async def _assert_denial_audited(
    *,
    wallet_id: str,
    tool_name: str,
    reason: str,
) -> None:
    events = await list_audit_events(
        event="mcp.invoke",
        wallet_id=wallet_id,
        tool=tool_name,
        ok=False,
        limit=5,
    )
    assert events
    assert events[0].error == reason
    assert events[0].policy_decision_id.startswith("pol-")
    assert events[0].metadata["transport"] == "jsonrpc"
    assert events[0].metadata.get("request_hash")


async def _assert_no_tool_debits(
    *,
    client: AsyncClient,
    wallet_id: str,
    headers: dict[str, str],
    tool_name: str,
) -> None:
    ledger_resp = await client.get(
        f"/v1/billing/ledger/{wallet_id}",
        headers=headers,
    )
    assert ledger_resp.status_code == 200
    debits = [
        entry
        for entry in ledger_resp.json()["entries"]
        if entry["service_category"] == "agent_comms"
        and tool_name in entry["description"]
    ]
    assert debits == []


async def _tool_debit_count(
    *,
    client: AsyncClient,
    wallet_id: str,
    headers: dict[str, str],
    tool_name: str,
) -> int:
    ledger_resp = await client.get(
        f"/v1/billing/ledger/{wallet_id}",
        headers=headers,
    )
    assert ledger_resp.status_code == 200
    return len(
        [
            entry
            for entry in ledger_resp.json()["entries"]
            if entry["service_category"] == "agent_comms"
            and entry["action"] == "debit"
            and tool_name in entry["description"]
        ]
    )


async def _governed_idempotency_record_count(
    *,
    wallet_id: str,
    idempotency_key: str,
) -> int:
    factory = get_session_factory()
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecordModel)
            .where(
                IdempotencyRecordModel.wallet_id == wallet_id,
                IdempotencyRecordModel.endpoint == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                IdempotencyRecordModel.idempotency_key == idempotency_key,
            )
        )
    return int(count or 0)


@pytest.mark.anyio
async def test_strict_mode_denies_unpermitted_mcp_invoke_and_audits(
    client,
    clean_database,
    strict_trust_mode,
):
    provisioned = await provision_agent_wallet(client)
    tool_name = "strict-unpermitted-tool"
    registry = get_service_registry()
    _register_echo_tool(tool_name)
    try:
        resp = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                request_id="strict-unpermitted-call",
            ),
            headers=provisioned["agent_headers"],
        )
        payload = resp.json()
        assert resp.status_code == 200
        assert payload["error"]["message"] == "permit_required"
        await _assert_denial_audited(
            wallet_id=provisioned["agent_wallet_id"],
            tool_name=tool_name,
            reason="permit_required",
        )
    finally:
        registry.unregister_local(tool_name)


@pytest.mark.anyio
async def test_strict_mode_missing_permit_denial_leaves_no_idempotency_record(
    client,
    clean_database,
    strict_trust_mode,
):
    """``permit_required`` on a resolved tool records nothing (D2, pinned).

    The governed idempotency record is begun only after the permit check, so
    the denial's completion call is a no-op: a same-key retry repeats the
    denial while nothing changed, and executes exactly once after the caller
    mints a permit. docs/failure-semantics.md and
    docs/authority-required-flow.md (D2) state this; change all three
    together.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    idempotency_key = "strict-missing-permit-replay-idem"
    tool_name = "strict-replay-missing-permit-tool"
    calls = {"count": 0}
    registry = get_service_registry()

    def counted_tool(message: str = "ok") -> dict:
        calls["count"] += 1
        return {"message": message}

    registry.register_local(
        service_id=tool_name,
        name="Strict Replay Missing Permit Tool",
        description="Trust mode missing permit replay test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=counted_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    body = _jsonrpc_body(
        tool_name=tool_name,
        wallet_id=wallet_id,
        request_id="strict-missing-permit-replay-call",
        idempotency_key=idempotency_key,
    )
    try:
        first = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        assert first.status_code == 200
        assert first.json()["error"]["message"] == "permit_required"
        assert replay.status_code == 200
        assert replay.json()["error"]["message"] == "permit_required"
        assert calls["count"] == 0
        await _assert_no_tool_debits(
            client=client,
            wallet_id=wallet_id,
            headers=provisioned["agent_headers"],
            tool_name=tool_name,
        )
        assert (
            await _governed_idempotency_record_count(
                wallet_id=wallet_id, idempotency_key=idempotency_key
            )
            == 0
        )

        permit = await create_tool_permit(
            client,
            wallet_id=wallet_id,
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        granted = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                request_id="strict-missing-permit-resume-call",
                permit_id=permit["permit_id"],
                idempotency_key=idempotency_key,
            ),
            headers=provisioned["agent_headers"],
        )
    finally:
        registry.unregister_local(tool_name)

    assert granted.status_code == 200
    granted_payload = granted.json()
    assert "error" not in granted_payload, granted_payload
    assert granted_payload["result"]["isError"] is False
    assert calls["count"] == 1
    assert (
        await _tool_debit_count(
            client=client,
            wallet_id=wallet_id,
            headers=provisioned["agent_headers"],
            tool_name=tool_name,
        )
        == 1
    )
    assert (
        await _governed_idempotency_record_count(
            wallet_id=wallet_id, idempotency_key=idempotency_key
        )
        == 1
    )


@pytest.mark.anyio
async def test_strict_mode_replays_unknown_permit_denial_with_idempotency_key(
    client,
    clean_database,
    strict_trust_mode,
):
    provisioned = await provision_agent_wallet(client)
    tool_name = "strict-replay-unknown-permit-tool"
    calls = {"count": 0}
    registry = get_service_registry()

    def counted_tool(message: str = "ok") -> dict:
        calls["count"] += 1
        return {"message": message}

    registry.register_local(
        service_id=tool_name,
        name="Strict Replay Unknown Permit Tool",
        description="Trust mode unknown permit replay test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=counted_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    body = _jsonrpc_body(
        tool_name=tool_name,
        wallet_id=provisioned["agent_wallet_id"],
        request_id="strict-unknown-permit-replay-call",
        permit_id="permit-does-not-exist",
        idempotency_key="strict-unknown-permit-replay-idem",
    )
    try:
        first = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
    finally:
        registry.unregister_local(tool_name)

    assert first.status_code == 200
    assert first.json()["error"]["message"] == "permit_not_found"
    assert replay.status_code == 200
    assert replay.json()["error"]["message"] == "permit_not_found"
    assert replay.json()["error"]["message"] != "idempotency_in_progress"
    assert calls["count"] == 0
    # docs/failure-semantics.md: permit_not_found completes a terminal record.
    assert (
        await _governed_idempotency_record_count(
            wallet_id=provisioned["agent_wallet_id"],
            idempotency_key="strict-unknown-permit-replay-idem",
        )
        == 1
    )
    await _assert_no_tool_debits(
        client=client,
        wallet_id=provisioned["agent_wallet_id"],
        headers=provisioned["agent_headers"],
        tool_name=tool_name,
    )


@pytest.mark.anyio
async def test_strict_mode_replays_missing_tool_lookup_error_with_idempotency_key(
    client,
    clean_database,
    strict_trust_mode,
):
    provisioned = await provision_agent_wallet(client)
    tool_name = "strict-missing-lookup-tool"
    body = _jsonrpc_body(
        tool_name=tool_name,
        wallet_id=provisioned["agent_wallet_id"],
        request_id="strict-missing-lookup-call",
        idempotency_key="strict-missing-lookup-idem",
    )

    first = await client.post(
        "/mcp/messages",
        json=body,
        headers=provisioned["agent_headers"],
    )
    replay = await client.post(
        "/mcp/messages",
        json=body,
        headers=provisioned["agent_headers"],
    )

    assert first.status_code == 200
    assert first.json()["error"]["code"] == -32001
    assert first.json()["error"]["message"] == f"Tool not found: {tool_name}"
    assert replay.status_code == 200
    assert replay.json()["error"]["code"] == -32001
    assert replay.json()["error"]["message"] == f"Tool not found: {tool_name}"
    assert replay.json()["error"]["message"] != "idempotency_in_progress"
    # docs/failure-semantics.md: an unknown tool completes a terminal record.
    assert (
        await _governed_idempotency_record_count(
            wallet_id=provisioned["agent_wallet_id"],
            idempotency_key="strict-missing-lookup-idem",
        )
        == 1
    )
    await _assert_no_tool_debits(
        client=client,
        wallet_id=provisioned["agent_wallet_id"],
        headers=provisioned["agent_headers"],
        tool_name=tool_name,
    )


@pytest.mark.anyio
async def test_strict_mode_replays_non_executable_tool_error_with_idempotency_key(
    client,
    clean_database,
    strict_trust_mode,
):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()
    service = await registry.register_persistent(
        name="Strict Non Executable Lookup Tool",
        description="Persistent service without local executable function",
        category=ServiceCategory.AGENT_COMMS,
        credits_per_unit=2.0,
        owner_wallet_id=provisioned["agent_wallet_id"],
    )
    tool_name = service["service_id"]
    body = _jsonrpc_body(
        tool_name=tool_name,
        wallet_id=provisioned["agent_wallet_id"],
        request_id="strict-non-executable-lookup-call",
        idempotency_key="strict-non-executable-lookup-idem",
    )

    first = await client.post(
        "/mcp/messages",
        json=body,
        headers=provisioned["agent_headers"],
    )
    replay = await client.post(
        "/mcp/messages",
        json=body,
        headers=provisioned["agent_headers"],
    )

    assert first.status_code == 200
    assert first.json()["error"]["code"] == -32002
    assert first.json()["error"]["message"] == f"Tool not executable: {tool_name}"
    assert replay.status_code == 200
    assert replay.json()["error"]["code"] == -32002
    assert replay.json()["error"]["message"] == f"Tool not executable: {tool_name}"
    assert replay.json()["error"]["message"] != "idempotency_in_progress"
    # docs/failure-semantics.md: a resolved but non-executable tool is
    # rejected before the governed record is begun, so nothing is recorded.
    assert (
        await _governed_idempotency_record_count(
            wallet_id=provisioned["agent_wallet_id"],
            idempotency_key="strict-non-executable-lookup-idem",
        )
        == 0
    )
    await _assert_no_tool_debits(
        client=client,
        wallet_id=provisioned["agent_wallet_id"],
        headers=provisioned["agent_headers"],
        tool_name=tool_name,
    )


@pytest.mark.anyio
async def test_metadata_only_persistent_service_is_not_mcp_discoverable(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()
    service = await registry.register_persistent(
        name="Metadata Only Service",
        description="Billing catalog metadata without an execution backend",
        category=ServiceCategory.AGENT_COMMS,
        credits_per_unit=2.0,
        owner_wallet_id=provisioned["agent_wallet_id"],
    )

    manifest = await client.get("/mcp/tools.json")
    detail = await client.get(f"/mcp/tools/{service['service_id']}")

    assert manifest.status_code == 200
    assert service["service_id"] not in {
        tool["name"] for tool in manifest.json()["tools"]
    }
    assert detail.status_code == 404
    assert detail.json()["detail"] == (
        f"Executable tool not found: {service['service_id']}"
    )


@pytest.mark.anyio
async def test_strict_mode_denies_missing_idempotency_and_audits(
    client,
    clean_database,
    strict_trust_mode,
):
    provisioned = await provision_agent_wallet(client)
    tool_name = "strict-missing-idempotency-tool"
    registry = get_service_registry()
    _register_echo_tool(tool_name)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        resp = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                request_id="strict-missing-idempotency-call",
                permit_id=permit["permit_id"],
            ),
            headers=provisioned["agent_headers"],
        )
        payload = resp.json()
        assert resp.status_code == 200
        assert payload["error"]["code"] == -32003
        assert payload["error"]["message"] == "idempotency_key_required"
        await _assert_denial_audited(
            wallet_id=provisioned["agent_wallet_id"],
            tool_name=tool_name,
            reason="idempotency_key_required",
        )
    finally:
        registry.unregister_local(tool_name)


@pytest.mark.anyio
async def test_strict_mode_denies_wrong_permit_key_wallet_and_tool_with_audit(
    client,
    clean_database,
    strict_trust_mode,
):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()
    allowed_tool = "strict-permit-allowed-tool"
    wrong_tool = "strict-permit-wrong-tool"
    _register_echo_tool(allowed_tool)
    _register_echo_tool(wrong_tool)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=allowed_tool,
        )

        second_key_resp = await client.post(
            "/v1/api-keys",
            json={
                "wallet_id": provisioned["agent_wallet_id"],
                "key_name": "trust-runtime-wrong-key",
                "expires_in_days": 30,
            },
            headers=BOOTSTRAP_HEADERS,
        )
        assert second_key_resp.status_code == 201
        second_key = second_key_resp.json()["api_key"]

        wrong_key_resp = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=allowed_tool,
                wallet_id=provisioned["agent_wallet_id"],
                request_id="strict-wrong-key-call",
                permit_id=permit["permit_id"],
                idempotency_key="strict-wrong-key-idem",
            ),
            headers={"X-API-Key": second_key},
        )
        assert wrong_key_resp.json()["error"]["message"] == "permit_key_mismatch"
        await _assert_denial_audited(
            wallet_id=provisioned["agent_wallet_id"],
            tool_name=allowed_tool,
            reason="permit_key_mismatch",
        )

        bootstrap_key_resp = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=allowed_tool,
                wallet_id=provisioned["agent_wallet_id"],
                request_id="strict-bootstrap-bound-key-call",
                permit_id=permit["permit_id"],
                idempotency_key="strict-bootstrap-bound-key-idem",
            ),
            headers=BOOTSTRAP_HEADERS,
        )
        assert bootstrap_key_resp.json()["error"]["message"] == "permit_key_mismatch"
        await _assert_denial_audited(
            wallet_id=provisioned["agent_wallet_id"],
            tool_name=allowed_tool,
            reason="permit_key_mismatch",
        )

        wrong_wallet_resp = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=allowed_tool,
                wallet_id=provisioned["sponsor_wallet_id"],
                request_id="strict-wrong-wallet-call",
                permit_id=permit["permit_id"],
                idempotency_key="strict-wrong-wallet-idem",
            ),
            headers=BOOTSTRAP_HEADERS,
        )
        assert wrong_wallet_resp.json()["error"]["message"] == "permit_wallet_mismatch"
        await _assert_denial_audited(
            wallet_id=provisioned["sponsor_wallet_id"],
            tool_name=allowed_tool,
            reason="permit_wallet_mismatch",
        )

        wrong_tool_resp = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=wrong_tool,
                wallet_id=provisioned["agent_wallet_id"],
                request_id="strict-wrong-tool-call",
                permit_id=permit["permit_id"],
                idempotency_key="strict-wrong-tool-idem",
            ),
            headers=provisioned["agent_headers"],
        )
        assert wrong_tool_resp.json()["error"]["message"] == "permit_tool_not_allowed"
        await _assert_denial_audited(
            wallet_id=provisioned["agent_wallet_id"],
            tool_name=wrong_tool,
            reason="permit_tool_not_allowed",
        )
    finally:
        registry.unregister_local(allowed_tool)
        registry.unregister_local(wrong_tool)


@pytest.mark.anyio
async def test_legacy_mode_still_allows_wallet_only_mcp_invoke(
    client,
    clean_database,
    legacy_trust_mode,
):
    provisioned = await provision_agent_wallet(client)
    tool_name = "legacy-wallet-only-tool"
    registry = get_service_registry()
    _register_echo_tool(tool_name)
    try:
        resp = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                request_id="legacy-wallet-only-call",
            ),
            headers=provisioned["agent_headers"],
        )
        payload = resp.json()
        assert resp.status_code == 200
        assert payload["result"]["isError"] is False
        assert "receipt" not in payload["result"]
    finally:
        registry.unregister_local(tool_name)


async def _force_wallet_status(wallet_id: str, status: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        wallet.status = status
        session.add(wallet)
        await session.commit()


async def _force_child_expiry(wallet_id: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        assert wallet.ttl_seconds is not None
        wallet.created_at = utc_now() - timedelta(seconds=wallet.ttl_seconds + 1)
        session.add(wallet)
        await session.commit()


@pytest.mark.anyio
async def test_legacy_mode_expired_wallet_is_permission_denial(
    client,
    clean_database,
    legacy_trust_mode,
):
    """Legacy invokes must surface wallet_expired as a permission denial, not
    a JSON-RPC internal error or an HTTP 400 caller error."""
    provisioned = await provision_agent_wallet(client)
    child_resp = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": provisioned["agent_wallet_id"],
            "child_agent_id": "legacy-expired-child",
            "budget_credits": 50,
            "max_spend": 50,
            "ttl_seconds": 60,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert child_resp.status_code == 201, child_resp.text
    child_wallet_id = child_resp.json()["wallet_id"]
    await _force_child_expiry(child_wallet_id)

    tool_name = "legacy-expired-wallet-tool"
    registry = get_service_registry()
    _register_echo_tool(tool_name)
    try:
        jsonrpc = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=tool_name,
                wallet_id=child_wallet_id,
                request_id="legacy-expired-call",
            ),
            headers=BOOTSTRAP_HEADERS,
        )
        assert jsonrpc.status_code == 200
        error = jsonrpc.json()["error"]
        assert error["code"] == -32003
        assert error["message"] == "wallet_expired"

        http = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json={
                "name": tool_name,
                "arguments": {"message": "hello"},
                "mcp_context": {"wallet_id": child_wallet_id},
            },
            headers=BOOTSTRAP_HEADERS,
        )
        assert http.status_code == 403
        assert http.json()["detail"]["error"] == "wallet_expired"
    finally:
        registry.unregister_local(tool_name)


@pytest.mark.anyio
async def test_legacy_mode_frozen_wallet_is_permission_denial(
    client,
    clean_database,
    legacy_trust_mode,
):
    """Legacy invokes must surface wallet_frozen as a permission denial, not
    a JSON-RPC internal error or an HTTP 400 caller error."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    await _force_wallet_status(wallet_id, "frozen")

    tool_name = "legacy-frozen-wallet-tool"
    registry = get_service_registry()
    _register_echo_tool(tool_name)
    try:
        jsonrpc = await client.post(
            "/mcp/messages",
            json=_jsonrpc_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                request_id="legacy-frozen-call",
            ),
            headers=BOOTSTRAP_HEADERS,
        )
        assert jsonrpc.status_code == 200
        error = jsonrpc.json()["error"]
        assert error["code"] == -32003
        assert error["message"] == "wallet_frozen"

        http = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json={
                "name": tool_name,
                "arguments": {"message": "hello"},
                "mcp_context": {"wallet_id": wallet_id},
            },
            headers=BOOTSTRAP_HEADERS,
        )
        assert http.status_code == 403
        assert http.json()["detail"]["error"] == "wallet_frozen"
    finally:
        registry.unregister_local(tool_name)
