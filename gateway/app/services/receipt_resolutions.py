"""Append-only, signed resolutions for delivery-uncertain receipts."""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any, NoReturn, cast

from sqlalchemy import desc, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.resilience import is_retryable_write_conflict
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import (
    ReceiptModel,
    ReceiptResolutionModel,
    ResolutionChainHeadModel,
)
from app.services.receipts import get_receipt_service
from app.services.signing_keys import (
    canonical_json,
    get_signing_key_service,
    sha256_hex,
)

RESOLUTION_VERSION = "resolution-v0.1"
RESOLUTION_OUTCOMES = frozenset({"confirmed_success", "confirmed_not_delivered"})


class ReceiptResolutionError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _HeadConflict(Exception):
    """A competing append moved this wallet's chain head."""


def _signed_fields(model: ReceiptResolutionModel) -> dict[str, Any]:
    return {
        "version": RESOLUTION_VERSION,
        "resolution_id": model.resolution_id,
        "receipt_id": model.receipt_id,
        "wallet_id": model.wallet_id,
        "sequence": model.sequence,
        "prev_hash": model.prev_hash,
        "receipt_hash": model.receipt_hash,
        "outcome": model.outcome,
        "created_at": model.created_at,
    }


def resolution_signing_input(
    model: ReceiptResolutionModel, key_id: str
) -> tuple[str, str]:
    """Return the exact bytes to sign and the pre-envelope payload hash."""
    payload = {**_signed_fields(model), "alg": "Ed25519", "kid": key_id}
    payload_hash = sha256_hex(payload)
    payload["payload_hash"] = payload_hash
    return canonical_json(payload), payload_hash


async def _receipt_input(
    session: AsyncSession, receipt_id: str
) -> tuple[ReceiptModel, str]:
    receipt = await session.get(ReceiptModel, receipt_id)
    if receipt is None:
        raise ReceiptResolutionError("receipt_not_found")
    if receipt.outcome != "delivery_uncertain":
        raise ReceiptResolutionError("receipt_not_delivery_uncertain")
    signing_input = await get_receipt_service().signing_input_for_model(
        receipt, session=session
    )
    if signing_input is None:
        raise ReceiptResolutionError("receipt_signature_invalid")
    return receipt, signing_input


async def _assert_valid_resolution(
    session: AsyncSession,
    model: ReceiptResolutionModel,
    *,
    expected_outcome: str | None = None,
) -> None:
    receipt, receipt_input = await _receipt_input(session, model.receipt_id)
    if expected_outcome is not None and model.outcome != expected_outcome:
        raise ReceiptResolutionError("resolution_outcome_conflict")
    if (
        model.wallet_id != receipt.wallet_id
        or model.receipt_hash != sha256_hex(receipt_input.encode("utf-8"))
        or model.sequence < 1
        or model.outcome not in RESOLUTION_OUTCOMES
    ):
        raise ReceiptResolutionError("resolution_integrity_invalid")
    signing_input, payload_hash = resolution_signing_input(
        model, model.signature_key_id
    )
    if (
        model.signing_input != signing_input
        or model.payload_hash != payload_hash
        or model.resolution_hash != sha256_hex(signing_input.encode("utf-8"))
        or not await get_signing_key_service().verify_payload(
            {
                **_signed_fields(model),
                "alg": "Ed25519",
                "kid": model.signature_key_id,
                "payload_hash": payload_hash,
            },
            signature=model.signature,
            key_id=model.signature_key_id,
            session=session,
        )
    ):
        raise ReceiptResolutionError("resolution_integrity_invalid")


