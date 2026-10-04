from dataclasses import replace

import pytest

from app.services import action_permits
from tests.test_action_permits import BINDING
from tests.test_permit_signing_input_snapshot import _base_model


def action_permit(**changes):
    fields = dict(
        action_contract_version=1,
        action_payload_hash="a" * 64,
        action_schema_id=BINDING.schema_id,
        action_schema_version=BINDING.schema_version,
        action_public_tool_id=BINDING.public_tool_id,
        action_upstream_binding_hash=BINDING.upstream_binding_hash,
        allowed_tools_json='["partner.pay"]',
        max_calls_per_tool_json='{"partner.pay":1}',
    )
    fields.update(changes)
    return _base_model().model_copy(update=fields)


def test_identity_is_stable_across_signing_and_transport_keys():
    permit = action_permit()
    first = action_permits.action_execution_identity(permit, BINDING)
    rotated = permit.model_copy(
        update={"key_id": "rotated", "signature": "new", "subject_key_id": "fresh"}
    )
    assert action_permits.action_execution_identity(rotated, BINDING) == first
    assert first.endpoint == "/mcp/action/v1"
    assert first.idempotency_key.startswith("act1-")
    assert len(first.idempotency_key) == 69
    assert first.request_payload["action_payload_hash"] == "a" * 64
    assert (
        action_permits.action_execution_identity(
            permit.model_copy(update={"permit_id": "another"}), BINDING
        )
        != first
    )
    assert (
        action_permits.action_execution_identity(
            permit.model_copy(update={"subject_wallet_id": "another"}), BINDING
        )
        != first
    )
    other = replace(BINDING, public_tool_id="other")
    changed = action_permit(
        action_public_tool_id="other",
        allowed_tools_json='["other"]',
        max_calls_per_tool_json='{"other":1}',
    )
    assert action_permits.action_execution_identity(changed, other) != first
    other = replace(BINDING, upstream_binding_hash="b" * 64)
    assert (
        action_permits.action_execution_identity(
            action_permit(action_upstream_binding_hash="b" * 64), other
        )
        != first
    )
    other = replace(BINDING, deployment_authority="another")
    assert (
        action_permits.action_execution_identity(permit, other).native_idempotency_key
        != first.native_idempotency_key
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"action_contract_version": 2},
        {"action_contract_version": None},
        {"action_payload_hash": None},
        {"action_payload_hash": "bad"},
        {"action_schema_id": "wrong"},
        {"action_schema_version": "wrong"},
        {"action_public_tool_id": "wrong"},
        {"action_upstream_binding_hash": "wrong"},
        {"allowed_tools_json": '["partner.pay","other"]'},
        {"max_calls_per_tool_json": '{"partner.pay":2}'},
    ],
)
def test_identity_refuses_unsupported_or_mismatched_permit(changes):
    with pytest.raises(ValueError):
        action_permits.action_execution_identity(action_permit(**changes), BINDING)


def test_identity_refuses_incomplete_binding():
    with pytest.raises(ValueError):
        action_permits.action_execution_identity(
            action_permit(), replace(BINDING, deployment_authority="")
        )


