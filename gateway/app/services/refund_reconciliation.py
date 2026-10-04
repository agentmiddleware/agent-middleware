"""Exact-once operator repair for failed governed MCP charge refunds."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Literal, cast

from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.resilience import run_with_write_conflict_retry
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import (
    HumanApprovalModel,
    IdempotencyRecordModel,
    LedgerEntryModel,
    PermitModel,
    ReceiptModel,
    WalletModel,
)
from app.db.sql_expressions import clamped_decrement
from app.schemas.trust import RefundReconciliationItem
from app.schemas.trust import ReceiptResponse
from app.services.permits import get_permit_service
from app.services.receipts import get_receipt_service
from app.services.signing_keys import SigningKeyError, get_signing_key_service


RECONCILIATION_KIND = "mcp_failed_refund"
RECONCILIATION_VERSION = 1


class RefundReconciliationError(RuntimeError):
    """Fail-closed reconciliation state or linkage error."""

    def __init__(self, reason: str, *, status_code: int = 409) -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


class RefundReconciliationContendedError(RuntimeError):
    """``create_pending`` lost write conflicts for its whole restart budget.

    Nothing is durable: the receipt, the work item and the record update share
    one transaction, so every exhausted attempt rolled back whole. The same
    budget bounds the restart on a signing key retired under an attempt, so
    this is also what a rotation that lands under every attempt ends in.

    It is deliberately its own type rather than a reuse of
    ``ReceiptWriteContendedError``, which is the closest-looking fit and would
    be actively dangerous here. This site is only ever reached *after* the tool
    ran, the caller was charged and the refund already failed, and the routers
    treat that neighbouring type as the pre-effect, retryable one: the legacy
    REST ladder answers it ``409`` with "retry this key", and the unwind in
    ``_execute_registered_tool`` abandons the in-progress idempotency record to
    make that advice true. Both are correct for a receipt lost before any
    effects and catastrophic for a charge already taken -- together they would
    free the key and invite a second execution of a call the caller has already
    paid for.

    Nothing in the routers catches this type, which is the point: it reaches
    ``_finalize_unrefunded_failure``, the one caller, and is re-typed there to
    the non-retryable ``TerminalRecordContendedError``. Left uncaught by
    accident it would fall to the unclassified channel -- the bug this replaces
    -- which is bad but never unsafe, so the failure mode of forgetting the
    re-type is the honest one rather than the double-charge.
    """

    reason = "refund_reconciliation_write_contended"

    def __init__(self) -> None:
        super().__init__(self.reason)


def build_pending_refund_reconciliation(
    *,
    receipt_id: str,
    wallet_id: str,
    permit_id: str,
    ledger_entry_id: str,
    credits: Decimal,
) -> dict[str, Any]:
    """Build the durable work-item embedded in the original replay record."""
    return {
        "kind": RECONCILIATION_KIND,
        "version": RECONCILIATION_VERSION,
        "status": "pending",
        "receipt_id": receipt_id,
        "wallet_id": wallet_id,
        "permit_id": permit_id,
        "ledger_entry_id": ledger_entry_id,
        "refund_entry_id": f"refund-{ledger_entry_id}",
        "credits": str(credits),
        "resolved_at": None,
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp_in_current_period(
    timestamp: datetime,
    period_start: datetime | None,
) -> bool:
    return period_start is None or _as_utc(timestamp) >= _as_utc(period_start)


class RefundReconciliationService:
    """Resolve pending failed refunds against one locked durable work item.

    The process-local lock makes SQLite tests and a single worker deterministic.
    PostgreSQL's row locks remain the cross-process correctness boundary.
    """

    _process_lock = asyncio.Lock()

    @staticmethod
    async def _assert_approval_binding(
        session: AsyncSession,
        *,
        record: IdempotencyRecordModel,
        permit: PermitModel,
        approval_id: str | None,
        wallet_id: str,
        tool_name: str,
    ) -> None:
        """Reject unsigned or mismatched approval evidence before receipt signing."""
        if not permit.requires_human_approval:
            if approval_id is not None:
                raise RefundReconciliationError(
                    "refund_reconciliation_approval_linkage_invalid"
                )
            return
        if approval_id is None:
            raise RefundReconciliationError("refund_reconciliation_approval_required")
        approval = await session.get(HumanApprovalModel, approval_id)
        if (
            approval is None
            or approval.wallet_id != wallet_id
            or approval.permit_id != permit.permit_id
            or approval.tool != tool_name
            or approval.idempotency_key != record.idempotency_key
            or approval.status != "consumed"
        ):
            raise RefundReconciliationError(
                "refund_reconciliation_approval_linkage_invalid"
            )

    async def create_pending(
        self,
        *,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
        permit_id: str,
        key_id: str | None,
        tool_name: str,
        request_payload: dict[str, Any],
        ledger_entry_id: str,
        credits_authorized: Decimal,
        credits_charged: Decimal,
        audit_event_id: str | None,
        reason: str,
        dispatch_attempt_id: str | None = None,
        approval_id: str | None = None,
        response_payload: dict[str, Any] | None = None,
        response_hash_override: str | None = None,
    ) -> tuple[ReceiptResponse, dict[str, Any]]:
        """Atomically insert the signed receipt and durable pending work item."""

        async def validated_checkpoint(
            session: AsyncSession,
            *,
            lock: bool = False,
        ) -> IdempotencyRecordModel:
            query = select(IdempotencyRecordModel).where(
                cast(
                    ColumnElement[bool],
                    IdempotencyRecordModel.wallet_id == wallet_id,
                ),
                cast(
                    ColumnElement[bool],
                    IdempotencyRecordModel.endpoint == endpoint,
                ),
                cast(
                    ColumnElement[bool],
                    IdempotencyRecordModel.idempotency_key == idempotency_key,
                ),
            )
            if lock:
                query = query.with_for_update()
            record = (await session.execute(query)).scalar_one_or_none()
            if (
                record is None
                or record.ledger_entry_id != ledger_entry_id
                or record.response_json is not None
                or record.response_reference is not None
            ):
                raise RefundReconciliationError(
                    "refund_reconciliation_checkpoint_invalid"
                )
            permit = await session.get(PermitModel, permit_id)
            if permit is None:
                raise RefundReconciliationError(
                    "refund_reconciliation_checkpoint_invalid"
                )
            await self._assert_approval_binding(
                session,
                record=record,
                permit=permit,
                approval_id=approval_id,
                wallet_id=wallet_id,
                tool_name=tool_name,
            )
            return record

        factory = get_session_factory()
        async with factory() as preflight_session:
            await validated_checkpoint(preflight_session)

        async def attempt() -> tuple[ReceiptResponse, dict[str, Any]]:
            """One whole transaction: locked read, receipt, work item, record.

            Everything the restart has to replay lives inside this session, so
            a losing attempt leaves nothing behind for the next one to trip
            over.

            The active key is resolved per attempt rather than once outside the
            loop, because a key resolved before the loop can retire during it.
            ``create_receipt`` revalidates a prepared key under a row lock and
            rejects a retired one with ``signing_key_not_active``. Resolving
            per attempt costs a read in the common case:
            ``ensure_active_key`` writes only when the key is not already
            active.

            It is resolved before the transaction opens because it owns its own
            session, and opening that while holding this one risks deadlocking
            the write against itself. That ordering leaves a gap of its own: a
            rotation can commit after the key is resolved and before the row
            lock, and the rejection it earns is a ``SigningKeyError``, not an
            ``OperationalError``, so the restart loop would not retry it by
            itself. ``retired_under_the_attempt`` names that one rejection as
            a restart, and the restart lands back here to resolve afresh.
            """
            signing_key = await get_signing_key_service().ensure_active_key()
            async with factory() as session:
                async with session.begin():
                    record = await validated_checkpoint(session, lock=True)

                    receipt = await get_receipt_service().create_receipt(
                        permit_id=permit_id,
                        wallet_id=wallet_id,
                        key_id=key_id,
                        tool=tool_name,
                        request_payload=request_payload,
                        response_payload=response_payload or {"error": reason},
                        ledger_entry_id=ledger_entry_id,
                        credits_authorized=credits_authorized,
                        credits_charged=credits_charged,
                        outcome="failed_unrefunded",
                        audit_event_id=audit_event_id,
                        reason_code="refund_failed",
                        idempotency_record_id=record.record_id,
                        dispatch_attempt_id=dispatch_attempt_id,
                        approval_id=approval_id,
                        response_hash_override=response_hash_override,
                        session=session,
                        prepared_signing_key_id=signing_key.key_id,
                    )
                    reconciliation = build_pending_refund_reconciliation(
                        receipt_id=receipt.receipt_id,
                        wallet_id=wallet_id,
                        permit_id=permit_id,
                        ledger_entry_id=ledger_entry_id,
                        credits=credits_charged,
                    )
                    receipt_payload = receipt.model_dump(mode="json")
                    record.response_reference = receipt.receipt_id
                    record.response_json = json.dumps(
                        {
                            "content": [],
                            "isError": True,
                            "error": reason,
                            "receipt": receipt_payload,
                            "refund_reconciliation": reconciliation,
                        },
                        default=str,
                    )
                    record.status_code = 500
                    session.add(record)
                    await session.flush()
                return receipt, reconciliation

        def retired_under_the_attempt(exc: BaseException) -> bool:
            """Whether ``create_receipt`` refused the key this attempt prepared.

            ``validate_prepared_signing_key`` answers with this one reason for
            a key that was active when ``ensure_active_key`` returned and is
            not by the time the receipt locks its row: a rotation committed in
            the gap, and a restart cures it by resolving the key again. No
            other reason is a restart. A missing, mismatched or disabled key is
            a configuration fault every attempt would repeat, and it has to
            fail on the first one rather than surface after the whole budget
            as a contention it was not.
            """
            return isinstance(exc, SigningKeyError) and str(exc) == (
                "signing_key_not_active"
            )

        return await run_with_write_conflict_retry(
            attempt,
            on_exhausted=lambda exc: RefundReconciliationContendedError(),
            restart_on=retired_under_the_attempt,
        )

    @staticmethod
    def _decode_response(record: IdempotencyRecordModel) -> dict[str, Any]:
        try:
            payload = json.loads(record.response_json or "")
        except (json.JSONDecodeError, TypeError) as exc:
            raise RefundReconciliationError(
                "refund_reconciliation_state_invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise RefundReconciliationError("refund_reconciliation_state_invalid")
        return payload

    @staticmethod
    def _parse_item(
        *,
        record: IdempotencyRecordModel,
        receipt: ReceiptModel,
        payload: dict[str, Any],
    ) -> RefundReconciliationItem:
        state = payload.get("refund_reconciliation")
        if not isinstance(state, dict):
            raise RefundReconciliationError("refund_reconciliation_state_invalid")
        if (
            state.get("kind") != RECONCILIATION_KIND
            or state.get("version") != RECONCILIATION_VERSION
        ):
            raise RefundReconciliationError("refund_reconciliation_state_invalid")
        status = state.get("status")
        if status not in {"pending", "resolved"}:
            raise RefundReconciliationError("refund_reconciliation_state_invalid")
        try:
            credits = Decimal(str(state["credits"]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise RefundReconciliationError(
                "refund_reconciliation_state_invalid"
            ) from exc
        if not credits.is_finite() or credits <= 0:
            raise RefundReconciliationError("refund_reconciliation_state_invalid")

        receipt_id = state.get("receipt_id")
        wallet_id = state.get("wallet_id")
        permit_id = state.get("permit_id")
        ledger_entry_id = state.get("ledger_entry_id")
        refund_entry_id = state.get("refund_entry_id")
        expected_refund_id = f"refund-{ledger_entry_id}"
        if (
            receipt.outcome != "failed_unrefunded"
            or receipt_id != receipt.receipt_id
            or record.response_reference != receipt.receipt_id
            or wallet_id != receipt.wallet_id
            or wallet_id != record.wallet_id
            or permit_id != receipt.permit_id
            or ledger_entry_id != receipt.ledger_entry_id
            or ledger_entry_id != record.ledger_entry_id
            or refund_entry_id != expected_refund_id
            or receipt.credits_charged != credits
            or receipt.credits_authorized != credits
        ):
            raise RefundReconciliationError("refund_reconciliation_linkage_invalid")

        resolved_at = state.get("resolved_at")
        if status == "resolved" and not isinstance(resolved_at, str):
            raise RefundReconciliationError("refund_reconciliation_state_invalid")
        if status == "pending" and resolved_at is not None:
            raise RefundReconciliationError("refund_reconciliation_state_invalid")

        return RefundReconciliationItem(
            record_id=record.record_id,
            receipt_id=receipt.receipt_id,
            wallet_id=receipt.wallet_id,
            permit_id=receipt.permit_id,
            ledger_entry_id=cast(str, receipt.ledger_entry_id),
            refund_entry_id=cast(str, refund_entry_id),
            credits=credits,
            status=cast(Literal["pending", "resolved"], status),
            created_at=record.created_at,
            resolved_at=resolved_at,
        )

    async def list_items(
        self,
        *,
        status: Literal["pending", "resolved"] | None = "pending",
        wallet_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RefundReconciliationItem], int]:
        """List failed-refund work items, optionally for one wallet.

        ``wallet_id`` narrows the view to a single tenant so a wallet key can
        see money owed back to *it* without an operator key. Applied before
        pagination, so a wallet's own totals are its own.
        """
        factory = get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    select(IdempotencyRecordModel, ReceiptModel)
                    .join(
                        ReceiptModel,
                        cast(
                            ColumnElement[bool],
                            ReceiptModel.receipt_id
                            == IdempotencyRecordModel.response_reference,
                        ),
                    )
                    .where(
                        cast(
                            ColumnElement[bool],
                            ReceiptModel.outcome == "failed_unrefunded",
                        )
                    )
                    .order_by(
                        cast(
                            ColumnElement[Any], IdempotencyRecordModel.created_at
                        ).desc()
                    )
                )
            ).all()
            items = [
                self._parse_item(
                    record=record,
                    receipt=receipt,
                    payload=self._decode_response(record),
                )
                for record, receipt in rows
            ]
            if wallet_id is not None:
                items = [item for item in items if item.wallet_id == wallet_id]
            if status is not None:
                items = [item for item in items if item.status == status]
            for item in items:
                if item.status == "resolved":
                    await self.validate_resolved_claim(
                        item=item,
                        session=session,
                    )

        total = len(items)
        return items[offset : offset + limit], total

    async def _load_item(
        self,
        session: AsyncSession,
        receipt_id: str,
    ) -> RefundReconciliationItem:
        row = (
            await session.execute(
                select(IdempotencyRecordModel, ReceiptModel)
                .join(
                    ReceiptModel,
                    cast(
                        ColumnElement[bool],
                        ReceiptModel.receipt_id
                        == IdempotencyRecordModel.response_reference,
                    ),
                )
                .where(
                    cast(
                        ColumnElement[bool],
                        ReceiptModel.receipt_id == receipt_id,
                    )
                )
            )
        ).one_or_none()
        if row is None:
            raise RefundReconciliationError(
                "refund_reconciliation_not_found", status_code=404
            )
        record, receipt = row
        return self._parse_item(
            record=record,
            receipt=receipt,
            payload=self._decode_response(record),
        )

    async def validate_resolved_claim(
        self,
        *,
        receipt_id: str | None = None,
        item: RefundReconciliationItem | None = None,
        session: AsyncSession | None = None,
    ) -> RefundReconciliationItem:
        """Require a resolved claim to be backed by one exact ledger refund.

        Loaded operator items can reuse their read or retry transaction. Agent
        replay handling can supply only the persisted receipt reference and
        gets the same read-only validation without invoking repair.
        """
        if item is not None and receipt_id is not None:
            if item.receipt_id != receipt_id:
                raise RefundReconciliationError("refund_reconciliation_linkage_invalid")

        if session is None:
            factory = get_session_factory()
            async with factory() as owned_session:
                return await self.validate_resolved_claim(
                    receipt_id=receipt_id,
                    item=item,
                    session=owned_session,
                )

        if item is None:
            if not receipt_id:
                raise RefundReconciliationError("refund_reconciliation_linkage_invalid")
            item = await self._load_item(session, receipt_id)
        if item.status != "resolved":
            raise RefundReconciliationError("refund_reconciliation_resolution_invalid")

        receipt = await session.get(ReceiptModel, item.receipt_id)
        if receipt is None:
            raise RefundReconciliationError("refund_reconciliation_linkage_invalid")
        if not await get_receipt_service().verify_model(receipt, session=session):
            raise RefundReconciliationError("refund_reconciliation_receipt_invalid")

        correlated_refunds = (
            (
                await session.execute(
                    select(LedgerEntryModel).where(
                        cast(
                            ColumnElement[bool],
                            LedgerEntryModel.correlation_id == item.ledger_entry_id,
                        ),
                        cast(
                            ColumnElement[bool],
                            LedgerEntryModel.action == "refund",
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        refund_by_id = await session.get(LedgerEntryModel, item.refund_entry_id)
        candidates = {
            refund.entry_id: refund
            for refund in [*correlated_refunds, refund_by_id]
            if refund is not None
        }
        refund = candidates.get(item.refund_entry_id)
        if (
            len(candidates) != 1
            or refund is None
            or refund.wallet_id != item.wallet_id
            or refund.action != "refund"
            or refund.correlation_id != item.ledger_entry_id
            or refund.amount != item.credits
        ):
            raise RefundReconciliationError("refund_reconciliation_resolution_invalid")
        return item

    async def get_item(self, receipt_id: str) -> RefundReconciliationItem:
        factory = get_session_factory()
        async with factory() as session:
            item = await self._load_item(session, receipt_id)
            if item.status == "resolved":
                await self.validate_resolved_claim(item=item, session=session)
            return item

    async def retry(self, receipt_id: str) -> tuple[RefundReconciliationItem, bool]:
        async with self._process_lock:
            return await self._retry_locked(receipt_id)

    async def _retry_locked(
        self, receipt_id: str
    ) -> tuple[RefundReconciliationItem, bool]:
        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                records = (
                    (
                        await session.execute(
                            select(IdempotencyRecordModel)
                            .where(
                                cast(
                                    ColumnElement[bool],
                                    IdempotencyRecordModel.response_reference
                                    == receipt_id,
                                )
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                if not records:
                    raise RefundReconciliationError(
                        "refund_reconciliation_not_found", status_code=404
                    )
                if len(records) != 1:
                    raise RefundReconciliationError(
                        "refund_reconciliation_linkage_invalid"
                    )
                record = records[0]
                receipt = await session.get(
                    ReceiptModel,
                    receipt_id,
                    with_for_update=True,
                )
                if receipt is None:
                    raise RefundReconciliationError(
                        "refund_reconciliation_not_found", status_code=404
                    )
                payload = self._decode_response(record)
                item = self._parse_item(
                    record=record,
                    receipt=receipt,
                    payload=payload,
                )
                if not await get_receipt_service().verify_model(
                    receipt,
                    session=session,
                ):
                    raise RefundReconciliationError(
                        "refund_reconciliation_receipt_invalid"
                    )

                if item.status == "resolved":
                    await self.validate_resolved_claim(item=item, session=session)
                    return item, True

                # Match BillingEngine.refund_charge's lock order: wallet before
                # charge. The work-item lock precedes both and is unique to this
                # operator workflow, so it cannot invert the billing path.
                wallet = await session.get(
                    WalletModel,
                    item.wallet_id,
                    with_for_update=True,
                )
                charge = await session.get(
                    LedgerEntryModel,
                    item.ledger_entry_id,
                    with_for_update=True,
                )
                permit = await session.get(
                    PermitModel,
                    item.permit_id,
                    with_for_update=True,
                )
                if wallet is None or charge is None or permit is None:
                    raise RefundReconciliationError(
                        "refund_reconciliation_linkage_invalid"
                    )
                if not await get_permit_service().verify_signature(
                    permit,
                    session=session,
                ):
                    raise RefundReconciliationError(
                        "refund_reconciliation_permit_invalid"
                    )
                if (
                    charge.wallet_id != item.wallet_id
                    or charge.action != "debit"
                    or charge.amount >= 0
                    or abs(charge.amount) != item.credits
                    or permit.subject_wallet_id != item.wallet_id
                ):
                    raise RefundReconciliationError(
                        "refund_reconciliation_linkage_invalid"
                    )

                correlated_refunds = (
                    (
                        await session.execute(
                            select(LedgerEntryModel).where(
                                cast(
                                    ColumnElement[bool],
                                    LedgerEntryModel.correlation_id
                                    == item.ledger_entry_id,
                                ),
                                cast(
                                    ColumnElement[bool],
                                    LedgerEntryModel.action == "refund",
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                refund_by_id = await session.get(
                    LedgerEntryModel,
                    item.refund_entry_id,
                    with_for_update=True,
                )
                existing_candidates = {
                    refund.entry_id: refund
                    for refund in [*correlated_refunds, refund_by_id]
                    if refund is not None
                }
                existing_refund = next(iter(existing_candidates.values()), None)
                if existing_refund is not None and (
                    len(existing_candidates) != 1
                    or existing_refund.entry_id != item.refund_entry_id
                    or existing_refund.wallet_id != item.wallet_id
                    or existing_refund.action != "refund"
                    or existing_refund.correlation_id != item.ledger_entry_id
                    or existing_refund.amount != item.credits
                ):
                    raise RefundReconciliationError(
                        "refund_reconciliation_state_conflict"
                    )
                if permit.spent_credits < item.credits:
                    raise RefundReconciliationError(
                        "refund_reconciliation_budget_conflict"
                    )

                refund = existing_refund
                if refund is None:
                    await self._apply_refund(session, wallet, charge, item.credits)
                    refund = LedgerEntryModel(
                        entry_id=item.refund_entry_id,
                        wallet_id=item.wallet_id,
                        action="refund",
                        amount=item.credits,
                        balance_after=wallet.balance,
                        service_category=charge.service_category,
                        description=f"Operator refund for {charge.description}",
                        request_path=charge.request_path,
                        compute_cost=Decimal("0"),
                        margin=Decimal("0"),
                        metadata_json=json.dumps(
                            {
                                "refund_reconciliation": {
                                    "record_id": item.record_id,
                                    "receipt_id": item.receipt_id,
                                    "permit_id": item.permit_id,
                                    "credits_released": str(item.credits),
                                    "status": "resolved",
                                }
                            }
                        ),
                        correlation_id=item.ledger_entry_id,
                    )
                    session.add(refund)
                # Release the permit reservation with a clamped relative UPDATE
                # for the same reason: ``permits.py`` guarantees that every
                # ``spent_credits`` write is decided by the database, and that
                # guarantee is only as good as its weakest caller. This one sat
                # outside that module and wrote back a value read earlier.
                await session.execute(
                    sa_update(PermitModel)
                    .where(
                        cast(
                            ColumnElement[bool],
                            PermitModel.permit_id == permit.permit_id,
                        )
                    )
                    .values(
                        spent_credits=clamped_decrement(
                            PermitModel.spent_credits, item.credits
                        ),
                        updated_at=utc_now(),
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.refresh(permit)
                resolved_at = utc_now().isoformat()
                state = cast(dict[str, Any], payload["refund_reconciliation"])
                state["status"] = "resolved"
                state["resolved_at"] = resolved_at
                record.response_json = json.dumps(payload, default=str)
                # `permit` was updated by the guarded statement above, not the ORM.
                session.add(record)
                await session.flush()

                resolved = self._parse_item(
                    record=record,
                    receipt=receipt,
                    payload=payload,
                )
            return resolved, False

    async def _apply_refund(
        self,
        session: AsyncSession,
        wallet: WalletModel,
        charge: LedgerEntryModel,
        amount: Decimal,
    ) -> None:
        """Credit a refund back to the wallet in a single relative statement.

        Reading each counter and writing back read±amount is a read-modify-write
        serialized only by a row lock, and ``SELECT ... FOR UPDATE`` is a silent
        no-op on SQLite. Two refunds reconciled together each computed a total
        from the same observed balance, so one credit was lost outright — and a
        lost credit here is the customer's money, not a metric.
        """
        values: dict[str, Any] = {
            "balance": WalletModel.balance + amount,
            "lifetime_debits": clamped_decrement(WalletModel.lifetime_debits, amount),
            "updated_at": utc_now(),
        }
        if _timestamp_in_current_period(charge.timestamp, wallet.hourly_reset_at):
            values["hourly_spent"] = clamped_decrement(WalletModel.hourly_spent, amount)
        if _timestamp_in_current_period(charge.timestamp, wallet.daily_reset_at):
            values["daily_spent"] = clamped_decrement(WalletModel.daily_spent, amount)
        await session.execute(
            sa_update(WalletModel)
            .where(
                cast(
                    ColumnElement[bool],
                    WalletModel.wallet_id == wallet.wallet_id,
                )
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await session.refresh(wallet)


_service: RefundReconciliationService | None = None


def get_refund_reconciliation_service() -> RefundReconciliationService:
    global _service
    if _service is None:
        _service = RefundReconciliationService()
    return _service
