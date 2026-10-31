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

from app.core.config import get_settings
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
    seq: int | None = None,
) -> dict[str, Any]:
    payload = {
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
    # None omits the field. Sequence 0 is a real stored value on rows signed
    # before seq was covered, so it must not mean "leave seq out".
    if seq is not None:
        payload["seq"] = seq
    return payload


def _event_audit_payload(
    event: ControlPlaneAuditEventModel,
    *,
    seq: int | None,
) -> dict[str, Any]:
    return audit_payload(
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
        seq=seq,
    )


def _payload_matching_stored_hash(
    event: ControlPlaneAuditEventModel,
) -> dict[str, Any] | None:
    """Return the body that produces ``event.payload_hash``, or None.

    New signatures include ``seq``. Rows signed before that still match the
    body that omits it. A rewritten sequence on a new signature matches
    neither, which is what makes the number tamper-evident.
    """
    current = _event_audit_payload(event, seq=event.seq)
    if event.payload_hash == sha256_hex(current):
        return current
    legacy = _event_audit_payload(event, seq=None)
    if event.payload_hash == sha256_hex(legacy):
        return legacy
    return None


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


def _sign_with_previous(
    model: ControlPlaneAuditEventModel,
    previous_hash: str | None,
    *,
    signing_key_id: str,
) -> None:
    """Sign ``model`` as the successor of ``previous_hash`` (seq set by caller).

    Pure (no DB I/O) so it can run inside an open chain-head transaction. The
    active signing key must be ensured by the caller beforehand. ``model.seq``
    is part of the signed payload, so a later edit of the sequence number
    fails verification. Rows signed before seq was covered still verify:
    their payload hash omits it, and verification accepts that older shape.
    """
    # The caller passes the predecessor. Copy it onto the model before hashing
    # so a retry against a newer head does not sign the previous attempt.
    model.previous_hash = previous_hash
    payload = _event_audit_payload(model, seq=model.seq)
    payload_hash = sha256_hex(payload)
    payload["payload_hash"] = payload_hash
    signature, signature_key_id, _ = get_signing_key_service().sign_payload_with_key_id(
        payload, signing_key_id
    )
    model.payload_hash = payload_hash
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

    if (
        _payload_matching_stored_hash(existing) is None
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
                    terminal = (
                        await session.execute(
                            select(
                                cast(Any, ControlPlaneAuditEventModel.seq),
                                cast(Any, ControlPlaneAuditEventModel.chain_hash),
                            )
                            .where(_chain_wallet_filter(model.wallet_id))
                            .order_by(
                                desc(
                                    cast(
                                        ColumnElement[Any],
                                        ControlPlaneAuditEventModel.seq,
                                    )
                                ),
                                desc(
                                    cast(
                                        ColumnElement[Any],
                                        ControlPlaneAuditEventModel.created_at,
                                    )
                                ),
                            )
                            .limit(1)
                        )
                    ).first()
                    if terminal is not None:
                        # A first writer may have committed between the head
                        # and event reads. Only that race warrants a retry.
                        if await session.get(AuditChainHeadModel, wallet_key):
                            raise _HeadConflict()
                        if terminal[0] != 0:
                            # A seq-signed row exists, so a head was written
                            # and has since gone. Refuse rather than guess.
                            raise AuditChainHeadMissingError("audit_chain_head_missing")
                        # Every stored row predates seq (migration 017 set
                        # them to 0) and no head was ever written. Continue
                        # the legacy chain from its terminal link at seq 1,
                        # which verification accepts after a legacy run.
                        previous_hash = terminal[1]
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


# Rows loaded by one statement while verifying. Memory is bounded by this page,
# not by the length of the chain.
_VERIFY_PAGE_SIZE = 250


@dataclass(frozen=True)
class AuditChainVerification:
    """Verdict over the span of a chain that one call actually walked.

    ``valid`` means no defect was found in that span. ``complete`` is true only
    when the walk reached the end of the chain and ran the head and deleted-tail
    checks. A capped walk returns ``complete=False`` and, for a single chain, the
    ``next_after_seq`` cursor to resume from. Never read ``valid=True`` with
    ``complete=False`` as a full-chain pass.
    """

    valid: bool
    checked_events: int
    first_event_id: str | None = None
    last_event_id: str | None = None
    reason: str | None = None
    broken_event_id: str | None = None
    complete: bool = True
    next_after_seq: int | None = None


def effective_verify_limit(limit: int | None) -> int:
    """Events one verification call may walk: the request, clamped to the cap."""
    cap = get_settings().AUDIT_VERIFY_MAX_EVENTS
    if limit is None:
        return cap
    return max(1, min(limit, cap))


async def verify_audit_chain(
    *,
    wallet_id: str | None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    after_seq: int | None = None,
    limit: int | None = None,
) -> AuditChainVerification:
    """Verify at most ``AUDIT_VERIFY_MAX_EVENTS`` events of each wallet chain.

    ``after_seq`` resumes a single chain after the event with that sequence
    (the ``next_after_seq`` of a partial verdict). ``limit`` lowers the cap for
    this call and is clamped to it.
    """
    max_events = effective_verify_limit(limit)
    if wallet_id is None:
        if after_seq is not None:
            raise ValueError("audit_after_seq_requires_wallet")
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
        all_complete = True
        first_event_id: str | None = None
        last_event_id: str | None = None
        for current_wallet_id in distinct_wallets:
            chain_result = await _verify_single_chain(
                wallet_id=current_wallet_id,
                created_after=created_after,
                created_before=created_before,
                after_seq=None,
                max_events=max_events,
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
                    complete=False,
                )
            all_complete = all_complete and chain_result.complete
        # No single cursor spans several chains. A partial global verdict says
        # so with complete=False; resume per wallet with after_seq.
        return AuditChainVerification(
            True,
            checked,
            first_event_id,
            last_event_id,
            complete=all_complete,
        )

    return await _verify_single_chain(
        wallet_id=wallet_id,
        created_after=created_after,
        created_before=created_before,
        after_seq=after_seq,
        max_events=max_events,
    )


async def _event_defect(
    event: ControlPlaneAuditEventModel,
    *,
    expected_previous_hash: str | None,
    check_signature: bool,
) -> tuple[str | None, dict[str, Any] | None]:
    """``(reason, payload)``: the first defect's reason, or None if sound.

    Check order is the verifier's contract: payload hash, previous-hash link,
    signature, then the chain hash that binds all three.
    """
    payload = _payload_matching_stored_hash(event)
    if payload is None:
        return "audit_payload_hash_mismatch", None
    if event.previous_hash != expected_previous_hash:
        return "audit_previous_hash_mismatch", payload
    if check_signature:
        if not event.signature or not event.signature_key_id:
            return "audit_signature_missing", payload
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
            return "audit_signature_invalid", payload
    expected_chain_hash = sha256_hex(
        {
            "previous_hash": event.previous_hash,
            "payload_hash": event.payload_hash,
            "signature": event.signature,
        }
    )
    if event.chain_hash != expected_chain_hash:
        return "audit_chain_hash_mismatch", payload
    return None, payload


async def verify_audit_event_link(
    event: ControlPlaneAuditEventModel,
) -> AuditChainVerification:
    """Verify one event and its link to its immediate predecessor.

    Checks the payload hash, the signature, the chain hash, and that
    ``previous_hash`` equals the chain hash of the closest earlier event of the
    same wallet chain (or that none exists when it is None). Reads one row, so
    its cost does not depend on the chain length. It is NOT a chain walk:
    ``complete`` is always False, and sequence holes or a deleted tail are
    outside its scope.
    """
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(
                cast(Any, ControlPlaneAuditEventModel.chain_hash),
            )
            .where(
                _chain_wallet_filter(event.wallet_id),
                cast(ColumnElement[bool], ControlPlaneAuditEventModel.seq < event.seq),
            )
            .order_by(
                desc(cast(ColumnElement[Any], ControlPlaneAuditEventModel.seq)),
                desc(cast(ColumnElement[Any], ControlPlaneAuditEventModel.created_at)),
            )
            .limit(1)
        )
        predecessor = result.first()
    expected_previous = predecessor[0] if predecessor else None
    reason, _ = await _event_defect(
        event, expected_previous_hash=expected_previous, check_signature=True
    )
    return AuditChainVerification(
        reason is None,
        1,
        event.event_id,
        event.event_id,
        reason,
        event.event_id if reason else None,
        complete=False,
    )


async def _verify_single_chain(
    *,
    wallet_id: str | None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    after_seq: int | None = None,
    max_events: int | None = None,
) -> AuditChainVerification:
    """Verify one wallet's audit chain, walking at most ``max_events`` events.

    ``wallet_id=None`` and ``wallet_id=""`` are the wallet-less chain
    (NULL and blank ids together). The walk pages by ``seq`` (keyset), carrying
    the previous chain hash across pages, so memory is bounded by the page size.
    """
    if max_events is None:
        max_events = effective_verify_limit(None)
    created_after = to_naive_utc(created_after) if created_after else None
    created_before = to_naive_utc(created_before) if created_before else None
    chain_filter = _chain_wallet_filter(wallet_id)
    seq_col = cast(ColumnElement[Any], ControlPlaneAuditEventModel.seq)
    factory = get_session_factory()

    previous_hash: str | None = None
    cursor = after_seq
    expected_seq = 1
    # Last walked event as (event_id, seq, chain_hash); seeded from the cursor.
    last_walked: tuple[str, int, str | None] | None = None
    if after_seq is not None:
        async with factory() as session:
            anchor_result = await session.execute(
                select(
                    cast(Any, ControlPlaneAuditEventModel.event_id),
                    cast(Any, ControlPlaneAuditEventModel.chain_hash),
                )
                .where(chain_filter, seq_col == after_seq)
                .order_by(
                    asc(
                        cast(ColumnElement[Any], ControlPlaneAuditEventModel.created_at)
                    )
                )
                .limit(2)
            )
            anchors = anchor_result.all()
        if not anchors:
            return AuditChainVerification(
                False, 0, reason="audit_cursor_event_missing", complete=False
            )
        if len(anchors) > 1:
            return AuditChainVerification(
                False,
                0,
                reason="audit_sequence_gap",
                broken_event_id=anchors[1][0],
                complete=False,
            )
        previous_hash = anchors[0][1]
        last_walked = (anchors[0][0], after_seq, anchors[0][1])
        expected_seq = after_seq + 1

    walked = 0
    checked = 0
    first_walked_id: str | None = None
    first_event_id: str | None = None
    last_event_id: str | None = None
    first_seq_break: str | None = None
    first_nonzero_seq: str | None = None
    head: AuditChainHeadModel | None = None
    reached_end = False

    while True:
        page_size = min(_VERIFY_PAGE_SIZE, max_events - walked)
        page_stmt = select(ControlPlaneAuditEventModel).where(chain_filter)
        if cursor is not None:
            page_stmt = page_stmt.where(seq_col > cursor)
        page_stmt = page_stmt.order_by(
            asc(seq_col),
            asc(cast(ColumnElement[Any], ControlPlaneAuditEventModel.created_at)),
        ).limit(page_size)

        async with factory() as session:
            # One statement gives this page and the head the same database
            # snapshot. A separate head SELECT could see a legitimate concurrent
            # append and label the earlier event snapshot as a truncated chain.
            # Anchor the outer joins to one row so an empty or deleted event
            # set still loads its head and truncation remains detectable.
            anchor = select(literal(1).label("anchor")).subquery()
            event_rows = aliased(ControlPlaneAuditEventModel, page_stmt.subquery())
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
        page = [row[0] for row in rows if row[0] is not None]
        head = rows[0][1] if rows else None

        for event in page:
            in_window = (
                created_after is None or event.created_at >= created_after
            ) and (created_before is None or event.created_at <= created_before)
            if in_window:
                checked += 1
                first_event_id = first_event_id or event.event_id
                last_event_id = event.event_id
            if first_walked_id is None:
                first_walked_id = event.event_id
            defect, payload = await _event_defect(
                event,
                expected_previous_hash=previous_hash,
                check_signature=in_window,
            )
            if defect is not None:
                return AuditChainVerification(
                    False,
                    checked,
                    first_event_id,
                    last_event_id,
                    defect,
                    event.event_id,
                    complete=False,
                )
            if payload is not None and "seq" not in payload:
                # Legacy signature: stored seq cannot prove contiguity.
                expected_seq = event.seq + 1
            else:
                if first_seq_break is None and event.seq != expected_seq:
                    first_seq_break = event.event_id
                expected_seq = event.seq + 1
            if first_nonzero_seq is None and event.seq != 0:
                first_nonzero_seq = event.event_id
            previous_hash = event.chain_hash
            last_walked = (event.event_id, event.seq, event.chain_hash)

        walked += len(page)
        if page:
            cursor = page[-1].seq
        if len(page) < page_size:
            reached_end = True
            break
        # A full page: more rows may follow. A second row sharing the last seq
        # would be skipped by the seq cursor, so look for one before moving on.
        assert last_walked is not None
        async with factory() as session:
            duplicate = (
                await session.execute(
                    select(cast(Any, ControlPlaneAuditEventModel.event_id))
                    .where(
                        chain_filter,
                        seq_col == last_walked[1],
                        cast(
                            ColumnElement[bool],
                            ControlPlaneAuditEventModel.event_id != last_walked[0],
                        ),
                    )
                    .limit(1)
                )
            ).first()
            if duplicate is not None:
                return AuditChainVerification(
                    False,
                    checked,
                    first_event_id,
                    last_event_id,
                    "audit_sequence_gap",
                    duplicate[0],
                    complete=False,
                )
            if walked >= max_events:
                more = (
                    await session.execute(
                        select(cast(Any, ControlPlaneAuditEventModel.event_id))
                        .where(chain_filter, seq_col > last_walked[1])
                        .limit(1)
                    )
                ).first()
                reached_end = more is None
                break

    def head_level_failure(reason: str, broken: str | None) -> AuditChainVerification:
        return AuditChainVerification(
            False,
            walked,
            first_walked_id,
            last_walked[0] if last_walked else None,
            reason,
            broken,
            complete=reached_end,
        )

    # The head and sequence also belong to the chain. Skipping them when the
    # head row is gone lets a deleted tail or renumbered seq verify. The
    # deleted-tail comparison needs the end of the chain, so it runs only when
    # the walk reached it.
    if head is None:
        if first_nonzero_seq is not None:
            return head_level_failure("audit_chain_head_missing", first_nonzero_seq)
    else:
        if reached_end and (
            last_walked is None
            or last_walked[1] != head.last_seq
            or last_walked[2] != head.last_chain_hash
        ):
            return head_level_failure(
                "audit_chain_truncated", last_walked[0] if last_walked else None
            )
        if first_seq_break is not None:
            return head_level_failure("audit_sequence_gap", first_seq_break)

    return AuditChainVerification(
        True,
        checked,
        first_event_id,
        last_event_id,
        complete=reached_end,
        next_after_seq=None if reached_end else cursor,
    )
