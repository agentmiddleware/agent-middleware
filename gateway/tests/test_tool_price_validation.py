"""A registered tool's price must be a finite, positive number.

``tool_price`` is the one definition of what a governed invoke and a signed
quote charge. It used to return whatever the registration held, so a
negative, NaN or infinite price reached the permit budget, the policy check
and the quote signer before anything noticed:

- a negative price reserved a negative amount on the permit, and the charge
  that then refused the negative units never handed it back -- the permit's
  budget grew by the price on every attempt;
- an infinite price was signed into a persisted quote before the response
  model rejected it;
- a NaN price raised ``InvalidOperation`` out of the quote endpoint.

Each is now refused at the price itself, before any policy, permit, quote or
ledger step runs.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.database import get_session_factory
from app.db.models import (
    LedgerEntryModel,
    PermitModel,
    QuoteModel,
    ReceiptModel,
    WalletModel,
)
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.pricing import DEFAULT_PRICING, tool_price
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

TOOL = "bad-price-echo"
CATEGORY = ServiceCategory.AGENT_COMMS

INVALID_FLOAT_PRICES = [
    float("nan"),
    float("inf"),
    float("-inf"),
    -1.0,
    0.0,
    0.123456789,
    0.000000001,
    1000000000000.0,
]


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def priced_tool():
    registry = get_service_registry()
    calls: list[str] = []

    def bad_price_echo(message: str = "ok") -> dict:
        calls.append(message)
        return {"message": message}

    def register(price: float) -> list[str]:
        registry.register_local(
            service_id=TOOL,
            name="Bad Price Echo",
            description="Local tool whose registered price is invalid",
            category=CATEGORY,
            func=bad_price_echo,
            credits_per_unit=price,
            unit_name="call",
        )
        return calls

    yield register
    registry.unregister_local(TOOL)


# --- tool_price itself --------------------------------------------------------


@pytest.mark.parametrize(
    "exact",
    [
        "NaN",
        "-NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "-1",
        "0",
        "-0",
        "-0.00000001",
        "abc",
        "",
        "0.123456789",
        "0.000000001",
        "1000000000000",
        "100000000000.12345678",
    ],
)
def test_tool_price_refuses_invalid_exact_override(exact):
    with pytest.raises(ValueError, match="tool_price_invalid"):
        tool_price({"credits_per_unit_exact": exact}, CATEGORY)


@pytest.mark.parametrize("price", [*INVALID_FLOAT_PRICES, None])
def test_tool_price_refuses_invalid_float_price(price):
    with pytest.raises(ValueError, match="tool_price_invalid"):
        tool_price({"credits_per_unit": price}, CATEGORY)


def test_tool_price_exact_override_is_checked_before_float_fallback():
    # A sound float does not rescue a broken exact override: the exact value
    # is the one the charge would use.
    with pytest.raises(ValueError, match="tool_price_invalid"):
        tool_price({"credits_per_unit": 2.0, "credits_per_unit_exact": "NaN"}, CATEGORY)


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        ({"credits_per_unit_exact": "2.50000000"}, Decimal("2.50000000")),
        ({"credits_per_unit_exact": "0.123456780"}, Decimal("0.12345678")),
        ({"credits_per_unit_exact": "0.00000001"}, Decimal("0.00000001")),
        ({"credits_per_unit": 3.0}, Decimal("3.0")),
        ({}, DEFAULT_PRICING[CATEGORY][1]),
    ],
)
def test_tool_price_accepts_finite_positive(service, expected):
    assert tool_price(service, CATEGORY) == expected


# --- governed invoke ----------------------------------------------------------


async def _counts(wallet_id: str, permit_id: str) -> dict:
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        permit = await session.get(PermitModel, permit_id)
        assert wallet is not None and permit is not None
        debits = (
            await session.execute(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(
                    LedgerEntryModel.wallet_id == wallet_id,  # type: ignore[arg-type]
                    LedgerEntryModel.amount < 0,  # type: ignore[arg-type]
                )
            )
        ).scalar_one()
        receipts = (
            await session.execute(
                select(func.count())
                .select_from(ReceiptModel)
                .where(ReceiptModel.permit_id == permit_id)  # type: ignore[arg-type]
            )
        ).scalar_one()
        return {
            "balance": wallet.balance,
            "debits": debits,
            "permit_spent": permit.spent_credits,
            "permit_status": permit.status,
            "receipts": receipts,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("price", INVALID_FLOAT_PRICES)
async def test_governed_invoke_refuses_invalid_price_before_budget_or_charge(
    client, clean_database, priced_tool, price
):
    calls = priced_tool(price)
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key=f"permit-{uuid.uuid4().hex[:8]}",
    )
    before = await _counts(agent["agent_wallet_id"], permit["permit_id"])

    resp = await client.post(
        f"/mcp/tools/{TOOL}/invoke",
        json={
            "name": TOOL,
            "arguments": {"message": "hi"},
            "mcp_context": {
                "wallet_id": agent["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": f"inv-{uuid.uuid4().hex[:12]}",
            },
        },
        headers=agent["agent_headers"],
    )
    body = resp.json()
    assert body["isError"] is True, body
    assert body.get("receipt") is None
    # A misconfigured price is the operator's fault, not the caller's; the
    # caller gets the opaque internal error, never the raw value.
    assert body["structuredContent"]["error"] == "internal_error"

    after = await _counts(agent["agent_wallet_id"], permit["permit_id"])
    # The permit budget in particular: a negative price used to reserve -1
    # credit here and leave it, growing the permit by a credit per attempt.
    assert after == before
    assert after["permit_spent"] == Decimal("0")
    assert calls == []


@pytest.mark.asyncio
async def test_governed_invoke_by_another_wallet_is_refused_before_pricing(
    client, clean_database, priced_tool
):
    """Cross-tenant: a caller naming a wallet it does not own is denied on
    ownership, whatever the tool's price looks like."""
    priced_tool(-1.0)
    owner = await provision_agent_wallet(client)
    intruder = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=owner["agent_wallet_id"],
        key_id=owner["key_id"],
        tool_name=TOOL,
        idem_key=f"permit-{uuid.uuid4().hex[:8]}",
    )
    before = await _counts(owner["agent_wallet_id"], permit["permit_id"])

    resp = await client.post(
        f"/mcp/tools/{TOOL}/invoke",
        json={
            "name": TOOL,
            "arguments": {"message": "hi"},
            "mcp_context": {
                "wallet_id": owner["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": f"inv-{uuid.uuid4().hex[:12]}",
            },
        },
        headers=intruder["agent_headers"],
    )
    assert resp.status_code == 403
    assert await _counts(owner["agent_wallet_id"], permit["permit_id"]) == before


# --- signed quotes ------------------------------------------------------------


async def _quote_rows(wallet_id: str) -> int:
    factory = get_session_factory()
    async with factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(QuoteModel)
                .where(QuoteModel.wallet_id == wallet_id)  # type: ignore[arg-type]
            )
        ).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize("price", INVALID_FLOAT_PRICES)
