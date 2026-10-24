from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import asc, desc, literal, or_, select, true, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.orm import aliased

from app.core.resilience import is_retryable_write_conflict
from app.core.time import to_naive_utc, utc_now
from app.db.database import get_session_factory
from app.db.models import AuditChainHeadModel, ControlPlaneAuditEventModel
from app.services.signing_keys import get_signing_key_service, sha256_hex


def audit_payload(
    *,
    event_id: str,
    created_at: datetime,
    event: str,
    wallet_id: str | None,
    tool: str | None,
    endpoint: str | None,
    auth_source: str | None,
    key_id: str | None,
    policy_decision_id: str | None,
    request_id: str | None,
    ok: bool,
    error: str | None,
    metadata_json: str | None,
    previous_hash: str | None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "created_at": created_at,
        "event": event,
        "wallet_id": wallet_id,
        "tool": tool,
        "endpoint": endpoint,
        "auth_source": auth_source,
        "key_id": key_id,
        "policy_decision_id": policy_decision_id,
        "request_id": request_id,
        "ok": ok,
        "error": error,
        "metadata_json": metadata_json,
        "previous_hash": previous_hash,
    }


def _chain_wallet_filter(wallet_id: str | None) -> ColumnElement[bool]:
    """Rows that belong to one wallet chain.

    The head table stores "" for a missing wallet id. A blank string is
    the same chain as NULL. Walking those rows apart made each half look
    truncated or unlinked.
    """
    column = cast(ColumnElement[str | None], ControlPlaneAuditEventModel.wallet_id)
    if wallet_id:
        return cast(ColumnElement[bool], column == wallet_id)
    return cast(ColumnElement[bool], or_(column.is_(None), column == ""))


def _first_sequence_break(
    events: list[ControlPlaneAuditEventModel],
    last_seq: int,
) -> str | None:
    """Event id where ``seq`` stops being 1..last_seq, in chain order.

    ``seq`` is not signed. A stored tail can be renumbered so it still
    matches the head while a hole or a duplicate sits in front of it.
    ``events`` must already be ordered by seq, then created_at.
    """
    for index, event in enumerate(events, start=1):
        if index > last_seq or event.seq != index:
            return event.event_id
    if len(events) != last_seq:
        return events[-1].event_id if events else None
    return None


def _sign_with_previous(
    model: ControlPlaneAuditEventModel,
    previous_hash: str | None,
    *,
    signing_key_id: str,
) -> None:
    """Sign ``model`` as the successor of ``previous_hash`` (seq set by caller).

    Pure (no DB I/O) so it can run inside an open chain-head transaction. The
    active signing key must be ensured by the caller beforehand. seq is
    deliberately NOT part of the signed payload, so events signed before that
    column existed still verify.
    """
    payload = audit_payload(
        event_id=model.event_id,
        created_at=model.created_at,
        event=model.event,
        wallet_id=model.wallet_id,
        tool=model.tool,
        endpoint=model.endpoint,
        auth_source=model.auth_source,
        key_id=model.key_id,
        policy_decision_id=model.policy_decision_id,
        request_id=model.request_id,
        ok=model.ok,
        error=model.error,
        metadata_json=model.metadata_json,
        previous_hash=previous_hash,
    )
    payload_hash = sha256_hex(payload)
    payload["payload_hash"] = payload_hash
    signature, signature_key_id, _ = get_signing_key_service().sign_payload_with_key_id(
        payload, signing_key_id
    )
    model.payload_hash = payload_hash
    model.previous_hash = previous_hash
    model.chain_hash = sha256_hex(
        {
            "previous_hash": previous_hash,
            "payload_hash": payload_hash,
            "signature": signature,
        }
    )
    model.signature = signature
    model.signature_key_id = signature_key_id


async def sign_audit_model(model: ControlPlaneAuditEventModel) -> None:
    """Sign an audit event by reading the current chain head (no insert).

    Self-contained convenience used outside the append path; the persisted
    write path uses :func:`append_chained_audit_event`, which serializes
    concurrent writers.
    """
    key = await get_signing_key_service().ensure_active_key()
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ControlPlaneAuditEventModel)
            .where(_chain_wallet_filter(model.wallet_id))
            .order_by(
                desc(cast(ColumnElement[Any], ControlPlaneAuditEventModel.seq)),
                desc(cast(ColumnElement[Any], ControlPlaneAuditEventModel.created_at)),
            )
            .limit(1)
        )
        previous = result.scalar_one_or_none()
    previous_hash = previous.chain_hash if previous else None
    model.seq = (previous.seq + 1) if previous else 1
    _sign_with_previous(model, previous_hash, signing_key_id=key.key_id)


