"""Completion retries persist a replay without repeating a governed action."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.core.resilience import WRITE_CONFLICT_MAX_ATTEMPTS
from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, ReceiptModel
from app.main import app
from app.services.agent_money import get_agent_money
from app.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyService,
    get_idempotency_service,
)
from tests.conftest import requires_sqlite_row_lock_noop
from tests.test_ledger_write_contention import (
    _call_body,
    _debit_count,
    _register_local_tool,
    _spent_credits,
)
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value


class CompletionSessions:
    """Observe real transaction lifetimes and interleave after the row read."""

    def __init__(self, factory, *, after_read=None, error=None, pin_snapshot=False):
        self.factory = factory
        self.after_read = after_read
        self.error = error
        self.pin_snapshot = pin_snapshot
        self.sessions = []
        self.errors = []
        self.commits = 0

    def __call__(self):
        probe = self

        class Session:
            async def __aenter__(self):
                self.inner = await probe.factory().__aenter__()
                probe.sessions.append(self.inner)
                if probe.pin_snapshot:
                    # Legacy sqlite3 SELECTs do not always BEGIN. Explicitly pin
                    # the read so the competing commit forces BUSY_SNAPSHOT.
                    await self.inner.execute(text("BEGIN"))
                return self

            def __getattr__(self, name):
                return getattr(self.inner, name)

            async def execute(self, *args, **kwargs):
                result = await self.inner.execute(*args, **kwargs)
                if probe.after_read is not None and len(probe.sessions) == 1:
                    await probe.after_read()
                return result

            async def commit(self):
                probe.commits += 1
                if probe.error is not None:
                    raise probe.error
                await self.inner.commit()

            async def __aexit__(self, exc_type, exc, traceback):
                if exc is not None:
                    probe.errors.append(exc)
                return await self.inner.__aexit__(exc_type, exc, traceback)

        return Session()


async def _begin_record():
    wallet = await get_agent_money().create_sponsor_wallet(
        sponsor_name="Completion contention", email="completion@example.com"
    )
    coordinates = {
        "wallet_id": wallet.wallet_id,
        "endpoint": "/mcp/invoke",
        "idempotency_key": "completion-key",
    }
    await get_idempotency_service().begin(
        **coordinates, request_payload={"message": "hello"}
    )
    return coordinates


def _patch_factory(monkeypatch, probe):
    monkeypatch.setattr("app.services.idempotency.get_session_factory", lambda: probe)


@pytest.mark.anyio
@requires_sqlite_row_lock_noop
async def test_real_wal_conflict_restarts_and_preserves_replay_identity(
    clean_database, monkeypatch
):
    coordinates = await _begin_record()
    service = get_idempotency_service()
    factory = get_session_factory()
    # The same key in another wallet and endpoint must remain untouched.
    other = await get_agent_money().create_sponsor_wallet(
        sponsor_name="Other wallet", email="other-completion@example.com"
    )
    other_coordinates = {**coordinates, "wallet_id": other.wallet_id}
    endpoint_coordinates = {**coordinates, "endpoint": "/v1/test"}
    for scoped in (other_coordinates, endpoint_coordinates):
        await service.begin(**scoped, request_payload={"message": "hello"})

    async def competing_commit():
        async with factory() as session:
            await session.execute(
                text("UPDATE wallets SET owner_name = :name WHERE wallet_id = :id"),
                {"name": "Competing writer", "id": other.wallet_id},
            )
            await session.commit()

    probe = CompletionSessions(factory, after_read=competing_commit, pin_snapshot=True)
    response = {"error": "permit_budget_exceeded", "receipt": {"receipt_id": "r-1"}}
    with monkeypatch.context() as patch:
        _patch_factory(patch, probe)
        await service.complete(
            **coordinates,
            response_reference="r-1",
            response_json=response,
            status_code=403,
        )

    assert len(probe.sessions) == 2
    assert probe.sessions[0] is not probe.sessions[1]
    assert len(probe.errors) == 1
    assert isinstance(probe.errors[0], OperationalError)
    assert probe.errors[0].orig.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
    replay = await service.begin(**coordinates, request_payload={"message": "hello"})
    assert replay is not None
    assert (replay.response_reference, replay.response_json, replay.status_code) == (
        "r-1",
        response,
        403,
    )
    with pytest.raises(IdempotencyConflictError):
        await service.begin(**coordinates, request_payload={"message": "changed"})
    for scoped in (other_coordinates, endpoint_coordinates):
        record = await service.get_record(**scoped)
        assert record is not None and record.response_json is None


@pytest.mark.anyio
@pytest.mark.parametrize("prior_completed", [False, True])
@pytest.mark.parametrize("message", ["database is locked", "disk I/O error"])
async def test_completion_faults_are_bounded_and_preserve_committed_state(
    clean_database, monkeypatch, message, prior_completed
):
    coordinates = await _begin_record()
    service = get_idempotency_service()
    if prior_completed:
        await service.complete(
            **coordinates,
            response_reference="r-original",
            response_json={"error": "original-denial"},
            status_code=403,
        )
    before = await service.get_record(**coordinates)
    error = OperationalError("UPDATE idempotency_records", {}, Exception(message))
    probe = CompletionSessions(get_session_factory(), error=error)
    with monkeypatch.context() as patch:
        _patch_factory(patch, probe)
        with pytest.raises(OperationalError) as raised:
            await service.complete(
                **coordinates,
                response_reference="r-new",
                response_json={"error": "new-denial"},
                status_code=402,
            )
    assert raised.value is error
    attempts = WRITE_CONFLICT_MAX_ATTEMPTS if message == "database is locked" else 1
    assert probe.commits == attempts
    assert len({id(session) for session in probe.sessions}) == attempts
    after = await service.get_record(**coordinates)
    assert before is not None and after is not None
    assert after.model_dump() == before.model_dump()
    if not prior_completed:
        with pytest.raises(IdempotencyInProgressError):
            await service.begin(**coordinates, request_payload={"message": "hello"})


@pytest.mark.anyio
@pytest.mark.parametrize("replacement", [{"reconciled": True}, None, {}])
async def test_existing_completion_refresh_semantics_remain_unchanged(
    clean_database, replacement
):
    coordinates = await _begin_record()
    service = get_idempotency_service()
    await service.complete(
        **coordinates,
        response_reference="r-original",
        response_json={"error": "original-denial"},
        status_code=403,
    )
    await service.complete(
        **coordinates,
        response_reference=None,
        response_json=replacement,
        status_code=200,
    )
    record = await service.get_record(**coordinates)
    assert record is not None
    assert record.response_reference is None
    assert record.status_code == 200
    if replacement:
        replay = await service.begin(
            **coordinates, request_payload={"message": "hello"}
        )
        assert replay is not None and replay.response_json == replacement
    else:
        assert record.response_json is None


@pytest.mark.anyio
async def test_missing_completion_remains_a_noop(clean_database, monkeypatch):
    coordinates = await _begin_record()
    probe = CompletionSessions(get_session_factory())
    with monkeypatch.context() as patch:
        _patch_factory(patch, probe)
        await get_idempotency_service().complete(
            **{**coordinates, "idempotency_key": "missing"},
            response_reference=None,
            response_json={"error": "denied"},
            status_code=403,
        )
    assert len(probe.sessions) == 1
    assert probe.commits == 0
    assert (
        await get_idempotency_service().get_record(
            **{**coordinates, "idempotency_key": "missing"}
        )
        is None
    )


@pytest.mark.anyio
@requires_sqlite_row_lock_noop
@pytest.mark.parametrize("denied", [False, True])
async def test_governed_completion_conflict_replays_one_receipt_and_one_action(
    client, clean_database, monkeypatch, denied
):
    from app.services.service_registry import get_service_registry

    ctx = await provision_agent_wallet(client)
    tool_name = "completion-conflict-echo"
    calls = []

    def echo(message: str):
        calls.append(message)
        return {"message": message}

    _register_local_tool(tool_name, echo)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=ctx["agent_wallet_id"],
            key_id=ctx["key_id"],
            tool_name=tool_name,
            max_credits=1 if denied else 10,
            idem_key="completion-conflict-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="completion-http",
        )
        factory = get_session_factory()

        async def competing_commit():
            async with factory() as session:
                await session.execute(
                    text("UPDATE wallets SET owner_name = :name WHERE wallet_id = :id"),
                    {"name": "Competing writer", "id": ctx["agent_wallet_id"]},
                )
                await session.commit()

        probe = CompletionSessions(
            factory, after_read=competing_commit, pin_snapshot=True
        )
        real_complete = IdempotencyService.complete

        async def complete_with_conflict(self, **kwargs: Any):
            with monkeypatch.context() as patch:
                _patch_factory(patch, probe)
                return await real_complete(self, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(IdempotencyService, "complete", complete_with_conflict)
            first = await client.post(
                "/mcp/messages", json=body, headers=ctx["agent_headers"]
            )
        replay = await client.post(
            "/mcp/messages", json=body, headers=ctx["agent_headers"]
        )
        assert first.status_code == replay.status_code == 200
        response = first.json()
        if denied:
            assert response["error"]["message"] == "permit_budget_exceeded", response
            receipt = response["error"]["data"]["receipt"]
            assert receipt["outcome"] == "denied"
            assert replay.json()["error"]["message"] == response["error"]["message"]
            assert replay.json()["error"]["code"] == response["error"]["code"]
            assert replay.json()["error"]["data"]["receipt"] == receipt
        else:
            assert "error" not in response, response
            assert replay.json() == response
            receipt = response["result"]["receipt"]
            assert receipt["outcome"] == "success"
        assert len(probe.sessions) == 2
        assert probe.errors[0].orig.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
        expected_actions = 0 if denied else 1
        assert len(calls) == expected_actions
        assert (
            await _debit_count(
                client, ctx["agent_wallet_id"], ctx["agent_headers"], tool_name
            )
            == expected_actions
        )
        assert await _spent_credits(permit["permit_id"]) == expected_actions * 2
        async with factory() as session:
            rows = (await session.execute(select(ReceiptModel))).scalars().all()
            assert len(rows) == 1
            assert rows[0].receipt_id == receipt["receipt_id"]
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.idempotency_key == "completion-http"
                    )
                )
            ).scalar_one()
            assert record.response_reference == receipt["receipt_id"]
            assert record.status_code == (403 if denied else 200)
    finally:
        get_service_registry().unregister_local(tool_name)
