"""Signed quotes: a price the agent can rely on, honored at charge time.

Covers the contract:

- A quote is signed over (wallet, tool, credits, window) and verifies against
  the published key; tampering with any of those fails verification.
- Presenting the quote on a governed invoke charges the quoted credits even
  after the tool's registered price moves — in both directions.
- Single use: the second invoke on the same quote is denied, not repriced.
- Expiry, wrong wallet, wrong tool, and unknown ids deny the invoke rather
  than silently falling back to the live price.
- An invoke that consumed a quote but could not charge hands the quote back.
- The permit budget is checked against the quoted price, not the live one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import QuoteModel, WalletModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.quotes import get_quote_service
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

TOOL = "quote-echo"
LIST_PRICE = 2.0


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def registered_tool():
    """A local tool whose price can be moved mid-test."""
    registry = get_service_registry()

    def quote_echo(message: str = "ok") -> dict:
        return {"message": message}

    def register(price: float) -> None:
        registry.register_local(
            service_id=TOOL,
            name="Quote Echo",
            description="Governed quote test tool",
            category=ServiceCategory.AGENT_COMMS,
            func=quote_echo,
            credits_per_unit=price,
            unit_name="call",
        )

    register(LIST_PRICE)
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
            "arguments": {"message": "hi"},
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
async def test_quote_carries_the_live_price_and_a_window(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    resp = await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["tool"] == TOOL
    assert Decimal(str(body["quoted_credits"])) == Decimal("2.0")
    assert body["status"] == "active"
    assert body["signature"] and body["key_id"]
    issued = datetime.fromisoformat(body["issued_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=9) <= expires - issued <= timedelta(minutes=11)


@pytest.mark.asyncio
async def test_quote_signature_verifies_and_detects_tampering(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    quote_id = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()["quote_id"]

    service = get_quote_service()
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(QuoteModel, quote_id)
        assert await service.verify_signature(model) is True

        # Repricing the stored row must not verify — the signature is what
        # makes the promise checkable without trusting this database.
        model.quoted_credits = Decimal("0.01")
        assert await service.verify_signature(model) is False

    # Consuming a quote must NOT invalidate the proof of what was promised.
    async with factory() as session:
        model = await session.get(QuoteModel, quote_id)
        assert await service.consume(quote_id) is True
        session.expire_all()
    async with factory() as session:
        consumed = await session.get(QuoteModel, quote_id)
        assert consumed.status == "consumed"
        assert await service.verify_signature(consumed) is True


@pytest.mark.asyncio
async def test_quote_locks_the_price_against_a_hike(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-quote-hike",
    )
    quote = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()
    assert Decimal(str(quote["quoted_credits"])) == Decimal("2.0")

    registered_tool(50.0)  # the world reprices after the quote was issued

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
    assert charged == Decimal("2.0")

    receipt = resp.json()["receipt"]
    assert Decimal(str(receipt["credits_charged"])) == Decimal("2.0")


@pytest.mark.asyncio
async def test_quote_locks_the_price_against_a_cut_too(
    client, clean_database, registered_tool
):
    """A lock is a commitment, not a best-price guarantee."""
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-quote-cut",
    )
    quote = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()

    registered_tool(0.5)

    before = await _balance(agent["agent_wallet_id"])
    resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=quote["quote_id"],
    )
    assert resp.status_code == 200, resp.text
    assert before - await _balance(agent["agent_wallet_id"]) == Decimal("2.0")


@pytest.mark.asyncio
async def test_invoke_without_a_quote_pays_the_live_price(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-quote-none",
    )
    registered_tool(7.0)

    before = await _balance(agent["agent_wallet_id"])
    resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
    )
    assert resp.status_code == 200, resp.text
    assert before - await _balance(agent["agent_wallet_id"]) == Decimal("7.0")


@pytest.mark.asyncio
async def test_quote_is_single_use(client, clean_database, registered_tool):
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-quote-single",
    )
    quote = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()

    first = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=quote["quote_id"],
    )
    assert first.status_code == 200

    before = await _balance(agent["agent_wallet_id"])
    second = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=quote["quote_id"],
    )
    assert second.status_code == 403
    assert "quote_already_consumed" in second.text
    # Denied, not silently repriced: nothing moved.
    assert await _balance(agent["agent_wallet_id"]) == before

    read = await client.get(
        f"/v1/quotes/{quote['quote_id']}", headers=agent["agent_headers"]
    )
    assert read.json()["status"] == "consumed"
    assert read.json()["consumed_at"] is not None


@pytest.mark.asyncio
async def test_expired_quote_denies_rather_than_repricing(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-quote-expired",
    )
    quote = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()

    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(QuoteModel, quote["quote_id"])
        model.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=1
        )
        session.add(model)
        await session.commit()

    before = await _balance(agent["agent_wallet_id"])
    resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=quote["quote_id"],
    )
    assert resp.status_code == 403
    assert "quote_expired" in resp.text
    assert await _balance(agent["agent_wallet_id"]) == before

    read = await client.get(
        f"/v1/quotes/{quote['quote_id']}", headers=agent["agent_headers"]
    )
    assert read.json()["status"] == "expired"


@pytest.mark.asyncio
async def test_quote_for_another_wallet_or_tool_is_refused(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-quote-mismatch",
    )
    stranger_quote = (
        await _quote(client, stranger["agent_headers"], stranger["agent_wallet_id"])
    ).json()

    wrong_wallet = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=stranger_quote["quote_id"],
    )
    assert wrong_wallet.status_code == 403
    assert "quote_wallet_mismatch" in wrong_wallet.text

    unknown = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id="quote-does-not-exist",
    )
    assert unknown.status_code == 403
    assert "quote_not_found" in unknown.text

    # A quote issued for a different tool cannot price this call.
    other_tool_quote = get_quote_service()
    quote = await other_tool_quote.create_quote(
        wallet_id=agent["agent_wallet_id"],
        tool="some-other-tool",
        quoted_credits=Decimal("0.01"),
        category=ServiceCategory.AGENT_COMMS.value,
    )
    wrong_tool = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=quote.quote_id,
    )
    assert wrong_tool.status_code == 403
    assert "quote_tool_mismatch" in wrong_tool.text


@pytest.mark.asyncio
async def test_quote_is_returned_when_the_charge_never_lands(
    client, clean_database, registered_tool
):
    """Consumed before the charge, so a failed charge must hand it back."""
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        max_credits=500,
        idem_key="permit-quote-broke",
    )
    registered_tool(200.0)
    quote = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()
    assert Decimal(str(quote["quoted_credits"])) == Decimal("200.0")

    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, agent["agent_wallet_id"])
        wallet.balance = Decimal("1")
        session.add(wallet)
        await session.commit()

    resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=quote["quote_id"],
    )
    assert resp.status_code in {402, 403}

    read = await client.get(
        f"/v1/quotes/{quote['quote_id']}", headers=agent["agent_headers"]
    )
    assert read.json()["status"] == "active"
    assert read.json()["consumed_at"] is None


@pytest.mark.asyncio
async def test_permit_budget_is_checked_against_the_quoted_price(
    client, clean_database, registered_tool
):
    """The permit sees what will be charged, not what the tool costs today."""
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        max_credits=10,
        idem_key="permit-quote-budget",
    )
    quote = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()

    # Live price now exceeds the permit budget; the locked price does not.
    registered_tool(500.0)
    ok = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=quote["quote_id"],
    )
    assert ok.status_code == 200, ok.text

    # Without the quote, the same call is over budget and denied.
    denied = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
    )
    assert denied.status_code == 403
    assert "permit_budget_exceeded" in denied.text


@pytest.mark.asyncio
async def test_quote_read_is_wallet_scoped(client, clean_database, registered_tool):
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    quote_id = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()["quote_id"]

    # A quote the caller may not read is indistinguishable from one that does
    # not exist: a 403 here confirmed the id was real and its body named the
    # owner's wallet_id.
    foreign = await client.get(
        f"/v1/quotes/{quote_id}", headers=stranger["agent_headers"]
    )
    missing = await client.get(
        "/v1/quotes/quote-0000000000000000", headers=stranger["agent_headers"]
    )
    assert foreign.status_code == 404, foreign.text
    assert foreign.json() == missing.json() == {"detail": "quote_not_found"}
    assert agent["agent_wallet_id"] not in foreign.text
    assert agent["sponsor_wallet_id"] not in foreign.text
    assert (
        await client.get(f"/v1/quotes/{quote_id}", headers=agent["agent_headers"])
    ).status_code == 200
    assert (
        await client.get(f"/v1/quotes/{quote_id}", headers=BOOTSTRAP_HEADERS)
    ).status_code == 200
    assert (
        await client.get("/v1/quotes/quote-nope", headers=agent["agent_headers"])
    ).status_code == 404


@pytest.mark.asyncio
async def test_quoting_someone_elses_wallet_or_an_unknown_tool_is_refused(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)

    foreign = await _quote(client, agent["agent_headers"], stranger["agent_wallet_id"])
    assert foreign.status_code == 403

    unknown = await _quote(
        client, agent["agent_headers"], agent["agent_wallet_id"], tool="no-such-tool"
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "tool_not_found"

    factory = get_session_factory()
    async with factory() as session:
        assert (await session.execute(select(QuoteModel))).scalars().all() == []


@pytest.mark.asyncio
async def test_concurrent_consume_spends_a_quote_once(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    quote = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()

    service = get_quote_service()
    first = await service.consume(quote["quote_id"], idempotency_key="a")
    second = await service.consume(quote["quote_id"], idempotency_key="b")
    assert (first, second) == (True, False)

    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(QuoteModel, quote["quote_id"])
    assert model.status == "consumed"
    # The winner's invoke is recorded on the quote, so a spent commitment can
    # be traced to the call that spent it.
    assert model.consumed_by_idempotency_key == "a"


@pytest.mark.asyncio
async def test_wallet_can_list_its_own_quotes(client, clean_database, registered_tool):
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    spendable = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()
    spent = (
        await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    ).json()
    await get_quote_service().consume(spent["quote_id"])
    await _quote(client, stranger["agent_headers"], stranger["agent_wallet_id"])

    listed = await client.get("/v1/me/quotes", headers=agent["agent_headers"])
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 2
    ids = {row["quote_id"] for row in body["quotes"]}
    assert ids == {spendable["quote_id"], spent["quote_id"]}

    active = await client.get(
        "/v1/me/quotes?status=active", headers=agent["agent_headers"]
    )
    assert [row["quote_id"] for row in active.json()["quotes"]] == [
        spendable["quote_id"]
    ]

    # An unspent quote whose window has passed reads as expired here too, so
    # the list agrees with what the invoke path would do with it.
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(QuoteModel, spendable["quote_id"])
        model.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=1
        )
        session.add(model)
        await session.commit()

    assert (
        await client.get("/v1/me/quotes?status=active", headers=agent["agent_headers"])
    ).json()["quotes"] == []
    stale = await client.get(
        "/v1/me/quotes?status=expired", headers=agent["agent_headers"]
    )
    assert [row["quote_id"] for row in stale.json()["quotes"]] == [
        spendable["quote_id"]
    ]

    # Bootstrap admins hold no wallet, so the self view refuses them.
    assert (
        await client.get("/v1/me/quotes", headers=BOOTSTRAP_HEADERS)
    ).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [0.00000001, 0.12345678])
async def test_eight_place_prices_keep_quote_and_receipt_signatures_after_reload(
    client, clean_database, registered_tool, price
):
    from app.services.receipts import get_receipt_service

    registered_tool(price)
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
    )
    quote = await _quote(client, agent["agent_headers"], agent["agent_wallet_id"])
    assert quote.status_code == 201
    quote_id = quote.json()["quote_id"]
    async with get_session_factory()() as session:
        model = await session.get(QuoteModel, quote_id)
        assert model is not None
        assert await get_quote_service().verify_signature(model) is True
    invoked = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
        quote_id=quote_id,
    )
    assert invoked.status_code == 200
    assert invoked.json().get("isError") is not True
    receipt = invoked.json()["receipt"]
    assert Decimal(str(receipt["credits_charged"])) == Decimal(str(price))
    valid, reason, _ = await get_receipt_service().verify_receipt(receipt["receipt_id"])
    assert valid, reason