class _HeadConflict(Exception):
    """A concurrent writer advanced the chain head; the append must retry."""


class AuditEventConflictError(RuntimeError):
    """A caller reused an audit event identity for different signed evidence."""


class AuditChainHeadMissingError(RuntimeError):
    """Events are stored for this chain but the head row is gone.

    Appending would sign a new genesis (empty previous hash, seq 1) and
    split the links that are already there. Callers get this instead.
    """

    reason = "audit_chain_head_missing"


class AuditChainContendedError(RuntimeError):
    """The per-wallet chain head stayed contended for the whole retry budget.

    Distinct from :class:`AuditEventConflictError`, which is a caller mistake.
    This is a transient loss: the event is signed and valid, and an append that
    reconverges on a quieter head would still record it. It exists so the
    failure leaves this module as a named, public type -- ``_HeadConflict`` is
    private and a bare ``OperationalError`` says nothing about which write lost
    -- and so a caller can tell "the chain was busy" from a substantive
    integrity fault, which is deliberately left to propagate as itself.
    """

    reason = "audit_chain_head_contention"


def _assert_same_audit_intent(
    existing: ControlPlaneAuditEventModel,
    intended: ControlPlaneAuditEventModel,
) -> None:
    """Adopt an existing event only when its complete signed intent matches.

    The sequence and chain fields are assigned by the winning append, so they
    are deliberately excluded from the caller intent comparison. Their hashes
    are still checked below before the persisted event is returned.
    """
    intent_fields = (
        "event_id",
        "created_at",
        "event",
        "wallet_id",
        "tool",
        "endpoint",
        "auth_source",
        "key_id",
        "policy_decision_id",
        "request_id",
        "ok",
        "error",
        "metadata_json",
    )
    if any(
        getattr(existing, field) != getattr(intended, field) for field in intent_fields
    ):
        raise AuditEventConflictError("audit_event_id_conflict")

    payload = audit_payload(
        event_id=existing.event_id,
        created_at=existing.created_at,
        event=existing.event,
        wallet_id=existing.wallet_id,
        tool=existing.tool,
        endpoint=existing.endpoint,
        auth_source=existing.auth_source,
        key_id=existing.key_id,
        policy_decision_id=existing.policy_decision_id,
        request_id=existing.request_id,
        ok=existing.ok,
        error=existing.error,
        metadata_json=existing.metadata_json,
        previous_hash=existing.previous_hash,
    )
    if (
        existing.payload_hash != sha256_hex(payload)
        or existing.signature is None
        or existing.signature_key_id is None
        or existing.chain_hash
        != sha256_hex(
            {
                "previous_hash": existing.previous_hash,
                "payload_hash": existing.payload_hash,
                "signature": existing.signature,
            }
        )
    ):
        raise AuditEventConflictError("audit_event_integrity_conflict")