@pytest.mark.anyio
async def test_http_transport_cannot_select_action_namespace(monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.requests import Request
    from app.routers import mcp

    normalize = AsyncMock(return_value=object())
    monkeypatch.setattr(mcp._mcp_adapter, "normalize_request", normalize)
    monkeypatch.setattr(mcp._mcp_adapter, "invoke", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        mcp._mcp_adapter, "normalize_response", AsyncMock(return_value={"content": []})
    )
    request = mcp.ToolCallRequest.model_validate(
        {
            "name": "partner.pay",
            "arguments": {},
            "endpoint": "/mcp/action/v1",
            "mcp_context": {
                "wallet_id": "fixture",
                "request_path": "/mcp/action/v1",
                "endpoint": "/mcp/action/v1",
                "idempotency_key": "act1-" + "a" * 64,
            },
        }
    )
    await mcp.invoke_tool(
        "partner.pay",
        request,
        Request({"type": "http", "headers": []}),
        auth=object(),
        money=object(),
    )
    assert normalize.call_args.kwargs["endpoint"] == "/mcp/tools/partner.pay/invoke"


@pytest.fixture
async def action_runtime(monkeypatch, clean_database, action_permit_route):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.core.config import get_settings
    from app.schemas.billing import ServiceCategory
    from app.services.service_registry import get_service_registry
    from tests.test_mcp_upstream_governed import FakeUpstreamExecutor
    from tests.test_action_permits import _action_request
    from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

    # The shared SQLite engine outlives pytest's function event loops. A
    # contended pool queue can retain a previous loop; give this fixture its
    # own pool lifetime without changing database rows or production auth.
    from app.db.database import get_engine

    engine = get_engine()
    if engine is not None and engine.dialect.name == "sqlite":
        await engine.dispose()

    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    registry = get_service_registry()
    executor = FakeUpstreamExecutor("success")
    registry.register_upstream(
        service_id=BINDING.public_tool_id,
        name="Payment fixture",
        description="Offline",
        category=ServiceCategory.AGENT_COMMS,
        executor=executor,
        input_schema=BINDING.input_schema,
        output_schema=None,
        credits_per_unit=2,
        upstream_tool_name="pay",
        upstream_origin="https://fixture.invalid",
        action_binding=BINDING,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wallets = await provision_agent_wallet(client)
        payload = _action_request(
            issuer_wallet_id=wallets["sponsor_wallet_id"],
            subject_wallet_id=wallets["agent_wallet_id"],
            subject_key_id=wallets["key_id"],
            arguments={"amount_minor": 1, "recipient": "alice"},
            max_credits=2,
        ).model_dump(mode="json")
        response = await client.post(
            "/v1/action-permits",
            json=payload,
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "action-issue"},
        )
        assert response.status_code == 201, response.text
        yield client, wallets, response.json()["permit_id"], executor
    if engine is not None and engine.dialect.name == "sqlite":
        await engine.dispose()
    get_settings.cache_clear()


async def invoke_action(runtime, surface, key, permit=True, arguments=None, meta=None):
    client, wallets, permit_id, _ = runtime
    args = arguments or {"amount_minor": 1, "recipient": "alice"}
    headers = {**wallets["agent_headers"], "Idempotency-Key": key}
    context = {
        "wallet_id": wallets["agent_wallet_id"],
        "idempotency_key": key,
        "actionPermitRequired": False,
    }
    if permit:
        context["permit_id"] = permit_id if permit is True else permit
    if surface == "rest":
        return await client.post(
            "/mcp/tools/partner.pay/invoke",
            json={"name": "partner.pay", "arguments": args, "mcp_context": context},
            headers=headers,
        )
    params = {"name": "partner.pay", "arguments": args, "mcpContext": context}
    if surface == "standard":
        headers["Accept"] = "application/json, text/event-stream"
        params["_meta"] = (
            meta
            if meta is not None
            else (
                {"io.agentmiddleware/permit_id": context["permit_id"]} if permit else {}
            )
        )
    return await client.post(
        "/mcp" if surface == "standard" else "/mcp/messages",
        json={"jsonrpc": "2.0", "id": key, "method": "tools/call", "params": params},
        headers=headers,
    )


async def action_rows():
    from sqlalchemy import select
    from app.db.database import get_session_factory
    from app.db.models import IdempotencyRecordModel

    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.endpoint.in_(
                            ["/mcp/action/v1", "/mcp/invoke", "/mcp#auto-permit"]
                        )
                    )
                )
            ).scalars()
        )


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["standard", "legacy", "rest"])
@pytest.mark.parametrize("refusal", ["oversized", "rate_limit", "malformed"])
async def test_action_transport_refusals_do_not_claim_authority(
    action_runtime, monkeypatch, surface, refusal
):
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from app.core.config import get_settings
    from app.core.rate_limiter import RateLimitMiddleware
    from app.db.database import get_session_factory
    from app.db.models import PermitModel, ReceiptModel

    client, wallets, permit_id, _ = action_runtime
    client.headers["Origin"] = "http://test"
    with monkeypatch.context() as patch:
        if refusal == "oversized":
            client.headers["Content-Length"] = str(
                get_settings().MAX_REQUEST_BODY_BYTES + 1
            )
            response = await invoke_action(action_runtime, surface, "early-refusal")
            del client.headers["Content-Length"]
            assert response.status_code == 413
        elif refusal == "rate_limit":
            patch.setattr(
                RateLimitMiddleware, "_reserve", AsyncMock(return_value=(True, 60))
            )
            response = await invoke_action(action_runtime, surface, "early-refusal")
            assert response.status_code == 429
            assert response.headers["Retry-After"] == "60"
        else:
            path = {
                "standard": "/mcp",
                "legacy": "/mcp/messages",
                "rest": "/mcp/tools/partner.pay/invoke",
            }[surface]
            response = await client.post(
                path,
                content=b"{",
                headers={
                    **wallets["agent_headers"],
                    "Idempotency-Key": "early-refusal",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert response.status_code == (422 if surface == "rest" else 400)
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
    assert await action_rows() == []
    await assert_action_accounting(action_runtime, expected=0)
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, permit_id)
        assert permit.spent_credits == 0
        receipts = await session.execute(
            select(ReceiptModel).where(ReceiptModel.permit_id == permit_id)
        )
        assert list(receipts.scalars()) == []
    retry = await invoke_action(action_runtime, surface, "early-refusal")
    assert retry.status_code == 200, retry.text
    assert "error" not in retry.json(), retry.text
    if surface != "rest":
        assert retry.json().get("id") == "early-refusal", retry.text
    await assert_action_accounting(action_runtime)


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["standard", "legacy", "rest"])
async def test_missing_action_reference(action_runtime, surface):
    response = await invoke_action(action_runtime, surface, "missing", permit=False)
    assert "action_permit_required" in response.text
    assert action_runtime[3].dispatch_count == 0
    assert await action_rows() == []


