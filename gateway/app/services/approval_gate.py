"""Human approval gate for irreversible MCP tool calls (AMW V2).

SEP-2322 flow: the first ``tools/call`` for an irreversible tool PAUSEs with a
``resultType='input_required'`` result carrying an ``inputRequests`` map (keyed
by request id, each value an ``elicitation/create`` client request) plus an
opaque ``requestState``. The client retries the same ``tools/call`` with an
"inputResponses" map keyed by the same id plus the ``requestState``; this gate
EXECUTEs only after a local human approved, else it REJECTs. The hook in
``mcp_standard.py`` that calls :meth:`ApprovalGate.evaluate` lands in a later
PR.

Fail-closed rules:

- The idempotency key is the single G1 key from
  ``goose_amw.canonical.idempotency_key`` (verified caller + tool + canonical
  args hash + agent session id). The Goose request id is logged only.
- ``requestState`` is HMAC-SHA256 sealed; missing secret fails closed.
- Clients without ``input_required`` support are rejected, with a receipt.
- Deny, timeout, tampering, and every retry mismatch each emit a receipt via
  an injectable sink and never execute.
- Pending approvals, decisions, and consumption live in the database
  (``DbApprovalStore``), so a restart cannot replay an approval.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Literal, TypeVar, cast

from sqlalchemy import select, update
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col

from app.core.time import to_naive_utc, utc_now
from app.db.approval_gate_models import ApprovalGateModel
from app.db.database import get_session_factory

logger = logging.getLogger(__name__)

STATE_SECRET_ENV = "AMW_APPROVAL_STATE_SECRET"
TTL_ENV = "AMW_APPROVAL_TTL_SECONDS"
DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 3600
STATE_VERSION = 1

DecisionAction = Literal["PAUSE", "EXECUTE", "REJECT"]
RejectReason = Literal[
    "denied",
    "timeout",
    "tampered",
    "unsupported_client",
    "tool_mismatch",
    "session_mismatch",
    "key_mismatch",
    "args_mismatch",
    "principal_mismatch",
    "response_mismatch",
    "replayed",
]
ApprovalStatus = Literal["pending", "approved", "denied", "expired"]


class ApprovalGateUnavailable(RuntimeError):
    """The gate cannot operate safely (missing secret or missing key module)."""


class _StateTampered(ValueError):
    """Internal: requestState failed authentication or parsing."""


def _load_canonical() -> Any:
    """Import goose_amw.canonical lazily; fail closed when absent."""
    try:
        from goose_amw import canonical as canonical_module  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ApprovalGateUnavailable(
            "goose_amw is not importable; approval gate refuses to run "
            "without the canonical idempotency key"
        ) from exc
    return canonical_module


def _ttl_from_env() -> int:
    try:
        return int(os.environ.get(TTL_ENV, str(DEFAULT_TTL_SECONDS)))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _clamp_ttl(value: int) -> int:
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, value))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError) as exc:
        raise _StateTampered("bad base64url") from exc


def _seal_state(secret: bytes, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret, body, hashlib.sha256).digest()
    return f"{_b64encode(body)}.{_b64encode(signature)}"


def _unseal_state(secret: bytes, token: str) -> dict[str, Any]:
    try:
        encoded_body, encoded_sig = token.split(".", 1)
        body = _b64decode(encoded_body)
        signature = _b64decode(encoded_sig)
    except ValueError as exc:
        raise _StateTampered("malformed state") from exc
    expected = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise _StateTampered("bad signature")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _StateTampered("bad payload") from exc
    if not isinstance(payload, dict) or payload.get("v") != STATE_VERSION:
        raise _StateTampered("unknown state version")
    return payload


@dataclass
class ApprovalRecord:
    """One pending human decision. Raw args are never stored, only their hash."""

    nonce: str
    idempotency_key: str
    args_hash: str
    principal: str
    tool: str
    session_id: str
    status: ApprovalStatus = "pending"
    created_at: float = 0.0
    deadline: float = 0.0
    decided_at: float | None = None
    consumed: bool = False


class ApprovalStore:
    """Interface for pending approvals so a DB-backed store can replace memory."""

    def save(self, record: ApprovalRecord) -> None:
        raise NotImplementedError

    def get(self, nonce: str) -> ApprovalRecord | None:
        raise NotImplementedError

    def list_pending(self) -> list[ApprovalRecord]:
        raise NotImplementedError

    def decide(
        self, nonce: str, approved: bool, *, now: float
    ) -> ApprovalRecord | None:
        """Flip pending to approved/denied; None when missing or not pending."""
        raise NotImplementedError

    def consume(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        """Single-use: first call on approved marks consumed; later calls None.

        Expired approvals (``deadline`` past ``now``) never consume.
        """
        raise NotImplementedError

    def mark_expired(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        """Flip pending to expired; None when missing."""
        raise NotImplementedError

    async def asave(self, record: ApprovalRecord) -> None:
        """Async ``save`` for handlers already on the app event loop."""
        raise NotImplementedError

    async def aget(self, nonce: str) -> ApprovalRecord | None:
        """Async ``get`` for handlers already on the app event loop."""
        raise NotImplementedError

    async def alist_pending(self) -> list[ApprovalRecord]:
        """Async ``list_pending`` for handlers already on the app event loop."""
        raise NotImplementedError

    async def adecide(
        self, nonce: str, approved: bool, *, now: float
    ) -> ApprovalRecord | None:
        """Async ``decide`` for handlers already on the app event loop."""
        raise NotImplementedError

    async def aconsume(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        """Async ``consume`` for handlers already on the app event loop."""
        raise NotImplementedError

    async def amark_expired(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        """Async ``mark_expired`` for handlers already on the app event loop."""
        raise NotImplementedError


class InMemoryApprovalStore(ApprovalStore):
    """Thread-safe in-memory store. Process restart drops pending approvals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ApprovalRecord] = {}

    def save(self, record: ApprovalRecord) -> None:
        with self._lock:
            self._records[record.nonce] = replace(record)

    def get(self, nonce: str) -> ApprovalRecord | None:
        with self._lock:
            record = self._records.get(nonce)
            return replace(record) if record else None

    def list_pending(self) -> list[ApprovalRecord]:
        with self._lock:
            return [
                replace(record)
                for record in self._records.values()
                if record.status == "pending"
            ]

    def decide(
        self, nonce: str, approved: bool, *, now: float
    ) -> ApprovalRecord | None:
        with self._lock:
            record = self._records.get(nonce)
            if record is None or record.status != "pending":
                return None
            updated = replace(
                record,
                status="approved" if approved else "denied",
                decided_at=now,
            )
            self._records[nonce] = updated
            return replace(updated)

    def consume(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        with self._lock:
            record = self._records.get(nonce)
            if record is None or record.status != "approved" or record.consumed:
                return None
            if record.deadline <= now:
                return None
            updated = replace(record, consumed=True)
            self._records[nonce] = updated
            return replace(updated)

    def mark_expired(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        with self._lock:
            record = self._records.get(nonce)
            if record is None:
                return None
            if record.status == "pending":
                record = replace(record, status="expired", decided_at=now)
                self._records[nonce] = record
            return replace(record)

    async def asave(self, record: ApprovalRecord) -> None:
        self.save(record)

    async def aget(self, nonce: str) -> ApprovalRecord | None:
        return self.get(nonce)

    async def alist_pending(self) -> list[ApprovalRecord]:
        return self.list_pending()

    async def adecide(
        self, nonce: str, approved: bool, *, now: float
    ) -> ApprovalRecord | None:
        return self.decide(nonce, approved, now=now)

    async def aconsume(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        return self.consume(nonce, now=now)

    async def amark_expired(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        return self.mark_expired(nonce, now=now)


_T = TypeVar("_T")


def _run_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Drive one DB coroutine from the store's synchronous interface.

    Sync callers (scripts, the sync ``tools/call`` path) run only when no
    event loop is running, via a plain ``asyncio.run``. From inside a
    running loop this refuses instead of hopping threads: the shared
    ``AsyncEngine`` is loop-bound (asyncpg), so touching it from another
    loop corrupts pooled connections. Async callers must await the
    store's ``a*`` methods directly on the app loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError(
        "DbApprovalStore sync methods cannot run inside a running event "
        "loop; await the async (a*) methods on the app loop instead"
    )


def _to_db_time(value: float) -> datetime:
    """Epoch seconds to the naive-UTC form used for persistence."""
    return to_naive_utc(datetime.fromtimestamp(value, tz=timezone.utc))


def _from_db_time(value: datetime) -> float:
    """Naive-UTC database timestamp back to epoch seconds."""
    return value.replace(tzinfo=timezone.utc).timestamp()


def _model_from_record(record: ApprovalRecord) -> ApprovalGateModel:
    return ApprovalGateModel(
        nonce=record.nonce,
        idempotency_key=record.idempotency_key,
        args_hash=record.args_hash,
        principal=record.principal,
        tool=record.tool,
        session_id=record.session_id,
        status=record.status,
        created_at=_to_db_time(record.created_at),
        deadline=_to_db_time(record.deadline),
        decided_at=(
            _to_db_time(record.decided_at) if record.decided_at is not None else None
        ),
        consumed_at=utc_now() if record.consumed else None,
    )


def _record_from_model(model: ApprovalGateModel) -> ApprovalRecord:
    # The table check constraint admits only the four ApprovalStatus values.
    status = cast(ApprovalStatus, model.status)
    return ApprovalRecord(
        nonce=model.nonce,
        idempotency_key=model.idempotency_key,
        args_hash=model.args_hash,
        principal=model.principal,
        tool=model.tool,
        session_id=model.session_id,
        status=status,
        created_at=_from_db_time(model.created_at),
        deadline=_from_db_time(model.deadline),
        decided_at=(
            _from_db_time(model.decided_at) if model.decided_at is not None else None
        ),
        consumed=model.consumed_at is not None,
    )


class DbApprovalStore(ApprovalStore):
    """Durable store on the gateway database via the shared session factory.

    Decisions and consumption survive a restart: a consumed nonce stays
    consumed because consume is a conditional ``UPDATE ... WHERE
    status='approved' AND consumed_at IS NULL AND deadline > now`` whose
    rowcount admits exactly one winner, so neither a restart nor concurrent
    retries can replay it, and an expired approval can never spend.
    """

    def save(self, record: ApprovalRecord) -> None:
        _run_sync(self.asave(record))

    async def asave(self, record: ApprovalRecord) -> None:
        factory = get_session_factory()
        async with factory() as session:
            await session.merge(_model_from_record(record))
            await session.commit()

    def get(self, nonce: str) -> ApprovalRecord | None:
        return _run_sync(self.aget(nonce))

    async def aget(self, nonce: str) -> ApprovalRecord | None:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ApprovalGateModel).where(
                    cast(ColumnElement[bool], ApprovalGateModel.nonce == nonce)
                )
            )
            model = result.scalar_one_or_none()
            return _record_from_model(model) if model is not None else None

    def list_pending(self) -> list[ApprovalRecord]:
        return _run_sync(self.alist_pending())

    async def alist_pending(self) -> list[ApprovalRecord]:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ApprovalGateModel)
                .where(
                    cast(
                        ColumnElement[bool],
                        ApprovalGateModel.status == "pending",
                    )
                )
                .order_by(
                    col(ApprovalGateModel.created_at).asc(),
                    col(ApprovalGateModel.nonce).asc(),
                )
            )
            return [_record_from_model(model) for model in result.scalars().all()]

    def decide(
        self, nonce: str, approved: bool, *, now: float
    ) -> ApprovalRecord | None:
        return _run_sync(self.adecide(nonce, approved, now=now))

    async def adecide(
        self, nonce: str, approved: bool, *, now: float
    ) -> ApprovalRecord | None:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ApprovalGateModel).where(
                    cast(ColumnElement[bool], ApprovalGateModel.nonce == nonce)
                )
            )
            model = result.scalar_one_or_none()
            if model is None or model.status != "pending":
                return None
            updated = await session.execute(
                update(ApprovalGateModel)
                .where(
                    cast(ColumnElement[bool], ApprovalGateModel.nonce == nonce),
                    cast(
                        ColumnElement[bool],
                        ApprovalGateModel.status == "pending",
                    ),
                )
                .values(
                    status="approved" if approved else "denied",
                    decided_at=_to_db_time(now),
                )
            )
            await session.commit()
            if cast(Any, updated).rowcount != 1:
                return None
            await session.refresh(model)
            return _record_from_model(model)

    def consume(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        return _run_sync(self.aconsume(nonce, now=now))

    async def aconsume(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        """Atomically spend an approved, unconsumed, unexpired approval.

        The ``WHERE status='approved' AND consumed_at IS NULL AND
        deadline > now`` clause is the single serialization point: exactly
        one caller (one retry, one transport) wins, a restart cannot
        resurrect a spent approval, and an approval whose deadline passed
        between the gate's expiry check and this UPDATE still cannot spend.
        """
        factory = get_session_factory()
        async with factory() as session:
            updated = await session.execute(
                update(ApprovalGateModel)
                .where(
                    cast(ColumnElement[bool], ApprovalGateModel.nonce == nonce),
                    cast(
                        ColumnElement[bool],
                        ApprovalGateModel.status == "approved",
                    ),
                    cast(
                        ColumnElement[bool],
                        cast(Any, ApprovalGateModel.consumed_at).is_(None),
                    ),
                    cast(
                        ColumnElement[bool],
                        ApprovalGateModel.deadline > _to_db_time(now),
                    ),
                )
                .values(consumed_at=utc_now())
            )
            await session.commit()
            if cast(Any, updated).rowcount != 1:
                return None
            result = await session.execute(
                select(ApprovalGateModel).where(
                    cast(ColumnElement[bool], ApprovalGateModel.nonce == nonce)
                )
            )
            model = result.scalar_one_or_none()
            return _record_from_model(model) if model is not None else None

    def mark_expired(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        return _run_sync(self.amark_expired(nonce, now=now))

    async def amark_expired(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ApprovalGateModel).where(
                    cast(ColumnElement[bool], ApprovalGateModel.nonce == nonce)
                )
            )
            model = result.scalar_one_or_none()
            if model is None:
                return None
            if model.status == "pending":
                await session.execute(
                    update(ApprovalGateModel)
                    .where(
                        cast(
                            ColumnElement[bool],
                            ApprovalGateModel.nonce == nonce,
                        ),
                        cast(
                            ColumnElement[bool],
                            ApprovalGateModel.status == "pending",
                        ),
                    )
                    .values(status="expired", decided_at=_to_db_time(now))
                )
                await session.commit()
                await session.refresh(model)
            return _record_from_model(model)


ReceiptSink = Callable[[str, dict[str, Any]], None]


def _log_receipt_sink(event: str, record: dict[str, Any]) -> None:
    logger.info(
        "approval_receipt event=%s record=%s", event, json.dumps(record, sort_keys=True)
    )


@dataclass(frozen=True)
class ApprovalDecision:
    action: DecisionAction
    payload: dict[str, Any] | None = None
    idempotency_key: str | None = None
    reason: RejectReason | None = None
    nonce: str | None = None


def _pause_payload(tool: str, nonce: str, request_state: str) -> dict[str, Any]:
    """Build the SEP-2322 ``input_required`` pause payload.

    ``inputRequests`` is a map keyed by request id; each value is the client
    request the server wants answered, here an ``elicitation/create`` asking
    a human to approve the paused tool call. The client answers with an
    MCP inputResponses map keyed by the same id.
    """
    return {
        "resultType": "input_required",
        "inputRequests": {
            nonce: {
                "method": "elicitation/create",
                "params": {
                    "message": f"Approve tool call: {tool}",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"approved": {"type": "boolean"}},
                        "required": ["approved"],
                    },
                },
            }
        },
        "requestState": request_state,
    }