async def get_resolution(receipt_id: str) -> ReceiptResolutionModel | None:
    """Read one publicly portable record, refusing damaged stored evidence."""
    factory = get_session_factory()
    async with factory() as session:
        model = (
            await session.execute(
                select(ReceiptResolutionModel).where(
                    cast(
                        ColumnElement[bool],
                        ReceiptResolutionModel.receipt_id == receipt_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if model is not None:
            await _assert_valid_resolution(session, model)
        return model


async def list_wallet_resolutions(
    wallet_id: str, *, limit: int = 100, offset: int = 0
) -> tuple[list[ReceiptResolutionModel], int]:
    """Read a sequence page for an offline holder to assemble the full chain."""
    factory = get_session_factory()
    async with factory() as session:
        wallet_filter = cast(
            ColumnElement[bool], ReceiptResolutionModel.wallet_id == wallet_id
        )
        total = await session.scalar(
            cast(
                Any,
                select(func.count())
                .select_from(ReceiptResolutionModel)
                .where(wallet_filter),
            )
        )
        rows = (
            (
                await session.execute(
                    select(ReceiptResolutionModel)
                    .where(wallet_filter)
                    .order_by(cast(ColumnElement[Any], ReceiptResolutionModel.sequence))
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        previous_hash = None
        if rows and offset:
            predecessor = (
                await session.execute(
                    select(cast(Any, ReceiptResolutionModel.resolution_hash)).where(
                        wallet_filter,
                        cast(
                            ColumnElement[bool],
                            ReceiptResolutionModel.sequence == offset,
                        ),
                    )
                )
            ).scalar_one_or_none()
            if predecessor is None:
                raise ReceiptResolutionError("resolution_chain_invalid")
            previous_hash = predecessor
        for position, model in enumerate(rows, start=offset + 1):
            if model.sequence != position or model.prev_hash != previous_hash:
                raise ReceiptResolutionError("resolution_chain_invalid")
            await _assert_valid_resolution(session, model)
            previous_hash = model.resolution_hash
        return list(rows), int(total or 0)


async def _assert_head_matches(
    session: AsyncSession,
    wallet_id: str,
    head: ResolutionChainHeadModel | None,
) -> None:
    """Refuse an append if the durable head no longer names the wallet tail."""

    async def reject_or_retry() -> NoReturn:
        # A concurrent append can commit between reading the head and reading
        # its tail under PostgreSQL READ COMMITTED. Retry that race, but reject
        # a stable mismatch as damaged chain state.
        observed = None if head is None else (head.last_sequence, head.last_hash)
        fresh = (
            await session.execute(
                select(
                    cast(Any, ResolutionChainHeadModel.last_sequence),
                    cast(Any, ResolutionChainHeadModel.last_hash),
                ).where(
                    cast(
                        ColumnElement[bool],
                        ResolutionChainHeadModel.wallet_id == wallet_id,
                    )
                )
            )
        ).first()
        current = None if fresh is None else (fresh[0], fresh[1])
        if current != observed:
            raise _HeadConflict()
        raise ReceiptResolutionError("resolution_chain_invalid")

    wallet_filter = cast(
        ColumnElement[bool], ReceiptResolutionModel.wallet_id == wallet_id
    )
    latest = (
        await session.execute(
            select(ReceiptResolutionModel)
            .where(wallet_filter)
            .order_by(desc(cast(ColumnElement[Any], ReceiptResolutionModel.sequence)))
            .limit(1)
        )
    ).scalar_one_or_none()
    if head is None:
        if latest is not None:
            await reject_or_retry()
        return
    count = await session.scalar(
        cast(
            Any,
            select(func.count())
            .select_from(ReceiptResolutionModel)
            .where(wallet_filter),
        )
    )
    if (
        latest is None
        or latest.sequence != head.last_sequence
        or latest.resolution_hash != head.last_hash
        or count != head.last_sequence
    ):
        await reject_or_retry()
    if latest.sequence == 1:
        if latest.prev_hash is not None:
            await reject_or_retry()
    else:
        predecessor_hash = (
            await session.execute(
                select(cast(Any, ReceiptResolutionModel.resolution_hash)).where(
                    wallet_filter,
                    cast(
                        ColumnElement[bool],
                        ReceiptResolutionModel.sequence == latest.sequence - 1,
                    ),
                )
            )
        ).scalar_one_or_none()
        if latest.prev_hash != predecessor_hash:
            await reject_or_retry()


async def append_resolution(receipt_id: str, outcome: str) -> ReceiptResolutionModel:
    """Sign and append one final outcome; exact webhook replays return its row."""
    # TODO(SEL-351, PR #187, claude/sel-351): After #187 merges, hook
    # append_resolution into gateway/app/services/mcp_dispatch_reconciliation.py.
    # _lookup_status decides the provider outcome; _reconcile_active records it
    # once via its guarded self._dispatch.complete() call. Append the matching
    # resolution there after completion; not-delivered uses
    # STATUS_NOT_DELIVERED_ERROR_CODE. Migration 044 may need renumbering.
    if outcome not in RESOLUTION_OUTCOMES:
        raise ReceiptResolutionError("resolution_outcome_invalid")

    existing = await get_resolution(receipt_id)
    if existing is not None:
        if existing.outcome != outcome:
            raise ReceiptResolutionError("resolution_outcome_conflict")
        return existing

    # Refuse invalid source receipts before provisioning a new signing key.
    async with get_session_factory()() as session:
        await _receipt_input(session, receipt_id)

    signing_keys = get_signing_key_service()
    factory = get_session_factory()
    integrity_failures = 0
    for attempt in range(32):
        session = factory()
        try:
            key = await signing_keys.ensure_active_key()
            async with session.begin():
                receipt, receipt_input = await _receipt_input(session, receipt_id)
                existing = (
                    await session.execute(
                        select(ReceiptResolutionModel).where(
                            cast(
                                ColumnElement[bool],
                                ReceiptResolutionModel.receipt_id == receipt_id,
                            )
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    await _assert_valid_resolution(
                        session, existing, expected_outcome=outcome
                    )
                    return existing

                await signing_keys.validate_prepared_signing_key(
                    key.key_id, session=session
                )
                head = await session.get(ResolutionChainHeadModel, receipt.wallet_id)
                await _assert_head_matches(session, receipt.wallet_id, head)
                observed_sequence = head.last_sequence if head else 0
                prev_hash = head.last_hash if head else None
                model = ReceiptResolutionModel(
                    resolution_id=f"rsl_{uuid.uuid4().hex}",
                    receipt_id=receipt_id,
                    wallet_id=receipt.wallet_id,
                    sequence=observed_sequence + 1,
                    prev_hash=prev_hash,
                    receipt_hash=sha256_hex(receipt_input.encode("utf-8")),
                    outcome=outcome,
                    created_at=utc_now(),
                    payload_hash="",
                    resolution_hash="",
                    signing_input="",
                    signature="",
                    signature_key_id=key.key_id,
                )
                model.signing_input, model.payload_hash = resolution_signing_input(
                    model, key.key_id
                )
                model.signature, _, signed_hash = signing_keys.sign_payload_with_key_id(
                    _signed_fields(model), key.key_id
                )
                if signed_hash != model.payload_hash:
                    raise ReceiptResolutionError("resolution_signing_input_mismatch")
                model.resolution_hash = sha256_hex(model.signing_input.encode("utf-8"))
                now = utc_now()
                if head is None:
                    session.add(
                        ResolutionChainHeadModel(
                            wallet_id=receipt.wallet_id,
                            last_sequence=model.sequence,
                            last_hash=model.resolution_hash,
                            updated_at=now,
                        )
                    )
                    await session.flush()
                else:
                    advanced = cast(
                        CursorResult[Any],
                        await session.execute(
                            update(ResolutionChainHeadModel)
                            .where(
                                cast(
                                    ColumnElement[bool],
                                    ResolutionChainHeadModel.wallet_id
                                    == receipt.wallet_id,
                                ),
                                cast(
                                    ColumnElement[bool],
                                    ResolutionChainHeadModel.last_sequence
                                    == observed_sequence,
                                ),
                            )
                            .values(
                                last_sequence=model.sequence,
                                last_hash=model.resolution_hash,
                                updated_at=now,
                            )
                        ),
                    )
                    if advanced.rowcount != 1:
                        raise _HeadConflict()
                session.add(model)
            return model
        except (_HeadConflict, IntegrityError, OperationalError) as exc:
            if isinstance(exc, OperationalError) and not is_retryable_write_conflict(
                exc
            ):
                raise
            if isinstance(exc, IntegrityError):
                integrity_failures += 1
                if integrity_failures > 2:
                    raise
            if attempt == 31:
                raise ReceiptResolutionError("resolution_chain_contended") from exc
            await asyncio.sleep(random.uniform(0.002, 0.02))
        finally:
            await session.close()
    raise AssertionError("resolution_retry_budget_invalid")
