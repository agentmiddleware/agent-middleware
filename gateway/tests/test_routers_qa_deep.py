"""Deep QA for gateway/app/routers: edge, isolation, and error-handling gaps.

Covers router-level behavior the existing suite does not pin down:

- POST /v1/quotes with a tool whose stored category is not a known
  ``ServiceCategory`` must answer 400 ``tool_price_invalid``, never 500.
- GET /v1/api-keys/{wallet}/logs bounds ``limit`` like every other list
  endpoint (1..200); unbounded reads and negative limits are refused.
- POST /v1/auth/revoke error handling: garbage tokens are refused, and a
  revoked refresh token stays revoked.
- GET /v1/me/receipts with another wallet's ``permit_id`` filter returns
  nothing from the caller's own ledger instead of leaking across tenants.
"""

from __future__ import annotations

import base64
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.receipts import get_receipt_service
from app.services.service_registry import get_service_registry
from app.services.audit_log import record_audit_event
from tests.test_trust_helpers import (
    create_tool_permit,
    provision_agent_wallet,
)

BAD_CATEGORY_TOOL = "qa-bad-category-tool"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def bad_category_tool():
    """A registered tool whose stored category is not a known category.

    Registrations validate the category, but the stored row is what the
    router reads: legacy rows and in-memory mutations can carry anything.
    """
    registry = get_service_registry()

    def tool_fn() -> dict:
        return {"ok": True}

    registry.register_local(
        service_id=BAD_CATEGORY_TOOL,
        name="Bad Category Tool",
        description="QA tool with a corrupt stored category",
        category=ServiceCategory.PLATFORM_FEE,
        func=tool_fn,
        credits_per_unit=1.0,
        unit_name="call",
    )
    registry.get_local(BAD_CATEGORY_TOOL)["category"] = "not-a-category"
    yield
    registry.unregister_local(BAD_CATEGORY_TOOL)


@pytest.mark.anyio
async def test_quote_with_unknown_stored_category_answers_400(
    client, clean_database, bad_category_tool
):
    """A corrupt stored category is a 400, not a 500.

    ``ServiceCategory(...)`` raises ``ValueError`` for an unknown string;
    the router must map that to ``tool_price_invalid`` like it does for a
    non-numeric price, so a bad registration row cannot crash quoting.
    """
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/v1/quotes",
        json={"wallet_id": provisioned["agent_wallet_id"], "tool": BAD_CATEGORY_TOOL},
        headers=provisioned["agent_headers"],
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "tool_price_invalid"


