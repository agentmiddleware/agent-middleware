"""Human approval gate for irreversible MCP tool calls (AMW V2).

SEP-2322 flow: the first ``tools/call`` for an irreversible tool PAUSEs with a
``resultType='input_required'`` result carrying an opaque ``requestState``. The
client retries the same ``tools/call`` with ``inputResponses`` plus the
``requestState``; this gate EXECUTEs only after a local human approved, else it
REJECTs. The hook in ``mcp_standard.py`` that calls :meth:`ApprovalGate.evaluate`
lands in a later PR.

Fail-closed rules:

- The idempotency key is the single G1 key from
  ``goose_amw.canonical.idempotency_key`` (verified caller + tool + canonical
  args hash + agent session id). The Goose request id is logged only.
- ``requestState`` is HMAC-SHA256 sealed; missing secret fails closed.
- Clients without ``input_required`` support are rejected.
- Deny, timeout, and tampering each emit a receipt via an injectable sink and
  never execute.
"""

from __future__ import annotations

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
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal

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
    "args_mismatch",
    "principal_mismatch",
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
        from goose_amw import canonical as canonical_module
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

    def consume(self, nonce: str) -> ApprovalRecord | None:
        """Single-use: first call on approved marks consumed; later calls None."""
        raise NotImplementedError

    def mark_expired(self, nonce: str, *, now: float) -> ApprovalRecord | None:
        """Flip pending to expired; None when missing."""
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

    def consume(self, nonce: str) -> ApprovalRecord | None:
        with self._lock:
            record = self._records.get(nonce)
            if record is None or record.status != "approved" or record.consumed:
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
        self.store = store or InMemoryApprovalStore()
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
        input_responses: list[Any] | dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> ApprovalDecision:
        """Decide PAUSE, EXECUTE, or REJECT for one tools/call attempt."""
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
                "deadline": deadline,
                "nonce": nonce,
            },
        )
        return ApprovalDecision(
            action="PAUSE",
            nonce=nonce,
            payload={
                "resultType": "input_required",
                "inputRequests": [
                    {
                        "requestId": nonce,
                        "prompt": f"Approve tool call: {tool}",
                        "schema": {
                            "type": "object",
                            "properties": {"approved": {"type": "boolean"}},
                            "required": ["approved"],
                        },
                    }
                ],
                "requestState": state,
            },
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
    ) -> ApprovalDecision:
        if not client_supports_input_required:
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
        record = self.store.get(nonce) if isinstance(nonce, str) else None
        if record is None:
            self._emit("tampered", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="tampered", nonce=nonce)
        now = self.clock()
        sealed_deadline = sealed.get("deadline")
        if (
            not isinstance(sealed_deadline, (int, float))
            or now > sealed_deadline
            or now > record.deadline
            or record.status == "expired"
        ):
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
                payload={
                    "resultType": "input_required",
                    "inputRequests": [
                        {
                            "requestId": nonce,
                            "prompt": f"Approve tool call: {tool}",
                            "schema": {
                                "type": "object",
                                "properties": {"approved": {"type": "boolean"}},
                                "required": ["approved"],
                            },
                        }
                    ],
                    "requestState": request_state,
                },
            )
        consumed = self.store.consume(nonce)
        if consumed is None:
            self._emit("replayed", nonce=nonce, tool=tool, principal=caller)
            return ApprovalDecision(action="REJECT", reason="replayed", nonce=nonce)
        return ApprovalDecision(
            action="EXECUTE", idempotency_key=idempotency_key, nonce=nonce
        )