@pytest.mark.anyio
async def test_standard_fresh_keys_same_action(action_runtime):
    first = await invoke_action(action_runtime, "standard", "first")
    assert "error" not in first.json(), first.text
    for surface in ("standard", "legacy", "rest"):
        response = await invoke_action(action_runtime, surface, surface)
        assert "error" not in response.json(), response.text
    rows = await action_rows()
    assert len(rows) == 1
    assert rows[0].endpoint == "/mcp/action/v1"
    assert action_runtime[3].dispatch_count == 1


@pytest.mark.anyio
async def test_mismatched_reference(action_runtime):
    response = await invoke_action(
        action_runtime,
        "standard",
        "conflict",
        meta={"io.agentmiddleware/permit_id": "other"},
    )
    assert "permit_reference_conflict" in response.text
    assert await action_rows() == []


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["standard", "legacy", "rest"])
async def test_invalid_digest_cannot_poison_owner(action_runtime, surface):
    response = await invoke_action(
        action_runtime,
        surface,
        "bad",
        arguments={"amount_minor": 2, "recipient": "alice"},
    )
    assert "action_payload_mismatch" in response.text
    assert await action_rows() == []
    response = await invoke_action(action_runtime, surface, "good")
    assert "error" not in response.json(), response.text


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["standard", "legacy", "rest"])
async def test_envelope_on_action_tool(action_runtime, surface):
    from tests.test_trust_helpers import create_tool_permit

    client, wallets, _, executor = action_runtime
    envelope = await create_tool_permit(
        client,
        wallet_id=wallets["agent_wallet_id"],
        key_id=wallets["key_id"],
        tool_name="partner.pay",
    )
    response = await invoke_action(
        action_runtime, surface, "envelope", permit=envelope["permit_id"]
    )
    assert "action_permit_required" in response.text
    assert executor.dispatch_count == 0
    assert await action_rows() == []


@pytest.mark.anyio
@pytest.mark.parametrize("value", [None, "", " ", 1, [], {}])
async def test_standard_invalid_reference(action_runtime, value):
    response = await invoke_action(
        action_runtime,
        "standard",
        "invalid",
        meta={"io.agentmiddleware/permit_id": value},
    )
    assert "invalid_permit_reference" in response.text
    assert await action_rows() == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change,reason",
    [
        ({"status": "revoked"}, "permit_revoked"),
        ({"signature": "broken"}, "permit_signature_invalid"),
        ({"subject_key_id": "foreign"}, "permit_key_mismatch"),
        ({"subject_wallet_id": "foreign"}, "permit_wallet_mismatch"),
        ({"expired": True}, "permit_expired"),
    ],
)
async def test_invalid_replay_preserves_owner(action_runtime, change, reason):
    from datetime import timedelta
    from app.core.time import utc_now
    from app.db.database import get_session_factory
    from app.db.models import PermitModel

    first = await invoke_action(action_runtime, "standard", "first")
    assert "error" not in first.json(), first.text
    before = (await action_rows())[0].model_dump()
    from tests.test_trust_helpers import provision_agent_wallet

    foreign = await provision_agent_wallet(action_runtime[0])
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
        for field, value in change.items():
            if field == "expired":
                permit.expires_at = utc_now() - timedelta(seconds=1)
            else:
                if field == "subject_key_id":
                    value = foreign["key_id"]
                if field == "subject_wallet_id":
                    value = foreign["agent_wallet_id"]
                setattr(permit, field, value)
        await session.commit()
    for surface in ("standard", "legacy", "rest"):
        response = await invoke_action(action_runtime, surface, surface)
        assert reason in response.text
    assert (await action_rows())[0].model_dump() == before
    assert action_runtime[3].dispatch_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("remove", ["binding", "registration"])
async def test_lost_binding_fails_closed_on_replay(action_runtime, remove):
    from app.services.service_registry import get_service_registry

    response = await invoke_action(action_runtime, "standard", "first")
    assert "error" not in response.json(), response.text
    before = (await action_rows())[0].model_dump()
    registry = get_service_registry()
    if remove == "binding":
        registry._action_bindings.pop("partner.pay")
    else:
        registry._local_registry.pop("partner.pay")
    for surface in ("legacy", "rest", "standard"):
        response = await invoke_action(action_runtime, surface, surface)
        assert "error" in response.json() or response.status_code >= 400
    assert (await action_rows())[0].model_dump() == before
    assert action_runtime[3].dispatch_count == 1


@pytest.mark.anyio
async def test_action_native_key_and_accounting_chain(action_runtime):
    from sqlalchemy import select
    from app.db.database import get_session_factory
    from app.db.models import (
        PermitModel,
        McpDispatchAttemptModel,
        LedgerEntryModel,
        ReceiptModel,
    )

    response = await invoke_action(action_runtime, "standard", "caller-key")
    assert "error" not in response.json(), response.text
    owner = (await action_rows())[0]
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
        identity = action_permits.action_execution_identity(permit, BINDING)
        attempts = list(
            (await session.execute(select(McpDispatchAttemptModel))).scalars()
        )
        debits = list(
            (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.operation_key == owner.record_id
                    )
                )
            ).scalars()
        )
        receipts = list(
            (
                await session.execute(
                    select(ReceiptModel).where(
                        ReceiptModel.permit_id == permit.permit_id
                    )
                )
            ).scalars()
        )
    assert len(attempts) == len(debits) == len(receipts) == 1
    assert attempts[0].idempotency_record_id == owner.record_id
    assert owner.ledger_entry_id == debits[0].entry_id
    assert (
        action_runtime[3].calls[0]["idempotency_key"] == identity.native_idempotency_key
    )
    assert action_runtime[3].calls[0]["arguments"]["currency"] == "USD"