@pytest.mark.anyio
async def test_rotation_logs_limit_is_bounded(client, clean_database):
    """The rotation-log ``limit`` accepts 1..200 like every other listing."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    headers = provisioned["agent_headers"]

    base = f"/v1/api-keys/{wallet_id}/logs"
    assert (await client.get(base, headers=headers)).status_code == 200
    for bad in ("0", "-1", "201", "1000000"):
        resp = await client.get(base, params={"limit": bad}, headers=headers)
        assert resp.status_code == 422, (bad, resp.status_code, resp.text)
    assert (
        await client.get(base, params={"limit": 200}, headers=headers)
    ).status_code == 200


# --- /v1/auth/revoke: the one auth endpoint with no coverage at all. ---


@pytest.fixture
async def auth_client(clean_database, monkeypatch):
    from app.core.config import get_settings
    from app.routers import auth as auth_router

    monkeypatch.setattr(
        get_settings(),
        "TRUST_SIGNING_PRIVATE_KEY_B64",
        base64.b64encode(bytes(range(32))).decode(),
    )
    mini = FastAPI()
    mini.include_router(auth_router.router)
    transport = ASGITransport(app=mini)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_wallet_key(wallet_id: str) -> dict:
    from app.db.database import get_session_factory
    from app.db.models import WalletModel
    from app.services.api_key_service import get_api_key_service

    async with get_session_factory()() as session:
        session.add(WalletModel(wallet_id=wallet_id, wallet_type="agent"))
        await session.commit()
    return await get_api_key_service().create_key(wallet_id)


@pytest.mark.anyio
async def test_revoke_garbage_refresh_token_answers_401(auth_client):
    resp = await auth_client.post(
        "/v1/auth/revoke", json={"refresh_token": "not-a-token"}
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"]["error"] == "invalid_refresh_token"


@pytest.mark.anyio
async def test_revoked_refresh_token_stays_revoked(auth_client):
    """Revoke is idempotent, and a revoked token can never refresh again."""
    key = await _seed_wallet_key("qa_auth_revoke")
    exchanged = await auth_client.post(
        "/v1/auth/token",
        json={"api_key": key["api_key"], "scopes": ["billing:read"]},
    )
    assert exchanged.status_code == 200, exchanged.text
    refresh_token = exchanged.json()["refresh_token"]

    first = await auth_client.post(
        "/v1/auth/revoke", json={"refresh_token": refresh_token}
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"status": "revoked"}

    second = await auth_client.post(
        "/v1/auth/revoke", json={"refresh_token": refresh_token}
    )
    assert second.status_code == 200, second.text

    retry = await auth_client.post(
        "/v1/auth/refresh", json={"refresh_token": refresh_token}
    )
    assert retry.status_code == 401, retry.text
    assert retry.json()["detail"]["error"] == "revoked_refresh_token"


# --- /v1/me/receipts tenant isolation through the permit_id filter. ---


async def _receipt_for(client, provisioned: dict, *, tool_name: str, idem_key: str):
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        idem_key=idem_key,
    )
    audit_event = await record_audit_event(
        event="mcp.invoke",
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool=tool_name,
        endpoint="/mcp/messages",
        auth_source="db",
        policy_decision_id=f"pol-{tool_name}",
        request_id=f"req-{tool_name}",
        ok=True,
        metadata={"permit_id": permit["permit_id"]},
    )
    receipt = await get_receipt_service().create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool=tool_name,
        request_payload={"message": "hello"},
        response_payload={"message": "world"},
        ledger_entry_id=None,
        credits_authorized=Decimal("2"),
        credits_charged=Decimal("0"),
        outcome="success",
        audit_event_id=audit_event.event_id,
    )
    return permit, receipt.receipt_id


@pytest.mark.anyio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "mcp.py:1395 builds ServiceCategory from the stored row outside any "
        "guard: on a governed invoke a corrupt category escapes as "
        "internal_error instead of a 400 denial, and the begun idempotency "
        "record is never completed. Fixing it means touching the "
        "denial-idempotency accounting on the money path, so it stays open."
    ),
)
async def test_governed_invoke_with_unknown_stored_category_answers_400(
    client, clean_database
):
    """A corrupt stored category on the invoke path is a 400 denial, not 500.

    Mirrors the quotes fix: the registration row is what the router reads,
    so an unknown stored category must be refused cleanly before any debit.
    """
    tool_id = "qa-mcp-bad-category-tool"

    def tool_fn(message: str = "hi") -> dict:
        return {"message": message}

    registry = get_service_registry()
    registry.register_local(
        service_id=tool_id,
        name="QA MCP Bad Category Tool",
        description="QA tool with a corrupt stored category",
        category=ServiceCategory.PLATFORM_FEE,
        func=tool_fn,
        credits_per_unit=1.0,
        unit_name="call",
    )
    try:
        provisioned = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_id,
            idem_key="qa-mcp-bad-category-permit",
        )
        registry.get_local(tool_id)["category"] = "not-a-category"
        resp = await client.post(
            f"/mcp/tools/{tool_id}/invoke",
            json={
                "name": tool_id,
                "arguments": {"message": "hi"},
                "mcp_context": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "qa-mcp-bad-category-invoke",
                },
            },
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 400, resp.text
    finally:
        registry.unregister_local(tool_id)


@pytest.mark.anyio
async def test_me_receipts_permit_filter_stays_inside_caller_wallet(
    client, clean_database
):
    """Filtering your own ledger by another wallet's permit shows nothing.

    The service ANDs ``permit_id`` with the caller's wallet, so a foreign
    permit id filters everything out instead of reaching across tenants.
    """
    wallet_a = await provision_agent_wallet(client)
    wallet_b = await provision_agent_wallet(client)
    permit_a, receipt_a = await _receipt_for(
        client, wallet_a, tool_name="qa-ledger-a", idem_key="qa-ledger-a-permit"
    )
    permit_b, receipt_b = await _receipt_for(
        client, wallet_b, tool_name="qa-ledger-b", idem_key="qa-ledger-b-permit"
    )

    own = await client.get(
        "/v1/me/receipts",
        params={"permit_id": permit_a["permit_id"]},
        headers=wallet_a["agent_headers"],
    )
    assert own.status_code == 200, own.text
    assert [r["receipt_id"] for r in own.json()["receipts"]] == [receipt_a]

    foreign = await client.get(
        "/v1/me/receipts",
        params={"permit_id": permit_b["permit_id"]},
        headers=wallet_a["agent_headers"],
    )
    assert foreign.status_code == 200, foreign.text
    assert foreign.json()["total"] == 0
    assert receipt_b not in foreign.text
    assert wallet_b["agent_wallet_id"] not in foreign.text
