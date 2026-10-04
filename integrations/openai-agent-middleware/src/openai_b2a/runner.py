"""Governed execution of OpenAI tool calls through the trust plane.

Operation identity
------------------
OpenAI assigns every tool call an id (``call_…``) once, and that id lives in
the conversation transcript the application already persists. A retry of the
same tool call — after a dropped connection, a crashed worker, a resumed run —
therefore carries the same id. That is exactly the property the trust plane's
``Idempotency-Key`` exists to capture, so this runner derives the key from the
id (``oai-<tool_call.id>``) instead of minting one per attempt.

The derivation is written to an :class:`OperationKeyStore` *before* the first
network call, so the key survives a crash between "the model asked" and "the
receipt came back". A tool call without an id is refused, never given a fresh
UUID: a fresh key would turn the retry into a second charged action — the same
failure the trust plane refuses on its side as ``invalid_idempotency_key``.

Permits
-------
One permit per (run, tool). Its idempotency key is ``oai-permit-<run>-<tool>``
and the complete permit request is recorded in the store before the permit
request goes out, so any later attempt — a retry after a crash, or the next
tool call in a resumed process — re-sends the identical request and the server
replays the permit instead of rejecting a fresh timestamp under the same key.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from b2a_sdk.models import InvocationResult, PermitRequest, Receipt

from .client import B2AClient

#: The trust plane stores a client key verbatim in a 128-character column and
#: ``b2a_sdk`` enforces the same bound before sending. Checked here so a
#: pathological tool-call id fails before any record is written.
MAX_IDEMPOTENCY_KEY_LENGTH = 128
_OPERATION_KEY_PREFIX = "oai-"
_PERMIT_KEY_PREFIX = "oai-permit-"
_FUNCTION_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_-]")


def _validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-blank string")
    if not value.isascii() or not value.isprintable() or value.strip() != value:
        raise ValueError(f"{label} must be printable ASCII with no surrounding whitespace")
    if len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(f"{label} is too long (limit {MAX_IDEMPOTENCY_KEY_LENGTH} characters)")
    return value


# ── Tool calls ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call as the model emitted it, in a shape-independent form."""

    id: str
    name: str
    arguments: dict[str, Any]


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError("tool call arguments must decode to a JSON object")
        return parsed
    raise TypeError("tool call arguments must be a JSON object or a JSON string")


def normalize_tool_call(tool_call: Any) -> ToolCall:
    """Accept the shapes OpenAI emits and return one :class:`ToolCall`.

    * Chat Completions ``tool_calls[i]``: ``id`` plus ``function.name`` and
      ``function.arguments`` (a JSON string). Objects or dicts.
    * Responses API / Agents SDK ``function_call`` items: ``call_id``,
      ``name``, ``arguments``. Here ``id`` is the *item* id (``fc_…``), not
      the call id, so it is deliberately not used as the operation identity.

    The id is the operation identity, so it must be present, printable, and
    short enough to fit the trust plane's key column once prefixed.
    """
    function = _field(tool_call, "function")
    if function is not None:
        call_id = _field(tool_call, "id")
        name = _field(function, "name")
        arguments = _field(function, "arguments")
    else:
        call_id = _field(tool_call, "call_id")
        name = _field(tool_call, "name")
        arguments = _field(tool_call, "arguments")

    if not isinstance(call_id, str) or not call_id.strip():
        raise ValueError(
            "tool call has no id: the model's tool_call.id is the operation identity "
            "and this runner never invents one"
        )
    operation_key_for(call_id)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("tool call has no function name")
    return ToolCall(id=call_id, name=name, arguments=_parse_arguments(arguments))


def operation_key_for(tool_call_id: str) -> str:
    """The trust plane ``Idempotency-Key`` for one OpenAI tool call."""
    _validate_identifier(tool_call_id, "tool call id")
    return _validate_identifier(
        f"{_OPERATION_KEY_PREFIX}{tool_call_id}", "operation idempotency key"
    )


