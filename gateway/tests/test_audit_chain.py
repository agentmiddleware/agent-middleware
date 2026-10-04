from __future__ import annotations

import asyncio
from datetime import timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import AuditChainHeadModel, ControlPlaneAuditEventModel
from app.main import app
from app.services.audit_chain import AuditEventConflictError, verify_audit_chain
from app.services.audit_log import record_audit_event
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_concurrent_audit_appends_do_not_fork_the_chain(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]

    # Concurrent same-wallet appends must serialize on the chain head so the
    # chain stays a single, verifiable line (no shared predecessor / fork).
    n_events = 10
    await asyncio.gather(
        *(
            record_audit_event(
                event="trust.race", wallet_id=wallet_id, metadata={"n": n}
            )
            for n in range(n_events)
        )
    )

    verify_resp = await client.post(
        "/v1/audit/verify-chain",
        json={"wallet_id": wallet_id},
        headers=provisioned["agent_headers"],
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["valid"] is True
    assert verify_resp.json()["checked_events"] == n_events

    factory = get_session_factory()
    async with factory() as session:
        seqs = [
            row[0]
            for row in (
                await session.execute(
                    select(ControlPlaneAuditEventModel.seq)
                    .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
                    .order_by(ControlPlaneAuditEventModel.seq)
                )
            ).all()
        ]
        head = await session.get(AuditChainHeadModel, wallet_id)
    # Sequences are contiguous with no duplicates, and the head points at the last.
    assert seqs == list(range(1, n_events + 1))
    assert head is not None
    assert head.last_seq == n_events


@pytest.mark.anyio
async def test_deterministic_audit_append_is_get_or_create_and_conflict_safe(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    event_id = "audit-dsp-fixed-concurrency-claim"
    created_at = utc_now()
    kwargs = {
        "event": "mcp.invoke",
        "event_id": event_id,
        "created_at": created_at,
        "wallet_id": wallet_id,
        "tool": "partner.lookup",
        "endpoint": "/mcp/invoke",
        "request_id": "dsp-fixed-concurrency-claim",
        "metadata": {"dispatch_attempt_id": "dsp-fixed-concurrency-claim"},
    }

    first, second = await asyncio.gather(
        record_audit_event(**kwargs),
        record_audit_event(**kwargs),
    )

    assert first.event_id == second.event_id == event_id
    factory = get_session_factory()
    async with factory() as session:
        persisted = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel).where(
                        ControlPlaneAuditEventModel.event_id == event_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(persisted) == 1
    assert (await verify_audit_chain(wallet_id=wallet_id)).valid is True

    with pytest.raises(AuditEventConflictError, match="audit_event_id_conflict"):
        await record_audit_event(
            **{
                **kwargs,
                "metadata": {"dispatch_attempt_id": "different-attempt"},
            }
        )


@pytest.mark.anyio
async def test_audit_chain_orders_by_monotonic_sequence(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]

    # Rapid same-wallet events can share a created_at timestamp; the chain must
    # still order deterministically and verify.
    for n in range(12):
        await record_audit_event(
            event="trust.seq", wallet_id=wallet_id, metadata={"n": n}
        )

    verify_resp = await client.post(
        "/v1/audit/verify-chain",
        json={"wallet_id": wallet_id},
        headers=provisioned["agent_headers"],
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["valid"] is True
    assert verify_resp.json()["checked_events"] == 12

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ControlPlaneAuditEventModel.seq)
            .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
            .order_by(ControlPlaneAuditEventModel.seq)
        )
        seqs = [row[0] for row in result.all()]
    assert seqs == list(range(1, 13))


@pytest.mark.anyio
async def test_audit_chain_verifies_and_detects_tampering(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    await record_audit_event(event="trust.test", wallet_id=wallet_id, metadata={"n": 1})
    await record_audit_event(event="trust.test", wallet_id=wallet_id, metadata={"n": 2})

    verify_resp = await client.post(
        "/v1/audit/verify-chain",
        json={"wallet_id": wallet_id},
        headers=provisioned["agent_headers"],
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["valid"] is True
    assert verify_resp.json()["checked_events"] == 2

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ControlPlaneAuditEventModel)
            .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
            .order_by(ControlPlaneAuditEventModel.created_at)
        )
        event = result.scalars().first()
        event.metadata_json = '{"n": 999}'
        session.add(event)
        await session.commit()

    tampered_resp = await client.post(
        "/v1/audit/verify-chain",
        json={"wallet_id": wallet_id},
        headers=BOOTSTRAP_HEADERS,
    )
    assert tampered_resp.status_code == 200
    assert tampered_resp.json()["valid"] is False
    assert tampered_resp.json()["reason"] == "audit_payload_hash_mismatch"


@pytest.mark.anyio
async def test_audit_chain_normalizes_offset_time_filters(
    clean_database,
    enforce_naive_utc_datetime_columns,
):
    wallet_id = "wlt-audit-time-filter"
    event = await record_audit_event(event="trust.time", wallet_id=wallet_id)
    event_utc = event.created_at.replace(tzinfo=timezone.utc)

    result = await verify_audit_chain(
        wallet_id=wallet_id,
        created_after=(event_utc - timedelta(minutes=1)).astimezone(
            timezone(timedelta(hours=5, minutes=30))
        ),
        created_before=(event_utc + timedelta(minutes=1)).astimezone(
            timezone(timedelta(hours=-7))
        ),
    )

    assert result.valid is True
    assert result.checked_events == 1


@pytest.mark.anyio
async def test_global_verify_covers_wallet_less_null_chain(clean_database):
    """Global verification must walk the wallet-less (wallet_id IS NULL) chain
    too. Tampering a system/denied-action record used to return valid=True
    because the NULL chain was dropped from the distinct list."""
    await record_audit_event(event="system.denied", wallet_id=None, metadata={"n": 1})
    await record_audit_event(event="system.denied", wallet_id=None, metadata={"n": 2})

    ok = await verify_audit_chain(wallet_id=None)
    assert ok.valid is True
    assert ok.checked_events == 2

    # Tamper the first wallet-less event.
    factory = get_session_factory()
    async with factory() as session:
        event = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel)
                    .where(ControlPlaneAuditEventModel.wallet_id.is_(None))
                    .order_by(ControlPlaneAuditEventModel.seq)
                )
            )
            .scalars()
            .first()
        )
        event.metadata_json = '{"n": 6660}'
        session.add(event)
        await session.commit()

    tampered = await verify_audit_chain(wallet_id=None)
    assert tampered.valid is False
    assert tampered.reason == "audit_payload_hash_mismatch"


