"""Opt-in local PostgreSQL proofs for single-action accounting and lock order."""

import os

import pytest
import pytest_asyncio

from tests import test_action_invocation as action
from tests.conftest import clean_database as sqlite_clean_database
from tests.support.action_database_guard import (
    require_action_database_url,
    require_action_test_environment,
)

# Collection precedes conftest's autouse session database setup. An opted-in
# unsafe target must fail here, before even schema inspection can connect.
if os.environ.get("RUN_POSTGRES_CONCURRENCY_TESTS") == "1":
    require_action_database_url(os.environ.get("DATABASE_URL", ""))
    require_action_test_environment(os.environ.get("ENVIRONMENT"))

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("RUN_POSTGRES_CONCURRENCY_TESTS") != "1",
        reason="requires explicitly opted-in disposable local amw_action PostgreSQL database",
    ),
]


@pytest_asyncio.fixture(scope="module", loop_scope="session", autouse=True)
async def require_fresh_action_schema():
    from sqlalchemy import inspect
    from app.db.database import get_engine
    from tests.test_action_migrations import FIELDS
    from tests.test_mcp_postgres_multiprocess import _assert_migrated_empty_database

    require_action_database_url(os.environ.get("DATABASE_URL", ""))
    require_action_test_environment(os.environ.get("ENVIRONMENT"))
    async with get_engine().connect() as connection:
        await _assert_migrated_empty_database(connection)
        for table in ("permits", "receipts"):
            columns = await connection.run_sync(lambda c: inspect(c).get_columns(table))
            assert set(FIELDS) <= {c["name"] for c in columns}, "stale 042 schema"


@pytest_asyncio.fixture(loop_scope="session")
async def clean_database():
    async for value in sqlite_clean_database.__wrapped__():
        yield value


@pytest_asyncio.fixture(loop_scope="session")
async def action_runtime(monkeypatch, clean_database, action_permit_route):
    async for value in action.action_runtime.__wrapped__(
        monkeypatch, clean_database, action_permit_route
    ):
        yield value


@pytest.mark.parametrize("already_prepared", [False, True])
async def test_atomic_action_prepare_rechecks_digest(action_runtime, already_prepared):
    await action.test_atomic_action_prepare_rechecks_digest(
        action_runtime, already_prepared
    )


async def test_twenty_fresh_action_keys_share_accounting(action_runtime):
    await action.test_twenty_fresh_action_keys_share_accounting(action_runtime)


async def test_new_trusted_permit_intentionally_repeats(action_runtime):
    await action.test_new_trusted_permit_intentionally_repeats(action_runtime)


async def wait_for_lock_waiter(blocker_pid):
    """Observe a real PostgreSQL waiter, rather than infer locking from timing."""
    import asyncio
    from sqlalchemy import text
    from app.db.database import get_session_factory

    async def poll():
        while True:
            async with get_session_factory()() as session:
                waiting = await session.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :pid = ANY(pg_blocking_pids(pid)))"
                    ),
                    {"pid": blocker_pid},
                )
            if waiting:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=5)


@pytest.mark.parametrize("revoke_first", [True, False])
async def test_action_revocation_serial_orders(
    action_runtime, monkeypatch, revoke_first
):
    import asyncio
    import json
    from sqlalchemy import select, text
    from app.db.database import get_session_factory
    from app.db.models import PermitModel, McpDispatchAttemptModel, LedgerEntryModel
    from app.services.permits import get_permit_service

    permits = get_permit_service()
    args = {"amount_minor": 1, "recipient": "alice"}
    if revoke_first:
        async with get_session_factory()() as session:
            async with session.begin():
                permit = await session.get(
                    PermitModel, action_runtime[2], with_for_update=True
                )
                blocker = await session.scalar(text("select pg_backend_pid()"))
                permit.status = "revoked"
                await session.flush()
                pending = asyncio.create_task(
                    action.prepare_action(action_runtime, args)
                )
                await wait_for_lock_waiter(blocker)
        validation, attempt = await asyncio.wait_for(pending, timeout=5)
        assert not validation.allowed
        assert validation.reason == "permit_revoked"
        assert attempt is None
    else:
        original_validate = permits._validate_model_for_action
        locked = asyncio.Event()
        release = asyncio.Event()
        blocker = None

        async def hold_validation(**kwargs):
            nonlocal blocker
            blocker = await kwargs["session"].scalar(text("select pg_backend_pid()"))
            locked.set()
            await release.wait()
            return await original_validate(**kwargs)

        monkeypatch.setattr(permits, "_validate_model_for_action", hold_validation)
        pending = asyncio.create_task(action.prepare_action(action_runtime, args))
        await asyncio.wait_for(locked.wait(), timeout=5)
        revocation = asyncio.create_task(permits.revoke_permit(action_runtime[2]))
        try:
            await wait_for_lock_waiter(blocker)
        finally:
            release.set()
        validation, attempt = await asyncio.wait_for(pending, timeout=5)
        await asyncio.wait_for(revocation, timeout=5)
        assert validation.allowed
        assert attempt is not None
    owners = await action.action_rows()
    assert len(owners) == 1
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
        assert permit.status == "revoked"
        assert permit.spent_credits == (0 if revoke_first else 2)
        assert json.loads(permit.tool_call_counts_json or "{}") == (
            {} if revoke_first else {"partner.pay": 1}
        )
        attempts = list(
            (await session.execute(select(McpDispatchAttemptModel))).scalars()
        )
        assert len(attempts) == (0 if revoke_first else 1)
        if attempts:
            assert attempts[0].idempotency_record_id == owners[0].record_id
            assert attempts[0].call_slot_reserved is True
            assert attempts[0].state == "prepared"
        entries = list(
            (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.operation_key == owners[0].record_id
                    )
                )
            ).scalars()
        )
        assert entries == []  # Preparation reserves; ledger charge follows separately.
    assert action_runtime[3].dispatch_count == 0


async def test_exhausted_action_replay_returns_identical_receipt(action_runtime):
    await action.test_exhausted_action_replay_returns_identical_receipt(action_runtime)


@pytest.mark.parametrize("tamper", ["namespace", "key", "hash", "origin", "tool"])
async def test_atomic_action_owner_and_destination_binding(action_runtime, tamper):
    await action.test_atomic_action_owner_and_destination_binding(
        action_runtime, tamper
    )