async def append_chained_audit_event(
    model: ControlPlaneAuditEventModel,
) -> ControlPlaneAuditEventModel:
    """Sign and persist ``model`` as the next link in its wallet's audit chain.

    Concurrency is handled with optimistic control on the per-wallet head row:
    the new ``seq``/``previous_hash`` are derived from the head, then the head
    is advanced with a conditional ``UPDATE ... WHERE last_seq = <observed>``.
    If a concurrent writer advanced the head first the update matches no rows
    and the append retries against the new head. This works identically on
    SQLite and Postgres (no reliance on ``SELECT ... FOR UPDATE``), so two
    racing writers can never share a predecessor and fork the chain.
    """
    wallet_key = model.wallet_id or ""
    # Provision the active signing key before the transaction so signing inside
    # it is pure crypto (no nested DB write / lock).
    key = await get_signing_key_service().ensure_active_key()
    factory = get_session_factory()
    # Optimistic-concurrency retry budget. Audit events are integrity records
    # and must not be dropped, so the budget is generous; backoff is a small
    # flat jitter (not growing) so contended writers reconverge quickly without
    # inflating tail latency on slow/CPU-bound runners.
    attempts = 64
    # Both races an IntegrityError can represent -- two writers inserting the
    # first head row for a wallet, or two writers inserting the same
    # deterministic event id -- are resolved by the very next pass, which
    # observes the winner's row and either returns it or updates against it. So
    # a violation that survives a couple of retries is not a race at all, and
    # spending the full budget on it only delays the real error.
    integrity_race_attempts = 2
    integrity_failures = 0
    missing_head_seen = False
    for attempt in range(attempts):
        session = factory()
        try:
            async with session.begin():
                # A deterministic event id is the durable uniqueness claim for
                # dispatch reconciliation. Check it inside the same optimistic
                # chain-head transaction: a racing writer either sees this row
                # now or loses the head update and sees it on the next retry.
                existing = await session.get(
                    ControlPlaneAuditEventModel,
                    model.event_id,
                )
                if existing is not None:
                    _assert_same_audit_intent(existing, model)
                    return existing

                row = (
                    await session.execute(
                        select(
                            cast(Any, AuditChainHeadModel.last_seq),
                            cast(Any, AuditChainHeadModel.last_chain_hash),
                        ).where(
                            cast(
                                ColumnElement[bool],
                                AuditChainHeadModel.wallet_key == wallet_key,
                            )
                        )
                    )
                ).first()
                observed_seq = row[0] if row else 0
                previous_hash = row[1] if row else None
                if row is None:
                    # No head does not mean an empty chain. A genesis signed
                    # over rows that are already stored splits the hash links.
                    prior = (
                        await session.execute(
                            select(cast(Any, ControlPlaneAuditEventModel.event_id))
                            .where(_chain_wallet_filter(model.wallet_id))
                            .limit(1)
                        )
                    ).first()
                    if prior is not None:
                        # The head read can precede another writer's commit,
                        # while this event read follows it. Recheck in a fresh
                        # transaction before treating the head as deleted.
                        if missing_head_seen:
                            raise AuditChainHeadMissingError("audit_chain_head_missing")
                        missing_head_seen = True
                        raise _HeadConflict()
                model.seq = observed_seq + 1
                _sign_with_previous(model, previous_hash, signing_key_id=key.key_id)
                now = utc_now()
                if row is None:
                    # First event for this wallet; a unique PK collision means a
                    # concurrent writer won the race — retry against their head.
                    session.add(
                        AuditChainHeadModel(
                            wallet_key=wallet_key,
                            last_seq=model.seq,
                            last_chain_hash=model.chain_hash,
                            updated_at=now,
                        )
                    )
                    await session.flush()
                else:
                    # An UPDATE statement always yields a CursorResult at runtime;
                    # the cast()-wrapped where() clauses above erase that from the
                    # static type, so make it explicit for `.rowcount` below.
                    result = cast(
                        CursorResult[Any],
                        await session.execute(
                            update(AuditChainHeadModel)
                            .where(
                                cast(
                                    ColumnElement[bool],
                                    AuditChainHeadModel.wallet_key == wallet_key,
                                ),
                                cast(
                                    ColumnElement[bool],
                                    AuditChainHeadModel.last_seq == observed_seq,
                                ),
                            )
                            .values(
                                last_seq=model.seq,
                                last_chain_hash=model.chain_hash,
                                updated_at=now,
                            )
                        ),
                    )
                    if result.rowcount == 0:
                        raise _HeadConflict()
                session.add(model)
            return model
        except (_HeadConflict, IntegrityError, OperationalError) as exc:
            # Classify before spending any of the budget. A fault no retry could
            # clear -- `no such table`, a disconnected pool -- used to burn all
            # 64 attempts and their backoff before propagating unchanged, so a
            # deterministic error arrived as a stall.
            if isinstance(exc, OperationalError) and not is_retryable_write_conflict(
                exc
            ):
                raise
            if isinstance(exc, IntegrityError):
                integrity_failures += 1
                if integrity_failures > integrity_race_attempts:
                    # Past the point where a race explains it. Re-raised with
                    # its own type: calling a real constraint violation
                    # "contended" would send a reader looking for a busy writer
                    # that never existed.
                    raise
            if attempt == attempts - 1:
                # Only a genuine contention loss is renamed, and by here the
                # exception is either a head conflict or a retryable write
                # conflict -- everything else left through the raises above.
                if isinstance(exc, IntegrityError):
                    raise
                raise AuditChainContendedError("audit_chain_head_contention") from exc
            await asyncio.sleep(random.uniform(0.002, 0.02))
        finally:
            await session.close()
    # Unreachable: attempts is a positive constant, so the loop either returns
    # or raises on its final pass. Kept as an assertion rather than the bare
    # `raise RuntimeError("audit_chain_head_contention")` that stood here, which
    # advertised a reason code no caller could ever actually receive.
    raise AssertionError("audit_chain_retry_budget_invalid")


