"""Adversarial checks for audit hash-chain tamper and sequence gaps.

These tests mutate stored rows the way a database writer can: delete a
link, renumber seq, drop the head, or split the wallet-less chain. They
do not call the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import AuditChainHeadModel, ControlPlaneAuditEventModel
from app.services.audit_chain import AuditChainHeadMissingError, verify_audit_chain
from app.services.audit_log import record_audit_event


@pytest.mark.anyio
async def test_middle_deletion_breaks_the_hash_link(clean_database):
    """Removing a middle link must not leave a valid chain.

    The survivor's previous hash still names the deleted link, so the
    walk cannot close. This was already rejected before the sequence
    check; the test pins that reason.
    """
    wallet_id = "wlt-middle-delete"
    for n in range(3):
        await record_audit_event(
            event="trust.middle", wallet_id=wallet_id, metadata={"n": n}
        )

    factory = get_session_factory()
    async with factory() as session:
        middle = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel)
                    .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
                    .order_by(ControlPlaneAuditEventModel.seq)
                )
            )
            .scalars()
            .all()
        )[1]
        await session.delete(middle)
        await session.commit()

    result = await verify_audit_chain(wallet_id=wallet_id)
    assert result.valid is False
    assert result.reason == "audit_previous_hash_mismatch"


@pytest.mark.anyio
async def test_renumbered_sequence_gap_is_rejected(clean_database):
    """A hole in seq is a break even when the hash walk still lines up.

    seq is not covered by the signature. Moving the tail's seq up to a
    new head seq keeps the hash order and the head hash, so a check that
    only compares the tail to the head reports the chain as valid.
    """
    wallet_id = "wlt-seq-gap"
    for n in range(3):
        await record_audit_event(
            event="trust.gap", wallet_id=wallet_id, metadata={"n": n}
        )

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
        rows[-1].seq = 4
        head = await session.get(AuditChainHeadModel, wallet_id)
        assert head is not None
        head.last_seq = 4
        session.add(rows[-1])
        session.add(head)
        await session.commit()

    result = await verify_audit_chain(wallet_id=wallet_id)
    assert result.valid is False
    assert result.reason == "audit_sequence_gap"
    assert result.broken_event_id is not None

    windowed = await verify_audit_chain(
        wallet_id=wallet_id,
        created_after=datetime(2000, 1, 1),
    )
    assert windowed.valid is False
    assert windowed.reason == "audit_sequence_gap"


@pytest.mark.anyio
async def test_duplicate_sequence_is_rejected(clean_database):
    """Two rows with one seq must not verify just because the tail matches.

    Collapsing the tail onto the previous seq and pointing the head at
    that same seq keeps hash order (created_at breaks the tie) and
    satisfies the tail check.
    """
    wallet_id = "wlt-dup-seq"
    for n in range(3):
        await record_audit_event(
            event="trust.dup", wallet_id=wallet_id, metadata={"n": n}
        )

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
        rows[-1].seq = rows[-2].seq
        head = await session.get(AuditChainHeadModel, wallet_id)
        assert head is not None
        head.last_seq = rows[-1].seq
        head.last_chain_hash = rows[-1].chain_hash
        session.add(rows[-1])
        session.add(head)
        await session.commit()

    result = await verify_audit_chain(wallet_id=wallet_id)
    assert result.valid is False
    assert result.reason == "audit_sequence_gap"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "window",
    [
        {"created_after": datetime(2000, 1, 1)},
        {"created_before": datetime(2099, 1, 1)},
        {
            "created_after": datetime(2000, 1, 1),
            "created_before": datetime(2099, 1, 1),
        },
    ],
)
async def test_time_window_does_not_hide_a_deleted_tail(clean_database, window):
    """A wide time window must not skip the head anchor.

    Deleting the tail leaves a hash-valid prefix. Verification used to
    load the head only when both time filters were absent, so any window
    that still covered the survivors returned valid.
    """
    wallet_id = "wlt-window-tail"
    for n in range(3):
        await record_audit_event(
            event="trust.windowtail", wallet_id=wallet_id, metadata={"n": n}
        )

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

    plain = await verify_audit_chain(wallet_id=wallet_id)
    assert plain.valid is False
    assert plain.reason == "audit_chain_truncated"

    windowed = await verify_audit_chain(wallet_id=wallet_id, **window)
    assert windowed.valid is False
    assert windowed.reason == "audit_chain_truncated"


@pytest.mark.anyio
async def test_window_that_excludes_every_event_keeps_a_valid_chain(clean_database):
    """An empty window is not a truncated chain when the full chain is intact."""
    wallet_id = "wlt-window-empty"
    await record_audit_event(event="trust.keep", wallet_id=wallet_id)
    result = await verify_audit_chain(
        wallet_id=wallet_id,
        created_before=datetime(2000, 1, 1),
    )
    assert result.valid is True
    assert result.checked_events == 0


@pytest.mark.anyio
async def test_missing_head_on_a_modern_chain_is_rejected(clean_database):
    """Dropping the head row must not make the remaining events look complete.

    The head is the anchor for tail deletion. With the row gone, the old
    verifier skipped that check and returned valid.
    """
    wallet_id = "wlt-head-gone"
    for n in range(2):
        await record_audit_event(
            event="trust.head", wallet_id=wallet_id, metadata={"n": n}
        )

    factory = get_session_factory()
    async with factory() as session:
        head = await session.get(AuditChainHeadModel, wallet_id)
        assert head is not None
        await session.delete(head)
        await session.commit()

    result = await verify_audit_chain(wallet_id=wallet_id)
    assert result.valid is False
    assert result.reason == "audit_chain_head_missing"


@pytest.mark.anyio
async def test_append_refuses_to_start_a_new_genesis_when_the_head_is_gone(
    clean_database,
):
    """The next append must not sign a fresh genesis over a live chain.

    Without a head the append treated the wallet as empty, stored seq 1
    with an empty previous hash, and left the older links unverifiable.
    """
    wallet_id = "wlt-head-append"
    for n in range(2):
        await record_audit_event(
            event="trust.headappend", wallet_id=wallet_id, metadata={"n": n}
        )

    factory = get_session_factory()
    async with factory() as session:
        head = await session.get(AuditChainHeadModel, wallet_id)
        assert head is not None
        await session.delete(head)
        await session.commit()

    with pytest.raises(AuditChainHeadMissingError, match="audit_chain_head_missing"):
        await record_audit_event(
            event="trust.headappend", wallet_id=wallet_id, metadata={"n": 2}
        )

    async with factory() as session:
        count = (
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
    assert len(count) == 2
    result = await verify_audit_chain(wallet_id=wallet_id)
    assert result.valid is False
    assert result.reason == "audit_chain_head_missing"


@pytest.mark.anyio
async def test_blank_wallet_id_shares_the_wallet_less_chain(clean_database):
    """NULL and "" are one chain, because they already share one head key.

    Verification used to walk them separately. Each half then failed: the
    NULL side looked truncated, and the blank side did not link to genesis.
    """
    await record_audit_event(event="system.a", wallet_id=None, metadata={"n": 1})
    await record_audit_event(event="system.b", wallet_id="", metadata={"n": 2})

    global_result = await verify_audit_chain(wallet_id=None)
    assert global_result.valid is True, global_result
    assert global_result.checked_events == 2

    blank = await verify_audit_chain(wallet_id="")
    assert blank.valid is True, blank
    assert blank.checked_events == 2


@pytest.mark.anyio
async def test_legacy_seq_zero_chain_without_a_head_still_verifies(clean_database):
    """Rows from before seq existed are all seq 0 and have no head.

    Those rows must keep verifying. A modern chain (seq starting at 1)
    with the head removed must not.
    """
    wallet_id = "wlt-legacy-seq0"
    base = utc_now()
    for n in range(2):
        await record_audit_event(
            event="trust.legacy",
            wallet_id=wallet_id,
            created_at=base + timedelta(seconds=n),
            metadata={"n": n},
        )

    factory = get_session_factory()
    async with factory() as session:
        rows = (
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
        for row in rows:
            row.seq = 0
            session.add(row)
        head = await session.get(AuditChainHeadModel, wallet_id)
        assert head is not None
        await session.delete(head)
        await session.commit()

    result = await verify_audit_chain(wallet_id=wallet_id)
    assert result.valid is True, result
    assert result.checked_events == 2
