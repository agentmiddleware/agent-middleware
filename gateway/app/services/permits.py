from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urlparse

from sqlalchemy import case, func, or_, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import get_settings
from app.core.credits import credit_amount_fits_storage
from app.core.resilience import (
    WRITE_CONFLICT_MAX_ATTEMPTS,
    run_with_write_conflict_retry,
)
from app.core.time import to_naive_utc, utc_now
from app.db.database import get_session_factory
from app.db.models import (
    BillingAlertModel,
    McpDispatchAttemptModel,
    PermitModel,
    ReceiptModel,
    WalletModel,
)
from app.schemas.billing import AlertType
from app.schemas.trust import ActionPermitFields, PermitCreateRequest, PermitResponse
from app.services.signing_keys import get_signing_key_service, sha256_hex

logger = logging.getLogger(__name__)


class PermitError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PermitCreationRejectedError(PermitError):
    """Issuance validation rejected before permit signing or persistence.

    Only raise at explicit pre-write validation boundaries. Generic signing,
    database, commit acknowledgement and response failures may have minted a
    permit and must retain their issuance idempotency owner.
    """


class PermitWriteContendedError(PermitError):
    """A guarded permit write lost write conflicts for its whole budget.

    Nothing the operation meant to write is durable:
    ``run_with_write_conflict_retry`` replays a whole transaction per attempt,
    so an exhausted budget leaves the permit row exactly as it was.

    A distinct type rather than a bare ``PermitError`` carrying the reason,
    because the MCP ladders classify contention by exception *type* -- that is
    how ``LedgerWriteContendedError``, ``AuditChainContendedError`` and
    ``ReceiptWriteContendedError`` reach their handlers -- while ``PermitError``
    itself carries a dozen other reasons (``permit_not_found``,
    ``dispatch_attempt_not_found``, ``permit_budget_exceeded``, ...) that must
    keep falling through to the unclassified channel. Matching the base class
    in those ladders would silently reclassify every one of them as retryable
    contention.

    Subclassing keeps every existing ``except PermitError`` handler -- the
    x402 router, the AWI governance path, the ACP bridge, the permits router --
    catching it exactly as before, and ``reason`` still reads
    ``permit_write_contended``, so the surfaces that branch on that string are
    untouched.

    Whether a caller may retry is not this module's call to make: a contended
    permit write on the reserve path, before anything ran, means something
    different than one on the release path after a tool has executed and been
    charged. The router owns that split, the same way it does for a contended
    receipt insert or audit append.
    """

    reason = "permit_write_contended"

    def __init__(self) -> None:
        super().__init__(self.reason)


@dataclass(frozen=True)
class PermitValidation:
    """Verdict on one governed action, with the numbers behind a denial.

    ``reason`` names what failed; ``details`` says by how much. An agent that
    is told only ``permit_budget_exceeded`` can do nothing but retry and fail
    again — the same denial with ``{"required": 50, "remaining": 12}`` tells it
    what permit to ask for. Details describe the caller's own permit, so they
    disclose nothing the permit holder cannot already read.
    """

    allowed: bool
    reason: str | None
    permit: PermitModel | None
    details: dict[str, Any] | None = None


def _num(value: Decimal | None) -> str | None:
    """Render a credit amount as an exact decimal string for a JSON payload."""
    return None if value is None else str(value)


def _stamp(value: datetime | None) -> str | None:
    """Render a naive-UTC column value as an explicit UTC timestamp."""
    return None if value is None else value.replace(microsecond=0).isoformat() + "Z"


# Every ``spent_credits`` mutation is applied as a single guarded UPDATE rather
# than a read-modify-write, so the permit cap is enforced atomically at the row
# level. ``SELECT ... FOR UPDATE`` is a silent no-op on SQLite, so without this
# two concurrent reservations both read the same ``spent_credits``, both pass the
# cap check, and their increments clobber each other (a lost update) — the exact
# over-spend this closes. Under genuine concurrency SQLite's WAL raises a
# transient "database is locked"/"snapshot" conflict on the second writer; we
# retry that with a small backoff. On PostgreSQL the row lock blocks instead of
# raising, so the retry never triggers there.
#
# The guard takes one of two forms, and both are decided by the database rather
# than by this process:
#
# * **Relative** — reservations and releases increment or decrement in place
#   (``spent_credits + amount``), with the cap, the status and the expiry in
#   the statement's own predicate. A row count other than 1 means the write
#   lost, and the caller re-reads to classify *why* before reporting a reason.
# * **Conditional on the observed value** — ``reconcile_budgets`` recomputes an
#   absolute total from receipts, which has no relative form, so it commits
#   only where the stored spend still equals the value that pass read. A
#   concurrent reservation makes that predicate false and the repair is skipped
#   rather than overwriting it. This is optimistic concurrency control: detect
#   the lost race by affected-row count instead of holding a lock.
#
# The rule this encodes: no ``spent_credits`` value may be written from a number
# this process read in an earlier statement without the database re-checking
# that the number still holds.
#: Kept as a named alias so this module's comments above still read as written;
#: the value and the loop now live in app/core/resilience.py, shared with the
#: ledger debit, which loses the same WAL snapshot race for the same reason.
_PERMIT_WRITE_MAX_ATTEMPTS = WRITE_CONFLICT_MAX_ATTEMPTS


def _loads_list(value: str) -> list[str]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


def _loads_dict(value: str | None) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def permit_constraints_snapshot(permit_model: Any) -> dict[str, Any]:
    """Build the permit v2 ``constraints_evaluated`` snapshot for receipt signing.

    This is the single source of the snapshot's byte representation. The live
    invoke path and the crash reconciler both sign receipts containing it, so
    they must format identically — ``aggregate_value_cap`` in particular is
    normalized (``Decimal("10.00")`` → ``"10"``) so a receipt minted during
    crash recovery hashes the same constraints a live receipt would.
    """

    ce: dict[str, Any] = {}
    max_calls = _loads_dict(permit_model.max_calls_per_tool_json or "{}")
    if max_calls:
        ce["max_calls_per_tool"] = max_calls
    if permit_model.aggregate_value_cap is not None:
        ce["aggregate_value_cap"] = format(
            permit_model.aggregate_value_cap.normalize(), "f"
        )
    forbidden = _loads_list(permit_model.forbidden_fields_json or "[]")
    if forbidden:
        ce["forbidden_fields"] = forbidden
    if permit_model.recipient_domain:
        ce["recipient_domain"] = permit_model.recipient_domain
    return ce


def _find_forbidden_field(arguments: Any, forbidden: set[str]) -> str | None:
    """Return the first forbidden key found anywhere in the argument tree.

    Walks nested dicts and lists so a forbidden key cannot be smuggled past
    the check by nesting it below the top level. Comparison is against dict
    keys only (a forbidden name appearing as a string *value* is not a match).
    """
    stack: list[Any] = [arguments]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, child in node.items():
                if key in forbidden:
                    return str(key)
                stack.append(child)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return None


def extract_recipient_identity(target: str) -> str:
    """Reduce one recipient value to the identity the permit binds.

    A URL contributes its hostname; a bare value with no host part (an
    x402 pay_to chain address, a registered origin without a scheme) counts
    as itself. This is the same reduction the upstream MCP path has always
    applied before comparing against the permit's recipient constraint.
    """
    return urlparse(target).hostname or target


def recipient_binding_matches(recipient_domain: str | None, target: str) -> bool:
    """Check one recipient value against a permit's recipient constraint.

    An unset constraint allows everything. Otherwise the target is reduced
    with extract_recipient_identity and must equal the bound value
    exactly. x402 callers pass the demand's pay_to address: x402 demands
    name no resource host, so the payee address is the only recipient the
    demand names, and a permit that binds x402 spending must carry that
    address exactly as the demand presents it.
    """
    if not recipient_domain:
        return True
    return extract_recipient_identity(target) == recipient_domain


def permit_model_to_response(model: PermitModel) -> PermitResponse:
    return PermitResponse(
        **ActionPermitFields.model_validate(
            {name: getattr(model, name) for name in ActionPermitFields.model_fields}
        ).model_dump(),
        permit_id=model.permit_id,
        issuer_wallet_id=model.issuer_wallet_id,
        subject_wallet_id=model.subject_wallet_id,
        subject_key_id=model.subject_key_id,
        scopes=_loads_list(model.scopes_json),
        allowed_tools=_loads_list(model.allowed_tools_json),
        max_credits=model.max_credits,
        spent_credits=model.spent_credits,
        expires_at=model.expires_at,
        nonce=model.nonce,
        status=model.status,
        requires_human_approval=model.requires_human_approval,
        signature=model.signature,
        key_id=model.key_id,
        issued_at=model.issued_at,
        revoked_at=model.revoked_at,
        max_calls_per_tool=_loads_dict(model.max_calls_per_tool_json),
        aggregate_value_cap=model.aggregate_value_cap,
        forbidden_fields=_loads_list(model.forbidden_fields_json or "[]"),
        recipient_domain=model.recipient_domain,
        allow_identical_repeats=model.allow_identical_repeats,
        repeat_window_seconds=model.repeat_window_seconds,
    )