@pytest.mark.anyio
async def test_action_manifest(action_runtime):
    client = action_runtime[0]
    response = await client.get("/mcp/tools.json")
    tool = next(t for t in response.json()["tools"] if t["name"] == "partner.pay")
    assert tool["annotations"]["actionPermitRequired"] is True


@pytest.mark.anyio
async def test_invalid_enterprise_replay_cannot_disclose(action_runtime, monkeypatch):
    from app.routers import mcp
    from app.core.oidc_iga import IGAError

    response = await invoke_action(action_runtime, "standard", "first")
    assert "error" not in response.json(), response.text
    before = (await action_rows())[0].model_dump()

    def invalid(_):
        raise IGAError("iga_token_expired")

    monkeypatch.setattr(mcp, "_verified_enterprise_principal", invalid)
    response = await invoke_action(action_runtime, "standard", "replay")
    assert "iga_token_expired" in response.text
    assert (await action_rows())[0].model_dump() == before


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changes",
    [
        {"requires_human_approval": True},
        {"allow_identical_repeats": True},
        {"repeat_window_seconds": 1},
        {"aggregate_value_cap": 5},
        {"forbidden_fields_json": '["secret"]'},
        {"recipient_domain": "fixture.invalid"},
    ],
)
async def test_action_gate_rejects_unsupported_signed_constraints(monkeypatch, changes):
    from unittest.mock import AsyncMock
    from datetime import timedelta
    from app.core.time import utc_now
    from app.services.permits import get_permit_service

    args = {"amount_minor": 1, "recipient": "alice"}
    model = action_permit(
        **changes,
        expires_at=utc_now() + timedelta(hours=1),
        subject_key_id=None,
        scopes_json='["tool:partner.pay:invoke","billing:charge"]',
    )
    model.action_payload_hash = action_permits.action_payload_hash(
        BINDING, model.subject_wallet_id, args
    )
    monkeypatch.setattr(
        get_permit_service(), "verify_signature", AsyncMock(return_value=True)
    )
    result = await action_permits.validate_action_request(
        model, BINDING, model.subject_wallet_id, None, args, "replay"
    )
    assert not result.allowed
    assert result.reason == "unsupported_action_constraints"


@pytest.mark.anyio
async def test_generic_abandon_retains_prepared_action_owner(action_runtime):
    from app.services.idempotency import get_idempotency_service
    from app.db.database import get_session_factory
    from app.db.models import PermitModel

    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
    identity = action_permits.action_execution_identity(permit, BINDING)
    idem = get_idempotency_service()
    begun = await idem.begin_action_with_record(
        wallet_id=permit.subject_wallet_id, identity=identity
    )
    accepted, _ = await prepare_action(
        action_runtime, {"amount_minor": 1, "recipient": "alice"}
    )
    assert accepted.allowed
    await idem.abandon(
        wallet_id=permit.subject_wallet_id,
        endpoint=identity.endpoint,
        idempotency_key=identity.idempotency_key,
        expected_record_id=begun.record_id,
    )
    assert (await action_rows())[0].record_id == begun.record_id