@dataclass(frozen=True)
class AuditChainVerification:
    valid: bool
    checked_events: int
    first_event_id: str | None = None
    last_event_id: str | None = None
    reason: str | None = None
    broken_event_id: str | None = None


async def verify_audit_chain(
    *,
    wallet_id: str | None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> AuditChainVerification:
    if wallet_id is None:
        # Global verification: walk every per-wallet chain, INCLUDING the
        # wallet-less chain (events whose wallet_id IS NULL -- system and
        # denied-action records, chained under wallet_key ""). Previously the
        # NULL chain was silently dropped from the distinct list, so tampering
        # a wallet-less event returned valid=True.
        factory = get_session_factory()
        async with factory() as session:
            wallet_ids_result = await session.execute(
                select(cast(Any, ControlPlaneAuditEventModel.wallet_id)).distinct()
            )
            # Blank wallet ids share the "" head with NULL. Fold them into
            # one entry so this walk does not count that chain twice.
            event_wallets = {
                (None if not row[0] else row[0]) for row in wallet_ids_result.all()
            }
            # Also enumerate wallets from the chain-head table. Deriving the list
            # from events alone let an attacker who deleted ALL of a wallet's
            # events erase that wallet from the check entirely: with no events it
            # never appears in distinct_wallets, so _verify_single_chain (which
            # would flag the surviving head as audit_chain_truncated) is never
            # called for it. The head row is the durable anchor, so a wallet with
            # a head must always be verified even when its events are gone. Head
            # keys use "" for the wallet-less chain; map it back to NULL.
            head_keys_result = await session.execute(
                select(cast(Any, AuditChainHeadModel.wallet_key)).distinct()
            )
            head_wallets = {
                (row[0] if row[0] != "" else None) for row in head_keys_result.all()
            }
            distinct_wallets = event_wallets | head_wallets
        checked = 0
        first_event_id: str | None = None
        last_event_id: str | None = None
        for current_wallet_id in distinct_wallets:
            chain_result = await _verify_single_chain(
                wallet_id=current_wallet_id,
                created_after=created_after,
                created_before=created_before,
            )
            checked += chain_result.checked_events
            first_event_id = first_event_id or chain_result.first_event_id
            last_event_id = chain_result.last_event_id or last_event_id
            if not chain_result.valid:
                return AuditChainVerification(
                    False,
                    checked,
                    first_event_id,
                    last_event_id,
                    chain_result.reason,
                    chain_result.broken_event_id,
                )
        return AuditChainVerification(True, checked, first_event_id, last_event_id)

    return await _verify_single_chain(
        wallet_id=wallet_id,
        created_after=created_after,
        created_before=created_before,
    )


async def _verify_single_chain(
    *,
    wallet_id: str | None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> AuditChainVerification:
    """Verify one wallet's audit chain.

    ``wallet_id=None`` and ``wallet_id=""`` are the wallet-less chain
    (NULL and blank ids together).
    """
    created_after = to_naive_utc(created_after) if created_after else None
    created_before = to_naive_utc(created_before) if created_before else None
    stmt = (
        select(ControlPlaneAuditEventModel)
        .where(_chain_wallet_filter(wallet_id))
        .order_by(
            asc(cast(ColumnElement[Any], ControlPlaneAuditEventModel.seq)),
            asc(cast(ColumnElement[Any], ControlPlaneAuditEventModel.created_at)),
        )
    )

    factory = get_session_factory()
    async with factory() as session:
        # One statement gives events and head the same database snapshot.
        # A separate head SELECT could see a legitimate concurrent append
        # and label the earlier event snapshot as a truncated chain.
        # Time filters used to skip this load, so a wide window hid a
        # deleted tail. Apply those filters in memory after the snapshot.
        # Anchor the outer joins to one row so an empty or deleted event
        # set still loads its head and truncation remains detectable.
        anchor = select(literal(1).label("anchor")).subquery()
        event_rows = aliased(ControlPlaneAuditEventModel, stmt.subquery())
        head_rows = aliased(
            AuditChainHeadModel,
            select(AuditChainHeadModel)
            .where(
                cast(
                    ColumnElement[bool],
                    AuditChainHeadModel.wallet_key == (wallet_id or ""),
                )
            )
            .subquery(),
        )
        snapshot = await session.execute(
            select(event_rows, head_rows)
            .select_from(anchor)
            .outerjoin(event_rows, true())
            .outerjoin(head_rows, true())
            .order_by(
                cast(ColumnElement[Any], event_rows.seq),
                cast(ColumnElement[Any], event_rows.created_at),
            )
        )
        rows = snapshot.all()
        all_events = [row[0] for row in rows if row[0] is not None]
        head = rows[0][1] if rows else None

    if created_after is not None or created_before is not None:
        events = [
            event
            for event in all_events
            if (created_after is None or event.created_at >= created_after)
            and (created_before is None or event.created_at <= created_before)
        ]
    else:
        events = all_events

    # A window that excludes the wallet's genesis event starts mid-chain: the
    # first in-window event legitimately links to an event outside the
    # window. Seed the expected predecessor from the last stored event before
    # the window instead of from genesis, otherwise every non-genesis window
    # reports a valid chain as tampered (audit_previous_hash_mismatch).
    previous_hash: str | None = None
    if events and created_after:
        first_seq = events[0].seq
        below = [event for event in all_events if event.seq < first_seq]
        if below:
            previous_hash = below[-1].chain_hash
    first_event_id = events[0].event_id if events else None
    last_event_id = events[-1].event_id if events else None
    for event in events:
        payload = audit_payload(
            event_id=event.event_id,
            created_at=event.created_at,
            event=event.event,
            wallet_id=event.wallet_id,
            tool=event.tool,
            endpoint=event.endpoint,
            auth_source=event.auth_source,
            key_id=event.key_id,
            policy_decision_id=event.policy_decision_id,
            request_id=event.request_id,
            ok=event.ok,
            error=event.error,
            metadata_json=event.metadata_json,
            previous_hash=event.previous_hash,
        )
        payload_hash = sha256_hex(payload)
        if event.payload_hash != payload_hash:
            return AuditChainVerification(
                False,
                len(events),
                first_event_id,
                last_event_id,
                "audit_payload_hash_mismatch",
                event.event_id,
            )
        if event.previous_hash != previous_hash:
            return AuditChainVerification(
                False,
                len(events),
                first_event_id,
                last_event_id,
                "audit_previous_hash_mismatch",
                event.event_id,
            )
        if not event.signature or not event.signature_key_id:
            return AuditChainVerification(
                False,
                len(events),
                first_event_id,
                last_event_id,
                "audit_signature_missing",
                event.event_id,
            )
        signed_payload = {
            **payload,
            "payload_hash": event.payload_hash,
            "alg": "Ed25519",
            "kid": event.signature_key_id,
        }
        ok = await get_signing_key_service().verify_payload(
            signed_payload,
            signature=event.signature,
            key_id=event.signature_key_id,
        )
        if not ok:
            return AuditChainVerification(
                False,
                len(events),
                first_event_id,
                last_event_id,
                "audit_signature_invalid",
                event.event_id,
            )
        expected_chain_hash = sha256_hex(
            {
                "previous_hash": event.previous_hash,
                "payload_hash": event.payload_hash,
                "signature": event.signature,
            }
        )
        if event.chain_hash != expected_chain_hash:
            return AuditChainVerification(
                False,
                len(events),
                first_event_id,
                last_event_id,
                "audit_chain_hash_mismatch",
                event.event_id,
            )
        previous_hash = event.chain_hash

    # The hash walk above covers the requested window only. The head and
    # the sequence belong to the whole chain. Checking them on the window,
    # or skipping them when the head row is gone, let a deleted tail or a
    # renumbered seq verify as a complete chain.
    anchor_first = all_events[0].event_id if all_events else None
    anchor_last = all_events[-1].event_id if all_events else None
    if head is None:
        if any(event.seq != 0 for event in all_events):
            broken = next(event for event in all_events if event.seq != 0)
            return AuditChainVerification(
                False,
                len(all_events),
                anchor_first,
                anchor_last,
                "audit_chain_head_missing",
                broken.event_id,
            )
    else:
        last_event = all_events[-1] if all_events else None
        if last_event is None or (
            last_event.seq != head.last_seq
            or last_event.chain_hash != head.last_chain_hash
        ):
            return AuditChainVerification(
                False,
                len(all_events),
                anchor_first,
                anchor_last,
                "audit_chain_truncated",
                last_event.event_id if last_event else None,
            )
        broken_id = _first_sequence_break(all_events, head.last_seq)
        if broken_id is not None:
            return AuditChainVerification(
                False,
                len(all_events),
                anchor_first,
                anchor_last,
                "audit_sequence_gap",
                broken_id,
            )

    return AuditChainVerification(
        True,
        len(events),
        first_event_id,
        last_event_id,
    )
