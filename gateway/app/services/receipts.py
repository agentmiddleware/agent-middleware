from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import DuplicateGuardMode, get_settings
from app.core.resilience import run_with_write_conflict_retry
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, PermitModel, ReceiptModel
from app.schemas.trust import ActionPermitFields, ReceiptResponse
from app.services.jev_guard_metadata import load_jev_guard_metadata
from app.services.pricing import credit_amount_fits_storage
from app.services.signing_keys import (
    canonical_json,
    get_signing_key_service,
    sha256_hex,
)


_REASON_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")


class ReceiptError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ReceiptWriteContendedError(RuntimeError):
    """The receipt insert lost SQLite write conflicts for its whole budget.

    No receipt row is durable: every attempt rolled back whole, and the
    exhaustion path re-reads the unique idempotency link before raising, so
    this is not a lost commit acknowledgement being misreported. Distinct from
    :class:`ReceiptError`, which is a caller mistake about the receipt's
    contents, and deliberately not a subclass of it -- the reconciler adopts an
    equivalent receipt on ``ReceiptError`` and there is nothing here to adopt.

    Whether a caller may retry is not this module's call to make: a contended
    receipt after a charged, dispatched invoke means something different than
    one after a denial that ran nothing. The router owns that split, the same
    way it does for a contended audit append.
    """

    reason = "receipt_write_contended"


import json


def _loads_dict(value: str | None) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _action_binding(model: PermitModel | ReceiptModel) -> dict[str, Any]:
    values = {name: getattr(model, name) for name in ActionPermitFields.model_fields}
    # Reject partial/unknown bindings. Unset legacy fields contribute no bytes.
    return ActionPermitFields.model_validate(values).model_dump(exclude_none=True)


def receipt_model_to_response(model: ReceiptModel) -> ReceiptResponse:
    return ReceiptResponse(
        **_action_binding(model),
        receipt_id=model.receipt_id,
        idempotency_record_id=model.idempotency_record_id,
        dispatch_attempt_id=model.dispatch_attempt_id,
        permit_id=model.permit_id,
        wallet_id=model.wallet_id,
        key_id=model.key_id,
        tool=model.tool,
        request_hash=model.request_hash,
        response_hash=model.response_hash,
        ledger_entry_id=model.ledger_entry_id,
        credits_authorized=model.credits_authorized,
        credits_charged=model.credits_charged,
        outcome=model.outcome,
        reason_code=model.reason_code,
        audit_event_id=model.audit_event_id,
        approval_id=model.approval_id,
        constraints_evaluated=_loads_dict(model.constraints_evaluated_json),
        created_at=model.created_at,
        signature=model.signature,
        signature_key_id=model.signature_key_id,
    )


