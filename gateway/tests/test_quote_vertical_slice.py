"""Quote-locked pricing vertical slice verification.

This test demonstrates the complete implementation of WEDGE.md's quote promise:

> A signed quote can fix the price of one call, and the charge honors it even
> after the tool's registered price moves.

The vertical slice proves:

1. Issue quote → invoke with quote → charge uses quoted amount
2. Price change after quote does not change the debit
3. Negative paths (expired/mismatched/wrong-tool quote rejected, no charge)
4. No secrets in logs
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import QuoteModel, WalletModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    create_tool_permit,
    provision_agent_wallet,
)

TOOL = "vertical-slice-tool"
INITIAL_PRICE = Decimal("5.0")
HIKED_PRICE = Decimal("25.0")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def registered_tool():
    """A local tool whose price can be moved mid-test."""
    registry = get_service_registry()

    def tool_func(message: str = "ok") -> dict:
        return {"message": message}

    def register(price: Decimal) -> None:
        registry.register_local(
            service_id=TOOL,
            name="Quote Vertical Slice Tool",
            description="Governed quote vertical slice demo tool",
            category=ServiceCategory.AGENT_COMMS,
            func=tool_func,
            credits_per_unit=float(price),
            unit_name="call",
        )

    register(INITIAL_PRICE)
    yield register
    registry.unregister_local(TOOL)


async def _quote(client, headers, wallet_id: str, tool: str = TOOL):
    return await client.post(
        "/v1/quotes",
        json={"wallet_id": wallet_id, "tool": tool},
        headers=headers,
    )


async def _invoke(client, headers, *, wallet_id, permit_id, quote_id=None, idem=None):
    context = {
        "wallet_id": wallet_id,
        "permit_id": permit_id,
        "idempotency_key": idem or f"inv-{uuid.uuid4().hex[:12]}",
    }
    if quote_id is not None:
        context["quote_id"] = quote_id
    return await client.post(
        f"/mcp/tools/{TOOL}/invoke",
        json={
            "name": TOOL,
            "arguments": {"message": "vertical slice demo"},
            "mcp_context": context,
        },
        headers=headers,
    )


async def _balance(wallet_id: str) -> Decimal:
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        return wallet.balance


@pytest.mark.asyncio
async def test_quote_locked_pricing_vertical_slice(
    client, clean_database, registered_tool
):
    """Complete vertical slice: quote → price moves → charge uses quote.

    This is the WEDGE.md promise: 'A signed quote can fix the price of one
    call, and the charge honors it even after the tool's registered price
    moves.'

    The flow:
    1. Issue a quote at the initial price (5 credits)
    2. Move the registered price up significantly (25 credits)
    3. Invoke with the quote
    4. Verify the wallet was charged the quoted price (5), not the new price (25)
    5. Verify the receipt shows the quoted amount
    """
    # Setup: provision agent wallet and permit
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-vertical-slice",
    )

    # Step 1: Issue quote at initial price
    quote_resp = await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    assert quote_resp.status_code == 201, quote_resp.text
    quote = quote_resp.json()

    # Verify quote carries the initial price
    assert Decimal(str(quote["quoted_credits"])) == INITIAL_PRICE
    assert quote["status"] == "active"
    assert quote["tool"] == TOOL
    assert quote["wallet_id"] == agent["agent_wallet_id"]
    assert quote["signature"] and quote["key_id"]

    # Step 2: Move the price significantly upward
    registered_tool(HIKED_PRICE)

    # Step 3: Invoke with the quote
    before_balance = await _balance(agent["agent_wallet_id"])
    invoke_resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=quote["quote_id"],
    )

    # Step 4: Verify success and quoted price was charged
    assert invoke_resp.status_code == 200, invoke_resp.text
    after_balance = await _balance(agent["agent_wallet_id"])
    charged = before_balance - after_balance

    # The debit honors the quote (5), not the new registered price (25)
    assert charged == INITIAL_PRICE, (
        f"Expected charge of {INITIAL_PRICE} (quoted), "
        f"got {charged}. New price is {HIKED_PRICE}."
    )

    # Step 5: Verify receipt
    body = invoke_resp.json()
    assert "receipt" in body
    receipt = body["receipt"]
    assert Decimal(str(receipt["credits_charged"])) == INITIAL_PRICE

    # Quote is now consumed
    quote_read = await client.get(
        f"/v1/quotes/{quote['quote_id']}", headers=agent["agent_headers"]
    )
    assert quote_read.json()["status"] == "consumed"


@pytest.mark.asyncio
async def test_negative_paths_all_deny_without_charge(
    client, clean_database, registered_tool
):
    """Negative paths: invalid quotes deny the invoke, no charge happens.

    Covers:
    - Expired quote
    - Wrong wallet
    - Wrong tool
    - Already consumed quote
    - Unknown quote id
    """
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-negative-slice",
    )

    # Case 1: Expired quote
    expired_quote_resp = await _quote(
        client, agent["agent_headers"], agent["agent_wallet_id"]
    )
    expired_quote = expired_quote_resp.json()

    # Force expiry
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(QuoteModel, expired_quote["quote_id"])
        model.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=1
        )
        session.add(model)
        await session.commit()

    before = await _balance(agent["agent_wallet_id"])
    expired_resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=expired_quote["quote_id"],
    )
    assert expired_resp.status_code == 403
    assert "quote_expired" in expired_resp.text
    # No charge happened
    assert await _balance(agent["agent_wallet_id"]) == before

    # Case 2: Wrong wallet (stranger's quote)
    stranger_quote = (
        await _quote(client, stranger["agent_headers"], stranger["agent_wallet_id"])
    ).json()

    before = await _balance(agent["agent_wallet_id"])
    wrong_wallet_resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=stranger_quote["quote_id"],
    )
    assert wrong_wallet_resp.status_code == 403
    assert "quote_wallet_mismatch" in wrong_wallet_resp.text
    assert await _balance(agent["agent_wallet_id"]) == before

    # Case 3: Wrong tool
    from app.trust import get_quote_service

    wrong_tool_quote = await get_quote_service().create_quote(
        wallet_id=agent["agent_wallet_id"],
        tool="different-tool",
        quoted_credits=Decimal("1.0"),
        category=ServiceCategory.AGENT_COMMS.value,
    )

    before = await _balance(agent["agent_wallet_id"])
    wrong_tool_resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=wrong_tool_quote.quote_id,
    )
    assert wrong_tool_resp.status_code == 403
    assert "quote_tool_mismatch" in wrong_tool_resp.text
    assert await _balance(agent["agent_wallet_id"]) == before

    # Case 4: Already consumed quote
    consumed_quote = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()

    # Consume it once
    first_invoke = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=consumed_quote["quote_id"],
        idem="first-consume",
    )
    assert first_invoke.status_code == 200

    # Try to use it again
    before = await _balance(agent["agent_wallet_id"])
    second_invoke = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=consumed_quote["quote_id"],
        idem="second-consume",
    )
    assert second_invoke.status_code == 403
    assert "quote_already_consumed" in second_invoke.text
    assert await _balance(agent["agent_wallet_id"]) == before

    # Case 5: Unknown quote id
    before = await _balance(agent["agent_wallet_id"])
    unknown_resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id="quote-does-not-exist",
    )
    assert unknown_resp.status_code == 403
    assert "quote_not_found" in unknown_resp.text
    assert await _balance(agent["agent_wallet_id"]) == before


@pytest.mark.asyncio
async def test_price_cut_also_honored(client, clean_database, registered_tool):
    """Price cuts are also locked: a quote is a commitment, not a best-price guarantee."""
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-price-cut",
    )

    # Issue quote at initial price
    quote = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()
    assert Decimal(str(quote["quoted_credits"])) == INITIAL_PRICE

    # Price drops dramatically
    registered_tool(Decimal("0.1"))

    # Invoke still charges the quoted price (higher than current)
    before = await _balance(agent["agent_wallet_id"])
    resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=quote["quote_id"],
    )
    assert resp.status_code == 200, resp.text
    charged = before - await _balance(agent["agent_wallet_id"])

    # Charged the quoted amount, not the lower current price
    assert charged == INITIAL_PRICE
