from __future__ import annotations

import asyncio

import pytest

from app.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyReplay,
    get_idempotency_service,
)
from app.services.agent_money import get_agent_money


@pytest.mark.anyio
async def test_idempotency_replays_same_payload_and_rejects_different_payload(
    clean_database,
):
    service = get_idempotency_service()
    wallet = await get_agent_money().create_sponsor_wallet(
        sponsor_name="Idempotency Sponsor",
        email="idem@example.com",
    )
    first = await service.begin(
        wallet_id=wallet.wallet_id,
        endpoint="/v1/test",
        idempotency_key="same-key",
        request_payload={"amount": 1},
    )
    assert first is None
    await service.complete(
        wallet_id=wallet.wallet_id,
        endpoint="/v1/test",
        idempotency_key="same-key",
        response_reference="receipt-1",
        response_json={"receipt_id": "receipt-1"},
    )

    replay = await service.begin(
        wallet_id=wallet.wallet_id,
        endpoint="/v1/test",
        idempotency_key="same-key",
        request_payload={"amount": 1},
    )
    assert replay is not None
    assert replay.response_reference == "receipt-1"
    assert replay.response_json == {"receipt_id": "receipt-1"}

    with pytest.raises(IdempotencyConflictError):
        await service.begin(
            wallet_id=wallet.wallet_id,
            endpoint="/v1/test",
            idempotency_key="same-key",
            request_payload={"amount": 2},
        )


@pytest.mark.anyio
async def test_idempotency_fails_closed_for_in_progress_record(clean_database):
    service = get_idempotency_service()
    wallet = await get_agent_money().create_sponsor_wallet(
        sponsor_name="In Progress Idempotency Sponsor",
        email="idem-in-progress@example.com",
    )
    first = await service.begin(
        wallet_id=wallet.wallet_id,
        endpoint="/v1/test",
        idempotency_key="in-progress-key",
        request_payload={"amount": 1},
    )
    assert first is None

    with pytest.raises(IdempotencyInProgressError) as exc_info:
        await service.begin(
            wallet_id=wallet.wallet_id,
            endpoint="/v1/test",
            idempotency_key="in-progress-key",
            request_payload={"amount": 1},
        )
    assert str(exc_info.value) == "idempotency_in_progress"


@pytest.mark.anyio
async def test_concurrent_begin_with_same_key_never_raises_unhandled_error(
    clean_database,
):
    """Two requests racing on the same idempotency key must not surface a raw
    IntegrityError (500) to either caller — one starts, the other observes
    either the in-progress state or a completed replay."""
    service = get_idempotency_service()
    wallet = await get_agent_money().create_sponsor_wallet(
        sponsor_name="Concurrent Idempotency Sponsor",
        email="idem-concurrent@example.com",
    )

    results = await asyncio.gather(
        *[
            service.begin(
                wallet_id=wallet.wallet_id,
                endpoint="/v1/test",
                idempotency_key="race-key",
                request_payload={"amount": 1},
            )
            for _ in range(5)
        ],
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, Exception):
            assert isinstance(
                result, (IdempotencyInProgressError, IdempotencyConflictError)
            )
        else:
            assert result is None or isinstance(result, IdempotencyReplay)

    started = [r for r in results if r is None]
    assert len(started) == 1


@pytest.mark.anyio
async def test_begin_translates_sqlite_lock_error_to_in_progress(
    clean_database, monkeypatch
):
    """SQLite exhausting busy_timeout under write contention fails the INSERT
    with OperationalError("database is locked") instead of a clean unique-
    constraint error. For an idempotent begin that is the same situation as
    losing the race — the caller must see the in-progress/replay contract,
    never a raw 500 (observed on a slow CI runner in the concurrency test
    above)."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    service = get_idempotency_service()
    wallet = await get_agent_money().create_sponsor_wallet(
        sponsor_name="Locked Sponsor",
        email="idem-locked@example.com",
    )

    real_commit = AsyncSession.commit

    async def locked_commit(self):
        raise OperationalError(
            "INSERT INTO idempotency_records ...",
            {},
            Exception("database is locked"),
        )

    monkeypatch.setattr(AsyncSession, "commit", locked_commit)
    with pytest.raises(IdempotencyInProgressError):
        await service.begin(
            wallet_id=wallet.wallet_id,
            endpoint="/v1/test",
            idempotency_key="locked-key",
            request_payload={"amount": 1},
        )

    # Once the lock clears, the same key begins normally (no phantom row).
    monkeypatch.setattr(AsyncSession, "commit", real_commit)
    started = await service.begin(
        wallet_id=wallet.wallet_id,
        endpoint="/v1/test",
        idempotency_key="locked-key",
        request_payload={"amount": 1},
    )
    assert started is None

    # Unrelated operational errors still surface.
    async def broken_commit(self):
        raise OperationalError("INSERT ...", {}, Exception("disk I/O error"))

    monkeypatch.setattr(AsyncSession, "commit", broken_commit)
    with pytest.raises(OperationalError):
        await service.begin(
            wallet_id=wallet.wallet_id,
            endpoint="/v1/test",
            idempotency_key="broken-key",
            request_payload={"amount": 1},
        )


@pytest.mark.anyio
async def test_abandon_pinned_to_a_record_id_ignores_a_row_it_does_not_own(
    clean_database,
):
    """A caller may only release the row it was actually granted.

    The coordinates (wallet, endpoint, key) name whichever row holds them at
    the moment of the call, which is not necessarily the caller's: a request
    refused ``idempotency_in_progress`` never held one, and a row can be
    released and re-taken between a caller's begin and its unwind. Releasing on
    coordinates alone would hand the current holder's at-most-once protection
    to whoever asked next, so ``expected_record_id`` pins it.

    A mismatch is a no-op rather than an error: it means the row moved on and
    this caller has nothing left to release.
    """
    service = get_idempotency_service()
    wallet = await get_agent_money().create_sponsor_wallet(
        sponsor_name="Abandon Pin Sponsor",
        email="abandon-pin@example.com",
    )
    held = await service.begin_with_record(
        wallet_id=wallet.wallet_id,
        endpoint="/v1/test",
        idempotency_key="pinned-key",
        request_payload={"amount": 1},
        operation_kind="local",
    )

    # Someone else's id: the live row must survive.
    await service.abandon(
        wallet_id=wallet.wallet_id,
        endpoint="/v1/test",
        idempotency_key="pinned-key",
        expected_record_id="idm-someone-elses-record",
    )
    survived = await service.get_record(
        wallet_id=wallet.wallet_id,
        endpoint="/v1/test",
        idempotency_key="pinned-key",
    )
    assert survived is not None
    assert survived.record_id == held.record_id

    # The owner's own id releases it, so the key is retryable again.
    await service.abandon(
        wallet_id=wallet.wallet_id,
        endpoint="/v1/test",
        idempotency_key="pinned-key",
        expected_record_id=held.record_id,
    )
    assert (
        await service.get_record(
            wallet_id=wallet.wallet_id,
            endpoint="/v1/test",
            idempotency_key="pinned-key",
        )
        is None
    )