@pytest.mark.anyio
async def test_verify_detects_tail_truncation(clean_database):
    """Deleting the last events of a chain must be detected via the head
    anchor -- a valid prefix must not verify as a valid whole chain."""
    wallet_id = "wlt-truncation-test"
    for n in range(3):
        await record_audit_event(
            event="trust.trunc", wallet_id=wallet_id, metadata={"n": n}
        )

    assert (await verify_audit_chain(wallet_id=wallet_id)).valid is True

    # Delete the last event (seq=3); head still says last_seq=3.
    factory = get_session_factory()
    async with factory() as session:
        last = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel)
                    .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
                    .order_by(ControlPlaneAuditEventModel.seq.desc())
                )
            )
            .scalars()
            .first()
        )
        await session.delete(last)
        await session.commit()

    result = await verify_audit_chain(wallet_id=wallet_id)
    assert result.valid is False
    assert result.reason == "audit_chain_truncated"

    # And the global path surfaces it too.
    global_result = await verify_audit_chain(wallet_id=None)
    assert global_result.valid is False
    assert global_result.reason == "audit_chain_truncated"


@pytest.mark.anyio
async def test_global_verify_detects_fully_deleted_wallet_via_head(clean_database):
    """Deleting ALL of a wallet's events must still be caught globally.

    Regression: the global verify enumerated wallets from the events table, so a
    wallet whose every event was deleted vanished from the check entirely. It is
    enumerated from the surviving chain-head rows too, so the truncation is
    caught.
    """
    wallet_id = "wlt-fulldelete-test"
    for n in range(3):
        await record_audit_event(
            event="trust.fulldel", wallet_id=wallet_id, metadata={"n": n}
        )
    assert (await verify_audit_chain(wallet_id=None)).valid is True

    factory = get_session_factory()
    async with factory() as session:
        events = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel).where(
                        ControlPlaneAuditEventModel.wallet_id == wallet_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            await session.delete(event)
        await session.commit()

    result = await verify_audit_chain(wallet_id=None)
    assert result.valid is False
    assert result.reason == "audit_chain_truncated"


@pytest.mark.anyio
async def test_exhausted_head_contention_raises_a_named_error(
    client, clean_database, monkeypatch
):
    """A chain head that stays contended must not leave as a private type.

    The append retries the optimistic head update 64 times. Before this the
    final attempt re-raised whatever it caught -- ``_HeadConflict``, which is
    private to the module, or a bare driver error -- so every caller saw an
    unclassified failure and the trailing
    ``raise RuntimeError("audit_chain_head_contention")`` was dead code,
    advertising a reason nothing could emit.
    """
    from app.services import audit_chain

    provisioned = await provision_agent_wallet(client)

    calls = {"n": 0}

    def _always_lost(*args, **kwargs):
        # Stands in for the conditional head UPDATE matching zero rows, which is
        # what a writer that keeps losing the race observes on every pass.
        calls["n"] += 1
        raise audit_chain._HeadConflict()

    monkeypatch.setattr(audit_chain, "_sign_with_previous", _always_lost)

    with pytest.raises(audit_chain.AuditChainContendedError) as excinfo:
        await record_audit_event(
            event="mcp.invoke",
            wallet_id=provisioned["agent_wallet_id"],
            tool="contended-tool",
            endpoint="/mcp/messages",
            ok=True,
            error=None,
        )

    assert str(excinfo.value) == "audit_chain_head_contention"
    assert audit_chain.AuditChainContendedError.reason == "audit_chain_head_contention"
    # The whole budget was spent before giving up, not one attempt.
    assert calls["n"] == 64, calls["n"]
    # The private type is chained, not swallowed, so a log still shows the cause.
    assert isinstance(excinfo.value.__cause__, audit_chain._HeadConflict)


@pytest.mark.anyio
async def test_a_substantive_integrity_fault_is_not_relabelled_as_contention(
    client, clean_database, monkeypatch
):
    """Renaming a real constraint violation would send readers hunting a ghost.

    Only a lost head race, or an ``OperationalError`` the shared classifier
    recognises as a write conflict, becomes ``AuditChainContendedError``. A
    persistent integrity fault that no amount of retrying could clear keeps its
    own type.
    """
    from sqlalchemy.exc import IntegrityError

    from app.services import audit_chain

    provisioned = await provision_agent_wallet(client)

    def _hard_integrity_fault(*args, **kwargs):
        raise IntegrityError(
            "INSERT INTO control_plane_audit_events ...",
            {},
            Exception("NOT NULL constraint failed: control_plane_audit_events.event"),
        )

    monkeypatch.setattr(audit_chain, "_sign_with_previous", _hard_integrity_fault)

    with pytest.raises(IntegrityError):
        await record_audit_event(
            event="mcp.invoke",
            wallet_id=provisioned["agent_wallet_id"],
            tool="broken-tool",
            endpoint="/mcp/messages",
            ok=True,
            error=None,
        )


@pytest.mark.anyio
async def test_a_fault_no_retry_can_clear_is_raised_without_spending_the_budget(
    client, clean_database, monkeypatch
):
    """A deterministic driver error must not be retried 64 times first.

    The retry budget is generous because a contended chain head reconverges,
    and the classification that separates contention from everything else used
    to run only on the final attempt. So a fault no retry could ever clear --
    ``no such table``, a bad column -- burned all 64 passes and their backoff
    before propagating unchanged, turning a deterministic error into a stall
    on a request path that holds a charged wallet open.

    The type is unchanged; only the delay is. Calling it "contended" would send
    a reader looking for a busy writer that never existed.
    """
    from sqlalchemy.exc import OperationalError

    from app.services import audit_chain

    provisioned = await provision_agent_wallet(client)

    calls = {"n": 0}

    def _hard_driver_fault(*args, **kwargs):
        calls["n"] += 1
        raise OperationalError(
            "SELECT 1", {}, Exception("no such table: control_plane_audit_events")
        )

    monkeypatch.setattr(audit_chain, "_sign_with_previous", _hard_driver_fault)

    with pytest.raises(OperationalError):
        await record_audit_event(
            event="mcp.invoke",
            wallet_id=provisioned["agent_wallet_id"],
            tool="broken-tool",
            endpoint="/mcp/messages",
            ok=True,
            error=None,
        )

    assert calls["n"] == 1, calls["n"]


@pytest.mark.anyio
async def test_a_substantive_integrity_fault_stops_once_a_race_cannot_explain_it(
    client, clean_database, monkeypatch
):
    """An IntegrityError is retried only as long as it could still be a race.

    Two races produce one legitimately: two writers inserting a wallet's first
    head row, and two inserting the same deterministic event id. Both are
    resolved by the very next pass, which observes the winner's row and either
    returns it or updates against it -- so a violation that outlives a couple
    of retries is not a race, and spending the rest of the budget on it only
    delays the real error. It keeps its own type either way.
    """
    from sqlalchemy.exc import IntegrityError

    from app.services import audit_chain

    provisioned = await provision_agent_wallet(client)

    calls = {"n": 0}

    def _hard_integrity_fault(*args, **kwargs):
        calls["n"] += 1
        raise IntegrityError("INSERT", {}, Exception("FOREIGN KEY constraint failed"))

    monkeypatch.setattr(audit_chain, "_sign_with_previous", _hard_integrity_fault)

    with pytest.raises(IntegrityError):
        await record_audit_event(
            event="mcp.invoke",
            wallet_id=provisioned["agent_wallet_id"],
            tool="violating-tool",
            endpoint="/mcp/messages",
            ok=True,
            error=None,
        )

    # Bounded by the race window, not by the contention budget.
    assert calls["n"] == 3, calls["n"]


@pytest.mark.anyio
async def test_windowed_verify_of_valid_chain_is_valid(client, clean_database):
    """A ``created_after`` window that excludes genesis must still verify.

    The first in-window event legitimately links to an event outside the
    window; seeding the expected predecessor from genesis flagged every
    non-genesis window of an untampered chain as ``audit_previous_hash_mismatch``.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    for n in range(4):
        await record_audit_event(
            event="trust.window", wallet_id=wallet_id, metadata={"n": n}
        )
        await asyncio.sleep(0.01)

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel)
                    .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
                    .order_by(ControlPlaneAuditEventModel.seq)
                )
            )
            .scalars()
            .all()
        )
    third_created = rows[2].created_at

    windowed = await verify_audit_chain(
        wallet_id=wallet_id,
        created_after=third_created - timedelta(microseconds=1),
    )
    assert windowed.valid is True, windowed
    assert windowed.checked_events == 2

    # The seeded predecessor is really checked: tampering the chain_hash of
    # the last event *outside* the window breaks the first in-window link.
    async with factory() as session:
        event = (
            await session.execute(
                select(ControlPlaneAuditEventModel).where(
                    ControlPlaneAuditEventModel.event_id == rows[1].event_id
                )
            )
        ).scalar_one()
        event.chain_hash = "0" * 64
        session.add(event)
        await session.commit()
    tampered = await verify_audit_chain(
        wallet_id=wallet_id,
        created_after=third_created - timedelta(microseconds=1),
    )
    assert tampered.valid is False
    assert tampered.reason == "audit_previous_hash_mismatch"