@pytest.mark.anyio
async def test_iga_replay_access_checks_grant_without_consuming(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.core import oidc_iga as iga
    from app.services import policies

    iga.reset_iga_counters()
    principal = iga.EnterprisePrincipal(
        "fixture", "okta", "https://fixture.invalid", groups=("payer",)
    )
    grant = iga.IGAGrant("payer", "fixture-policy", max_uses=1)
    monkeypatch.setattr(iga, "resolve_policy_grants", lambda _: [grant])
    bundle = SimpleNamespace(is_active=True, allowed_tools=["partner.pay"])
    monkeypatch.setattr(policies, "get_policy_bundle", AsyncMock(return_value=bundle))
    assert (await iga.enforce_tool_call(principal, "partner.pay")).allowed
    before = dict(iga._lifetime_uses)
    for _ in range(3):
        assert (
            await iga.enforce_tool_call(principal, "partner.pay", consume=False)
        ).allowed
    assert iga._lifetime_uses == before
    assert not (await iga.enforce_tool_call(principal, "partner.pay")).allowed
    bundle.is_active = False
    assert (
        await iga.enforce_tool_call(principal, "partner.pay", consume=False)
    ).reason == "iga_policy_inactive"
    bundle.is_active = True
    bundle.allowed_tools = []
    assert (
        await iga.enforce_tool_call(principal, "partner.pay", consume=False)
    ).reason == "iga_tool_not_allowed"
    monkeypatch.setattr(iga, "resolve_policy_grants", lambda _: [])
    assert (
        await iga.enforce_tool_call(principal, "partner.pay", consume=False)
    ).reason == "iga_no_matching_role"
    assert iga._lifetime_uses == before
    iga.reset_iga_counters()


@pytest.mark.anyio
async def test_prepared_action_survives_ledger_contention(action_runtime, monkeypatch):
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from app.routers import mcp
    from app.services.billing_engine import LedgerWriteContendedError
    from app.db.database import get_session_factory
    from app.db.models import McpDispatchAttemptModel, PermitModel

    monkeypatch.setattr(
        mcp,
        "_charge_and_checkpoint",
        AsyncMock(side_effect=LedgerWriteContendedError()),
    )
    response = await invoke_action(action_runtime, "standard", "contention")
    assert "idempotency_in_progress" in response.text
    owner = (await action_rows())[0]
    async with get_session_factory()() as session:
        attempts = list(
            (await session.execute(select(McpDispatchAttemptModel))).scalars()
        )
        permit = await session.get(PermitModel, action_runtime[2])
    assert len(attempts) == 1
    assert attempts[0].idempotency_record_id == owner.record_id
    assert attempts[0].state == "prepared"
    assert permit.spent_credits == 2
    assert owner.ledger_entry_id is None
    assert action_runtime[3].dispatch_count == 0


@pytest.mark.anyio
async def test_action_does_not_adopt_ordinary_owner(action_runtime):
    from app.services.idempotency import get_idempotency_service
    from app.db.database import get_session_factory
    from app.db.models import PermitModel

    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
    identity = action_permits.action_execution_identity(permit, BINDING)
    idem = get_idempotency_service()
    legacy = await idem.begin_with_record(
        wallet_id=permit.subject_wallet_id,
        endpoint="/mcp/invoke",
        idempotency_key=identity.idempotency_key,
        request_payload=identity.request_payload,
    )
    await idem.complete(
        wallet_id=permit.subject_wallet_id,
        endpoint="/mcp/invoke",
        idempotency_key=identity.idempotency_key,
        response_reference=None,
        response_json={"legacy": True},
    )
    response = await invoke_action(action_runtime, "legacy", identity.idempotency_key)
    assert "error" not in response.json(), response.text
    rows = await action_rows()
    assert len(rows) == 2
    old = next(row for row in rows if row.record_id == legacy.record_id)
    assert old.response_json == '{"legacy": true}'
    assert action_runtime[3].dispatch_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["signature", "expired", "removed_key"])
async def test_action_replay_rejects_invalid_enterprise_token(
    action_runtime, monkeypatch, invalid
):
    import json
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app.core.config import get_settings
    from tests.test_iga_policy import _mint, _okta_issuers

    first = await invoke_action(action_runtime, "standard", "first")
    assert "error" not in first.json(), first.text
    before = (await action_rows())[0].model_dump()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("IGA_TRUSTED_ISSUERS", json.dumps(_okta_issuers(key)))
    get_settings.cache_clear()
    if invalid == "signature":
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = _mint(other)
        reason = "iga_signature_invalid"
    elif invalid == "expired":
        token = _mint(key, exp_delta=-600)
        reason = "iga_token_expired"
    else:
        token = _mint(key, kid="removed")
        reason = "iga_signing_key_not_found"
    monkeypatch.setitem(
        action_runtime[1]["agent_headers"], "Authorization", "Bearer " + token
    )
    response = await invoke_action(action_runtime, "standard", "replay")
    assert reason in response.text
    assert (await action_rows())[0].model_dump() == before
    assert action_runtime[3].dispatch_count == 1


async def denial_evidence(runtime, response, reason):
    from sqlalchemy import select
    from app.db.database import get_session_factory
    from app.db.models import ReceiptModel, ControlPlaneAuditEventModel
    from app.services.receipts import get_receipt_service

    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(ReceiptModel).where(
                        ReceiptModel.permit_id == runtime[2],
                        ReceiptModel.outcome == "denied",
                    )
                )
            ).scalars()
        )
        assert rows, response.text
        receipt = rows[-1]
        audit = await session.get(ControlPlaneAuditEventModel, receipt.audit_event_id)
    assert receipt.receipt_id in response.text
    assert receipt.reason_code == reason
    assert receipt.idempotency_record_id is None
    assert receipt.dispatch_attempt_id is None
    assert receipt.ledger_entry_id is None
    assert receipt.credits_charged == 0
    assert audit is not None
    assert (await get_receipt_service().verify_receipt(receipt.receipt_id))[0]
    return receipt


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["standard", "legacy", "rest"])
async def test_eligible_digest_denial_has_detached_signed_evidence(
    action_runtime, surface
):
    from sqlalchemy import select
    from app.db.database import get_session_factory
    from app.db.models import ReceiptModel

    bad_args = {"amount_minor": 2, "recipient": "alice"}
    initial = await invoke_action(
        action_runtime, surface, "initial-denial", arguments=bad_args
    )
    first_denial = await denial_evidence(
        action_runtime, initial, "action_payload_mismatch"
    )
    assert await action_rows() == []
    assert action_runtime[3].dispatch_count == 0
    accepted = await invoke_action(action_runtime, surface, "accepted")
    assert "error" not in accepted.json(), accepted.text
    owner_before = (await action_rows())[0].model_dump()
    async with get_session_factory()() as session:
        original = (
            await session.execute(
                select(ReceiptModel).where(
                    ReceiptModel.idempotency_record_id == owner_before["record_id"]
                )
            )
        ).scalar_one()
        receipt_before = original.model_dump()
    retry = await invoke_action(
        action_runtime, surface, "invalid-retry", arguments=bad_args
    )
    second_denial = await denial_evidence(
        action_runtime, retry, "action_payload_mismatch"
    )
    assert first_denial.receipt_id != second_denial.receipt_id
    assert second_denial.request_hash != original.request_hash
    assert (await action_rows())[0].model_dump() == owner_before
    async with get_session_factory()() as session:
        original = await session.get(ReceiptModel, original.receipt_id)
        assert original.model_dump() == receipt_before
    assert action_runtime[3].dispatch_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["standard", "legacy", "rest"])
