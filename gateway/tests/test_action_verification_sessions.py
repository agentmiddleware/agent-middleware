"""Action authority verification must fit in its caller's connection pool."""

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import database
from app.db.models import McpDispatchAttemptModel, PermitModel, SigningKeyModel
from app.services.mcp_dispatch_attempts import get_mcp_dispatch_attempt_service
from tests.test_action_invocation import (
    action_runtime as _action_runtime,
    prepare_action,
)

action_runtime = _action_runtime
ARGS = {"amount_minor": 1, "recipient": "alice"}


@pytest.mark.anyio
@pytest.mark.parametrize("pool_size", [1, 2])
@pytest.mark.parametrize("recover", [False, True])
async def test_action_preparation_and_recovery_use_caller_connection(
    action_runtime, monkeypatch, pool_size, recover
):
    original_attempt = None
    if recover:
        validation, original_attempt = await prepare_action(action_runtime, ARGS)
        assert validation.allowed
        service = get_mcp_dispatch_attempt_service()
        original_lookup = service._get_by_idempotency_record
        injected = False

        async def fail_first_lookup(session, record_id):
            nonlocal injected
            if not injected:
                injected = True
                raise RuntimeError("synthetic preparation recovery entry")
            return await original_lookup(session, record_id)

        monkeypatch.setattr(service, "_get_by_idempotency_record", fail_first_lookup)

    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=0.15,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(database, "_session_factory", factory)
            validation, attempt = await prepare_action(action_runtime, ARGS)
            assert validation.allowed
            assert attempt is not None
            if recover:
                assert injected
                assert attempt.attempt_id == original_attempt.attempt_id
            async with factory() as session:
                permit = await session.get(PermitModel, action_runtime[2])
                assert permit.spent_credits == 2
                attempts = (
                    (await session.execute(select(McpDispatchAttemptModel)))
                    .scalars()
                    .all()
                )
                assert len(attempts) == 1
        assert action_runtime[3].dispatch_count == 0
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["tampered", "disabled"])
async def test_single_connection_action_verification_fails_closed(
    action_runtime, monkeypatch, invalid
):
    async with database.get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
        if invalid == "tampered":
            permit.max_credits += 1
        else:
            key = await session.get(SigningKeyModel, permit.key_id)
            key.status = "disabled"
        await session.commit()
    engine = create_async_engine(
        os.environ["DATABASE_URL"], pool_size=1, max_overflow=0, pool_timeout=0.15
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(database, "_session_factory", factory)
            validation, attempt = await prepare_action(action_runtime, ARGS)
            assert not validation.allowed
            assert validation.reason == "permit_signature_invalid"
            assert attempt is None
            async with factory() as session:
                permit = await session.get(PermitModel, action_runtime[2])
                assert permit.spent_credits == 0
                assert (
                    not (await session.execute(select(McpDispatchAttemptModel)))
                    .scalars()
                    .all()
                )
        assert action_runtime[3].dispatch_count == 0
    finally:
        await engine.dispose()
