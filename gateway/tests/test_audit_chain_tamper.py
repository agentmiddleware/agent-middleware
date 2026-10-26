"""Adversarial checks for audit hash-chain tamper and sequence gaps.

These tests mutate stored rows the way a database writer can: delete a
link, renumber seq, drop the head, or split the wallet-less chain. They
do not call the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import AuditChainHeadModel, ControlPlaneAuditEventModel
from app.services.audit_chain import (
    AuditChainHeadMissingError,
    _sign_with_previous,
    verify_audit_chain,
)
from app.services.audit_log import record_audit_event
from app.services.signing_keys import get_signing_key_service, sha256_hex
from tests.test_audit_chain import _payload_without_seq


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
@pytest.mark.parametrize("deleted_seqs", [(4,), (3,), (3, 4), (2,)])
async def test_windowed_verify_finds_deleted_legacy_and_modern_links(
    clean_database, deleted_seqs
):
    wallet_id = f"wlt-{uuid4()}"
    base = utc_now()
    for seq in range(1, 7):
        await record_audit_event(
            event="trust.window-link",
            wallet_id=wallet_id,
            created_at=base + timedelta(seconds=seq),
            metadata={"seq": seq},
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
        assert [row.seq for row in rows] == list(range(1, 7))
        previous_hash = None
        for row in rows[:3]:
            row.previous_hash = previous_hash
            payload = _payload_without_seq(row)
            row.payload_hash = sha256_hex(payload)
            payload["payload_hash"] = row.payload_hash
            assert row.signature_key_id is not None
            row.signature, row.signature_key_id, _ = (
                get_signing_key_service().sign_payload_with_key_id(
                    payload, row.signature_key_id
                )
            )
            row.chain_hash = sha256_hex(
                {
                    "previous_hash": previous_hash,
                    "payload_hash": row.payload_hash,
                    "signature": row.signature,
                }
            )
            previous_hash = row.chain_hash
        for row in rows[3:]:
            assert row.signature_key_id is not None
            _sign_with_previous(row, previous_hash, signing_key_id=row.signature_key_id)
            previous_hash = row.chain_hash
        head = await session.get(AuditChainHeadModel, wallet_id)
        assert head is not None
        head.last_chain_hash = previous_hash
        await session.commit()

    window = {"created_after": rows[-1].created_at}
    intact = await verify_audit_chain(wallet_id=wallet_id, **window)
    assert intact.valid is True, intact
    assert intact.checked_events == 1
    assert intact.first_event_id == intact.last_event_id == rows[-1].event_id

    async with factory() as session:
        for row in rows:
            if row.seq in deleted_seqs:
                stored = await session.get(ControlPlaneAuditEventModel, row.event_id)
                assert stored is not None
                await session.delete(stored)
        await session.commit()

    result = await verify_audit_chain(wallet_id=wallet_id, **window)
    assert result.valid is False
    assert result.reason == "audit_previous_hash_mismatch"
    assert result.broken_event_id == rows[max(deleted_seqs)].event_id

    if deleted_seqs == (3,):
        # A writer can repair linkage and re-sign the modern suffix. The
        # first seq-signed row must still reveal the gap after legacy rows.
        async with factory() as session:
            survivors = (
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
            previous_hash = survivors[1].chain_hash
            for row in survivors[2:]:
                assert row.signature_key_id is not None
                _sign_with_previous(
                    row, previous_hash, signing_key_id=row.signature_key_id
                )
                previous_hash = row.chain_hash
            head = await session.get(AuditChainHeadModel, wallet_id)
            assert head is not None
            head.last_chain_hash = previous_hash
            await session.commit()

        relinked = await verify_audit_chain(wallet_id=wallet_id, **window)
        assert relinked.valid is False
        assert relinked.reason == "audit_sequence_gap"
        assert relinked.broken_event_id == rows[3].event_id


@pytest.mark.anyio
async def test_renumbered_sequence_gap_is_rejected(clean_database):
    """Renumbering breaks a signature; a signed gap breaks contiguity."""
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
        rows[-1].seq += 1
        head = await session.get(AuditChainHeadModel, wallet_id)
        assert head is not None
        head.last_seq = rows[-1].seq
        session.add(rows[-1])
        session.add(head)
        await session.commit()

    result = await verify_audit_chain(wallet_id=wallet_id)
    assert result.valid is False
    assert result.reason == "audit_payload_hash_mismatch"

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
        tail = rows[-1]
        assert tail.signature_key_id is not None
        # Seq-less signatures are exempt; only a valid seq-signed gap reaches
        # the contiguity check.
        _sign_with_previous(
            tail, rows[-2].chain_hash, signing_key_id=tail.signature_key_id
        )
        head = await session.get(AuditChainHeadModel, wallet_id)
        assert head is not None
        head.last_chain_hash = tail.chain_hash
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
    """A duplicate breaks its signature and, if re-signed, contiguity."""
    wallet_id = "wlt-dup-seq"
    base = utc_now()
    for n in range(3):
        await record_audit_event(
            event="trust.dup",
            wallet_id=wallet_id,
            created_at=base + timedelta(seconds=n),
            metadata={"n": n},
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
    assert result.reason == "audit_payload_hash_mismatch"

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel)
                    .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
                    .order_by(
                        ControlPlaneAuditEventModel.seq,
                        ControlPlaneAuditEventModel.created_at,
                    )
                )
            )
            .scalars()
            .all()
        )
        tail = rows[-1]
        assert tail.signature_key_id is not None
        _sign_with_previous(
            tail, rows[-2].chain_hash, signing_key_id=tail.signature_key_id
        )
        head = await session.get(AuditChainHeadModel, wallet_id)
        assert head is not None
        head.last_chain_hash = tail.chain_hash
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
                    select(ControlPlaneAuditEventModel)
                    .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
                    .order_by(ControlPlaneAuditEventModel.created_at)
                )
            )
            .scalars()
            .all()
        )
        previous_hash = None
        for row in rows:
            row.seq = 0
            row.previous_hash = previous_hash
            payload = _payload_without_seq(row)
            row.payload_hash = sha256_hex(payload)
            payload["payload_hash"] = row.payload_hash
            assert row.signature_key_id is not None
            row.signature, row.signature_key_id, _ = (
                get_signing_key_service().sign_payload_with_key_id(
                    payload, row.signature_key_id
                )
            )
            row.chain_hash = sha256_hex(
                {
                    "previous_hash": previous_hash,
                    "payload_hash": row.payload_hash,
                    "signature": row.signature,
                }
            )
            previous_hash = row.chain_hash
            session.add(row)
        head = await session.get(AuditChainHeadModel, wallet_id)
        assert head is not None
        await session.delete(head)
        await session.commit()

    result = await verify_audit_chain(wallet_id=wallet_id)
    assert result.valid is True, result
    assert result.checked_events == 2