@pytest.mark.parametrize(
    "denial,reason",
    [
        ("budget", "permit_budget_exceeded"),
        ("expired", "permit_expired"),
        ("revoked", "permit_revoked"),
        ("policy", "policy_tool_not_allowed"),
        ("grant", "iga_no_matching_role"),
    ],
)
async def test_eligible_preowner_denial_evidence(
    action_runtime, monkeypatch, surface, denial, reason
):
    from datetime import timedelta
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.core.time import utc_now
    from app.db.database import get_session_factory
    from app.db.models import PermitModel
    from app.services.permits import get_permit_service
    from app.services.signing_keys import get_signing_key_service
    from app.routers import mcp

    if denial in {"budget", "expired", "revoked"}:
        async with get_session_factory()() as session:
            permit = await session.get(PermitModel, action_runtime[2])
            if denial == "budget":
                permit.max_credits = Decimal("1")
            elif denial == "expired":
                permit.expires_at = utc_now() - timedelta(seconds=1)
            else:
                permit.status = "revoked"
            signature, key_id, _ = await get_signing_key_service().sign_payload(
                get_permit_service()._unsigned_payload(permit)
            )
            permit.signature, permit.key_id = signature, key_id
            await session.commit()
    elif denial == "policy":
        monkeypatch.setattr(
            mcp,
            "evaluate_wallet_policy",
            AsyncMock(return_value=SimpleNamespace(allowed=False, reason=reason)),
        )
    else:
        monkeypatch.setattr(mcp, "_verified_enterprise_principal", lambda _: object())
        monkeypatch.setattr(
            mcp,
            "enforce_tool_call",
            AsyncMock(return_value=SimpleNamespace(allowed=False, reason=reason)),
        )
    response = await invoke_action(action_runtime, surface, "denied")
    await denial_evidence(action_runtime, response, reason)
    assert await action_rows() == []
    assert action_runtime[3].dispatch_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["standard", "legacy", "rest"])
@pytest.mark.parametrize(
    "ineligible", ["missing", "foreign_wallet", "foreign_key", "signature"]
)
async def test_ineligible_action_denial_does_not_disclose_receipt(
    action_runtime, surface, ineligible
):
    from sqlalchemy import select
    from app.db.database import get_session_factory
    from app.db.models import PermitModel, ReceiptModel
    from tests.test_trust_helpers import provision_agent_wallet

    foreign = await provision_agent_wallet(action_runtime[0])
    if ineligible != "missing":
        async with get_session_factory()() as session:
            permit = await session.get(PermitModel, action_runtime[2])
            if ineligible == "foreign_wallet":
                permit.subject_wallet_id = foreign["agent_wallet_id"]
            elif ineligible == "foreign_key":
                permit.subject_key_id = foreign["key_id"]
            else:
                permit.signature = "invalid"
            if ineligible != "signature":
                from app.services.permits import get_permit_service
                from app.services.signing_keys import get_signing_key_service

                signature, key_id, _ = await get_signing_key_service().sign_payload(
                    get_permit_service()._unsigned_payload(permit)
                )
                permit.signature, permit.key_id = signature, key_id
            await session.commit()
    response = await invoke_action(
        action_runtime, surface, "ineligible", permit=ineligible != "missing"
    )
    assert "receipt" not in response.text
    async with get_session_factory()() as session:
        receipts = list(
            (
                await session.execute(
                    select(ReceiptModel).where(
                        ReceiptModel.permit_id == action_runtime[2]
                    )
                )
            ).scalars()
        )
    assert receipts == []
    assert await action_rows() == []
    assert action_runtime[3].dispatch_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["standard", "legacy", "rest"])