class PermitService:
    async def get_permit(self, permit_id: str) -> PermitResponse | None:
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(PermitModel, permit_id)
            return permit_model_to_response(model) if model else None

    async def list_permits(
        self,
        *,
        wallet_id: str | None = None,
        status: str | None = None,
        subject_key_id: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        expires_after: datetime | None = None,
        expires_before: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[PermitResponse], int]:
        stmt = select(PermitModel)
        count_stmt = select(func.count()).select_from(PermitModel)

        filters: list[ColumnElement[bool]] = []
        if wallet_id:
            filters.append(
                cast(
                    ColumnElement[bool],
                    or_(
                        cast(
                            ColumnElement[bool],
                            PermitModel.issuer_wallet_id == wallet_id,
                        ),
                        cast(
                            ColumnElement[bool],
                            PermitModel.subject_wallet_id == wallet_id,
                        ),
                    ),
                )
            )
        if status:
            filters.append(cast(ColumnElement[bool], PermitModel.status == status))
        if subject_key_id:
            filters.append(
                cast(ColumnElement[bool], PermitModel.subject_key_id == subject_key_id)
            )
        if created_after:
            created_after = to_naive_utc(created_after)
            filters.append(
                cast(ColumnElement[bool], PermitModel.issued_at >= created_after)
            )
        if created_before:
            created_before = to_naive_utc(created_before)
            filters.append(
                cast(ColumnElement[bool], PermitModel.issued_at <= created_before)
            )
        if expires_after:
            expires_after = to_naive_utc(expires_after)
            filters.append(
                cast(ColumnElement[bool], PermitModel.expires_at >= expires_after)
            )
        if expires_before:
            expires_before = to_naive_utc(expires_before)
            filters.append(
                cast(ColumnElement[bool], PermitModel.expires_at <= expires_before)
            )

        if filters:
            stmt = stmt.where(*filters)
            count_stmt = count_stmt.where(*filters)

        stmt = (
            stmt.order_by(cast(ColumnElement[Any], PermitModel.issued_at).desc())
            .limit(limit)
            .offset(offset)
        )

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(stmt)
            total = await session.scalar(count_stmt)
            permits = [permit_model_to_response(model) for model in result.scalars()]
            return permits, int(total or 0)

    async def create_permit(
        self,
        request: PermitCreateRequest,
        subject_key_id: str | None = None,
        permit_id: str | None = None,
    ) -> PermitResponse:
        """Mint a signed permit.

        ``permit_id`` lets a caller that pre-allocated an identifier (the
        permit-request flow reserves one when the human is paged) mint under
        it, so a retried mint collides on the primary key instead of issuing a
        second permit carrying the same authority.
        """
        if any(
            getattr(request, field) is not None
            for field in ActionPermitFields.model_fields
        ):
            raise PermitCreationRejectedError("action_permit_requires_trusted_issuance")
        return await self._persist_permit(request, subject_key_id, permit_id)

    async def _persist_permit(
        self,
        request: PermitCreateRequest,
        subject_key_id: str | None = None,
        permit_id: str | None = None,
    ) -> PermitResponse:
        if request.max_credits.is_finite() and request.max_credits <= Decimal("0"):
            raise PermitCreationRejectedError("max_credits_must_be_positive")
        if not credit_amount_fits_storage(request.max_credits):
            raise PermitCreationRejectedError("max_credits_not_storable")
        if request.aggregate_value_cap is not None and (
            not credit_amount_fits_storage(request.aggregate_value_cap)
            or request.aggregate_value_cap == 0
        ):
            raise PermitCreationRejectedError("aggregate_value_cap_not_storable")
        if (
            request.repeat_window_seconds is not None
            and not get_settings().ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE
        ):
            raise PermitCreationRejectedError("repeat_window_issuance_disabled")
        # Normalize to naive UTC before any comparison, signing, or persistence.
        # Guarantees the signed timestamp and persisted timestamp are identical
        # on every dialect (SQLite, PostgreSQL, asyncpg).
        expires_at = to_naive_utc(request.expires_at)
        now = utc_now()

        if expires_at <= now:
            raise PermitCreationRejectedError("permit_expired_at_creation")

        if request.requires_human_approval:
            # Fail at creation rather than minting a permit every invoke of
            # which would be denied (simulated approvals are refused in
            # production-like environments; real mode needs Sentinel config).
            from app.services.human_approval import human_approval_available

            available, reason = human_approval_available()
            if not available:
                raise PermitCreationRejectedError(
                    reason or "human_approval_not_configured"
                )

        scopes = request.scopes or [
            f"tool:{tool}:invoke" for tool in request.allowed_tools
        ]
        if "billing:charge" not in scopes:
            scopes = [*scopes, "billing:charge"]

        factory = get_session_factory()
        async with factory() as session:
            issuer = await session.get(WalletModel, request.issuer_wallet_id)
            subject = await session.get(WalletModel, request.subject_wallet_id)
            if not issuer:
                raise PermitCreationRejectedError("issuer_wallet_not_found")
            if not subject:
                raise PermitCreationRejectedError("subject_wallet_not_found")
            if subject.balance < request.max_credits:
                raise PermitCreationRejectedError(
                    "permit_budget_exceeds_wallet_balance"
                )

        permit_id = permit_id or f"permit-{uuid.uuid4().hex[:16]}"
        nonce = request.nonce or uuid.uuid4().hex
        # Prefer the explicitly-passed key_id (from auth context) over the
        # request body, so wallet-bound self-service permits show up in
        # /v1/me/permits queries filtered by subject_key_id.
        effective_key_id = request.subject_key_id or subject_key_id
        model = PermitModel(
            permit_id=permit_id,
            issuer_wallet_id=request.issuer_wallet_id,
            subject_wallet_id=request.subject_wallet_id,
            subject_key_id=effective_key_id,
            scopes_json=json.dumps(scopes),
            allowed_tools_json=json.dumps(request.allowed_tools),
            max_credits=request.max_credits,
            expires_at=expires_at,
            nonce=nonce,
            status="active",
            requires_human_approval=request.requires_human_approval,
            signature="",
            key_id="",
            issued_at=now,
            max_calls_per_tool_json=json.dumps(request.max_calls_per_tool)
            if request.max_calls_per_tool
            else None,
            aggregate_value_cap=request.aggregate_value_cap,
            forbidden_fields_json=json.dumps(request.forbidden_fields)
            if request.forbidden_fields
            else None,
            recipient_domain=request.recipient_domain,
            allow_identical_repeats=request.allow_identical_repeats,
            repeat_window_seconds=request.repeat_window_seconds,
            action_contract_version=request.action_contract_version,
            action_payload_hash=request.action_payload_hash,
            action_schema_id=request.action_schema_id,
            action_schema_version=request.action_schema_version,
            action_public_tool_id=request.action_public_tool_id,
            action_upstream_binding_hash=request.action_upstream_binding_hash,
        )
        # Sign the same dict verify reconstructs. Building it twice let a
        # field added on one path only keep verifying in tests that never
        # round-tripped a freshly minted permit.
        signature, key_id, _ = await get_signing_key_service().sign_payload(
            self._unsigned_payload(model)
        )
        model.signature = signature
        model.key_id = key_id
        async with factory() as session:
            session.add(model)
            await session.commit()
            await session.refresh(model)
        return permit_model_to_response(model)

    async def revoke_permit(self, permit_id: str) -> PermitResponse:
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(PermitModel, permit_id)
            if not model:
                raise PermitError("permit_not_found")
            model.status = "revoked"
            model.revoked_at = utc_now()
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return permit_model_to_response(model)

    async def validate_for_action(
        self,
        *,
        permit_id: str,
        wallet_id: str,
        tool_name: str,
        estimated_credits: Decimal,
        key_id: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> PermitValidation:
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(PermitModel, permit_id)
            if not model:
                return PermitValidation(False, "permit_not_found", None)
            return await self._validate_model_for_action(
                session=session,
                model=model,
                wallet_id=wallet_id,
                tool_name=tool_name,
                estimated_credits=estimated_credits,
                key_id=key_id,
                arguments=arguments,
            )

    async def validate_replay_access(
        self,
        *,
        permit_id: str,
        wallet_id: str,
        tool_name: str,
        key_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> PermitValidation:
        """Authorize access to an already-finalized governed invocation.

        Replay deliberately ignores mutable execution constraints such as
        expiry, revocation, and remaining budget: those were enforced before
        the original dispatch, and changing them must not make its evidence
        disappear. Stable wallet/key identity constraints still apply so a
        second key on the same wallet cannot retrieve a result produced under
        a key-bound permit. Tool scope is intentionally not re-evaluated: a
        signed denial for an out-of-scope tool must itself remain replayable.
        """

        async def validate(current_session: AsyncSession) -> PermitValidation:
            model = await current_session.get(PermitModel, permit_id)
            if model is None:
                return PermitValidation(False, "permit_not_found", None)
            if model.subject_wallet_id != wallet_id:
                return PermitValidation(False, "permit_wallet_mismatch", model)
            if model.subject_key_id and model.subject_key_id != key_id:
                return PermitValidation(False, "permit_key_mismatch", model)
            if not await self.verify_signature(model, session=current_session):
                return PermitValidation(False, "permit_signature_invalid", model)
            return PermitValidation(True, None, model)

        if session is not None:
            return await validate(session)
        factory = get_session_factory()
        async with factory() as owned_session:
            return await validate(owned_session)

    async def authorize_and_reserve(
        self,
        *,
        permit_id: str,
        wallet_id: str,
        tool_name: str,
        estimated_credits: Decimal,
        key_id: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> PermitValidation:
        """Atomically authorize an action and reserve its permit budget.

        The permit row remains locked from the final authorization checks
        through the ``spent_credits`` update. A revocation, expiry, scope
        change, key mismatch, signature failure, or concurrent reservation
        therefore cannot slip between a stale read and the budget mutation.
        """
        factory = get_session_factory()

        async def _once() -> PermitValidation:
            async with factory() as session:
                async with session.begin():
                    model = await session.get(
                        PermitModel, permit_id, with_for_update=True
                    )
                    if not model:
                        return PermitValidation(False, "permit_not_found", None)
                    validation = await self._validate_model_for_action(
                        session=session,
                        model=model,
                        wallet_id=wallet_id,
                        tool_name=tool_name,
                        estimated_credits=estimated_credits,
                        key_id=key_id,
                        arguments=arguments,
                    )
                    if not validation.allowed:
                        return validation
                    # Reserve atomically: a single guarded UPDATE enforces the
                    # cap at the row level, so two concurrent reservations cannot
                    # both pass even on SQLite, where the FOR UPDATE above is a
                    # silent no-op. The read-validated numbers are advisory; this
                    # write is the authority.
                    #
                    # Expiry is in the predicate for the same reason the cap is.
                    # An expired permit keeps status="active" in storage (expiry
                    # is a dynamic check, not a stored state), so without this
                    # term a permit could pass the read-time expiry check, cross
                    # expires_at, and still be reserved against -- dispatching
                    # under a just-expired permit wherever the row lock above did
                    # not engage. Naive-UTC comparison matches the column type;
                    # see reconcile_expired_permits for the same pattern.
                    #
                    # For max_calls_per_tool enforcement, we use an optimistic
                    # lock: read current counts, compute new counts, and UPDATE
                    # only WHERE tool_call_counts_json still matches what we read.
                    # A concurrent call that committed between our read and write
                    # causes the UPDATE to match zero rows, and we retry.
                    now = utc_now()

                    # Compute the updated call counter. If max_calls_per_tool is
                    # set for this tool, the UPDATE will atomically increment it
                    # via optimistic concurrency control (CAS on the JSON column).
                    max_calls_config = _loads_dict(
                        model.max_calls_per_tool_json or "{}"
                    )
                    call_limit = max_calls_config.get(tool_name)
                    original_counts_json = model.tool_call_counts_json
                    updated_counts_json = None
                    if call_limit is not None and type(call_limit) is int:
                        current_counts = _loads_dict(original_counts_json or "{}")
                        current_tool_count = current_counts.get(tool_name, 0)
                        if not isinstance(current_tool_count, int):
                            current_tool_count = 0
                        new_tool_count = current_tool_count + 1
                        if new_tool_count > call_limit:
                            # This attempt would exceed the limit. Deny without
                            # mutating anything.
                            return PermitValidation(
                                False,
                                "permit_max_calls_exceeded",
                                model,
                                {
                                    "tool": tool_name,
                                    "limit": call_limit,
                                    "calls_made": current_tool_count,
                                },
                            )
                        updated_counts = dict(current_counts)
                        updated_counts[tool_name] = new_tool_count
                        updated_counts_json = json.dumps(updated_counts)

                    # Build the UPDATE values and WHERE predicates.
                    update_values: dict[str, Any] = {
                        "spent_credits": PermitModel.spent_credits + estimated_credits,
                        "updated_at": now,
                    }
                    where_conditions: list[ColumnElement[bool]] = [
                        cast(ColumnElement[bool], PermitModel.permit_id == permit_id),
                        cast(ColumnElement[bool], PermitModel.status == "active"),
                        cast(ColumnElement[bool], PermitModel.expires_at > now),
                        cast(
                            ColumnElement[bool],
                            PermitModel.spent_credits + estimated_credits
                            <= PermitModel.max_credits,
                        ),
                    ]
                    # aggregate_value_cap is enforced in the same guarded write
                    # against spent_credits plus any receipt history that
                    # spent_credits no longer reflects, so an in-flight
                    # reservation that has not yet produced a receipt already
                    # counts toward the cap and a settled charge whose
                    # reservation was released is never forgotten. The
                    # receipt-sum check in _validate_model_for_action is the
                    # advisory read; this predicate is the authority, and it
                    # is what holds under concurrency where the row lock is a
                    # no-op.
                    aggregate_cap = model.aggregate_value_cap
                    floor_excess = Decimal("0")
                    if aggregate_cap is not None:
                        floor_excess = await self._aggregate_cap_floor_excess(
                            session, model
                        )
                        where_conditions.append(
                            self._aggregate_cap_predicate(
                                floor_excess=floor_excess,
                                amount=estimated_credits,
                            )
                        )

                    if updated_counts_json is not None:
                        # Optimistic lock: only UPDATE if tool_call_counts_json
                        # still equals the value we read. If another transaction
                        # committed a count increment between our read and this
                        # write, the WHERE won't match and we'll retry.
                        update_values["tool_call_counts_json"] = updated_counts_json
                        if original_counts_json is None:
                            where_conditions.append(
                                cast(
                                    ColumnElement[bool],
                                    cast(Any, PermitModel.tool_call_counts_json).is_(
                                        None
                                    ),
                                )
                            )
                        else:
                            where_conditions.append(
                                cast(
                                    ColumnElement[bool],
                                    PermitModel.tool_call_counts_json
                                    == original_counts_json,
                                )
                            )

                    reserved = await session.execute(
                        sa_update(PermitModel)
                        .where(*where_conditions)
                        .values(**update_values)
                        .execution_options(synchronize_session=False)
                    )
                    if (cast(Any, reserved).rowcount or 0) != 1:
                        # A concurrent reservation consumed the remaining budget,
                        # flipped the status, or (when max_calls_per_tool is set)
                        # incremented the call counter, breaking the optimistic
                        # lock. Re-read to distinguish denial from contention.
                        await session.refresh(model)
                        if model.status != "active":
                            return PermitValidation(
                                False,
                                f"permit_{model.status}",
                                model,
                                {
                                    "status": model.status,
                                    "revoked_at": _stamp(model.revoked_at),
                                },
                            )
                        # Reachable now that expiry is in the predicate above.
                        # Classified before budget so a permit that expired
                        # mid-flight is not misreported as out of money.
                        expired_at = to_naive_utc(model.expires_at)
                        if expired_at <= now:
                            return PermitValidation(
                                False,
                                "permit_expired",
                                model,
                                {
                                    "expired_at": _stamp(expired_at),
                                    "checked_at": _stamp(now),
                                },
                            )
                        # Re-check max_calls_per_tool; a concurrent call may have
                        # incremented the counter between our pre-check and the
                        # failed optimistic UPDATE.
                        if call_limit is not None and type(call_limit) is int:
                            refreshed_counts = _loads_dict(
                                model.tool_call_counts_json or "{}"
                            )
                            refreshed_count = refreshed_counts.get(tool_name, 0)
                            if not isinstance(refreshed_count, int):
                                refreshed_count = 0
                            if refreshed_count >= call_limit:
                                return PermitValidation(
                                    False,
                                    "permit_max_calls_exceeded",
                                    model,
                                    {
                                        "tool": tool_name,
                                        "limit": call_limit,
                                        "calls_made": refreshed_count,
                                    },
                                )
                        # Classified before max_credits: a permit whose cap is
                        # below its budget is out of delegated value, not out
                        # of money, and the two reasons send the holder to
                        # different remedies.
                        if (
                            aggregate_cap is not None
                            and model.spent_credits + floor_excess + estimated_credits
                            > aggregate_cap
                        ):
                            return PermitValidation(
                                False,
                                "permit_aggregate_value_cap_exceeded",
                                model,
                                self._aggregate_cap_details(
                                    model,
                                    estimated_credits=estimated_credits,
                                    total_charged=None,
                                    floor_excess=floor_excess,
                                ),
                            )
                        if model.spent_credits + estimated_credits > model.max_credits:
                            return PermitValidation(
                                False,
                                "permit_budget_exceeded",
                                model,
                                {
                                    "required_credits": _num(estimated_credits),
                                    "remaining_credits": _num(
                                        model.max_credits - model.spent_credits
                                    ),
                                    "spent_credits": _num(model.spent_credits),
                                    "max_credits": _num(model.max_credits),
                                },
                            )
                        # A stale call counter can lose its comparison while
                        # authority remains. Restart the whole reservation;
                        # no debit or invocation has occurred at this boundary.
                        raise PermitWriteContendedError()
                    # Reflect the committed reservation on the returned model.
                    await session.refresh(model)
                return validation

        return await self._run_with_write_retry(_once)

    async def _run_with_write_retry(self, operation):
        """Run one full-transaction DB operation, restarting it on transient
        SQLite write conflicts. The mechanism and the reason PostgreSQL never
        needs it are documented on the shared helper; this keeps the permit's
        own reason code, which callers match on."""
        return await run_with_write_conflict_retry(
            operation,
            max_attempts=_PERMIT_WRITE_MAX_ATTEMPTS,
            on_exhausted=lambda exc: PermitWriteContendedError(),
            restart_on=lambda exc: isinstance(exc, PermitWriteContendedError),
        )

    async def _validate_model_for_action(
        self,
        *,
        session: AsyncSession | None = None,
        model: PermitModel,
        wallet_id: str,
        tool_name: str,
        estimated_credits: Decimal,
        key_id: str | None,
        arguments: dict[str, Any] | None = None,
    ) -> PermitValidation:
        now = utc_now()
        if model.status != "active":
            return PermitValidation(
                False,
                f"permit_{model.status}",
                model,
                {
                    "status": model.status,
                    "revoked_at": _stamp(model.revoked_at),
                },
            )
        expires_at = to_naive_utc(model.expires_at)
        if expires_at <= now:
            return PermitValidation(
                False,
                "permit_expired",
                model,
                {
                    "expired_at": _stamp(expires_at),
                    "checked_at": _stamp(now),
                },
            )
        # Binding mismatches carry no values: the caller failed to prove it is
        # the subject, so telling it which wallet or key the permit is bound to
        # would answer a question it has not earned.
        if model.subject_wallet_id != wallet_id:
            return PermitValidation(
                False,
                "permit_wallet_mismatch",
                model,
                {"bound_to": "subject_wallet_id"},
            )
        if model.subject_key_id and model.subject_key_id != key_id:
            return PermitValidation(
                False, "permit_key_mismatch", model, {"bound_to": "subject_key_id"}
            )
        allowed_tools = _loads_list(model.allowed_tools_json)
        if allowed_tools and tool_name not in allowed_tools:
            return PermitValidation(
                False,
                "permit_tool_not_allowed",
                model,
                {"requested_tool": tool_name, "allowed_tools": allowed_tools},
            )
        scopes = set(_loads_list(model.scopes_json))
        required_scope = f"tool:{tool_name}:invoke"
        if required_scope not in scopes or "billing:charge" not in scopes:
            required = [required_scope, "billing:charge"]
            return PermitValidation(
                False,
                "permit_scope_missing",
                model,
                {
                    "required_scopes": required,
                    "missing_scopes": [s for s in required if s not in scopes],
                },
            )
        if model.spent_credits + estimated_credits > model.max_credits:
            return PermitValidation(
                False,
                "permit_budget_exceeded",
                model,
                {
                    "required_credits": _num(estimated_credits),
                    "remaining_credits": _num(model.max_credits - model.spent_credits),
                    "spent_credits": _num(model.spent_credits),
                    "max_credits": _num(model.max_credits),
                },
            )

        # Permit schema v2 constraint checks
        # 1. max_calls_per_tool
        max_calls = _loads_dict(model.max_calls_per_tool_json or "{}")
        if max_calls and tool_name in max_calls:
            limit = max_calls[tool_name]
            # Require a genuine JSON integer. `int()` would silently coerce
            # 2.5 -> 2 or "3" -> 3 (and bool is an int subclass), turning a
            # malformed constraint into a permissive one; fail closed instead
            # of raising a 500 on the governed path.
            if type(limit) is not int:
                return PermitValidation(
                    False,
                    "permit_max_calls_exceeded",
                    model,
                    {"tool": tool_name, "limit": "malformed"},
                )
            # Read the atomic call counter. Missing or null defaults to empty dict.
            call_counts = _loads_dict(model.tool_call_counts_json or "{}")
            current_count = call_counts.get(tool_name, 0)
            if not isinstance(current_count, int):
                current_count = 0
            if current_count >= limit:
                return PermitValidation(
                    False,
                    "permit_max_calls_exceeded",
                    model,
                    {"tool": tool_name, "limit": limit, "calls_made": current_count},
                )

        # 2. aggregate_value_cap
        if model.aggregate_value_cap is not None:
            total_charged = await self._sum_permit_charges(
                model.permit_id,
                session=session,
            )
            # Reserved authority counts, not only receipted charges. A call
            # that has reserved budget but not yet written its receipt is in
            # flight against this cap; summing receipts alone let a second
            # distinct-key call pass while the first had not finished. The
            # receipt total is kept as a floor so a settled charge whose
            # reservation was later released is never overlooked. This read
            # is advisory; the guarded UPDATE in authorize_and_reserve and
            # reserve_budget is the authority for the same arithmetic.
            floor_excess = max(total_charged - model.spent_credits, Decimal("0"))
            if (
                model.spent_credits + floor_excess + estimated_credits
                > model.aggregate_value_cap
            ):
                return PermitValidation(
                    False,
                    "permit_aggregate_value_cap_exceeded",
                    model,
                    self._aggregate_cap_details(
                        model,
                        estimated_credits=estimated_credits,
                        total_charged=total_charged,
                        floor_excess=floor_excess,
                    ),
                )

        # 3. forbidden_fields
        forbidden = _loads_list(model.forbidden_fields_json or "[]")
        if forbidden and arguments:
            hit = _find_forbidden_field(arguments, set(forbidden))
            if hit is not None:
                # The field NAME is echoed, never its value: the value is the
                # thing the permit forbade carrying.
                return PermitValidation(
                    False,
                    f"permit_forbidden_field:{hit}",
                    model,
                    {"field": hit, "forbidden_fields": forbidden},
                )

        if not await self.verify_signature(model, session=session):
            return PermitValidation(
                False,
                "permit_signature_invalid",
                model,
                {"key_id": model.key_id},
            )
        return PermitValidation(True, None, model)

    @staticmethod
    def _aggregate_cap_details(
        model: PermitModel,
        *,
        estimated_credits: Decimal,
        total_charged: Decimal | None,
        floor_excess: Decimal = Decimal("0"),
    ) -> dict[str, Any]:
        """Denial details for ``permit_aggregate_value_cap_exceeded``.

        ``reserved_credits`` is the authority the cap is enforced against:
        ``spent_credits`` (receipted charges plus in-flight reservations)
        raised to the receipt total wherever history exceeds it.
        ``charged_to_date`` is the receipt total when the caller has it.
        """
        assert model.aggregate_value_cap is not None
        details: dict[str, Any] = {
            "required_credits": _num(estimated_credits),
            "reserved_credits": _num(model.spent_credits + floor_excess),
            "aggregate_value_cap": _num(model.aggregate_value_cap),
        }
        if total_charged is not None:
            details["charged_to_date"] = _num(total_charged)
        return details

    async def _aggregate_cap_floor_excess(
        self,
        session: AsyncSession,
        model: PermitModel,
    ) -> Decimal:
        """Receipt history that ``spent_credits`` no longer reflects.

        ``spent_credits`` can fall below the receipt total: a charged call
        whose reservation was later handed back still has its receipt, and
        the reconciler rebuilds ``spent_credits`` from a subset of outcomes.
        The cap is a bound on cumulative value, so that history must still
        count. Read inside the reservation transaction, the excess is folded
        into the guarded UPDATE as a constant. A stale ``spent_credits``
        read (SQLite, where the row lock is a no-op) can only make the
        excess larger, never smaller, so the predicate is conservative
        under contention from the same snapshot. A later transaction that
        observes ``spent_credits`` already covering the receipt total computes
        a zero floor and is then bounded by ``spent_credits + amount``
        against the cap — the same arithmetic as a settled receipt that
        was never released.
        """
        total_charged = await self._sum_permit_charges(model.permit_id, session=session)
        return max(total_charged - model.spent_credits, Decimal("0"))

    @staticmethod
    def _aggregate_cap_predicate(
        *,
        floor_excess: Decimal,
        amount: Decimal,
    ) -> ColumnElement[bool]:
        """WHERE term enforcing ``aggregate_value_cap`` inside a reservation."""
        return cast(
            ColumnElement[bool],
            PermitModel.spent_credits + floor_excess + amount
            <= cast(Any, PermitModel.aggregate_value_cap),
        )

    async def _sum_permit_charges(
        self,
        permit_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> Decimal:
        """Sum credits_charged across all receipts for this permit."""
        if session is not None:
            result = await session.execute(
                select(func.sum(ReceiptModel.credits_charged)).where(
                    cast(ColumnElement[bool], ReceiptModel.permit_id == permit_id),
                )
            )
            total = result.scalar()
            return Decimal(str(total)) if total is not None else Decimal("0")
        factory = get_session_factory()
        async with factory() as owned_session:
            result = await owned_session.execute(
                select(func.sum(ReceiptModel.credits_charged)).where(
                    cast(ColumnElement[bool], ReceiptModel.permit_id == permit_id),
                )
            )
            total = result.scalar()
            return Decimal(str(total)) if total is not None else Decimal("0")

    async def reserve_budget(self, permit_id: str, amount: Decimal) -> None:
        factory = get_session_factory()

        async def _once() -> None:
            async with factory() as session:
                async with session.begin():
                    now = utc_now()
                    # Locked read first: aggregate_value_cap needs the receipt
                    # floor computed inside this transaction (see
                    # _aggregate_cap_floor_excess). A missing permit is
                    # classified here, before any write is attempted.
                    model = await session.get(
                        PermitModel, permit_id, with_for_update=True
                    )
                    if model is None:
                        raise PermitError("permit_not_found")
                    # Atomic guarded reserve (see authorize_and_reserve): the cap
                    # *and the expiry* are enforced by the WHERE clause, not a
                    # read-then-write. The expiry term matters for the same
                    # reason it does in authorize_and_reserve: an expired permit
                    # keeps status="active" in storage because the sweeper flips
                    # it lazily, so without this term a second reservation path
                    # still spends against a permit that has crossed expires_at.
                    where_conditions: list[ColumnElement[bool]] = [
                        cast(
                            ColumnElement[bool],
                            PermitModel.permit_id == permit_id,
                        ),
                        cast(
                            ColumnElement[bool],
                            PermitModel.status == "active",
                        ),
                        cast(
                            ColumnElement[bool],
                            PermitModel.expires_at > now,
                        ),
                        cast(
                            ColumnElement[bool],
                            PermitModel.spent_credits + amount
                            <= PermitModel.max_credits,
                        ),
                    ]
                    # Same aggregate_value_cap authority as authorize_and_reserve:
                    # this is the reservation the AWI governed path takes, and a
                    # cap that only one reservation path honored would be no cap.
                    aggregate_cap = model.aggregate_value_cap
                    floor_excess = Decimal("0")
                    if aggregate_cap is not None:
                        floor_excess = await self._aggregate_cap_floor_excess(
                            session, model
                        )
                        where_conditions.append(
                            self._aggregate_cap_predicate(
                                floor_excess=floor_excess,
                                amount=amount,
                            )
                        )
                    reserved = await session.execute(
                        sa_update(PermitModel)
                        .where(*where_conditions)
                        .values(
                            spent_credits=PermitModel.spent_credits + amount,
                            updated_at=now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if (cast(Any, reserved).rowcount or 0) != 1:
                        # Classify in the same order as authorize_and_reserve:
                        # status, then expiry, then aggregate cap, then budget.
                        # Reporting an expired or revoked permit as "out of
                        # money" sends the operator to top up a permit that more
                        # money cannot revive.
                        await session.refresh(model)
                        if model.status != "active":
                            raise PermitError(f"permit_{model.status}")
                        if to_naive_utc(model.expires_at) <= now:
                            raise PermitError("permit_expired")
                        if (
                            aggregate_cap is not None
                            and model.spent_credits + floor_excess + amount
                            > aggregate_cap
                        ):
                            raise PermitError("permit_aggregate_value_cap_exceeded")
                        raise PermitError("permit_budget_exceeded")

                    # Budget percentage alerts, recomputed from the committed
                    # ``spent_credits`` so a concurrent reservation cannot skew
                    # the threshold arithmetic.
                    await session.refresh(model)
                    if model.max_credits > 0:
                        pct = (model.spent_credits / model.max_credits) * 100
                        thresholds = [
                            (
                                Decimal("100"),
                                "critical",
                                AlertType.PERMIT_BUDGET_EXHAUSTED,
                            ),
                            (Decimal("90"), "warning", AlertType.PERMIT_BUDGET_90PCT),
                            (Decimal("80"), "info", AlertType.PERMIT_BUDGET_80PCT),
                        ]
                        for threshold, severity, alert_type in thresholds:
                            if pct >= threshold:
                                # Only alert on the transition across a threshold.
                                prior_pct = (
                                    (model.spent_credits - amount) / model.max_credits
                                ) * 100
                                if prior_pct < threshold:
                                    session.add(
                                        BillingAlertModel(
                                            alert_id=f"alt-{uuid.uuid4().hex[:12]}",
                                            wallet_id=model.subject_wallet_id,
                                            alert_type=alert_type.value,
                                            threshold_amount=threshold,
                                            current_balance=model.max_credits
                                            - model.spent_credits,
                                            message=(
                                                f"Permit {permit_id}: {pct:.0f}% of "
                                                f"{model.max_credits} credits spent."
                                            ),
                                            severity=severity,
                                        )
                                    )
                                break  # Only fire the highest crossed threshold

        await self._run_with_write_retry(_once)

    async def release_budget(self, permit_id: str, amount: Decimal) -> None:
        factory = get_session_factory()

        async def _once() -> None:
            async with factory() as session:
                async with session.begin():
                    # Atomic clamped decrement: a single UPDATE so a concurrent
                    # reservation on the same permit cannot be lost to a
                    # read-modify-write refund. A missing permit is a no-op.
                    await session.execute(
                        sa_update(PermitModel)
                        .where(
                            cast(
                                ColumnElement[bool],
                                PermitModel.permit_id == permit_id,
                            )
                        )
                        .values(
                            spent_credits=case(
                                (
                                    cast(
                                        ColumnElement[bool],
                                        PermitModel.spent_credits - amount
                                        < Decimal("0"),
                                    ),
                                    Decimal("0"),
                                ),
                                else_=PermitModel.spent_credits - amount,
                            ),
                            updated_at=utc_now(),
                        )
                        .execution_options(synchronize_session=False)
                    )

        await self._run_with_write_retry(_once)

    async def record_absorbed_release_drift(
        self,
        *,
        permit_id: str,
        amount: Decimal,
        site: str,
    ) -> bool:
        """Record budget a post-effects release could not hand back.

        The MCP routers absorb a ``PermitWriteContendedError`` raised by
        ``release_budget``/``release_dispatch_budget_once`` on paths where the
        receipt is written *after* the release, because propagating would
        destroy the governance artifact for a call that ran. The wallet is
        already whole -- the refund succeeded -- but the permit keeps
        ``amount`` reserved against a call that was refunded, and
        ``reconcile_budgets`` will not repair that while the permit is live.
        Until now the only trace was a log line.

        This is deliberately best-effort and **never raises**. It is called
        from inside the absorbing except block, immediately before the receipt
        write it exists to protect; an observability write that could fail the
        request would re-create precisely the failure that absorb prevents. A
        lost alert costs visibility, and ``reconcile_budgets`` still reports
        the drift from the permit row itself. Hence the bare except here,
        against the narrow one at the call site: there, only a contended write
        is a known-reconcilable loss, while here *nothing* may escape.

        Returns whether the row was written.
        """
        from app.db.models import BillingAlertModel

        try:
            factory = get_session_factory()
            async with factory() as session:
                async with session.begin():
                    model = await session.get(PermitModel, permit_id)
                    if model is None:
                        return False
                    session.add(
                        BillingAlertModel(
                            alert_id=f"alt-{uuid.uuid4().hex[:12]}",
                            wallet_id=model.subject_wallet_id,
                            # billing_alerts is keyed on wallet_id and has no
                            # permit column, so the permit travels in the
                            # message the way the budget-threshold alerts above
                            # already do. threshold_amount is the only numeric
                            # column that fits the stranded reservation; it is
                            # not a threshold, and the alert_type is what tells
                            # a reader which reading applies.
                            #
                            # From the public enum, not a bare string: every
                            # alert read converts stored rows through
                            # AlertType, so a type it does not name would not
                            # merely hide this row -- it would fail the whole
                            # listing for the wallet for as long as the row
                            # exists, turning a visibility aid into an outage.
                            alert_type=AlertType.PERMIT_RELEASE_CONTENDED.value,
                            threshold_amount=amount,
                            current_balance=model.max_credits - model.spent_credits,
                            message=(
                                f"Permit {permit_id}: {amount} credits stayed "
                                f"reserved after a contended release at {site}; "
                                f"spent_credits is inflated until the permit "
                                f"expires."
                            ),
                            severity="warning",
                        )
                    )
            return True
        except Exception:
            logger.exception(
                "permit_release_drift_alert_failed",
                extra={"permit_id": permit_id, "site": site},
            )
            return False

    async def release_tool_call(self, permit_id: str, tool_name: str) -> None:
        """Give back one ``max_calls_per_tool`` use consumed by a reservation.

        Compensation partner to :meth:`release_budget`: ``authorize_and_reserve``
        increments the per-tool call counter atomically with the budget
        reservation, so an action that fails after reserving must release both
        or a capped permit's legitimate retry is denied
        ``permit_max_calls_exceeded`` with no receipt behind the consumed use.
        Uses the same optimistic CAS on ``tool_call_counts_json`` as the
        reserve path so a concurrent reservation's increment is never lost to
        this refund; clamped at zero; a missing permit, absent counter, or
        persistently contended CAS is a no-op (the counter then stays
        conservatively high — never low).
        """
        factory = get_session_factory()

        async def _once() -> None:
            for _ in range(5):
                async with factory() as session:
                    async with session.begin():
                        model = await session.get(
                            PermitModel, permit_id, with_for_update=True
                        )
                        if model is None:
                            return
                        original_counts_json = model.tool_call_counts_json
                        counts = _loads_dict(original_counts_json or "{}")
                        current = counts.get(tool_name, 0)
                        # type() not isinstance(): bool subclasses int, and a
                        # malformed stored counter of `true` must not be
                        # coerced into a decrementable number (same rule the
                        # reserve path applies to the configured limit).
                        if type(current) is not int or current <= 0:
                            return
                        updated = dict(counts)
                        updated[tool_name] = current - 1
                        result = await session.execute(
                            sa_update(PermitModel)
                            .where(
                                cast(
                                    ColumnElement[bool],
                                    PermitModel.permit_id == permit_id,
                                ),
                                cast(
                                    ColumnElement[bool],
                                    PermitModel.tool_call_counts_json
                                    == original_counts_json,
                                ),
                            )
                            .values(
                                tool_call_counts_json=json.dumps(updated),
                                updated_at=utc_now(),
                            )
                            .execution_options(synchronize_session=False)
                        )
                        if (cast(Any, result).rowcount or 0) == 1:
                            return
                # CAS miss: a concurrent reservation moved the counter between
                # our read and write (only possible where the row lock above is
                # a no-op, i.e. SQLite). Re-read and try again.
            # Exhausted retries: the counter stays conservatively high, which
            # can deny a legitimate retry with permit_max_calls_exceeded.
            # Surface it the way reconcile_budgets surfaces its skips so a
            # permit under sustained contention is visible to an operator.
            logger.info(
                "permit_tool_call_release_contended permit_id=%s tool=%s",
                permit_id,
                tool_name,
            )

        await self._run_with_write_retry(_once)

    async def release_dispatch_budget_once(self, attempt_id: str) -> bool:
        """Release one remote attempt's reservation exactly once.

        The permit mutation and attempt checkpoint share one transaction. This
        closes the crash window that exists when a plain ``release_budget``
        call succeeds but the caller dies before recording that it succeeded.
        Returns ``True`` only for the transaction that performed the release.
        """
        factory = get_session_factory()

        async def _once() -> bool:
            async with factory() as session:
                async with session.begin():
                    attempt = await session.get(McpDispatchAttemptModel, attempt_id)
                    if attempt is None:
                        raise PermitError("dispatch_attempt_not_found")
                    # Only a terminal returned_error attempt has a reservation
                    # to give back. Releasing budget for a prepared or
                    # dispatched attempt frees credits that attempt may still
                    # go on to spend, so the cap would be enforced against a
                    # reservation that no longer exists. This pre-check is the
                    # contract; the guarded claim below is the once-only gate.
                    if attempt.state != "returned_error":
                        raise PermitError("dispatch_budget_release_state_invalid")
                    if attempt.budget_released_at is not None:
                        return False
                    now = utc_now()
                    # Claim the release atomically before touching the permit.
                    # The read above is guarded by a row lock, but SQLAlchemy
                    # silently drops FOR UPDATE on engines that do not support
                    # it (SQLite), so on those two concurrent callers would both
                    # observe budget_released_at IS NULL and both decrement.
                    # This guarded UPDATE is the once-only gate on every engine:
                    # exactly one caller can flip NULL -> now, and only that
                    # caller proceeds to release the budget.
                    claimed = await session.execute(
                        sa_update(McpDispatchAttemptModel)
                        .where(
                            cast(
                                ColumnElement[bool],
                                McpDispatchAttemptModel.attempt_id == attempt_id,
                            ),
                            cast(
                                ColumnElement[bool],
                                cast(
                                    Any, McpDispatchAttemptModel.budget_released_at
                                ).is_(None),
                            ),
                        )
                        .values(budget_released_at=now, updated_at=now)
                        .execution_options(synchronize_session=False)
                    )
                    if (cast(Any, claimed).rowcount or 0) != 1:
                        # Another caller claimed it first; its transaction owns
                        # the single decrement.
                        return False
                    # If this attempt holds a call slot, release it by decrementing
                    # the tool_call_counts_json counter. This happens in the same
                    # transaction as the budget release, so it's once-only.
                    permit_update_values: dict[str, Any] = {
                        "spent_credits": case(
                            (
                                cast(
                                    ColumnElement[bool],
                                    PermitModel.spent_credits
                                    - attempt.credits_authorized
                                    < Decimal("0"),
                                ),
                                Decimal("0"),
                            ),
                            else_=PermitModel.spent_credits
                            - attempt.credits_authorized,
                        ),
                        "updated_at": now,
                    }

                    original_counts_json = None
                    if attempt.call_slot_reserved:
                        # Only release call slot if attempt never dispatched.
                        # A dispatched attempt consumed its slot even if it later
                        # errored and was refunded - the slot was used.
                        if attempt.dispatched_at is None:
                            # Pre-dispatch failure: release the call slot
                            permit = await session.get(PermitModel, attempt.permit_id)
                            if permit is not None:
                                original_counts_json = permit.tool_call_counts_json
                                current_counts = _loads_dict(
                                    original_counts_json or "{}"
                                )
                                tool_name = attempt.public_tool_id
                                if tool_name in current_counts:
                                    current_count = current_counts[tool_name]
                                    if (
                                        isinstance(current_count, int)
                                        and current_count > 0
                                    ):
                                        updated_counts = dict(current_counts)
                                        updated_counts[tool_name] = current_count - 1
                                        permit_update_values[
                                            "tool_call_counts_json"
                                        ] = json.dumps(updated_counts)

                    # Atomic clamped decrement so a concurrent reservation on the
                    # same permit is not clobbered by a read-modify-write here.
                    # Add CAS on tool_call_counts_json when releasing a slot to
                    # prevent concurrent releases from clobbering each other.
                    where_conditions = [
                        cast(
                            ColumnElement[bool],
                            PermitModel.permit_id == attempt.permit_id,
                        )
                    ]
                    if (
                        attempt.call_slot_reserved
                        and "tool_call_counts_json" in permit_update_values
                    ):
                        # Add optimistic lock: only succeed if counts haven't changed
                        if original_counts_json is None:
                            where_conditions.append(
                                cast(
                                    ColumnElement[bool],
                                    cast(Any, PermitModel.tool_call_counts_json).is_(
                                        None
                                    ),
                                )
                            )
                        else:
                            where_conditions.append(
                                cast(
                                    ColumnElement[bool],
                                    PermitModel.tool_call_counts_json
                                    == original_counts_json,
                                )
                            )

                    released = await session.execute(
                        sa_update(PermitModel)
                        .where(*where_conditions)
                        .values(**permit_update_values)
                        .execution_options(synchronize_session=False)
                    )
                    if (cast(Any, released).rowcount or 0) == 0:
                        # CAS failed - either permit doesn't exist or counts changed.
                        # Check if permit exists to distinguish the cases.
                        check_permit = await session.get(PermitModel, attempt.permit_id)
                        if check_permit is None:
                            raise PermitError("permit_not_found")
                        # Permit exists but CAS failed - counts changed concurrently.
                        # Raise a retryable error so _run_with_write_retry retries.
                        raise PermitWriteContendedError()
                    # budget_released_at was already set by the guarded claim
                    # above; refresh the identity-mapped instance so callers
                    # holding it observe the committed value.
                    attempt.budget_released_at = now
                    attempt.updated_at = now
                    session.add(attempt)
                return True

        return await self._run_with_write_retry(_once)

    async def _consumed_credits(self, session: Any, permit_id: str) -> Decimal:
        """Credits a permit's receipts prove it actually consumed.

        The ground truth ``spent_credits`` is reconciled against. A
        ``failed_unrefunded`` receipt counts as consumed unless its refund
        is proven complete -- an exactly-matching refund ledger entry *and*
        an idempotency record carrying the resolved reconciliation state --
        so a half-finished refund is never credited back twice.

        Read-only: it issues no writes and takes no locks, which is what
        lets ``reconcile_budgets`` reuse it to *report* drift on live
        permits it must not touch.
        """
        from app.db.models import (
            IdempotencyRecordModel,
            LedgerEntryModel,
            ReceiptModel,
        )

        receipt_rows = (
            await session.execute(
                select(
                    cast(Any, ReceiptModel.receipt_id),
                    cast(Any, ReceiptModel.outcome),
                    cast(Any, ReceiptModel.credits_charged),
                    cast(Any, ReceiptModel.ledger_entry_id),
                    cast(Any, ReceiptModel.wallet_id),
                ).where(
                    cast(
                        ColumnElement[bool],
                        ReceiptModel.permit_id == permit_id,
                    ),
                    cast(
                        ColumnElement[bool],
                        cast(Any, ReceiptModel.outcome).in_(
                            [
                                "success",
                                "delivery_uncertain",
                                "response_rejected",
                                "failed_unrefunded",
                            ]
                        ),
                    ),
                )
            )
        ).all()
        pending_ledger_ids = [
            ledger_entry_id
            for (
                _receipt_id,
                outcome,
                _credits,
                ledger_entry_id,
                _wallet_id,
            ) in receipt_rows
            if outcome == "failed_unrefunded" and ledger_entry_id is not None
        ]
        exact_refunded_ledger_ids: set[str] = set()
        if pending_ledger_ids:
            refund_rows = (
                await session.execute(
                    select(
                        cast(Any, LedgerEntryModel.entry_id),
                        cast(Any, LedgerEntryModel.wallet_id),
                        cast(Any, LedgerEntryModel.amount),
                        cast(Any, LedgerEntryModel.correlation_id),
                    ).where(
                        cast(
                            ColumnElement[bool],
                            LedgerEntryModel.action == "refund",
                        ),
                        cast(
                            ColumnElement[bool],
                            cast(
                                Any,
                                LedgerEntryModel.correlation_id,
                            ).in_(pending_ledger_ids),
                        ),
                    )
                )
            ).all()
            failed_by_ledger = {
                ledger_entry_id: (wallet_id, Decimal(str(credits)))
                for (
                    _receipt_id,
                    outcome,
                    credits,
                    ledger_entry_id,
                    wallet_id,
                ) in receipt_rows
                if outcome == "failed_unrefunded" and ledger_entry_id is not None
            }
            exact_refunded_ledger_ids = {
                correlation_id
                for entry_id, wallet_id, amount, correlation_id in refund_rows
                if correlation_id in failed_by_ledger
                and entry_id == f"refund-{correlation_id}"
                and wallet_id == failed_by_ledger[correlation_id][0]
                and Decimal(str(amount)) == failed_by_ledger[correlation_id][1]
            }
        failed_receipt_by_id = {
            receipt_id: ledger_entry_id
            for (
                receipt_id,
                outcome,
                _credits,
                ledger_entry_id,
                _wallet_id,
            ) in receipt_rows
            if outcome == "failed_unrefunded"
            and ledger_entry_id in exact_refunded_ledger_ids
        }
        resolved_receipt_ids: set[str] = set()
        if failed_receipt_by_id:
            state_rows = (
                await session.execute(
                    select(
                        cast(
                            Any,
                            IdempotencyRecordModel.response_reference,
                        ),
                        cast(Any, IdempotencyRecordModel.response_json),
                    ).where(
                        cast(
                            ColumnElement[bool],
                            cast(
                                Any,
                                IdempotencyRecordModel.response_reference,
                            ).in_(list(failed_receipt_by_id)),
                        )
                    )
                )
            ).all()
            for receipt_id, response_json in state_rows:
                try:
                    response = json.loads(response_json or "")
                except (json.JSONDecodeError, TypeError):
                    continue
                state = (
                    response.get("refund_reconciliation")
                    if isinstance(response, dict)
                    else None
                )
                if (
                    isinstance(state, dict)
                    and state.get("status") == "resolved"
                    and state.get("receipt_id") == receipt_id
                    and state.get("ledger_entry_id") == failed_receipt_by_id[receipt_id]
                ):
                    resolved_receipt_ids.add(receipt_id)
        consumed_decimal = sum(
            (
                Decimal(str(credits))
                for (
                    receipt_id,
                    outcome,
                    credits,
                    _ledger_entry_id,
                    _wallet_id,
                ) in receipt_rows
                if outcome in {"success", "delivery_uncertain", "response_rejected"}
                or (
                    outcome == "failed_unrefunded"
                    and receipt_id not in resolved_receipt_ids
                )
            ),
            Decimal("0"),
        )
        return consumed_decimal

    @staticmethod
    def _should_report_live_drift(permit: PermitModel, drift: Decimal) -> bool:
        """Whether a live permit's budget drift is worth an operator line.

        The policy seam for the report-only pass in ``reconcile_budgets``. It
        is separated from the scan because the scan is a fact (what the
        receipts say) and this is a judgement (what is worth waking someone
        for), and the two change for different reasons.

        The default reports any inflation. ``spent_credits`` should never sit
        below what the receipts prove consumed -- a reservation is taken
        before the receipt is written -- so a negative drift is a different
        and more serious claim (budget enforcement bypassed, not stranded)
        and deliberately not folded into this signal.

        Cost of reporting every pass is bounded: the caller emits one
        aggregated line per pass listing every drifting permit, not one line
        per permit, so a permanently drifting permit costs ~288 lines a day
        in total rather than per permit. Tighten here if that is still too
        much -- for example, only report once the stranded amount is a
        meaningful fraction of ``max_credits - spent_credits``, which is what
        actually decides whether the drift can wrongly deny a call.
        """
        return drift > 0

    async def reconcile_budgets(self, *, idle_seconds: int = 900) -> int:
        """Repair budget reservations orphaned by a crash mid-invocation.

        A governed call reserves budget before charging, so a process death
        between reserve and the receipt write leaves ``spent_credits`` above the
        budget actually consumed. This resets such drift to the sum of the
        permit's successful receipts.

        Crucially, it only ever touches permits that can no longer admit a new
        charge -- non-active (revoked) OR already past ``expires_at``. A live,
        chargeable permit is never downward-reset here, because a governed call
        that outlives ``idle_seconds`` looks identical to a crashed one from the
        outside (no mid-call heartbeat), and resetting a still-live reservation
        would let a concurrent request over-spend past ``max_credits``.
        ``validate_for_action`` rejects both non-active and expired permits, so
        reclaiming their budget can never enable an over-spend. A crashed
        reservation on a still-active permit is left conservatively in place
        (the agent can spend *less* than authorized, never more) and is
        reclaimed once the permit expires.

        Returns the number of permits corrected. A permit whose guarded repair
        matched no row -- a concurrent reservation moved ``spent_credits``
        between the receipt scan and the write -- is *not* counted, is left
        exactly as found, and is re-examined on the next pass; those skips are
        logged rather than returned, so the count stays a count of writes.

        A second, read-only pass then *reports* drift on the live permits the
        repair must never touch, in its own session after the repair has
        committed so no lock outlives the write it protected. It changes
        nothing and is not counted; see the comment at the pass itself.
        """
        # Persisted datetimes in this codebase are naive UTC (see
        # app.core.time.utc_now); the reconcile columns (expires_at,
        # updated_at, issued_at) are naive DateTime. Build the comparison
        # bounds naive too, so the SQL comparison isn't skewed by a tz-aware
        # parameter being cast against the session timezone on Postgres.
        now = utc_now()
        cutoff = now - timedelta(seconds=idle_seconds)
        factory = get_session_factory()
        corrected = 0
        skipped = 0
        reported: list[tuple[str, Decimal]] = []
        async with factory() as session:
            async with session.begin():
                stale = (
                    (
                        await session.execute(
                            select(PermitModel)
                            .where(
                                or_(
                                    cast(
                                        ColumnElement[bool],
                                        PermitModel.status != "active",
                                    ),
                                    cast(
                                        ColumnElement[bool],
                                        PermitModel.expires_at <= now,
                                    ),
                                ),
                                cast(
                                    ColumnElement[bool],
                                    func.coalesce(
                                        PermitModel.updated_at, PermitModel.issued_at
                                    )
                                    < cutoff,
                                ),
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                for permit in stale:
                    consumed_decimal = await self._consumed_credits(
                        session, permit.permit_id
                    )
                    observed = permit.spent_credits
                    if observed != consumed_decimal:
                        # This is an absolute set recomputed from receipts, not
                        # an increment, so it cannot be expressed as a relative
                        # guarded UPDATE. Guard it on the value this pass
                        # actually observed instead: if a reservation landed
                        # between the receipt scan and this write, the row no
                        # longer matches and we correct nothing rather than
                        # erasing that reservation. ``with_for_update()`` above
                        # does not cover this on SQLite, where it is a no-op.
                        # The skipped permit is simply re-examined next pass.
                        repaired = await session.execute(
                            sa_update(PermitModel)
                            .where(
                                cast(
                                    ColumnElement[bool],
                                    PermitModel.permit_id == permit.permit_id,
                                ),
                                cast(
                                    ColumnElement[bool],
                                    PermitModel.spent_credits == observed,
                                ),
                            )
                            .values(
                                spent_credits=consumed_decimal,
                                updated_at=utc_now(),
                            )
                            .execution_options(synchronize_session=False)
                        )
                        if (cast(Any, repaired).rowcount or 0) == 1:
                            corrected += 1
                        else:
                            skipped += 1
            await session.commit()

        # Report-only pass over the permits the repair above must never touch.
        # Drift on a live permit is the residual cost of the routers absorbing
        # a contended post-effects budget release: spent_credits stays
        # inflated by a reservation that was refunded but never handed back,
        # so a later legitimate call can be wrongly denied
        # permit_budget_exceeded. The repair cannot run here -- a live permit
        # can still admit a charge, and a downward reset would open an
        # over-spend window past max_credits -- but staying silent until
        # expiry is what made the drift undiagnosable.
        #
        # Its own session, opened only after the repair has committed. The
        # repair's transaction holds FOR UPDATE on every stale row it scanned
        # (and, on SQLite, the database's single write lock from its first
        # UPDATE onward), and this pass is one receipts query per live permit.
        # Running it inside that transaction would keep those locks -- and
        # every reservation waiting on them -- held for the length of a scan
        # that writes nothing and needs no consistency with the repair. No
        # with_for_update() here either: a reporting pass must not lock rows
        # that in-flight reservations need, and the figure is advisory rather
        # than a premise for a write, so a racing reservation costs accuracy
        # for one pass and nothing else.
        #
        # Not bounded by a LIMIT. A cap would silently omit exactly the
        # permits this pass exists to surface, which is the silence being
        # removed, in a new place. It is bounded instead by what can drift:
        # spent_credits > 0 excludes every idle permit that has never
        # reserved, in practice most of them, and cannot exclude a reportable
        # one, because consumed credits are never negative, so a zero
        # reservation cannot sit above them. The per-permit query is
        # deliberately the same _consumed_credits the repair uses, so the two
        # passes cannot disagree about what "consumed" means; because it is
        # read-only and lock-free, a large active population costs latency on
        # this background tick, not contention with the request path.
        async with factory() as session:
            live = (
                (
                    await session.execute(
                        select(PermitModel).where(
                            cast(
                                ColumnElement[bool],
                                PermitModel.status == "active",
                            ),
                            cast(
                                ColumnElement[bool],
                                PermitModel.expires_at > now,
                            ),
                            cast(
                                ColumnElement[bool],
                                PermitModel.spent_credits > 0,
                            ),
                            cast(
                                ColumnElement[bool],
                                func.coalesce(
                                    PermitModel.updated_at, PermitModel.issued_at
                                )
                                < cutoff,
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for permit in live:
                drift = permit.spent_credits - await self._consumed_credits(
                    session, permit.permit_id
                )
                if self._should_report_live_drift(permit, drift):
                    reported.append((permit.permit_id, drift))
        if skipped:
            # A skipped permit is not a failure and needs no operator action --
            # it is re-examined on the next pass, and the guard is the whole
            # point (a concurrent reservation landed, so this pass's recomputed
            # figure is already out of date). It is logged because the return
            # value counts only repairs: without this line a pass that skipped
            # every permit is indistinguishable from a pass that found nothing
            # to repair, and a permit that never stops being skipped -- the
            # signature of a hot permit under sustained contention -- would
            # never surface anywhere.
            logger.info(
                "permit_budget_reconcile_skipped",
                extra={"skipped": skipped, "corrected": corrected},
            )
        if reported:
            # Warning, not info: unlike a skip, this does not clear itself on
            # the next pass. It persists until the permit expires, and while
            # it persists the permit can deny a call it has the budget for.
            # The total is the operator-facing number -- how much authorized
            # budget is currently unspendable -- and the per-permit ids are
            # what makes it actionable (reissue the permit, or wait out the
            # expiry that will reclaim it).
            logger.warning(
                "permit_budget_live_drift",
                extra={
                    "permits": len(reported),
                    "total_drift": str(sum((d for _, d in reported), Decimal("0"))),
                    "permit_ids": [pid for pid, _ in reported],
                },
            )
        return corrected

    @staticmethod
    def _unsigned_payload(model: PermitModel) -> dict[str, Any]:
        """The permit fields ``sign_payload`` receives, before alg/kid/hash.

        Additive fields enter only when set so pre-existing signatures keep
        verifying. ``status`` is hardcoded ``"active"`` — revocation is
        enforced by validation, not by breaking the signature. ``kid`` is
        omitted here: ``sign_payload`` folds it in from the active signing
        key, so an empty ``model.key_id`` at mint time cannot leak into the
        signed bytes.
        """
        payload: dict[str, Any] = {
            "permit_id": model.permit_id,
            "issuer_wallet_id": model.issuer_wallet_id,
            "subject_wallet_id": model.subject_wallet_id,
            "subject_key_id": model.subject_key_id,
            "scopes": _loads_list(model.scopes_json),
            "allowed_tools": _loads_list(model.allowed_tools_json),
            "max_credits": model.max_credits,
            "expires_at": model.expires_at,
            "nonce": model.nonce,
            "status": "active",
            "issued_at": model.issued_at,
        }
        if model.requires_human_approval:
            payload["requires_human_approval"] = True
        max_calls = _loads_dict(model.max_calls_per_tool_json or "{}")
        if max_calls:
            payload["max_calls_per_tool"] = max_calls
        if model.aggregate_value_cap is not None:
            payload["aggregate_value_cap"] = model.aggregate_value_cap
        forbidden = _loads_list(model.forbidden_fields_json or "[]")
        if forbidden:
            payload["forbidden_fields"] = forbidden
        if model.recipient_domain:
            payload["recipient_domain"] = model.recipient_domain
        if model.allow_identical_repeats:
            payload["allow_identical_repeats"] = True
        if model.repeat_window_seconds is not None:
            payload["repeat_window_seconds"] = model.repeat_window_seconds
        action = ActionPermitFields.model_validate(
            {name: getattr(model, name) for name in ActionPermitFields.model_fields}
        )
        payload.update(action.model_dump(exclude_none=True))
        return payload

    @staticmethod
    def _verification_payload(model: PermitModel) -> dict[str, Any]:
        """Rebuild the exact dict the permit signature covers.

        Same as :meth:`_unsigned_payload` plus the ``alg`` / ``kid`` /
        ``payload_hash`` fields ``sign_payload`` folds in.
        """
        payload = dict(PermitService._unsigned_payload(model))
        payload["alg"] = "Ed25519"
        payload["kid"] = model.key_id
        payload["payload_hash"] = sha256_hex(payload)
        return payload

    async def verify_signature(
        self,
        model: PermitModel,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        payload = self._verification_payload(model)
        return await get_signing_key_service().verify_payload(
            payload,
            signature=model.signature,
            key_id=model.key_id,
            session=session,
        )


_service: PermitService | None = None


def get_permit_service() -> PermitService:
    global _service
    if _service is None:
        _service = PermitService()
    return _service
