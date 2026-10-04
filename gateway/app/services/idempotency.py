from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col

from app.core.resilience import run_with_write_conflict_retry
from app.core.config import get_settings
from app.core.time import to_naive_utc, utc_now
from app.db.database import get_session_factory
from app.db.models import (
    IdempotencyRecordModel,
    LedgerEntryModel,
    McpDispatchAttemptModel,
    ReceiptModel,
    PermitModel,
)
from app.services.signing_keys import sha256_hex
from app.services.action_permits import ActionExecutionIdentity

GOVERNED_MCP_IDEMPOTENCY_ENDPOINT = "/mcp/invoke"
ACTION_MCP_IDEMPOTENCY_ENDPOINT = "/mcp/action/v1"


async def _lock_action_cleanup(session: AsyncSession, endpoint: str) -> None:
    if (
        endpoint == ACTION_MCP_IDEMPOTENCY_ENDPOINT
        and session.get_bind().dialect.name == "sqlite"
    ):
        from sqlalchemy import update

        await session.execute(
            update(IdempotencyRecordModel)
            .where(col(IdempotencyRecordModel.endpoint) == endpoint)
            .values(status_code=IdempotencyRecordModel.status_code)
        )


async def may_abandon_action_owner(
    record: IdempotencyRecordModel, session: AsyncSession
) -> bool:
    """Prove this locked owner never reached accepted preparation.

    Callers hold the owner lock through deletion. Missing registry/permit data
    is uncertainty, not permission to discard an execution identity.
    """
    if (
        record.endpoint != ACTION_MCP_IDEMPOTENCY_ENDPOINT
        or record.operation_kind != "upstream_mcp"
        or record.response_json is not None
        or record.response_reference is not None
        or record.ledger_entry_id is not None
    ):
        return False
    for model, column in (
        (McpDispatchAttemptModel, McpDispatchAttemptModel.idempotency_record_id),
        (ReceiptModel, ReceiptModel.idempotency_record_id),
        (LedgerEntryModel, LedgerEntryModel.operation_key),
    ):
        if (
            await session.scalar(
                select(model).where(cast(Any, column) == record.record_id)
            )
            is not None
        ):
            return False
    from app.services.action_permits import action_execution_identity
    from app.services.service_registry import get_service_registry

    permits = (
        (
            await session.execute(
                select(PermitModel)
                .where(
                    col(PermitModel.subject_wallet_id) == record.wallet_id,
                    col(PermitModel.action_contract_version) == 1,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    registry = get_service_registry()
    for permit in permits:
        service = await registry.get(permit.action_public_tool_id or "")
        binding = registry.get_action_binding(service) if service else None
        if binding is None:
            continue
        try:
            identity = action_execution_identity(permit, binding)
        except ValueError:
            continue
        if identity.idempotency_key != record.idempotency_key:
            continue
        # updated_at survives reservation release and is conservative evidence
        # of prior accounting. Never infer non-acceptance from a zero balance.
        return (
            record.request_hash == sha256_hex(identity.request_payload)
            and permit.spent_credits == 0
            and not json.loads(permit.tool_call_counts_json or "{}")
            and permit.updated_at is None
        )
    return False


class IdempotencyConflictError(RuntimeError):
    """Raised when an idempotency key is reused for a different request."""


class IdempotencyInProgressError(RuntimeError):
    """Raised when an idempotency key is already executing without a result."""


class IdempotencyCreationDisabledError(RuntimeError):
    """Raised when only existing requests may be replayed."""


# Caller-supplied replay keys are stored verbatim in the
# idempotency_records.idempotency_key column, a String(128) since migration
# 016, so 128 is the durable limit; the Python SDK enforces the same bound
# before sending. Accepting a longer key would pass on SQLite in tests and be
# refused by PostgreSQL mid-call, after the caller's retry protection had
# already been promised.
MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH = 128
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class InvalidIdempotencyKeyError(ValueError):
    """A caller explicitly supplied a replay key the gateway cannot honor.

    This is distinct from an absent key. Absence means an un-keyed call, and
    the transports document what that costs. Presence with an unusable value
    means the caller asked for retry protection and would silently not get
    it, so the call is refused before any permit, debit, or dispatch.
    """

    def __init__(self, reason_code: str, message: str, *, source: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.source = source

    def as_error_data(self) -> dict[str, Any]:
        """Machine-actionable detail for the transport's error envelope."""
        return {
            "error": "invalid_idempotency_key",
            "reason_code": self.reason_code,
            "source": self.source,
            "remediation": {
                "type": "retry_with_valid_idempotency_key",
                "detail": (
                    "Send exactly one idempotency key: a non-blank string of at "
                    f"most {MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH} characters with no "
                    "control characters (UTF-8 bytes when sent as a header), or "
                    "the same key in every source. Nothing was minted, charged, "
                    "or dispatched."
                ),
            },
        }


def validate_client_idempotency_key(value: object, *, source: str) -> str:
    """Return ``value`` exactly as supplied or raise ``InvalidIdempotencyKeyError``.

    A usable key is a JSON string with at least one non-whitespace character,
    at most ``MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH`` characters, and no C0 control
    characters or DEL. The value is never stripped, truncated, stringified,
    or otherwise normalized: two distinct valid keys must stay two distinct
    keys, and the key the caller sent is the key the ledger records.
    """
    if not isinstance(value, str):
        raise InvalidIdempotencyKeyError(
            "idempotency_key_not_a_string",
            f"invalid_idempotency_key: {source} must be a JSON string",
            source=source,
        )
    if not value.strip():
        raise InvalidIdempotencyKeyError(
            "idempotency_key_blank",
            f"invalid_idempotency_key: {source} must contain a non-whitespace "
            "character",
            source=source,
        )
    if len(value) > MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidIdempotencyKeyError(
            "idempotency_key_too_long",
            f"invalid_idempotency_key: {source} must be at most "
            f"{MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH} characters",
            source=source,
        )
    if _CONTROL_CHARACTERS.search(value):
        raise InvalidIdempotencyKeyError(
            "idempotency_key_control_characters",
            f"invalid_idempotency_key: {source} must not contain control characters",
            source=source,
        )
    return value


IDEMPOTENCY_KEY_HEADER_SOURCE = "Idempotency-Key header"


def decode_idempotency_key_header(line: str) -> str:
    """Return an ``Idempotency-Key`` header line as the text the client sent.

    Header bytes reach the application decoded as latin-1, so a key a client
    sent as UTF-8 would otherwise arrive as mojibake: it would not equal the
    same key sent in the body, and UTF-8 continuation bytes in 0x80-0x9F
    would read as C1 control characters. The line is re-read as UTF-8 and
    nothing else, so every wire value has at most one reading and two
    different byte sequences can never alias to one key; bytes that are not
    valid UTF-8 are refused (``idempotency_key_not_utf8``) rather than kept
    as latin-1 text.
    """
    try:
        return line.encode("latin-1").decode("utf-8")
    except UnicodeError as exc:
        raise InvalidIdempotencyKeyError(
            "idempotency_key_not_utf8",
            f"invalid_idempotency_key: {IDEMPOTENCY_KEY_HEADER_SOURCE} must be "
            "valid UTF-8",
            source=IDEMPOTENCY_KEY_HEADER_SOURCE,
        ) from exc


def resolve_client_idempotency_key(
    sources: Sequence[tuple[str, object]],
) -> str | None:
    """Reduce every explicitly supplied key source to the one key to honor.

    ``sources`` holds ``(source_name, value)`` for each source the caller
    actually sent; absent sources are simply not listed. Every present value
    must validate, and when more than one source is present they must agree
    exactly. Disagreement is refused as ambiguous rather than letting one
    source silently win. Returns ``None`` only when no source was supplied.
    """
    validated: list[tuple[str, str]] = []
    for source, value in sources:
        validated.append(
            (source, validate_client_idempotency_key(value, source=source))
        )
    if not validated:
        return None
    first_source, first_key = validated[0]
    for source, key in validated[1:]:
        if key != first_key:
            raise InvalidIdempotencyKeyError(
                "idempotency_key_conflict",
                f"invalid_idempotency_key: {first_source} and {source} disagree; "
                "send one key, or the same key in both",
                source=f"{first_source}, {source}",
            )
    return first_key


@dataclass(frozen=True)
class IdempotencyReplay:
    response_reference: str | None
    response_json: dict[str, Any] | None
    status_code: int


@dataclass(frozen=True)
class IdempotencyBegin:
    """Identity and replay result for one idempotent request."""

    record_id: str
    request_hash: str
    replay: IdempotencyReplay | None


def _idempotency_predicates(
    wallet_id: str, endpoint: str, idempotency_key: str
) -> tuple[ColumnElement[bool], ...]:
    return (
        cast(ColumnElement[bool], IdempotencyRecordModel.wallet_id == wallet_id),
        cast(ColumnElement[bool], IdempotencyRecordModel.endpoint == endpoint),
        cast(
            ColumnElement[bool],
            IdempotencyRecordModel.idempotency_key == idempotency_key,
        ),
    )


def _replay_from_record(
    existing: IdempotencyRecordModel, request_hash: str
) -> IdempotencyReplay | None:
    if existing.request_hash != request_hash:
        raise IdempotencyConflictError("idempotency_key_reused")
    if existing.response_json:
        try:
            decoded = json.loads(existing.response_json)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            return IdempotencyReplay(
                response_reference=existing.response_reference,
                response_json=decoded,
                status_code=existing.status_code,
            )
    raise IdempotencyInProgressError("idempotency_in_progress")


class IdempotencyService:
    async def _try_reconcile_and_replay(
        self,
        session: AsyncSession,
        *,
        existing: IdempotencyRecordModel,
        request_hash: str,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
        wait_timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> IdempotencyReplay:
        """Try immediate reconciliation for dispatched/terminal attempts before wait."""
        # Check if there's a dispatch attempt linked to this idempotency record
        # that is either dispatched (ambiguous) or terminal but not finalized.
        dispatch_attempt = (
            await session.execute(
                select(McpDispatchAttemptModel).where(
                    cast(
                        ColumnElement[bool],
                        McpDispatchAttemptModel.idempotency_record_id
                        == existing.record_id,
                    )
                )
            )
        ).scalar_one_or_none()

        if dispatch_attempt is not None:
            # Import here to avoid circular dependency
            from app.services.mcp_dispatch_attempts import (
                DISPATCH_TERMINAL_STATES,
                dispatch_reconciliation_idle_seconds,
            )
            from app.services.mcp_dispatch_reconciliation import (
                get_mcp_dispatch_reconciliation_service,
            )

            # Terminal rows own no send authority. Repair their missing receipt
            # or replay immediately; the long idle window exists only to avoid
            # stealing a live prepared/claimed attempt.
            if dispatch_attempt.state in DISPATCH_TERMINAL_STATES:
                if existing.response_json is not None:
                    replay = _replay_from_record(existing, request_hash)
                    if replay is not None:
                        return replay
                should_reconcile = True
                reconcile_idle_seconds: int | None = None
            else:
                should_reconcile = False
                reconcile_idle_seconds = None

            # Only reconcile active attempts that are truly stale, using the
            # same globally conservative idle window as the periodic sweep.
            # Live attempts with active owners must wait, not be stolen.
            # Check staleness using updated_at (last state change), matching
            # list_stale_contexts logic in mcp_dispatch_attempts.py
            if (
                dispatch_attempt.state not in DISPATCH_TERMINAL_STATES
                and dispatch_attempt.updated_at is not None
            ):
                settings = get_settings()
                stale_idle_seconds = dispatch_reconciliation_idle_seconds(
                    connect_timeout_seconds=(
                        settings.MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS
                    ),
                    call_timeout_seconds=settings.MCP_UPSTREAM_CALL_TIMEOUT_SECONDS,
                )
                cutoff = to_naive_utc(utc_now() - timedelta(seconds=stale_idle_seconds))
                updated_naive = to_naive_utc(dispatch_attempt.updated_at)
                is_stale = updated_naive < cutoff

                if is_stale:
                    # Attempt exceeded the configured live-call window.
                    should_reconcile = True
                    reconcile_idle_seconds = stale_idle_seconds
                # else: fresh update, assume live owner, wait

            if should_reconcile:
                reconciler = get_mcp_dispatch_reconciliation_service()
                attempt_id = dispatch_attempt.attempt_id
                # The lookup transaction otherwise retains the application's
                # only pooled connection while reconciliation opens its own
                # session. Release it before crossing that service boundary.
                await session.rollback()
                session.expire_all()
                try:
                    await reconciler.reconcile_attempt(
                        attempt_id,
                        prepared_error_code="reconciled_stale_prepared",
                        idle_seconds=reconcile_idle_seconds,
                    )
                except Exception:
                    # Reconciliation failed; fall through to normal wait path
                    pass
                else:
                    # Reconciliation succeeded; re-read the record
                    fresh = (
                        await session.execute(
                            select(IdempotencyRecordModel).where(
                                *_idempotency_predicates(
                                    wallet_id,
                                    endpoint,
                                    idempotency_key,
                                )
                            )
                        )
                    ).scalar_one_or_none()
                    if fresh is not None and fresh.response_json is not None:
                        replay = _replay_from_record(fresh, request_hash)
                        if replay is not None:
                            return replay

        # No dispatch attempt or reconciliation failed/incomplete; wait normally
        if wait_timeout_seconds <= 0:
            raise IdempotencyInProgressError("idempotency_in_progress")
        return await self._wait_for_replay(
            session,
            wallet_id=wallet_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    async def _wait_for_replay(
        self,
        session: AsyncSession,
        *,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
        request_hash: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> IdempotencyReplay:
        """Wait boundedly for an identical concurrent request to finalize."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            await session.rollback()
            await asyncio.sleep(poll_interval_seconds)
            session.expire_all()
            existing = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        *_idempotency_predicates(
                            wallet_id,
                            endpoint,
                            idempotency_key,
                        )
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                raise IdempotencyInProgressError("idempotency_in_progress")
            try:
                replay = _replay_from_record(existing, request_hash)
            except IdempotencyInProgressError:
                continue
            assert replay is not None
            return replay
        raise IdempotencyInProgressError("idempotency_in_progress")

    async def begin_with_record(
        self,
        *,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        operation_kind: str | None = None,
        wait_timeout_seconds: float = 0.0,
        poll_interval_seconds: float = 0.05,
        allow_new_record: bool = True,
    ) -> IdempotencyBegin:
        if wait_timeout_seconds < 0 or poll_interval_seconds <= 0:
            raise ValueError("idempotency_wait_invalid")
        request_hash = sha256_hex(request_payload)
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(IdempotencyRecordModel).where(
                    *_idempotency_predicates(wallet_id, endpoint, idempotency_key)
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                try:
                    replay = _replay_from_record(existing, request_hash)
                except IdempotencyInProgressError:
                    # Before declaring in-progress, check if there's a terminal or
                    # dispatched attempt that can be reconciled immediately.
                    replay = await self._try_reconcile_and_replay(
                        session,
                        existing=existing,
                        request_hash=request_hash,
                        wallet_id=wallet_id,
                        endpoint=endpoint,
                        idempotency_key=idempotency_key,
                        wait_timeout_seconds=wait_timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                    )
                return IdempotencyBegin(
                    record_id=existing.record_id,
                    request_hash=request_hash,
                    replay=replay,
                )

            if not allow_new_record:
                raise IdempotencyCreationDisabledError("idempotency_creation_disabled")

            record = IdempotencyRecordModel(
                record_id=f"idm-{uuid.uuid4().hex[:16]}",
                wallet_id=wallet_id,
                endpoint=endpoint,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                operation_kind=operation_kind,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                # Lost a race to a concurrent identical request: the other
                # request's row already landed under the unique constraint.
                # Treat it the same as finding it on the initial SELECT
                # instead of surfacing a raw 500 to the caller.
                await session.rollback()
                result = await session.execute(
                    select(IdempotencyRecordModel).where(
                        *_idempotency_predicates(wallet_id, endpoint, idempotency_key)
                    )
                )
                existing = result.scalar_one_or_none()
                if existing is None:
                    raise
                try:
                    replay = _replay_from_record(existing, request_hash)
                except IdempotencyInProgressError:
                    replay = await self._try_reconcile_and_replay(
                        session,
                        existing=existing,
                        request_hash=request_hash,
                        wallet_id=wallet_id,
                        endpoint=endpoint,
                        idempotency_key=idempotency_key,
                        wait_timeout_seconds=wait_timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                    )
                return IdempotencyBegin(
                    record_id=existing.record_id,
                    request_hash=request_hash,
                    replay=replay,
                )
            except OperationalError as exc:
                # SQLite can report a write-contention race as "database is
                # locked" instead of a unique-key IntegrityError. Preserve the
                # same bounded replay behavior and never expose a raw 500.
                if "database is locked" not in str(exc):
                    raise
                await session.rollback()
                result = await session.execute(
                    select(IdempotencyRecordModel).where(
                        *_idempotency_predicates(
                            wallet_id,
                            endpoint,
                            idempotency_key,
                        )
                    )
                )
                existing = result.scalar_one_or_none()
                if existing is None:
                    raise IdempotencyInProgressError("idempotency_in_progress")
                try:
                    replay = _replay_from_record(existing, request_hash)
                except IdempotencyInProgressError:
                    replay = await self._try_reconcile_and_replay(
                        session,
                        existing=existing,
                        request_hash=request_hash,
                        wallet_id=wallet_id,
                        endpoint=endpoint,
                        idempotency_key=idempotency_key,
                        wait_timeout_seconds=wait_timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                    )
                return IdempotencyBegin(
                    record_id=existing.record_id,
                    request_hash=request_hash,
                    replay=replay,
                )
            return IdempotencyBegin(
                record_id=record.record_id,
                request_hash=request_hash,
                replay=None,
            )

    async def begin(
        self,
        *,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        allow_new_record: bool = True,
        compatible_request_payload: dict[str, Any] | None = None,
    ) -> IdempotencyReplay | None:
        """Replay an explicitly supplied historical shape without writing that shape."""
        begun = await self.begin_with_compatible_record(
            wallet_id=wallet_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            allow_new_record=allow_new_record,
            compatible_request_payload=compatible_request_payload,
        )
        return begun.replay

    async def begin_with_compatible_record(
        self,
        *,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        allow_new_record: bool = True,
        compatible_request_payload: dict[str, Any] | None = None,
    ) -> IdempotencyBegin:
        """Keep the granted identity while permitting historical-shape replay."""
        try:
            begun = await self.begin_with_record(
                wallet_id=wallet_id,
                endpoint=endpoint,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                allow_new_record=allow_new_record,
            )
        except IdempotencyConflictError as conflict:
            if compatible_request_payload is None:
                raise
            try:
                begun = await self.begin_with_record(
                    wallet_id=wallet_id,
                    endpoint=endpoint,
                    idempotency_key=idempotency_key,
                    request_payload=compatible_request_payload,
                    allow_new_record=False,
                )
            except IdempotencyCreationDisabledError:
                # A record removed between reads must not mint under the old hash.
                raise conflict
        return begun

    async def get_record(
        self,
        *,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
    ) -> IdempotencyRecordModel | None:
        factory = get_session_factory()
        async with factory() as session:
            return (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        *_idempotency_predicates(
                            wallet_id,
                            endpoint,
                            idempotency_key,
                        )
                    )
                )
            ).scalar_one_or_none()

    async def get_action_record(
        self, *, wallet_id: str, internal_key: str
    ) -> IdempotencyRecordModel | None:
        """Look up only the dedicated owner; never adopt transport/legacy rows."""
        return await self.get_record(
            wallet_id=wallet_id,
            endpoint=ACTION_MCP_IDEMPOTENCY_ENDPOINT,
            idempotency_key=internal_key,
        )

    async def begin_action_with_record(
        self,
        *,
        wallet_id: str,
        identity: ActionExecutionIdentity,
        wait_timeout_seconds: float = 0.0,
        poll_interval_seconds: float = 0.05,
        allow_new_record: bool = True,
    ) -> IdempotencyBegin:
        if identity.endpoint != ACTION_MCP_IDEMPOTENCY_ENDPOINT:
            raise ValueError("invalid_action_execution_namespace")
        if identity.request_payload.get("subject_wallet_id") != wallet_id:
            raise ValueError("action_execution_wallet_mismatch")
        return await self.begin_with_record(
            wallet_id=wallet_id,
            endpoint=ACTION_MCP_IDEMPOTENCY_ENDPOINT,
            idempotency_key=identity.idempotency_key,
            request_payload=identity.request_payload,
            operation_kind="upstream_mcp",
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            allow_new_record=allow_new_record,
        )

    async def complete_action(
        self,
        *,
        wallet_id: str,
        internal_key: str,
        response_reference: str | None,
        response_json: dict[str, Any] | None,
        status_code: int = 200,
    ) -> None:
        await self.complete(
            wallet_id=wallet_id,
            endpoint=ACTION_MCP_IDEMPOTENCY_ENDPOINT,
            idempotency_key=internal_key,
            response_reference=response_reference,
            response_json=response_json,
            status_code=status_code,
        )

    async def get_governed_mcp_record(
        self,
        *,
        wallet_id: str,
        idempotency_key: str,
    ) -> IdempotencyRecordModel | None:
        """Return the one row protected by the normalized MCP identity index."""
        factory = get_session_factory()
        async with factory() as session:
            return (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        col(IdempotencyRecordModel.wallet_id) == wallet_id,
                        col(IdempotencyRecordModel.idempotency_key) == idempotency_key,
                        or_(
                            col(IdempotencyRecordModel.endpoint)
                            == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                            col(IdempotencyRecordModel.endpoint) == "/mcp/messages",
                            col(IdempotencyRecordModel.endpoint).like(
                                "/mcp/tools/%/invoke"
                            ),
                        ),
                    )
                )
            ).scalar_one_or_none()

    async def complete(
        self,
        *,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
        response_reference: str | None,
        response_json: dict[str, Any] | None,
        status_code: int = 200,
    ) -> None:
        """Persist the replay response, restarting a contended transaction.

        A receipt or charge may already be committed. Retry only this response
        write, never the caller's action, and use a fresh session so a stale
        SQLite WAL snapshot cannot poison the next attempt.
        """

        async def _once() -> None:
            factory = get_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(IdempotencyRecordModel).where(
                        *_idempotency_predicates(wallet_id, endpoint, idempotency_key)
                    )
                )
                record = result.scalar_one_or_none()
                if not record:
                    return
                record.response_reference = response_reference
                record.response_json = (
                    json.dumps(response_json, default=str) if response_json else None
                )
                record.status_code = status_code
                session.add(record)
                await session.commit()

        # Exhaustion must not invent a completed outcome or release the key:
        # the caller may have already committed effects before reaching here.
        await run_with_write_conflict_retry(_once, on_exhausted=lambda exc: exc)

    async def abandon(
        self,
        *,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
        expected_record_id: str | None = None,
    ) -> bool:
        """Release an in-progress record so the caller may retry the key.

        Used when a governed invoke stops on a retryable, side-effect-free
        condition (human approval still pending, approval backend
        unreachable): completing the record would replay that transient state
        forever, and leaving it in progress would reject the retry with
        ``idempotency_in_progress``. Only an uncharged, unfinished record is
        deleted — a completed response or a ``ledger_entry_id`` checkpoint
        means money moved, and the record must survive for replay/repair.

        ``expected_record_id`` pins the release to the row the caller was
        actually granted. Without it the coordinates (wallet, endpoint, key)
        name whichever row holds them *now*, which is not necessarily the
        caller's: a request refused with ``idempotency_in_progress`` never
        owned that row, and releasing it there would hand the winner's
        at-most-once protection to whoever asked next. Pass it whenever the
        caller holds a record id; a mismatch is a no-op, not an error, since
        it means the row moved on and nothing is left to release. Returns true
        only when this call committed deletion of the eligible owner.
        """
        if endpoint == ACTION_MCP_IDEMPOTENCY_ENDPOINT and expected_record_id is None:
            return False
        factory = get_session_factory()
        async with factory() as session:
            await _lock_action_cleanup(session, endpoint)
            result = await session.execute(
                select(IdempotencyRecordModel)
                .where(*_idempotency_predicates(wallet_id, endpoint, idempotency_key))
                .with_for_update()
            )
            record = result.scalar_one_or_none()
            if not record:
                return False
            if (
                expected_record_id is not None
                and record.record_id != expected_record_id
            ):
                return False
            if record.response_json is not None or record.ledger_entry_id:
                return False
            if (
                endpoint == ACTION_MCP_IDEMPOTENCY_ENDPOINT
                and not await may_abandon_action_owner(record, session)
            ):
                return False
            await session.delete(record)
            await session.commit()
            return True

    async def mark_charged(
        self,
        *,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
        ledger_entry_id: str,
    ) -> None:
        """Checkpoint the charge, restarting on a transient write conflict.

        This runs after the debit has committed, so once the debit itself stops
        losing the WAL snapshot race this is where contention lands -- and a
        loss here costs the record its checkpoint while the money has already
        moved. Restarting is safe because the underlying write is a
        last-writer-wins field set on a single row.
        """

        async def _once() -> None:
            await self._mark_charged_once(
                wallet_id=wallet_id,
                endpoint=endpoint,
                idempotency_key=idempotency_key,
                ledger_entry_id=ledger_entry_id,
            )

        # On exhaustion, re-raise the driver error unchanged. The debit has
        # already committed and carries this operation key, so the reaper can
        # still link it; inventing a new terminal reason here would claim more
        # about the outcome than is known. The retry is a strict improvement on
        # the previous behaviour, not a change to it.
        await run_with_write_conflict_retry(_once, on_exhausted=lambda exc: exc)

    async def _mark_charged_once(
        self,
        *,
        wallet_id: str,
        endpoint: str,
        idempotency_key: str,
        ledger_entry_id: str,
    ) -> None:
        """Checkpoint that this idempotency record's charge has landed.

        Called right after a governed invoke charges a wallet and before the
        receipt/audit/complete finalization sequence runs, so a later crash
        that leaves the record stuck "in progress" can be told apart from a
        record that was never charged at all -- see reconcile_stuck_records.
        """
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(IdempotencyRecordModel).where(
                    *_idempotency_predicates(wallet_id, endpoint, idempotency_key)
                )
            )
            record = result.scalar_one_or_none()
            if not record:
                return
            record.ledger_entry_id = ledger_entry_id
            session.add(record)
            await session.commit()

    async def reconcile_stuck_records(
        self, *, idle_seconds: int = 900
    ) -> tuple[int, int]:
        """Repair governed idempotency records orphaned around finalization.

        Effect-free canonical MCP rows that never reached the atomic prepared
        checkpoint are deleted so the same key can safely retry. A governed
        invoke that did debit calls mark_charged() before finalization. For
        each such record idle for at least ``idle_seconds`` (so live in-flight
        requests are never touched):
        if a receipt already exists for its ledger_entry_id (finalization got
        as far as writing the receipt but not completing this record), the
        record is completed from that receipt so a retry replays cleanly
        instead of hanging forever on IdempotencyInProgressError. If no
        receipt exists at all, the charge succeeded but nothing about it can
        be safely reconstructed after the fact (the original tool response
        was never persisted) -- these are left untouched and counted
        separately for manual/operator review.

        Returns (repaired_count, needs_manual_review_count).
        """
        # created_at is a naive-UTC DateTime column (see app.core.time.utc_now);
        # build the idle cutoff naive too so the SQL comparison isn't skewed by
        # a tz-aware parameter being cast against the session timezone on
        # Postgres.
        cutoff = utc_now() - timedelta(seconds=idle_seconds)
        factory = get_session_factory()
        repaired = 0
        needs_review = 0
        async with factory() as session:
            async with session.begin():
                # The upstream pipeline creates its idempotency row before the
                # atomic permit-reservation/prepared-attempt transaction. A
                # crash in that narrow, effect-free gap leaves no budget,
                # debit, attempt, or receipt to recover. Expire only this
                # upstream MCP identity so a retry can safely start again.
                # Local tools share the canonical replay scope but have
                # different side-effect ordering and are excluded by the
                # internal operation kind.
                await _lock_action_cleanup(session, ACTION_MCP_IDEMPOTENCY_ENDPOINT)
                unstarted = (
                    (
                        await session.execute(
                            select(IdempotencyRecordModel)
                            .where(
                                cast(
                                    ColumnElement[bool],
                                    col(IdempotencyRecordModel.endpoint).in_(
                                        [
                                            GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                                            ACTION_MCP_IDEMPOTENCY_ENDPOINT,
                                        ]
                                    ),
                                ),
                                cast(
                                    ColumnElement[bool],
                                    IdempotencyRecordModel.operation_kind
                                    == "upstream_mcp",
                                ),
                                cast(
                                    ColumnElement[bool],
                                    cast(Any, IdempotencyRecordModel.response_json).is_(
                                        None
                                    ),
                                ),
                                cast(
                                    ColumnElement[bool],
                                    cast(
                                        Any, IdempotencyRecordModel.ledger_entry_id
                                    ).is_(None),
                                ),
                                cast(
                                    ColumnElement[bool],
                                    IdempotencyRecordModel.created_at < cutoff,
                                ),
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                for record in unstarted:
                    if record.endpoint == ACTION_MCP_IDEMPOTENCY_ENDPOINT:
                        if await may_abandon_action_owner(record, session):
                            await session.delete(record)
                            repaired += 1
                        continue
                    attempt_id = await session.scalar(
                        select(cast(Any, McpDispatchAttemptModel.attempt_id)).where(
                            cast(
                                ColumnElement[bool],
                                McpDispatchAttemptModel.idempotency_record_id
                                == record.record_id,
                            )
                        )
                    )
                    receipt_id = await session.scalar(
                        select(cast(Any, ReceiptModel.receipt_id)).where(
                            cast(
                                ColumnElement[bool],
                                ReceiptModel.idempotency_record_id == record.record_id,
                            )
                        )
                    )
                    if attempt_id is None and receipt_id is None:
                        await session.delete(record)
                        repaired += 1

                # Local governed calls fall through both passes above: the
                # unstarted pass is scoped to operation_kind="upstream_mcp",
                # and the stuck pass below requires a linked ledger_entry_id.
                # A crash between the committed debit and the write that
                # attaches that debit to the record therefore left an
                # unresolvable row -- response_json IS NULL and
                # ledger_entry_id IS NULL -- that no branch selected, so it
                # never repaired and never counted toward needs_review. The
                # charge carries operation_key=record_id (see the governed
                # money.charge call in app/routers/mcp.py), so the orphaned
                # debit is recoverable by that key: link it here and the stuck
                # pass below resolves it exactly like any other crashed
                # finalization. Engine-independent -- no row-lock behaviour
                # involved. AWI HTTP uses the same operation-key ownership:
                # interrupted admission must preserve and recover its debit too.
                orphaned_local = (
                    (
                        await session.execute(
                            select(IdempotencyRecordModel)
                            .where(
                                or_(
                                    and_(
                                        col(IdempotencyRecordModel.endpoint)
                                        == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                                        col(IdempotencyRecordModel.operation_kind)
                                        == "local",
                                    ),
                                    col(IdempotencyRecordModel.operation_kind)
                                    == "awi_http",
                                ),
                                cast(
                                    ColumnElement[bool],
                                    cast(Any, IdempotencyRecordModel.response_json).is_(
                                        None
                                    ),
                                ),
                                cast(
                                    ColumnElement[bool],
                                    cast(
                                        Any, IdempotencyRecordModel.ledger_entry_id
                                    ).is_(None),
                                ),
                                cast(
                                    ColumnElement[bool],
                                    IdempotencyRecordModel.created_at < cutoff,
                                ),
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                for record in orphaned_local:
                    debit = (
                        await session.execute(
                            select(LedgerEntryModel).where(
                                cast(
                                    ColumnElement[bool],
                                    LedgerEntryModel.wallet_id == record.wallet_id,
                                ),
                                cast(
                                    ColumnElement[bool],
                                    LedgerEntryModel.operation_key == record.record_id,
                                ),
                                # Compensation proof means money left the
                                # wallet. The wallet/operation-key uniqueness
                                # constraint already makes a same-key credit
                                # impossible alongside the debit, but stating
                                # the direction here means this pass can never
                                # adopt a credit as evidence of a charge.
                                cast(
                                    ColumnElement[bool],
                                    LedgerEntryModel.amount < 0,
                                ),
                            )
                        )
                    ).scalar_one_or_none()
                    if debit is None:
                        # Nothing provably moved, and a local identity must NOT
                        # be expired on that basis: local tools run their side
                        # effect before the debit exists, so deleting the record
                        # would let a retry execute it a second time. That
                        # invariant is asserted by
                        # test_stale_local_identity_is_not_deleted_without
                        # _compensation_proof. Leave it exactly as found.
                        continue
                    # A committed debit is compensation proof: money moved under
                    # this identity. Link it, and the stuck pass below resolves
                    # it from the receipt or counts it for manual review.
                    record.ledger_entry_id = debit.entry_id
                    session.add(record)
                # The session does not autoflush, so the links just written are
                # still pending; flush them so the stuck pass below selects the
                # rows this pass just repaired into scope.
                await session.flush()

                stuck = (
                    (
                        await session.execute(
                            select(IdempotencyRecordModel)
                            .where(
                                cast(
                                    ColumnElement[bool],
                                    cast(Any, IdempotencyRecordModel.response_json).is_(
                                        None
                                    ),
                                ),
                                cast(
                                    ColumnElement[bool],
                                    or_(
                                        cast(
                                            Any, IdempotencyRecordModel.ledger_entry_id
                                        ).is_not(None),
                                        col(IdempotencyRecordModel.operation_kind)
                                        == "awi_http",
                                    ),
                                ),
                                cast(
                                    ColumnElement[bool],
                                    IdempotencyRecordModel.created_at < cutoff,
                                ),
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                for record in stuck:
                    # Remote MCP attempts retain a bounded canonical upstream
                    # result and have a state-aware reconciler that can rebuild
                    # the exact replay contract. A generic receipt-only repair
                    # would discard that result and erase delivery uncertainty.
                    dispatch_attempt_id = await session.scalar(
                        select(cast(Any, McpDispatchAttemptModel.attempt_id)).where(
                            cast(
                                ColumnElement[bool],
                                McpDispatchAttemptModel.idempotency_record_id
                                == record.record_id,
                            )
                        )
                    )
                    if dispatch_attempt_id is not None:
                        continue
                    receipt = (
                        await session.execute(
                            select(ReceiptModel).where(
                                cast(
                                    ColumnElement[bool],
                                    ReceiptModel.idempotency_record_id
                                    == record.record_id,
                                )
                            )
                        )
                    ).scalar_one_or_none()
                    if receipt is None and record.ledger_entry_id is not None:
                        # Legacy receipts predate the explicit idempotency FK.
                        receipt = (
                            await session.execute(
                                select(ReceiptModel).where(
                                    cast(
                                        ColumnElement[bool],
                                        ReceiptModel.ledger_entry_id
                                        == record.ledger_entry_id,
                                    )
                                )
                            )
                        ).scalar_one_or_none()
                    if receipt is None:
                        needs_review += 1
                        continue
                    # The receipt's outcome is the ground truth -- a crash can
                    # leave a failed_refunded/denied/insufficient_funds
                    # receipt just as easily as a success one (e.g. a crash
                    # inside _finalize_governed_denial between the receipt
                    # write and idem.complete()). Reconciling it as a bare 200
                    # success regardless of outcome would tell a replaying
                    # client the call succeeded when it didn't.
                    is_error = receipt.outcome != "success"
                    status_code = {
                        "success": 200,
                        "insufficient_funds": 402,
                        "denied": 403,
                        "delivery_uncertain": 504,
                        "response_rejected": 502,
                    }.get(receipt.outcome, 500)
                    recovered_response = {
                        "reconciled": True,
                        "outcome": receipt.outcome,
                        "isError": is_error,
                        "receipt_id": receipt.receipt_id,
                        "ledger_entry_id": record.ledger_entry_id,
                        "message": (
                            "The original response could not be replayed: "
                            "finalization crashed after the charge and "
                            f"receipt (outcome={receipt.outcome!r}) were "
                            "already written. Inspect "
                            f"/v1/evidence/{receipt.receipt_id} for the "
                            "full record of what happened."
                        ),
                    }
                    if is_error:
                        recovered_response["error"] = receipt.outcome
                    record.response_reference = receipt.receipt_id
                    record.response_json = json.dumps(recovered_response)
                    record.status_code = status_code
                    session.add(record)
                    repaired += 1

        return repaired, needs_review


_service: IdempotencyService | None = None


def get_idempotency_service() -> IdempotencyService:
    global _service
    if _service is None:
        _service = IdempotencyService()
    return _service