async def test_quote_refuses_invalid_price_without_signing_or_storing(
    client, clean_database, priced_tool, price
):
    priced_tool(price)
    agent = await provision_agent_wallet(client)

    resp = await client.post(
        "/v1/quotes",
        json={"wallet_id": agent["agent_wallet_id"], "tool": TOOL},
        headers=agent["agent_headers"],
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "tool_price_invalid"
    # Nothing was signed into a durable promise.
    assert await _quote_rows(agent["agent_wallet_id"]) == 0


@pytest.mark.asyncio
async def test_quote_for_another_wallet_is_refused_before_pricing(
    client, clean_database, priced_tool
):
    priced_tool(float("inf"))
    owner = await provision_agent_wallet(client)
    intruder = await provision_agent_wallet(client)

    resp = await client.post(
        "/v1/quotes",
        json={"wallet_id": owner["agent_wallet_id"], "tool": TOOL},
        headers=intruder["agent_headers"],
    )
    assert resp.status_code == 403
    assert await _quote_rows(owner["agent_wallet_id"]) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount", ["0", "NaN", "Infinity", "0.123456789", "100000000000.12345678"]
)
async def test_direct_quote_service_rejects_invalid_amount_before_signing(
    amount, monkeypatch
):
    from unittest.mock import AsyncMock
    from app.services.quotes import QuoteError, QuoteService
    from app.services.signing_keys import get_signing_key_service

    signer = AsyncMock()
    monkeypatch.setattr(get_signing_key_service(), "sign_payload", signer)
    with pytest.raises(QuoteError, match="quoted_credits_invalid"):
        await QuoteService().create_quote(
            wallet_id="unused",
            tool=TOOL,
            quoted_credits=Decimal(amount),
            category=CATEGORY.value,
        )
    signer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount", ["NaN", "Infinity", "0.123456789", "100000000000.12345678"]
)
@pytest.mark.parametrize("field", ["credits_authorized", "credits_charged"])
async def test_direct_receipt_service_rejects_non_storable_amount_before_signing(
    amount, field, monkeypatch
):
    from unittest.mock import AsyncMock
    from app.services.receipts import ReceiptError, ReceiptService
    from app.services.signing_keys import get_signing_key_service

    signer = AsyncMock()
    monkeypatch.setattr(get_signing_key_service(), "sign_payload", signer)
    credits = {"credits_authorized": Decimal("1"), "credits_charged": Decimal("0")}
    credits[field] = Decimal(amount)
    with pytest.raises(ReceiptError, match="receipt_credits_invalid"):
        await ReceiptService().create_receipt(
            permit_id="unused",
            wallet_id="unused",
            key_id=None,
            tool=TOOL,
            request_payload={},
            response_payload={},
            ledger_entry_id=None,
            outcome="success",
            audit_event_id=None,
            **credits,
        )
    signer.assert_not_awaited()