async def test_policy_replay_denial_preserves_accepted_evidence(
    action_runtime, monkeypatch, surface
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from app.routers import mcp
    from app.db.database import get_session_factory
    from app.db.models import ReceiptModel

    accepted = await invoke_action(action_runtime, surface, "accepted")
    assert "error" not in accepted.json(), accepted.text
    before_owner = (await action_rows())[0].model_dump()
    async with get_session_factory()() as session:
        original = (
            await session.execute(
                select(ReceiptModel).where(
                    ReceiptModel.idempotency_record_id == before_owner["record_id"]
                )
            )
        ).scalar_one()
        before_receipt = original.model_dump()
    monkeypatch.setattr(
        mcp,
        "evaluate_wallet_policy",
        AsyncMock(
            return_value=SimpleNamespace(
                allowed=False, reason="policy_tool_not_allowed"
            )
        ),
    )
    denied = await invoke_action(action_runtime, surface, "policy-retry")
    await denial_evidence(action_runtime, denied, "policy_tool_not_allowed")
    assert (await action_rows())[0].model_dump() == before_owner
    async with get_session_factory()() as session:
        original = await session.get(ReceiptModel, original.receipt_id)
        assert original.model_dump() == before_receipt
    assert action_runtime[3].dispatch_count == 1


async def assert_action_accounting(runtime, expected=1):
    """Assert the persisted economic chain, not merely HTTP success."""
    import json
    from decimal import Decimal
    from sqlalchemy import select
    from app.db.database import get_session_factory
    from app.db.models import (
        PermitModel,
        McpDispatchAttemptModel,
        LedgerEntryModel,
        ReceiptModel,
        WalletModel,
    )

    owners = [row for row in await action_rows() if row.endpoint == "/mcp/action/v1"]
    assert len(owners) == expected
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, runtime[1]["agent_wallet_id"])
        assert wallet.balance == Decimal("1000") - Decimal("2") * expected
        all_debits = list(
            (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.wallet_id == wallet.wallet_id,
                        LedgerEntryModel.amount < 0,
                    )
                )
            ).scalars()
        )
        assert len(all_debits) == expected
        assert {entry.operation_key for entry in all_debits} == {
            owner.record_id for owner in owners
        }
        all_attempts = list(
            (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        McpDispatchAttemptModel.wallet_id == wallet.wallet_id,
                    )
                )
            ).scalars()
        )
        assert len(all_attempts) == expected
        for owner in owners:
            attempt = (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        McpDispatchAttemptModel.idempotency_record_id == owner.record_id
                    )
                )
            ).scalar_one()
            debit = (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.operation_key == owner.record_id
                    )
                )
            ).scalar_one()
            receipt = (
                await session.execute(
                    select(ReceiptModel).where(
                        ReceiptModel.idempotency_record_id == owner.record_id
                    )
                )
            ).scalar_one()
            permit = await session.get(PermitModel, attempt.permit_id)
            assert permit.spent_credits == Decimal("2")
            assert json.loads(permit.tool_call_counts_json) == {"partner.pay": 1}
            assert attempt.call_slot_reserved is True
            assert owner.ledger_entry_id == debit.entry_id == receipt.ledger_entry_id
            assert receipt.dispatch_attempt_id == attempt.attempt_id
            assert receipt.credits_charged == Decimal("2")
            assert debit.amount == Decimal("-2")
            assert attempt.state == "succeeded"
    assert runtime[3].dispatch_count == expected


@pytest.mark.anyio
async def test_twenty_fresh_action_keys_share_accounting(action_runtime):
    import asyncio

    surfaces = ("standard", "legacy", "rest")
    responses = await asyncio.gather(
        *(
            invoke_action(action_runtime, surfaces[i % 3], f"concurrent-{i}")
            for i in range(20)
        )
    )
    for response in responses:
        assert ("error" not in response.json() and response.status_code < 400) or (
            "idempotency_in_progress" in response.text
        ), response.text
    replay = await invoke_action(action_runtime, "standard", "completed-replay")
    assert "error" not in replay.json(), replay.text
    await assert_action_accounting(action_runtime)


@pytest.mark.anyio
async def test_new_trusted_permit_intentionally_repeats(action_runtime):
    from tests.test_action_permits import _action_request
    from tests.test_trust_helpers import BOOTSTRAP_HEADERS

    client, wallets, _, executor = action_runtime
    first = await invoke_action(action_runtime, "standard", "first-action")
    assert "error" not in first.json(), first.text
    issued = await client.post(
        "/v1/action-permits",
        json=_action_request(
            issuer_wallet_id=wallets["sponsor_wallet_id"],
            subject_wallet_id=wallets["agent_wallet_id"],
            subject_key_id=wallets["key_id"],
            arguments={"amount_minor": 1, "recipient": "alice"},
            max_credits=2,
        ).model_dump(mode="json"),
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "new-authority"},
    )
    assert issued.status_code == 201, issued.text
    second_runtime = (client, wallets, issued.json()["permit_id"], executor)
    second = await invoke_action(second_runtime, "standard", "second-action")
    assert "error" not in second.json(), second.text
    await assert_action_accounting(action_runtime, expected=2)
    assert executor.calls[0]["idempotency_key"] != executor.calls[1]["idempotency_key"]


