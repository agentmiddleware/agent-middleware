"""Operator-only profitability reports use one explicit 24-hour window."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel
from app.main import app
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value


@pytest.mark.anyio
@pytest.mark.dormant
async def test_arbitrage_requires_admin_before_loading_global_economics(
    client, clean_database
):
    owner = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    charged = await client.post(
        "/v1/billing/charge",
        params={"wallet_id": owner["agent_wallet_id"], "service": "platform_fee"},
        headers=owner["agent_headers"],
    )
    assert charged.status_code == 200
    for agent in (owner, other):
        denied = await client.get(
            "/v1/billing/arbitrage", headers=agent["agent_headers"]
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["error"] == "admin_access_denied"
    for headers, expected_status in (
        ({}, 401),
        ({"X-API-Key": "invalid-test-key"}, 403),
    ):
        denied = await client.get("/v1/billing/arbitrage", headers=headers)
        assert denied.status_code == expected_status
    permitted = await client.get("/v1/billing/arbitrage", headers=BOOTSTRAP_HEADERS)
    assert permitted.status_code == 200
    assert Decimal(permitted.json()["total_revenue_exact"]) == Decimal("0.1")


@pytest.mark.anyio
@pytest.mark.dormant
async def test_arbitrage_bounds_totals_and_top_actions_to_the_reported_window(
    client, clean_database, monkeypatch
):
    owner = await provision_agent_wallet(client)
    end = utc_now().replace(microsecond=0)
    start = end - timedelta(days=1)
    monkeypatch.setattr("app.services.billing_engine.utc_now", lambda: end)
    included = set()
    async with get_session_factory()() as session:
        for when, in_window in (
            (start - timedelta(microseconds=1), False),
            (start, True),
            (end - timedelta(microseconds=1), True),
            (end, False),
            (end + timedelta(days=1), False),
        ):
            entry_id = str(uuid4())
            if in_window:
                included.add(entry_id)
            session.add(
                LedgerEntryModel(
                    entry_id=entry_id,
                    wallet_id=owner["agent_wallet_id"],
                    action="debit",
                    amount=Decimal("-0.3"),
                    compute_cost=Decimal("0.1"),
                    margin=Decimal("0.2"),
                    balance_after=Decimal("999"),
                    service_category="platform_fee",
                    timestamp=when,
                )
            )
        # A credit inside the window is not service revenue.
        session.add(
            LedgerEntryModel(
                entry_id=str(uuid4()),
                wallet_id=owner["agent_wallet_id"],
                action="credit",
                amount=Decimal("100"),
                balance_after=Decimal("1099"),
                timestamp=start,
            )
        )
        await session.commit()
    response = await client.get("/v1/billing/arbitrage", headers=BOOTSTRAP_HEADERS)
    assert response.status_code == 200
    report = response.json()
    assert (
        report["period"]
        == f"{start.isoformat()}Z to {end.isoformat()}Z (end exclusive)"
    )
    assert Decimal(report["total_revenue_exact"]) == Decimal("0.6")
    assert Decimal(report["total_compute_cost_exact"]) == Decimal("0.2")
    assert Decimal(report["gross_margin_exact"]) == Decimal("0.4")
    assert report["by_service"]["platform_fee"]["transactions"] == 2
    assert {entry["entry_id"] for entry in report["top_profitable_actions"]} == included
