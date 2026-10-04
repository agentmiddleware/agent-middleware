"""Budget mutations must be decided by the database, not by a stale read.

Every test here targets a path that wrote ``spent_credits`` from a value this
process had read in an earlier statement. On PostgreSQL the surrounding
``SELECT ... FOR UPDATE`` hid the problem; on SQLite that lock is a silent
no-op, so these were live defects rather than theory.

``validate_trust_mode_config`` now refuses a SQLite ``DATABASE_URL`` in
production-like environments, which closes the configuration these exploited.
That guard is a second line, not a replacement: it is one environment-variable
check away from being bypassed, it does not apply to the local and staging
databases people do real work against, and a path that writes money from a
stale read is wrong on its own terms. These tests therefore keep running on
SQLite, where the missing lock makes the defect observable at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import PermitModel
from app.main import app
from app.schemas.trust import PermitCreateRequest
from app.services.permits import PermitError, get_permit_service
from tests.test_trust_helpers import provision_agent_wallet
from tests.conftest import requires_sqlite_row_lock_noop


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _permit_with_budget(client, *, max_credits: str = "10"):
    """A live, active permit with room in its cap."""
    provisioned = await provision_agent_wallet(client)
    permit = await get_permit_service().create_permit(
        PermitCreateRequest(
            issuer_wallet_id=provisioned["agent_wallet_id"],
            subject_wallet_id=provisioned["agent_wallet_id"],
            subject_key_id=provisioned["key_id"],
            allowed_tools=["budget-integrity-tool"],
            scopes=["tool:budget-integrity-tool:invoke"],
            max_credits=Decimal(max_credits),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).replace(
                tzinfo=None
            ),
        )
    )
    return provisioned, permit


async def _spent(permit_id: str) -> Decimal:
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit_id)
        assert model is not None
        return model.spent_credits


async def _backdate_expiry(permit_id: str) -> None:
    """Push ``expires_at`` into the past while leaving ``status`` active.

    This is the real steady state, not a contrived one: expiry is a timestamp
    comparison and the sweeper flips ``status`` lazily, so between the deadline
    and the sweep every permit in the table looks exactly like this.
    """
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            model = await session.get(PermitModel, permit_id)
            assert model is not None
            assert model.status == "active"
            model.expires_at = utc_now() - timedelta(minutes=5)
            session.add(model)


@pytest.mark.anyio
async def test_reserve_budget_refuses_a_permit_past_its_deadline(
    client, clean_database, enforce_naive_utc_datetime_columns
):
    """An expired permit must not fund anything, even with budget remaining.

    ``reserve_budget`` gated only on ``status == "active"`` and the cap, so a
    permit whose ``expires_at`` had passed kept reserving until some other
    process happened to flip its status. That is authority outliving its own
    deadline — the one bound a permit exists to carry.
    """
    _, permit = await _permit_with_budget(client)
    await _backdate_expiry(permit.permit_id)

    with pytest.raises(PermitError) as denied:
        await get_permit_service().reserve_budget(permit.permit_id, Decimal("1"))

    assert denied.value.reason == "permit_expired"
    # And no budget moved on the way to the denial.
    assert await _spent(permit.permit_id) == Decimal("0")


@pytest.mark.anyio
async def test_reserve_budget_reports_revocation_rather_than_exhaustion(
    client, clean_database, enforce_naive_utc_datetime_columns
):
    """A revoked permit is not an out-of-money permit.

    The guarded UPDATE fails for several distinct reasons and the old code
    collapsed all of them to ``permit_budget_exceeded``, which tells an
    operator to top up a permit that more money cannot revive.
    """
    _, permit = await _permit_with_budget(client)
    await get_permit_service().revoke_permit(permit.permit_id)

    with pytest.raises(PermitError) as denied:
        await get_permit_service().reserve_budget(permit.permit_id, Decimal("1"))

    assert denied.value.reason == "permit_revoked"
    assert await _spent(permit.permit_id) == Decimal("0")


@pytest.mark.anyio
async def test_reserve_budget_still_denies_a_genuine_cap_breach(
    client, clean_database, enforce_naive_utc_datetime_columns
):
    """The added predicates must not swallow the reason they sit in front of."""
    _, permit = await _permit_with_budget(client, max_credits="5")

    await get_permit_service().reserve_budget(permit.permit_id, Decimal("4"))
    with pytest.raises(PermitError) as denied:
        await get_permit_service().reserve_budget(permit.permit_id, Decimal("2"))

    assert denied.value.reason == "permit_budget_exceeded"
    assert await _spent(permit.permit_id) == Decimal("4")


@requires_sqlite_row_lock_noop
@pytest.mark.anyio
async def test_reconcile_budgets_does_not_erase_a_concurrent_reservation(
    client, clean_database, enforce_naive_utc_datetime_columns, monkeypatch
):
    """The repair is an absolute overwrite and must not clobber a live write.

    ``reconcile_budgets`` recomputes ``spent_credits`` from receipts and writes
    the total back. Between the receipt scan and that write, a reservation can
    land. The old code overwrote it unconditionally — the reservation vanished,
    the permit under-counted its own spend, and the cap it was meant to enforce
    could then be exceeded by exactly the erased amount.

    The interleave is forced deterministically rather than raced: the service
    calls ``utc_now()`` once before the scan and again while building the
    repair statement, so the second call is a seam that sits after the value
    has been observed and before the UPDATE executes.
    """
    _, permit = await _permit_with_budget(client, max_credits="100")

    # Make the permit look orphaned: spend on the row, no receipts to back it,
    # and idle long enough for the reconciler to pick it up.
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            model = await session.get(PermitModel, permit.permit_id)
            assert model is not None
            model.spent_credits = Decimal("5")
            model.status = "revoked"
            model.updated_at = utc_now() - timedelta(hours=1)
            session.add(model)

    import app.services.permits as permits_module

    real_factory = get_session_factory()

    async def _concurrent_reservation() -> None:
        """Another caller reserves while the reconcile pass is mid-flight."""
        async with real_factory() as s:
            async with s.begin():
                row = await s.get(PermitModel, permit.permit_id)
                assert row is not None
                row.spent_credits = Decimal("9")
                s.add(row)

    fired = {"done": False, "executes": 0}

    class _InterleavingSession:
        """Delegates to the real session, firing the hook once mid-pass."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def execute(self, *args, **kwargs):
            fired["executes"] += 1
            # Fire *after* the first statement — the stale-permit SELECT that
            # loads the row and fixes the value this pass will reason about.
            # Firing before it would land the reservation ahead of the
            # observation, which is not the race and proves nothing.
            if fired["executes"] == 2 and not fired["done"]:
                fired["done"] = True
                await _concurrent_reservation()
            return await self._inner.execute(*args, **kwargs)

    class _InterleavingFactory:
        def __init__(self, cm):
            self._cm = cm

        async def __aenter__(self):
            return _InterleavingSession(await self._cm.__aenter__())

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

    monkeypatch.setattr(
        permits_module,
        "get_session_factory",
        lambda: (lambda: _InterleavingFactory(real_factory())),
    )

    # Observe the service's own logger object rather than stdlib capture:
    # tests/test_migrations.py runs Alembic in-process, and migrations/env.py
    # calls logging.config.fileConfig, which disables every logger created
    # before it -- app.services.permits included. That is a test-ordering
    # artifact (production runs migrations in a separate process, see
    # scripts/docker_entrypoint.sh), but it makes handler- or caplog-based
    # capture depend on which test ran first. Recording the call itself does
    # not.
    calls: list[tuple[str, dict[str, Any]]] = []

    class _RecordingLogger:
        def __getattr__(self, name):
            return getattr(permits_module.logger, name)

        def info(self, event, *args, **kwargs):
            calls.append((event, kwargs.get("extra") or {}))

    monkeypatch.setattr(permits_module, "logger", _RecordingLogger())

    corrected = await get_permit_service().reconcile_budgets(idle_seconds=1)

    # The concurrent value survives; the stale repair is skipped, not applied.
    assert fired["done"], "the interleave never ran — the test proved nothing"
    assert await _spent(permit.permit_id) == Decimal("9")
    assert corrected == 0

    # A skip is correct, but it must not be silent: the return value counts
    # only writes, so a pass that skipped everything and a pass that found
    # nothing to do are the same number. The log is what tells them apart.
    skips = [
        extra for event, extra in calls if event == "permit_budget_reconcile_skipped"
    ]
    assert skips, "the skipped repair was not reported anywhere"
    assert skips[0].get("skipped") == 1
    assert skips[0].get("corrected") == 0