async def prepare_action(runtime, arguments):
    from decimal import Decimal
    from app.db.database import get_session_factory
    from app.db.models import PermitModel
    from app.services.idempotency import get_idempotency_service
    from app.services.mcp_dispatch_attempts import get_mcp_dispatch_attempt_service

    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, runtime[2])
    identity = action_permits.action_execution_identity(permit, BINDING)
    idem = get_idempotency_service()
    owner = await idem.get_action_record(
        wallet_id=permit.subject_wallet_id, internal_key=identity.idempotency_key
    )
    if owner is None:
        begun = await idem.begin_action_with_record(
            wallet_id=permit.subject_wallet_id, identity=identity
        )
        owner = await idem.get_action_record(
            wallet_id=permit.subject_wallet_id, internal_key=identity.idempotency_key
        )
        assert owner.record_id == begun.record_id
    return await get_mcp_dispatch_attempt_service().authorize_reserve_and_prepare(
        idempotency_record_id=owner.record_id,
        wallet_id=permit.subject_wallet_id,
        permit_id=permit.permit_id,
        key_id=permit.subject_key_id,
        public_tool_id="partner.pay",
        upstream_tool_name="pay",
        upstream_origin="https://fixture.invalid",
        request_hash=owner.request_hash,
        credits_authorized=Decimal("2"),
        arguments=arguments,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("already_prepared", [False, True])
async def test_atomic_action_prepare_rechecks_digest(action_runtime, already_prepared):
    from app.db.database import get_session_factory
    from app.db.models import PermitModel

    if already_prepared:
        validation, original = await prepare_action(
            action_runtime, {"amount_minor": 1, "recipient": "alice"}
        )
        assert validation.allowed
    before = (await action_rows())[0].model_dump() if already_prepared else None
    validation, attempt = await prepare_action(
        action_runtime, {"amount_minor": 2, "recipient": "alice"}
    )
    assert not validation.allowed
    assert validation.reason == "action_payload_mismatch"
    assert attempt is None
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
        assert permit.spent_credits == (2 if already_prepared else 0)
    if before:
        assert (await action_rows())[0].model_dump() == before
    assert action_runtime[3].dispatch_count == 0


@pytest.mark.anyio
async def test_exhausted_action_replay_returns_identical_receipt(action_runtime):
    import json

    first = await invoke_action(action_runtime, "standard", "first")
    assert "error" not in first.json(), first.text
    owner = (await action_rows())[0]
    original_receipt = json.loads(owner.response_json)["receipt"]
    for surface in ("standard", "legacy", "rest"):
        response = await invoke_action(action_runtime, surface, "replay-" + surface)
        assert "error" not in response.json(), response.text
        # Standard MCP wraps the governed result; compare the durable response
        # and require the original receipt identifier in every wire response.
        assert original_receipt["receipt_id"] in response.text
        assert (await action_rows())[0].model_dump() == owner.model_dump()
    await assert_action_accounting(action_runtime)


@pytest.mark.anyio
@pytest.mark.parametrize("tamper", ["namespace", "key", "hash", "origin", "tool"])
async def test_atomic_action_owner_and_destination_binding(action_runtime, tamper):
    from decimal import Decimal
    from app.db.database import get_session_factory
    from app.db.models import PermitModel, IdempotencyRecordModel
    from app.services.idempotency import get_idempotency_service
    from app.services.mcp_dispatch_attempts import (
        DispatchAttemptConflictError,
        get_mcp_dispatch_attempt_service,
    )
    from app.services.signing_keys import sha256_hex

    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
    identity = action_permits.action_execution_identity(permit, BINDING)
    begun = await get_idempotency_service().begin_action_with_record(
        wallet_id=permit.subject_wallet_id, identity=identity
    )
    request_hash = sha256_hex(identity.request_payload)
    async with get_session_factory()() as session:
        owner = await session.get(IdempotencyRecordModel, begun.record_id)
        if tamper == "namespace":
            owner.endpoint = "/mcp/invoke"
        elif tamper == "key":
            owner.idempotency_key = "caller-selected"
        elif tamper == "hash":
            owner.request_hash = request_hash = "a" * 64
        await session.commit()
    kwargs = dict(
        idempotency_record_id=begun.record_id,
        wallet_id=permit.subject_wallet_id,
        permit_id=permit.permit_id,
        key_id=permit.subject_key_id,
        public_tool_id="partner.pay",
        upstream_tool_name="other" if tamper == "tool" else "pay",
        upstream_origin="https://other.invalid"
        if tamper == "origin"
        else "https://fixture.invalid",
        request_hash=request_hash,
        credits_authorized=Decimal("2"),
        arguments={"amount_minor": 1, "recipient": "alice"},
    )
    service = get_mcp_dispatch_attempt_service()
    if tamper in {"namespace", "key", "hash"}:
        with pytest.raises(
            DispatchAttemptConflictError, match="dispatch_action_owner_invalid"
        ):
            await service.authorize_reserve_and_prepare(**kwargs)
    else:
        validation, attempt = await service.authorize_reserve_and_prepare(**kwargs)
        assert validation.reason == "action_binding_mismatch"
        assert not validation.allowed and attempt is None
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, permit.permit_id)
        assert permit.spent_credits == 0
        assert permit.tool_call_counts_json in (None, "{}")
    assert action_runtime[3].dispatch_count == 0