class ReceiptService:
    @staticmethod
    def _verification_payload(
        model: ReceiptModel,
        *,
        include_linkage: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "receipt_id": model.receipt_id,
            "permit_id": model.permit_id,
            "wallet_id": model.wallet_id,
            "key_id": model.key_id,
            "tool": model.tool,
            "request_hash": model.request_hash,
            "response_hash": model.response_hash,
            "ledger_entry_id": model.ledger_entry_id,
            "credits_authorized": model.credits_authorized,
            "credits_charged": model.credits_charged,
            "outcome": model.outcome,
            "audit_event_id": model.audit_event_id,
            "created_at": model.created_at,
            "alg": "Ed25519",
            "kid": model.signature_key_id,
        }
        if model.reason_code is not None:
            payload["reason_code"] = model.reason_code
        if include_linkage and model.idempotency_record_id is not None:
            payload["idempotency_record_id"] = model.idempotency_record_id
        if include_linkage and model.dispatch_attempt_id is not None:
            payload["dispatch_attempt_id"] = model.dispatch_attempt_id
        # Approval linkage predates the governed-dispatch linkage migration and
        # remains part of both current and constrained legacy signatures.
        if model.approval_id is not None:
            payload["approval_id"] = model.approval_id
        # Permit schema v2: constraints_evaluated signed when present
        if model.constraints_evaluated_json is not None:
            try:
                ce = json.loads(model.constraints_evaluated_json)
                if ce:
                    payload["constraints_evaluated"] = ce
            except json.JSONDecodeError:
                pass
        payload.update(_action_binding(model))
        payload["payload_hash"] = sha256_hex(payload)
        return payload

    @staticmethod
    async def _has_unambiguous_historical_idempotency_link(
        model: ReceiptModel,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        """Corroborate the only linkage migration was allowed to backfill."""
        if model.idempotency_record_id is None or model.dispatch_attempt_id is not None:
            return False

        if session is None:
            factory = get_session_factory()
            async with factory() as owned_session:
                return (
                    await ReceiptService._has_unambiguous_historical_idempotency_link(
                        model,
                        session=owned_session,
                    )
                )
        records = (
            (
                await session.execute(
                    select(IdempotencyRecordModel)
                    .where(
                        cast(
                            ColumnElement[bool],
                            IdempotencyRecordModel.response_reference
                            == model.receipt_id,
                        )
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(records) != 1:
            return False
        record = records[0]
        return (
            record.record_id == model.idempotency_record_id
            and record.wallet_id == model.wallet_id
            and record.request_hash == model.request_hash
        )

    @staticmethod
    def _assert_idempotent_match(
        model: ReceiptModel,
        *,
        idempotency_record_id: str,
        dispatch_attempt_id: str | None,
        permit_id: str,
        wallet_id: str,
        key_id: str | None,
        tool: str,
        request_hash: str,
        response_hash: str | None,
        ledger_entry_id: str | None,
        credits_authorized: Decimal,
        credits_charged: Decimal,
        outcome: str,
        reason_code: str | None,
        audit_event_id: str | None,
        approval_id: str | None,
        constraints_evaluated: dict[str, Any] | None = None,
        action_binding: dict[str, Any] | None = None,
    ) -> None:
        """Reject reuse of one idempotency record for different evidence."""
        expected = {
            "idempotency_record_id": idempotency_record_id,
            "dispatch_attempt_id": dispatch_attempt_id,
            "permit_id": permit_id,
            "wallet_id": wallet_id,
            "key_id": key_id,
            "tool": tool,
            "request_hash": request_hash,
            "response_hash": response_hash,
            "ledger_entry_id": ledger_entry_id,
            "credits_authorized": credits_authorized,
            "credits_charged": credits_charged,
            "outcome": outcome,
            "reason_code": reason_code,
            "audit_event_id": audit_event_id,
            "approval_id": approval_id,
        }
        if _action_binding(model) != (action_binding or {}):
            raise ReceiptError("receipt_action_binding_conflict")
        if any(getattr(model, name) != value for name, value in expected.items()):
            raise ReceiptError("receipt_idempotency_conflict")

    @staticmethod
    async def _get_by_idempotency_record(
        session: AsyncSession,
        idempotency_record_id: str,
    ) -> ReceiptModel | None:
        return (
            await session.execute(
                select(ReceiptModel).where(
                    cast(
                        ColumnElement[bool],
                        ReceiptModel.idempotency_record_id == idempotency_record_id,
                    )
                )
            )
        ).scalar_one_or_none()

    async def action_binding_for_permit(
        self, permit_id: str, *, session: AsyncSession | None = None
    ) -> dict[str, Any]:
        if session is None:
            async with get_session_factory()() as owned_session:
                return await self.action_binding_for_permit(
                    permit_id, session=owned_session
                )
        permit = await session.get(PermitModel, permit_id)
        return _action_binding(permit) if permit is not None else {}

    async def assert_action_receipt_binding(self, receipt: ReceiptResponse) -> None:
        expected = await self.action_binding_for_permit(receipt.permit_id)
        actual = {name: getattr(receipt, name) for name in expected}
        if actual != expected or any(
            getattr(receipt, name) is not None and name not in expected
            for name in ActionPermitFields.model_fields
        ):
            raise ReceiptError("receipt_action_binding_conflict")

    async def create_receipt(
        self,
        *,
        permit_id: str,
        wallet_id: str,
        key_id: str | None,
        tool: str,
        request_payload: dict[str, Any] | None,
        response_payload: dict[str, Any] | None,
        ledger_entry_id: str | None,
        credits_authorized: Decimal,
        credits_charged: Decimal,
        outcome: str,
        audit_event_id: str | None,
        reason_code: str | None = None,
        idempotency_record_id: str | None = None,
        dispatch_attempt_id: str | None = None,
        request_hash: str | None = None,
        response_hash_override: str | None = None,
        session: AsyncSession | None = None,
        approval_id: str | None = None,
        constraints_evaluated: dict[str, Any] | None = None,
        prepared_signing_key_id: str | None = None,
    ) -> ReceiptResponse:
        if not all(
            credit_amount_fits_storage(amount)
            for amount in (credits_authorized, credits_charged)
        ):
            raise ReceiptError("receipt_credits_invalid")
        # Reuse the existing signed JSON field; no receipt schema/migration.
        # The audit link also covers denial/refund/reconciliation helpers.
        if get_settings().JEV_RISK_GUARD != DuplicateGuardMode.OFF:
            jev = await load_jev_guard_metadata(
                audit_event_id, wallet_id, session=session
            )
            if jev is not None:
                constraints_evaluated = {
                    **(constraints_evaluated or {}),
                    "jev_risk_guard": jev,
                }
        if reason_code is not None:
            if outcome == "success" or not _REASON_CODE_PATTERN.fullmatch(reason_code):
                raise ReceiptError("receipt_reason_code_invalid")
        if (request_payload is None) == (request_hash is None):
            raise ReceiptError("receipt_request_identity_invalid")
        if request_hash is not None:
            if len(request_hash) != 64:
                raise ReceiptError("receipt_request_hash_invalid")
            try:
                int(request_hash, 16)
            except ValueError as exc:
                raise ReceiptError("receipt_request_hash_invalid") from exc
            effective_request_hash = request_hash.lower()
        else:
            assert request_payload is not None
            effective_request_hash = sha256_hex(request_payload)
        response_hash: str | None
        if response_hash_override is not None:
            if dispatch_attempt_id is None or response_payload is None:
                raise ReceiptError("receipt_response_hash_override_invalid")
            if len(response_hash_override) != 64:
                raise ReceiptError("receipt_response_hash_invalid")
            try:
                int(response_hash_override, 16)
                canonical_response = canonical_json(
                    response_payload,
                    ensure_ascii=False,
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise ReceiptError("receipt_response_hash_invalid") from exc
            response_hash = response_hash_override.lower()
            if sha256_hex(canonical_response) != response_hash:
                raise ReceiptError("receipt_response_hash_mismatch")
        else:
            # Preserve the historical receipt hash for all callers that do not
            # opt into the linked dispatch result's UTF-8 canonical bytes.
            response_hash = (
                sha256_hex(response_payload) if response_payload is not None else None
            )

        action_binding = await self.action_binding_for_permit(
            permit_id, session=session
        )

        async def existing_response(
            target_session: AsyncSession,
        ) -> ReceiptResponse | None:
            if idempotency_record_id is None:
                return None
            existing = await self._get_by_idempotency_record(
                target_session,
                idempotency_record_id,
            )
            if existing is None:
                return None
            self._assert_idempotent_match(
                existing,
                idempotency_record_id=idempotency_record_id,
                dispatch_attempt_id=dispatch_attempt_id,
                permit_id=permit_id,
                wallet_id=wallet_id,
                key_id=key_id,
                tool=tool,
                request_hash=effective_request_hash,
                response_hash=response_hash,
                ledger_entry_id=ledger_entry_id,
                credits_authorized=credits_authorized,
                credits_charged=credits_charged,
                outcome=outcome,
                reason_code=reason_code,
                audit_event_id=audit_event_id,
                approval_id=approval_id,
                constraints_evaluated=constraints_evaluated,
                action_binding=action_binding,
            )
            return receipt_model_to_response(existing)

        target_session = session
        if target_session is not None:
            existing = await existing_response(target_session)
            if existing is not None:
                return existing

        created_at = utc_now()
        receipt_id = f"rcpt-{uuid.uuid4().hex[:16]}"
        payload: dict[str, Any] = {
            "receipt_id": receipt_id,
            "permit_id": permit_id,
            "wallet_id": wallet_id,
            "key_id": key_id,
            "tool": tool,
            "request_hash": effective_request_hash,
            "response_hash": response_hash,
            "ledger_entry_id": ledger_entry_id,
            "credits_authorized": credits_authorized,
            "credits_charged": credits_charged,
            "outcome": outcome,
            "audit_event_id": audit_event_id,
            "created_at": created_at,
        }
        if reason_code is not None:
            payload["reason_code"] = reason_code
        # Keep historical receipt signatures valid: these additive fields are
        # signed only on receipts that actually carry the new linkage.
        if idempotency_record_id is not None:
            payload["idempotency_record_id"] = idempotency_record_id
        if dispatch_attempt_id is not None:
            payload["dispatch_attempt_id"] = dispatch_attempt_id
        # Signed only when set, so signatures on receipts written before this
        # field existed keep verifying (verify_receipt mirrors this).
        if approval_id is not None:
            payload["approval_id"] = approval_id
        if constraints_evaluated:
            payload["constraints_evaluated"] = constraints_evaluated
        payload.update(action_binding)
        signing_keys = get_signing_key_service()
        if prepared_signing_key_id is None:
            signature, signature_key_id, _ = await signing_keys.sign_payload(payload)
        else:
            if target_session is None:
                raise ReceiptError("receipt_prepared_signing_key_requires_session")
            await signing_keys.validate_prepared_signing_key(
                prepared_signing_key_id,
                session=target_session,
            )
            signature, signature_key_id, _ = signing_keys.sign_payload_with_key_id(
                payload,
                prepared_signing_key_id,
            )

        def build_model() -> ReceiptModel:
            """A fresh ORM instance for this receipt's already-fixed identity.

            The owned-session write below restarts its whole transaction on a
            write conflict, and an instance whose flush failed carries session
            state that must not be replayed into the next attempt. Everything
            the signature covers -- ``receipt_id``, ``created_at``, the payload
            hashes -- was computed once above, so every instance this returns
            describes the identical row.
            """
            return ReceiptModel(
                **action_binding,
                receipt_id=receipt_id,
                idempotency_record_id=idempotency_record_id,
                dispatch_attempt_id=dispatch_attempt_id,
                permit_id=permit_id,
                wallet_id=wallet_id,
                key_id=key_id,
                tool=tool,
                request_hash=effective_request_hash,
                response_hash=response_hash,
                ledger_entry_id=ledger_entry_id,
                credits_authorized=credits_authorized,
                credits_charged=credits_charged,
                outcome=outcome,
                reason_code=reason_code,
                audit_event_id=audit_event_id,
                approval_id=approval_id,
                constraints_evaluated_json=json.dumps(constraints_evaluated)
                if constraints_evaluated
                else None,
                created_at=created_at,
                signature=signature,
                signature_key_id=signature_key_id,
            )

        if target_session is not None:
            model = build_model()
            if idempotency_record_id is None:
                target_session.add(model)
                await target_session.flush()
                return receipt_model_to_response(model)
            bind = target_session.get_bind()
            if bind.dialect.name == "sqlite":
                # Python's sqlite driver can treat a SAVEPOINT as the outermost
                # transaction when no write has started yet; releasing it then
                # survives a later caller rollback. Keep caller-owned SQLite
                # work in the actual outer transaction. PostgreSQL below uses
                # a savepoint for unique-key race recovery.
                target_session.add(model)
                await target_session.flush()
                return receipt_model_to_response(model)
            try:
                # Isolate a unique-key race to a savepoint so the caller's
                # wider finalization transaction remains usable.
                async with target_session.begin_nested():
                    target_session.add(model)
                    await target_session.flush()
            except IntegrityError:
                existing = await existing_response(target_session)
                if existing is None:
                    raise
                return existing
            return receipt_model_to_response(model)

        factory = get_session_factory()

        async def write_once() -> ReceiptResponse:
            """One full owned transaction: re-check, insert, commit.

            Owns and rebuilds its own transaction on every call, which is what
            the restart helper requires. The re-check at the top is also the
            recovery read: an attempt whose COMMIT landed before its
            acknowledgement was lost is found here by the next attempt rather
            than colliding with itself.
            """
            attempt_model = build_model()
            async with factory() as owned_session:
                existing = await existing_response(owned_session)
                if existing is not None:
                    return existing
                owned_session.add(attempt_model)
                try:
                    await owned_session.commit()
                    await owned_session.refresh(attempt_model)
                except Exception:
                    # A driver can lose the commit acknowledgement after the row
                    # is durable. Roll back local state, then recover through the
                    # unique idempotency link before deciding this really failed.
                    try:
                        await owned_session.rollback()
                    except Exception:
                        # Recovery uses a fresh session because this connection may
                        # be exactly what lost the commit acknowledgement.
                        pass
                    if idempotency_record_id is not None:
                        async with factory() as recovery_session:
                            existing = await existing_response(recovery_session)
                        if existing is not None:
                            return existing
                    raise
            return receipt_model_to_response(attempt_model)

        # The receipt is the last write on the governed path, so it is where a
        # burst of concurrent invokes still collides after the debit stops
        # losing. Restarting is safe because the row is deduped by the unique
        # idempotency link and re-checked at the top of every attempt; without
        # this the loss escaped as an unclassified internal_error, which on the
        # money path tells a caller nothing about whether it was charged.
        try:
            return cast(
                ReceiptResponse,
                await run_with_write_conflict_retry(
                    write_once,
                    on_exhausted=lambda exc: ReceiptWriteContendedError(
                        "receipt_write_contended"
                    ),
                ),
            )
        except ReceiptWriteContendedError:
            # One last read before claiming no receipt exists. Attempts 1..N-1
            # get this from the next attempt's re-check, but the final loss has
            # no successor to do it, and the answer decides whether the caller
            # is told a durable receipt is missing. Only the idempotency link
            # can answer: it is unique, and a dispatch-linked receipt always
            # carries one too, because mcp_dispatch_attempts.idempotency_record_id
            # is NOT NULL -- so a second lookup by dispatch_attempt_id could
            # only re-find the same row.
            if idempotency_record_id is not None:
                async with factory() as recovery_session:
                    existing = await existing_response(recovery_session)
                if existing is not None:
                    return existing
            raise

    async def get_receipt(self, receipt_id: str) -> ReceiptResponse | None:
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(ReceiptModel, receipt_id)
            return receipt_model_to_response(model) if model else None

    async def get_receipt_by_ledger_entry_id(
        self, ledger_entry_id: str
    ) -> ReceiptResponse | None:
        """Return an existing receipt for a ledger charge, if one was written."""
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ReceiptModel)
                .where(
                    cast(
                        ColumnElement[bool],
                        ReceiptModel.ledger_entry_id == ledger_entry_id,
                    )
                )
                .limit(1)
            )
            model = result.scalar_one_or_none()
            return receipt_model_to_response(model) if model else None

    async def get_receipt_by_idempotency_record_id(
        self,
        idempotency_record_id: str,
    ) -> ReceiptResponse | None:
        factory = get_session_factory()
        async with factory() as session:
            model = await self._get_by_idempotency_record(
                session,
                idempotency_record_id,
            )
            return receipt_model_to_response(model) if model else None

    async def list_receipts(
        self,
        *,
        permit_id: str | None = None,
        wallet_id: str | None = None,
        tool: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ReceiptResponse], int]:
        stmt = select(ReceiptModel)
        count_stmt = select(func.count()).select_from(ReceiptModel)

        filters: list[ColumnElement[bool]] = []
        if permit_id:
            filters.append(
                cast(ColumnElement[bool], ReceiptModel.permit_id == permit_id)
            )
        if wallet_id:
            filters.append(
                cast(ColumnElement[bool], ReceiptModel.wallet_id == wallet_id)
            )
        if tool:
            filters.append(cast(ColumnElement[bool], ReceiptModel.tool == tool))
        if outcome:
            filters.append(cast(ColumnElement[bool], ReceiptModel.outcome == outcome))

        if filters:
            stmt = stmt.where(*filters)
            count_stmt = count_stmt.where(*filters)

        stmt = (
            stmt.order_by(cast(ColumnElement[Any], ReceiptModel.created_at).desc())
            .limit(limit)
            .offset(offset)
        )

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(stmt)
            total = await session.scalar(count_stmt)
            receipts = [receipt_model_to_response(model) for model in result.scalars()]
            return receipts, int(total or 0)

    async def verify_receipt(
        self,
        receipt_id: str,
    ) -> tuple[bool, str | None, ReceiptResponse | None]:
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(ReceiptModel, receipt_id)
            if not model:
                return False, "receipt_not_found", None
            ok = await self.verify_model(model, session=session)
            return (
                ok,
                None if ok else "receipt_signature_invalid",
                receipt_model_to_response(model),
            )

    async def signing_input_for_model(
        self,
        model: ReceiptModel,
        *,
        session: AsyncSession | None = None,
    ) -> str | None:
        """Return the exact canonical bytes this receipt's signature covers.

        Mirrors :meth:`verify_model` branch for branch, so the exported bytes
        are the ones that actually verify — including for receipts whose
        signature predates the linkage fields. Returns ``None`` when neither
        branch verifies, so a receipt that cannot be proven is never handed
        out as evidence.
        """
        signing_keys = get_signing_key_service()
        current_payload = self._verification_payload(model, include_linkage=True)
        if await signing_keys.verify_payload(
            current_payload,
            signature=model.signature,
            key_id=model.signature_key_id,
            session=session,
        ):
            return canonical_json(current_payload)

        if not await self._has_unambiguous_historical_idempotency_link(
            model,
            session=session,
        ):
            return None
        legacy_payload = self._verification_payload(model, include_linkage=False)
        if await signing_keys.verify_payload(
            legacy_payload,
            signature=model.signature,
            key_id=model.signature_key_id,
            session=session,
        ):
            return canonical_json(legacy_payload)
        return None

    async def signing_input(self, receipt_id: str) -> str | None:
        """Load a receipt and return the canonical bytes its signature covers."""
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(ReceiptModel, receipt_id)
            if model is None:
                return None
            return await self.signing_input_for_model(model, session=session)

    async def verify_model(
        self,
        model: ReceiptModel,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        """Verify current signatures, then the constrained migration fallback."""
        signing_keys = get_signing_key_service()
        current_payload = self._verification_payload(model, include_linkage=True)
        if await signing_keys.verify_payload(
            current_payload,
            signature=model.signature,
            key_id=model.signature_key_id,
            session=session,
        ):
            return True

        # Migration 026 backfilled one unambiguous idempotency link onto some
        # receipts whose original signature predates linkage fields. Do not
        # generalize legacy verification: dispatch-linked receipts and links
        # that are absent, mismatched, or ambiguous must fail closed.
        if not await self._has_unambiguous_historical_idempotency_link(
            model,
            session=session,
        ):
            return False
        legacy_payload = self._verification_payload(model, include_linkage=False)
        return await signing_keys.verify_payload(
            legacy_payload,
            signature=model.signature,
            key_id=model.signature_key_id,
            session=session,
        )


_service: ReceiptService | None = None


def get_receipt_service() -> ReceiptService:
    global _service
    if _service is None:
        _service = ReceiptService()
    return _service