class ApprovalGate:
    """Entry point for the future mcp_standard.py hook."""

    def __init__(
        self,
        *,
        secret: str | None = None,
        ttl_seconds: int | None = None,
        store: ApprovalStore | None = None,
        clock: Callable[[], float] = time.time,
        receipt_sink: ReceiptSink | None = None,
    ) -> None:
        raw_secret = (
            secret if secret is not None else os.environ.get(STATE_SECRET_ENV, "")
        )
        self._secret = raw_secret.encode("utf-8") if raw_secret else None
        ttl = ttl_seconds if ttl_seconds is not None else _ttl_from_env()
        self._ttl = _clamp_ttl(ttl)
        self.store = store or DbApprovalStore()
        self.clock = clock
        self.receipt_sink = receipt_sink or _log_receipt_sink

    def _require_secret(self) -> bytes:
        if not self._secret:
            raise ApprovalGateUnavailable(
                f"{STATE_SECRET_ENV} is not set; approval gate refuses to run unsealed"
            )
        return self._secret

    def _emit(self, event: str, **fields: Any) -> None:
        self.receipt_sink(event, fields)

    def evaluate(
        self,
        caller: str,
        tool: str,
        arguments: dict[str, Any] | None,
        session_id: str,
        client_supports_input_required: bool,
        request_state: str | None = None,
        input_responses: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> ApprovalDecision:
        """Decide PAUSE, EXECUTE, or REJECT for one tools/call attempt.

        Sync path for callers with no running event loop. Async callers
        (FastAPI handlers, the async MCP hook) must use :meth:`aevaluate`,
        which awaits the store on the app loop instead of crossing loops
        with the shared engine.
        """
        canonical = _load_canonical()
        if request_id is not None:
            logger.debug("approval request_id=%s logged only, never keyed", request_id)
        args_hash = canonical.args_hash(arguments)
        idempotency_key = canonical.idempotency_key(
            caller=caller, tool=tool, arguments=arguments, session_id=session_id
        )
        if request_state is None:
            return self._pause_new_call(
                caller=caller,
                tool=tool,
                session_id=session_id,
                args_hash=args_hash,
                idempotency_key=idempotency_key,
                client_supports_input_required=client_supports_input_required,
            )
        return self._evaluate_retry(
            caller=caller,
            tool=tool,
            session_id=session_id,
            args_hash=args_hash,
            idempotency_key=idempotency_key,
            client_supports_input_required=client_supports_input_required,
            request_state=request_state,
            input_responses=input_responses,
        )

    async def aevaluate(
        self,
        caller: str,
        tool: str,
        arguments: dict[str, Any] | None,
        session_id: str,
        client_supports_input_required: bool,
        request_state: str | None = None,
        input_responses: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> ApprovalDecision:
        """Async mirror of :meth:`evaluate` for callers on the app loop.

        Awaits the store directly on the running loop; never crosses loops
        with the shared engine.
        """
        canonical = _load_canonical()
        if request_id is not None:
            logger.debug("approval request_id=%s logged only, never keyed", request_id)
        args_hash = canonical.args_hash(arguments)
        idempotency_key = canonical.idempotency_key(
            caller=caller, tool=tool, arguments=arguments, session_id=session_id
        )
        if request_state is None:
            return await self._apause_new_call(
                caller=caller,
                tool=tool,
                session_id=session_id,
                args_hash=args_hash,
                idempotency_key=idempotency_key,
                client_supports_input_required=client_supports_input_required,
            )
        return await self._aevaluate_retry(
            caller=caller,
            tool=tool,
            session_id=session_id,
            args_hash=args_hash,
            idempotency_key=idempotency_key,
            client_supports_input_required=client_supports_input_required,
            request_state=request_state,
            input_responses=input_responses,
        )

    def _check_sealed_fields(
        self,
        sealed: dict[str, Any],
        *,
        caller: str,
        tool: str,
        session_id: str,
        args_hash: str,
        idempotency_key: str,
        nonce: str,
    ) -> ApprovalDecision | None:
        """Reject when the retry inputs drift from the sealed state."""
        if sealed.get("tool") != tool:
            self._emit("tool_mismatch", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(
                action="REJECT", reason="tool_mismatch", nonce=nonce
            )
        if sealed.get("sessionId") != session_id:
            self._emit("session_mismatch", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(
                action="REJECT", reason="session_mismatch", nonce=nonce
            )
        if sealed.get("principal") != caller:
            self._emit("principal_mismatch", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(
                action="REJECT", reason="principal_mismatch", nonce=nonce
            )
        if sealed.get("argsHash") != args_hash:
            self._emit("args_mismatch", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(
                action="REJECT", reason="args_mismatch", nonce=nonce
            )
        # Last: the key is derived from caller, tool, args, and session, so a
        # mismatch here with all inputs matching means the key derivation
        # itself drifted between pause and retry.
        if sealed.get("idempotencyKey") != idempotency_key:
            self._emit("key_mismatch", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="key_mismatch", nonce=nonce)
        return None

    def _check_input_responses(
        self,
        input_responses: dict[str, Any] | None,
        *,
        nonce: str,
        tool: str,
        caller: str,
    ) -> ApprovalDecision | None:
        """Reject when the retry answers no input request for this pause.

        SEP-2322 keys MCP inputResponses by the same request id the pause
        carried in ``inputRequests``. The response content itself is ignored:
        only the local human decision in the store can approve.
        """
        if not isinstance(input_responses, dict) or nonce not in input_responses:
            self._emit("response_mismatch", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(
                action="REJECT", reason="response_mismatch", nonce=nonce
            )
        return None

    def _check_record(
        self,
        record: ApprovalRecord,
        *,
        tool: str,
        session_id: str,
        idempotency_key: str,
        caller: str,
    ) -> ApprovalDecision | None:
        """Reject when the stored record drifts from the retry inputs."""
        nonce = record.nonce
        if record.tool != tool:
            self._emit("tool_mismatch", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(
                action="REJECT", reason="tool_mismatch", nonce=nonce
            )
        if record.session_id != session_id:
            self._emit("session_mismatch", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(
                action="REJECT", reason="session_mismatch", nonce=nonce
            )
        if record.idempotency_key != idempotency_key:
            self._emit("key_mismatch", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="key_mismatch", nonce=nonce)
        return None

    @staticmethod
    def _is_expired(now: float, sealed_deadline: Any, record: ApprovalRecord) -> bool:
        return (
            not isinstance(sealed_deadline, (int, float))
            or now > sealed_deadline
            or now > record.deadline
            or record.status == "expired"
        )

    def _pause_new_call(
        self,
        *,
        caller: str,
        tool: str,
        session_id: str,
        args_hash: str,
        idempotency_key: str,
        client_supports_input_required: bool,
    ) -> ApprovalDecision:
        if not client_supports_input_required:
            self._emit(
                "unsupported_client",
                nonce=None,
                tool=tool,
                principal=caller,
                session_id=session_id,
            )
            return ApprovalDecision(action="REJECT", reason="unsupported_client")
        secret = self._require_secret()
        now = self.clock()
        nonce = secrets.token_urlsafe(16)
        deadline = now + self._ttl
        self.store.save(
            ApprovalRecord(
                nonce=nonce,
                idempotency_key=idempotency_key,
                args_hash=args_hash,
                principal=caller,
                tool=tool,
                session_id=session_id,
                created_at=now,
                deadline=deadline,
            )
        )
        state = _seal_state(
            secret,
            {
                "v": STATE_VERSION,
                "argsHash": args_hash,
                "principal": caller,
                "tool": tool,
                "sessionId": session_id,
                "idempotencyKey": idempotency_key,
                "deadline": deadline,
                "nonce": nonce,
            },
        )
        return ApprovalDecision(
            action="PAUSE",
            nonce=nonce,
            payload=_pause_payload(tool, nonce, state),
        )

    async def _apause_new_call(
        self,
        *,
        caller: str,
        tool: str,
        session_id: str,
        args_hash: str,
        idempotency_key: str,
        client_supports_input_required: bool,
    ) -> ApprovalDecision:
        if not client_supports_input_required:
            self._emit(
                "unsupported_client",
                nonce=None,
                tool=tool,
                principal=caller,
                session_id=session_id,
            )
            return ApprovalDecision(action="REJECT", reason="unsupported_client")
        secret = self._require_secret()
        now = self.clock()
        nonce = secrets.token_urlsafe(16)
        deadline = now + self._ttl
        await self.store.asave(
            ApprovalRecord(
                nonce=nonce,
                idempotency_key=idempotency_key,
                args_hash=args_hash,
                principal=caller,
                tool=tool,
                session_id=session_id,
                created_at=now,
                deadline=deadline,
            )
        )
        state = _seal_state(
            secret,
            {
                "v": STATE_VERSION,
                "argsHash": args_hash,
                "principal": caller,
                "tool": tool,
                "sessionId": session_id,
                "idempotencyKey": idempotency_key,
                "deadline": deadline,
                "nonce": nonce,
            },
        )
        return ApprovalDecision(
            action="PAUSE",
            nonce=nonce,
            payload=_pause_payload(tool, nonce, state),
        )

    def _evaluate_retry(
        self,
        *,
        caller: str,
        tool: str,
        session_id: str,
        args_hash: str,
        idempotency_key: str,
        client_supports_input_required: bool,
        request_state: str,
        input_responses: dict[str, Any] | None,
    ) -> ApprovalDecision:
        if not client_supports_input_required:
            self._emit(
                "unsupported_client",
                nonce=None,
                tool=tool,
                principal=caller,
                session_id=session_id,
            )
            return ApprovalDecision(action="REJECT", reason="unsupported_client")
        secret = self._require_secret()
        try:
            sealed = _unseal_state(secret, request_state)
        except _StateTampered:
            self._emit(
                "tampered",
                nonce=None,
                tool=tool,
                principal=caller,
                session_id=session_id,
            )
            return ApprovalDecision(action="REJECT", reason="tampered")
        nonce = sealed.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            self._emit(
                "tampered",
                nonce=None,
                tool=tool,
                principal=caller,
                session_id=session_id,
            )
            return ApprovalDecision(action="REJECT", reason="tampered")
        rejected = self._check_sealed_fields(
            sealed,
            caller=caller,
            tool=tool,
            session_id=session_id,
            args_hash=args_hash,
            idempotency_key=idempotency_key,
            nonce=nonce,
        )
        if rejected is not None:
            return rejected
        rejected = self._check_input_responses(
            input_responses, nonce=nonce, tool=tool, caller=caller
        )
        if rejected is not None:
            return rejected
        record = self.store.get(nonce)
        if record is None:
            self._emit("tampered", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="tampered", nonce=nonce)
        rejected = self._check_record(
            record,
            tool=tool,
            session_id=session_id,
            idempotency_key=idempotency_key,
            caller=caller,
        )
        if rejected is not None:
            return rejected
        now = self.clock()
        if self._is_expired(now, sealed.get("deadline"), record):
            self.store.mark_expired(nonce, now=now)
            self._emit("timeout", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="timeout", nonce=nonce)
        if record.status == "denied":
            self._emit("denied", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="denied", nonce=nonce)
        if record.status == "pending":
            return ApprovalDecision(
                action="PAUSE",
                nonce=nonce,
                payload=_pause_payload(tool, nonce, request_state),
            )
        consumed = self.store.consume(nonce, now=now)
        if consumed is None:
            self._emit("replayed", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="replayed", nonce=nonce)
        return ApprovalDecision(
            action="EXECUTE", idempotency_key=idempotency_key, nonce=nonce
        )

    async def _aevaluate_retry(
        self,
        *,
        caller: str,
        tool: str,
        session_id: str,
        args_hash: str,
        idempotency_key: str,
        client_supports_input_required: bool,
        request_state: str,
        input_responses: dict[str, Any] | None,
    ) -> ApprovalDecision:
        """Async mirror of :meth:`_evaluate_retry` on the app loop."""
        if not client_supports_input_required:
            self._emit(
                "unsupported_client",
                nonce=None,
                tool=tool,
                principal=caller,
                session_id=session_id,
            )
            return ApprovalDecision(action="REJECT", reason="unsupported_client")
        secret = self._require_secret()
        try:
            sealed = _unseal_state(secret, request_state)
        except _StateTampered:
            self._emit(
                "tampered",
                nonce=None,
                tool=tool,
                principal=caller,
                session_id=session_id,
            )
            return ApprovalDecision(action="REJECT", reason="tampered")
        nonce = sealed.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            self._emit(
                "tampered",
                nonce=None,
                tool=tool,
                principal=caller,
                session_id=session_id,
            )
            return ApprovalDecision(action="REJECT", reason="tampered")
        rejected = self._check_sealed_fields(
            sealed,
            caller=caller,
            tool=tool,
            session_id=session_id,
            args_hash=args_hash,
            idempotency_key=idempotency_key,
            nonce=nonce,
        )
        if rejected is not None:
            return rejected
        rejected = self._check_input_responses(
            input_responses, nonce=nonce, tool=tool, caller=caller
        )
        if rejected is not None:
            return rejected
        record = await self.store.aget(nonce)
        if record is None:
            self._emit("tampered", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="tampered", nonce=nonce)
        rejected = self._check_record(
            record,
            tool=tool,
            session_id=session_id,
            idempotency_key=idempotency_key,
            caller=caller,
        )
        if rejected is not None:
            return rejected
        now = self.clock()
        if self._is_expired(now, sealed.get("deadline"), record):
            await self.store.amark_expired(nonce, now=now)
            self._emit("timeout", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="timeout", nonce=nonce)
        if record.status == "denied":
            self._emit("denied", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="denied", nonce=nonce)
        if record.status == "pending":
            return ApprovalDecision(
                action="PAUSE",
                nonce=nonce,
                payload=_pause_payload(tool, nonce, request_state),
            )
        consumed = await self.store.aconsume(nonce, now=now)
        if consumed is None:
            self._emit("replayed", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="replayed", nonce=nonce)
        return ApprovalDecision(
            action="EXECUTE", idempotency_key=idempotency_key, nonce=nonce
        )