def function_name_for(tool_name: str) -> str:
    """OpenAI function names are ``[a-zA-Z0-9_-]{1,64}``; MCP names may carry dots."""
    return _FUNCTION_NAME_INVALID.sub("_", tool_name)[:64]


# ── Durable records ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class OperationRecord:
    """What must survive a crash for a tool call to be retried as *one* action."""

    tool_call_id: str
    tool_name: str
    idempotency_key: str
    permit_idempotency_key: str
    permit_id: str | None = None
    receipt_id: str | None = None
    wallet_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationRecord:
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass(slots=True)
class PermitRecord:
    """The permit request a (run, tool) pair sends, fixed before it is sent.

    The server hashes the whole permit body under the idempotency key. The
    JSON-safe snapshot fixes its wallet, budget, scopes, and expiry across
    retries, even if a resumed runner has different configuration.
    """

    permit_idempotency_key: str
    tool_name: str
    expires_at: str
    permit_id: str | None = None
    request_payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PermitRecord:
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


class OperationKeyStore(Protocol):
    """Persistence for operation and permit records."""

    def get_operation(self, tool_call_id: str) -> OperationRecord | None: ...

    def put_operation(self, record: OperationRecord) -> None: ...

    def get_permit(self, permit_idempotency_key: str) -> PermitRecord | None: ...

    def put_permit(self, record: PermitRecord) -> None: ...


class InMemoryOperationKeyStore:
    """Process-local store: fine for tests and for runs that never resume."""

    def __init__(self) -> None:
        self._operations: dict[str, dict[str, Any]] = {}
        self._permits: dict[str, dict[str, Any]] = {}

    def get_operation(self, tool_call_id: str) -> OperationRecord | None:
        raw = self._operations.get(tool_call_id)
        return OperationRecord.from_dict(raw) if raw else None

    def put_operation(self, record: OperationRecord) -> None:
        self._operations[record.tool_call_id] = record.to_dict()

    def get_permit(self, permit_idempotency_key: str) -> PermitRecord | None:
        raw = self._permits.get(permit_idempotency_key)
        return PermitRecord.from_dict(raw) if raw else None

    def put_permit(self, record: PermitRecord) -> None:
        self._permits[record.permit_idempotency_key] = record.to_dict()


