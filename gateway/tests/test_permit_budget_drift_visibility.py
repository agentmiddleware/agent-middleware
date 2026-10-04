"""Making an absorbed budget release visible instead of silent.

The MCP routers absorb a ``PermitWriteContendedError`` from the post-effects
budget release rather than propagating it, because the receipt is written
*after* that release and propagating would destroy the governance artifact for
a call that ran (pinned in ``test_permit_write_contention_surface``). The
absorb is correct; what it leaves behind is not free.

The permit keeps ``amount`` reserved against a call that was refunded, so
``spent_credits`` sits above what the receipts prove consumed.
``reconcile_budgets`` repairs exactly that drift -- but deliberately only for
permits that can no longer admit a charge, because downward-resetting a live
permit would open an over-spend window past ``max_credits``. A long-lived
permit therefore carries the inflation until it expires, and a later
legitimate call under it can be wrongly denied ``permit_budget_exceeded``.

These tests pin the two halves of making that observable without touching the
live-permit safety rule:

* the absorb records the stranded amount durably, and can never fail the call
  it is reporting on;
* what it records comes back out through the alert API instead of breaking it;
* ``reconcile_budgets`` *reports* live drift while still refusing to repair it.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.resilience import WRITE_CONFLICT_MAX_ATTEMPTS
from app.db.database import get_session_factory
from app.db.models import BillingAlertModel, PermitModel, ReceiptModel
from app.main import app
from app.schemas.billing import AlertType, ServiceCategory
from app.services.permits import get_permit_service
from app.services.service_registry import get_service_registry
from tests.test_permit_write_contention_surface import (
    TOOL_COST,
    _lose_permit_writes,
    _rest_body,
)
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def failing_tool():
    """A tool that runs, has its effect, then fails -- see the contention suite.

    Redeclared rather than imported: a fixture imported by name would register
    and unregister the same service id as the suite it came from, and the two
    would collide when both run in one session.
    """
    tool_name = "permit-drift-boom"
    runs = {"count": 0}

    def boom(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        raise RuntimeError("tool blew up")

    get_service_registry().register_local(
        service_id=tool_name,
        name="Permit drift boom",
        description="Post-effects budget drift visibility test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=boom,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        yield tool_name, runs
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.fixture
def echo_tool():
    """A tool that runs and succeeds, so a permit can carry a receipted charge.

    The healthy-permit case needs a reservation that the receipts account for,
    which only a call that actually completed produces.
    """
    tool_name = "permit-drift-echo"

    def echo(message: str = "ok") -> dict[str, Any]:
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Permit drift echo",
        description="Post-effects budget drift visibility test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        yield tool_name
    finally:
        get_service_registry().unregister_local(tool_name)


async def _drift_alerts() -> list[BillingAlertModel]:
    factory = get_session_factory()
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(BillingAlertModel).where(
                        BillingAlertModel.alert_type == "permit_release_contended"
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.anyio
async def test_an_absorbed_release_records_the_credits_it_stranded(
    client: AsyncClient, clean_database: None, failing_tool
) -> None:
    """The absorb leaves a durable, per-permit trace, not just a log line.

    ``failures`` is bounded to the release ladder exactly, so the release
    exhausts and is absorbed while the alert write that follows it lands. That
    ordering is the test: the alert is written from inside the absorbing
    except block, after the release has definitively lost.
    """
    tool_name, runs = failing_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="permit-drift-permit-1",
    )
    body = _rest_body(
        tool_name=tool_name,
        wallet_id=ctx["agent_wallet_id"],
        permit_id=permit["permit_id"],
        idempotency_key="permit-drift-posteffects-1",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        _lose_permit_writes(
            monkeypatch, allow_first=1, failures=WRITE_CONFLICT_MAX_ATTEMPTS
        )
        resp = await client.post(
            f"/mcp/tools/{tool_name}/invoke", json=body, headers=ctx["agent_headers"]
        )

    # The premise: the tool ran, so this really is the post-effects release.
    assert runs["count"] == 1
    assert resp.json()["detail"]["receipt"]["outcome"] == "failed_refunded"

    alerts = await _drift_alerts()
    assert len(alerts) == 1, alerts
    alert = alerts[0]
    assert alert.wallet_id == ctx["agent_wallet_id"]
    assert alert.severity == "warning"
    # billing_alerts has no permit column, so the permit id travels in the
    # message the way the budget-threshold alerts already do.
    assert permit["permit_id"] in alert.message
    # The stranded reservation, which is what makes the drift actionable.
    assert alert.threshold_amount == Decimal(str(TOOL_COST))


@pytest.mark.anyio
async def test_a_lost_drift_alert_never_fails_the_call_it_reports_on(
    client: AsyncClient, clean_database: None, failing_tool
) -> None:
    """The alert write is best-effort, and must stay that way.

    It runs from inside the absorbing except block, *before* the receipt the
    absorb exists to protect has been written. An observability write that
    could propagate would re-create precisely the failure the absorb prevents
    -- so here every permit write loses, including the alert's own, and the
    call must still come back with its receipt.
    """
    tool_name, runs = failing_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="permit-drift-permit-2",
    )
    body = _rest_body(
        tool_name=tool_name,
        wallet_id=ctx["agent_wallet_id"],
        permit_id=permit["permit_id"],
        idempotency_key="permit-drift-posteffects-2",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        # failures=None: nothing lands after the reserve, the alert included.
        _lose_permit_writes(monkeypatch, allow_first=1)
        resp = await client.post(
            f"/mcp/tools/{tool_name}/invoke", json=body, headers=ctx["agent_headers"]
        )

    assert runs["count"] == 1
    # The caller's real answer survived, receipt and all.
    detail = resp.json()["detail"]
    assert "tool blew up" in detail["error"], detail
    assert detail["receipt"]["outcome"] == "failed_refunded", detail

    factory = get_session_factory()
    async with factory() as session:
        receipts = (await session.execute(select(ReceiptModel))).scalars().all()
    assert len(receipts) == 1

    # The alert is what was sacrificed, and losing it costs only visibility --
    # reconcile_budgets still reports the drift from the permit row itself.
    assert await _drift_alerts() == []


@pytest.mark.anyio
@pytest.mark.dormant
async def test_the_drift_alert_is_readable_through_the_alert_api(
    client: AsyncClient, clean_database: None
) -> None:
    """The row has to come back out, or writing it made things worse.

    Both alert reads -- the wallet's own ``/v1/me/alerts`` and the operator's
    ``/v1/billing/alerts`` -- convert every stored row through the public
    ``AlertType`` enum. A stored type the enum does not name would not merely
    hide that one alert: the conversion raises, and the whole listing for the
    wallet fails for as long as the row exists. So this reads the alert back
    through both routes rather than through the table, which is the only
    reading that proves the type is public.

    Marked ``dormant`` because the operator route lives on the billing
    expansion router, which the default test app leaves unmounted.
    """
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name="trust-echo",
        idem_key="permit-drift-permit-5",
    )

    assert await get_permit_service().record_absorbed_release_drift(
        permit_id=permit["permit_id"],
        amount=Decimal("3"),
        site="release_budget",
    )

    mine = await client.get("/v1/me/alerts", headers=ctx["agent_headers"])
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["total"] == 1, body
    assert body["unacknowledged"] == 1, body
    (alert,) = body["alerts"]
    assert alert["alert_type"] == AlertType.PERMIT_RELEASE_CONTENDED.value
    assert alert["wallet_id"] == ctx["agent_wallet_id"]
    assert alert["severity"] == "warning"
    assert Decimal(alert["threshold_amount_exact"]) == Decimal("3")
    assert permit["permit_id"] in alert["message"]

    listed = await client.get("/v1/billing/alerts", headers=ctx["agent_headers"])
    assert listed.status_code == 200, listed.text
    assert [a["alert_id"] for a in listed.json()["alerts"]] == [alert["alert_id"]]


async def _make_idle(permit_id: str, *, spent: Decimal | None = None) -> None:
    """Age a permit past the idle window, with ``spent`` reserved if given.

    Active and unexpired throughout: this is the permit ``reconcile_budgets``
    must never repair, which is exactly why its drift needs reporting.
    """
    from datetime import datetime, timedelta, timezone

    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit_id)
        if spent is not None:
            model.spent_credits = spent
        model.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.add(model)
        await session.commit()


def _drift_records(caplog) -> list[Any]:
    return [r for r in caplog.records if r.message == "permit_budget_live_drift"]


@pytest.mark.anyio
async def test_reconcile_reports_live_drift_but_still_refuses_to_repair_it(
    client: AsyncClient, clean_database: None, caplog
) -> None:
    """The whole point: visible before expiry, still never downward-reset.

    ``test_reconcile_budgets_never_resets_a_live_active_permit`` pins the
    safety half of this and must keep passing unchanged -- a live permit can
    still admit a charge, so resetting its reservation would let a concurrent
    request over-spend past ``max_credits``. What changes is only that the
    drift stops being invisible while it waits for expiry.
    """
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name="trust-echo",
        idem_key="permit-drift-permit-3",
    )
    # Nine credits reserved against zero receipts: pure drift.
    await _make_idle(permit["permit_id"], spent=Decimal("9"))

    caplog.set_level(logging.WARNING, logger="app.services.permits")
    corrected = await get_permit_service().reconcile_budgets(idle_seconds=900)

    # Unrepaired, exactly as before.
    assert corrected == 0
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        assert model.spent_credits == Decimal("9")

    # But no longer silent.
    records = _drift_records(caplog)
    assert len(records) == 1, [r.message for r in caplog.records]
    assert records[0].permit_ids == [permit["permit_id"]]
    assert records[0].permits == 1
    # Compared as a Decimal, not a string: the column carries 8 decimal
    # places and pinning "9.00000000" would pin storage precision.
    assert Decimal(records[0].total_drift) == Decimal("9")


@pytest.mark.anyio
async def test_a_live_permit_whose_receipts_match_is_not_reported(
    client: AsyncClient, clean_database: None, caplog, echo_tool
) -> None:
    """The report has to stay quiet on healthy permits or it is unreadable.

    The permit carries one real, receipted charge and is aged the same way as
    the drift case, so it is genuinely examined by the scan -- which skips
    permits with nothing reserved as well as permits too recent to judge --
    rather than quiet for the wrong reason. Without both, this test would pass
    while the scan never looked at the permit at all.
    """
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=echo_tool,
        idem_key="permit-drift-permit-4",
    )
    resp = await client.post(
        f"/mcp/tools/{echo_tool}/invoke",
        json=_rest_body(
            tool_name=echo_tool,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="permit-drift-healthy-1",
        ),
        headers=ctx["agent_headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["receipt"]["outcome"] == "success"
    # Reserved and receipted agree: one charge, one receipt, so no drift --
    # and spent_credits is non-zero, so the scan does not filter it out.
    await _make_idle(permit["permit_id"])
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        assert model.spent_credits == Decimal(str(TOOL_COST))

    caplog.set_level(logging.WARNING, logger="app.services.permits")
    assert await get_permit_service().reconcile_budgets(idle_seconds=900) == 0

    assert _drift_records(caplog) == []
