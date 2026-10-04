"""Budget threshold alerts have to be readable, not merely writable.

PermitService.reserve_budget writes a billing alert when a reservation crosses
80%, 90% or 100% of a permit's ``max_credits``. Every alert read converts
stored rows through the public ``AlertType`` enum, so a threshold type the
enum does not name does not merely hide its own alert: the conversion raises,
and the entire listing for that wallet fails for as long as the row exists.

The failure is worst exactly where it is most likely. A wallet carrying budget
drift has an inflated ``spent_credits``, which is what trips these thresholds
in the first place -- so the wallets most likely to hold a threshold alert are
the ones whose alert listing can least afford to break.

Both tests drive real reservations through ``reserve_budget`` rather than
inserting rows, because the property under test is that the writer and the
enum agree. Both are marked ``dormant``: the operator route lives on the
billing expansion router, which the default test app leaves unmounted.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import AlertType
from app.services.permits import get_permit_service
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.mark.anyio
@pytest.mark.dormant
async def test_a_budget_threshold_alert_is_readable_through_the_alert_api(
    client: AsyncClient, clean_database: None
) -> None:
    """The row has to come back out, or writing it made things worse.

    Read through both alert routes -- the wallet's own ``/v1/me/alerts`` and
    the operator's ``/v1/billing/alerts`` -- rather than through the table,
    because only a read that converts through ``AlertType`` proves the stored
    type is public.
    """
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name="trust-echo",
        max_credits=50,
        idem_key="permit-threshold-permit-1",
    )

    # 41 of 50 credits: past 80% and short of 90%, so exactly one alert fires.
    await get_permit_service().reserve_budget(permit["permit_id"], Decimal("41"))

    mine = await client.get("/v1/me/alerts", headers=ctx["agent_headers"])
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["total"] == 1, body
    assert body["unacknowledged"] == 1, body
    (alert,) = body["alerts"]
    assert alert["alert_type"] == AlertType.PERMIT_BUDGET_80PCT.value
    assert alert["wallet_id"] == ctx["agent_wallet_id"]
    assert alert["severity"] == "info"
    # billing_alerts has no permit column, so the permit id travels in the
    # message, which is what ties the alert back to the budget that tripped.
    assert permit["permit_id"] in alert["message"]

    listed = await client.get("/v1/billing/alerts", headers=ctx["agent_headers"])
    assert listed.status_code == 200, listed.text
    assert [a["alert_id"] for a in listed.json()["alerts"]] == [alert["alert_id"]]


@pytest.mark.anyio
@pytest.mark.dormant
async def test_every_budget_threshold_the_writer_can_store_is_readable(
    client: AsyncClient, clean_database: None
) -> None:
    """One listing has to survive all three types, not just the first.

    ``reserve_budget`` breaks after the highest threshold crossed, so each
    threshold is a separate branch storing a separate type. A permit walked
    up through all three ends up holding one row of each, and a single
    unconvertible row among them would fail the whole read -- so this asserts
    on the listing, which is the thing the enum gap actually broke.
    """
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name="trust-echo",
        max_credits=50,
        idem_key="permit-threshold-permit-2",
    )
    permit_id = permit["permit_id"]

    service = get_permit_service()
    # 82%, then 92%, then exactly 100%: one crossing per reservation.
    await service.reserve_budget(permit_id, Decimal("41"))
    await service.reserve_budget(permit_id, Decimal("5"))
    await service.reserve_budget(permit_id, Decimal("4"))

    mine = await client.get("/v1/me/alerts", headers=ctx["agent_headers"])
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["total"] == 3, body
    assert {a["alert_type"] for a in body["alerts"]} == {
        AlertType.PERMIT_BUDGET_80PCT.value,
        AlertType.PERMIT_BUDGET_90PCT.value,
        AlertType.PERMIT_BUDGET_EXHAUSTED.value,
    }

    listed = await client.get("/v1/billing/alerts", headers=ctx["agent_headers"])
    assert listed.status_code == 200, listed.text
    assert {a["alert_id"] for a in listed.json()["alerts"]} == {
        a["alert_id"] for a in body["alerts"]
    }