class JsonFileOperationKeyStore:
    """One JSON file, rewritten atomically on every put.

    Enough for a single agent process that may crash and resume. Multi-process
    deployments should back the protocol with their own database.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {"operations": {}, "permits": {}}
        data = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            raise TypeError(f"{self._path}: operation key store must be a JSON object")
        data.setdefault("operations", {})
        data.setdefault("permits", {})
        return data

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=self._path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get_operation(self, tool_call_id: str) -> OperationRecord | None:
        raw = self._load()["operations"].get(tool_call_id)
        return OperationRecord.from_dict(raw) if raw else None

    def put_operation(self, record: OperationRecord) -> None:
        data = self._load()
        data["operations"][record.tool_call_id] = record.to_dict()
        self._save(data)

    def get_permit(self, permit_idempotency_key: str) -> PermitRecord | None:
        raw = self._load()["permits"].get(permit_idempotency_key)
        return PermitRecord.from_dict(raw) if raw else None

    def put_permit(self, record: PermitRecord) -> None:
        data = self._load()
        data["permits"][record.permit_idempotency_key] = record.to_dict()
        self._save(data)


# ── Results ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class GovernedToolResult:
    """The governed outcome of one tool call, ready to hand back to the model."""

    tool_call_id: str
    tool_name: str
    idempotency_key: str
    result: InvocationResult

    @property
    def receipt(self) -> Receipt:
        return self.result.receipt

    def output_payload(self) -> dict[str, Any]:
        receipt = self.result.receipt
        return {
            "content": self.result.content,
            "structured_content": self.result.structured_content,
            "receipt": {
                "receipt_id": receipt.receipt_id,
                "outcome": receipt.outcome,
                "credits_charged": str(receipt.credits_charged),
                "signature": receipt.signature,
                "signature_key_id": receipt.signature_key_id,
            },
            "idempotency_key": self.idempotency_key,
        }

    def as_tool_message(self) -> dict[str, Any]:
        """Chat Completions: the ``role: tool`` message answering the tool call."""
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": json.dumps(self.output_payload()),
        }

    def as_function_call_output(self) -> dict[str, Any]:
        """Responses API / Agents SDK: the ``function_call_output`` item."""
        return {
            "type": "function_call_output",
            "call_id": self.tool_call_id,
            "output": json.dumps(self.output_payload()),
        }


# ── Runner ───────────────────────────────────────────────────────────────────


class GovernedToolRunner:
    """Run OpenAI tool calls as governed permit → invoke → receipt actions.

    Args:
        client: connected :class:`B2AClient` (wallet-scoped API key).
        wallet_id: the wallet every call is metered against.
        run_id: a stable identifier for this agent run (an OpenAI response id,
            thread id, or your own job id). It scopes the per-tool permit key,
            so it must be the *same* string when a crashed run is resumed. A
            new run with new tool-call IDs is needed for work requiring fresh
            permits after expiry; recorded calls remain bound to their run.
        key_store: where operation and permit records persist. Defaults to
            in-memory, which is only safe for runs that are never resumed.
        permit_budget / permit_ttl_minutes: the shape of each auto-issued permit.
    """

    def __init__(
        self,
        client: B2AClient,
        *,
        wallet_id: str,
        run_id: str,
        key_store: OperationKeyStore | None = None,
        permit_budget: Decimal = Decimal(100),
        permit_ttl_minutes: int = 30,
    ) -> None:
        if not isinstance(run_id, str):
            raise TypeError("run_id must be a string")
        _validate_identifier(run_id, "run_id")
        self._client = client
        self._wallet_id = wallet_id
        self._run_id = run_id
        self._key_store: OperationKeyStore = key_store or InMemoryOperationKeyStore()
        self._permit_budget = permit_budget
        self._permit_ttl = timedelta(minutes=permit_ttl_minutes)
        self._functions: dict[str, str] = {}  # OpenAI function name -> MCP tool name

    # -- tool definitions --------------------------------------------------

    def register_tool(
        self,
        tool_name: str,
        *,
        description: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return an OpenAI function-tool definition for a governed MCP tool.

        The returned dict goes straight into ``tools=[...]``. The function
        name is the MCP tool name made OpenAI-legal; the runner maps it back.
        """
        self.permit_key_for(tool_name)
        function_name = function_name_for(tool_name)
        existing = self._functions.get(function_name)
        if existing is not None and existing != tool_name:
            raise ValueError(
                f"function name {function_name!r} already maps to tool {existing!r}; "
                f"cannot also map it to {tool_name!r}"
            )
        self._functions[function_name] = tool_name
        return {
            "type": "function",
            "function": {
                "name": function_name,
                "description": description,
                "parameters": (
                    dict(parameters) if parameters else {"type": "object", "properties": {}}
                ),
            },
        }

    def tool_name_for(self, function_name: str) -> str:
        try:
            return self._functions[function_name]
        except KeyError:
            raise ValueError(f"function {function_name!r} is not registered") from None

    def permit_key_for(self, tool_name: str) -> str:
        _validate_identifier(tool_name, "tool name")
        return _validate_identifier(
            f"{_PERMIT_KEY_PREFIX}{self._run_id}-{tool_name}", "permit idempotency key"
        )

    # -- execution ---------------------------------------------------------

    async def run(self, tool_call: Any) -> GovernedToolResult:
        """Execute one tool call as exactly one governed action.

        Retrying the same tool call — same id — reuses the recorded key and
        permit, so the trust plane replays the original receipt instead of
        charging again.
        """
        call = normalize_tool_call(tool_call)
        tool_name = self.tool_name_for(call.name)
        operation_key = operation_key_for(call.id)
        permit_key = self.permit_key_for(tool_name)

        record = self._key_store.get_operation(call.id)
        if record is None:
            record = OperationRecord(
                tool_call_id=call.id,
                tool_name=tool_name,
                idempotency_key=operation_key,
                permit_idempotency_key=permit_key,
                wallet_id=self._wallet_id,
            )
        elif record.tool_name != tool_name:
            raise ValueError(
                f"tool call {call.id!r} was first recorded for tool {record.tool_name!r}; "
                f"refusing to replay it as {tool_name!r}"
            )
        if (
            record.tool_call_id != call.id
            or record.idempotency_key != operation_key
            or record.permit_idempotency_key != permit_key
        ):
            raise ValueError("recorded operation identity does not match this call, run, and tool")
        if record.wallet_id is not None and record.wallet_id != self._wallet_id:
            raise ValueError("operation was recorded for a different wallet")

        permit = self._key_store.get_permit(record.permit_idempotency_key)
        if permit is not None:
            if permit.permit_idempotency_key != permit_key or permit.tool_name != tool_name:
                raise ValueError("recorded permit identity does not match this run and tool")
            if permit.request_payload is not None:
                if any(
                    permit.request_payload.get(field) != self._wallet_id
                    for field in ("issuer_wallet_id", "subject_wallet_id")
                ):
                    raise ValueError("permit request was recorded for a different wallet")
            elif permit.permit_id is None:
                raise ValueError(
                    "incomplete legacy permit has no request snapshot; cannot safely resume"
                )

        # Durable before any network call, after local validation succeeds.
        self._key_store.put_operation(record)
        permit_id = await self._ensure_permit(record, permit)
        result = await self._client.invoke_tool(
            tool_name,
            call.arguments,
            wallet_id=self._wallet_id,
            permit_id=permit_id,
            idempotency_key=record.idempotency_key,
        )
        record.receipt_id = result.receipt.receipt_id
        self._key_store.put_operation(record)
        return GovernedToolResult(
            tool_call_id=call.id,
            tool_name=tool_name,
            idempotency_key=record.idempotency_key,
            result=result,
        )

    async def run_all(self, tool_calls: Iterable[Any]) -> list[GovernedToolResult]:
        """Execute a message's tool calls in order; each is its own action."""
        return [await self.run(tool_call) for tool_call in tool_calls]

    async def _ensure_permit(self, record: OperationRecord, permit: PermitRecord | None) -> str:
        if record.permit_id:
            return record.permit_id
        if permit is None:
            # Fixed and persisted before the request so every later attempt
            # under this key sends the identical body.
            request = PermitRequest(
                issuer_wallet_id=self._wallet_id,
                subject_wallet_id=self._wallet_id,
                max_credits=self._permit_budget,
                expires_at=datetime.now(UTC) + self._permit_ttl,
                allowed_tools=[record.tool_name],
                scopes=[f"tool:{record.tool_name}:invoke", "billing:charge"],
            )
            permit = PermitRecord(
                permit_idempotency_key=record.permit_idempotency_key,
                tool_name=record.tool_name,
                expires_at=request.expires_at.isoformat(),
                request_payload=request.to_payload(),
            )
            self._key_store.put_permit(permit)
        if permit.permit_id is None:
            if permit.request_payload is None:
                raise ValueError(
                    "incomplete legacy permit has no request snapshot; cannot safely resume"
                )
            payload = dict(permit.request_payload)
            payload["max_credits"] = Decimal(payload["max_credits"])
            payload["expires_at"] = datetime.fromisoformat(payload["expires_at"])
            request = PermitRequest(**payload)
            created = await self._client.create_permit(
                request, idempotency_key=permit.permit_idempotency_key
            )
            permit.permit_id = created.permit_id
            self._key_store.put_permit(permit)
        record.permit_id = permit.permit_id
        self._key_store.put_operation(record)
        return permit.permit_id


__all__ = [
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "GovernedToolResult",
    "GovernedToolRunner",
    "InMemoryOperationKeyStore",
    "JsonFileOperationKeyStore",
    "OperationKeyStore",
    "OperationRecord",
    "PermitRecord",
    "ToolCall",
    "function_name_for",
    "normalize_tool_call",
    "operation_key_for",
]
