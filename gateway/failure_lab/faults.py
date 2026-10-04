"""The fault injection layer between the gateway and the downstream tool.

Architecturally this is an ASGI middleware wrapped around the simulated
business tool. Every request that would execute the tool passes through it,
which gives it two jobs:

1. **Inject one failure at a time**, at the exact place the PRD names:
   after the downstream executed (response lost, connection severed, HTTP
   error, process crash), before it executed (connection refused, HTTP
   error), during MCP initialization (so the gateway's pre-claim path is
   exercised), or merely slowly (latency).
2. **Observe dispatches independently.** The layer counts every execution
   request that crossed it, per business operation. "Gateway dispatches" in
   a report is this count, not a number read back from the gateway's tables.

Faults are *armed* as :class:`FaultPlan` objects that match a request kind and
optionally one business operation id, and expire after ``remaining`` uses.
Everything else passes through untouched, so a scenario's control requests
and the gateway's discovery calls are never perturbed by accident.

The in-process and subprocess downstream runners share this one middleware;
only the crash hook differs (mark-down-and-sever in process, ``os._exit`` in
a real process).
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

# Request kinds the layer recognizes. Anything else passes through.
KIND_TOOLS_CALL = "tools_call"  # MCP tools/call (the gateway path)
KIND_INITIALIZE = "initialize"  # MCP initialize (pre-dispatch on the gateway path)
KIND_REFUND_REST = "refund_rest"  # POST /refunds (the direct, no-gateway path)
EXECUTION_KINDS = (KIND_TOOLS_CALL, KIND_REFUND_REST)

MCP_PATH = "/mcp"
REST_PATH = "/refunds"

_IDEMPOTENCY_META_KEY = "io.agentmiddleware/idempotency_key"
_INVOCATION_META_KEY = "io.agentmiddleware/invocation_id"
_MAX_BODY_BYTES = 1_048_576


class FaultMode(str, Enum):
    NORMAL = "normal"
    #: Execute, wait ``delay_ms``, then deliver the response.
    DELAYED = "delayed"
    #: Execute, then never deliver the response. The caller sees a timeout.
    RESPONSE_LOST_AFTER_EXECUTION = "response_lost_after_execution"
    #: Sever the connection before the tool runs. Nothing executes.
    CONNECTION_FAILURE_BEFORE_EXECUTION = "connection_failure_before_execution"
    #: Execute, then sever the connection before any byte of the response.
    CONNECTION_FAILURE_AFTER_EXECUTION = "connection_failure_after_execution"
    #: Answer HTTP 503 without running the tool.
    HTTP_ERROR_BEFORE_EXECUTION = "http_error_before_execution"
    #: Execute, then answer HTTP 502 as a broken proxy would.
    HTTP_ERROR_AFTER_EXECUTION = "http_error_after_execution"
    #: Refuse the MCP ``initialize`` handshake. The gateway has not claimed
    #: dispatch yet, so this exercises its provably-not-sent path.
    INITIALIZE_FAILURE = "initialize_failure"
    #: Execute, commit, then the downstream process dies. In a real process
    #: this is ``os._exit``; in-process the layer marks itself down and
    #: refuses connections until :meth:`FaultInjector.restart`.
    CRASH_AFTER_EXECUTION = "crash_after_execution"


@dataclass
class FaultPlan:
    mode: FaultMode
    operation_id: str | None = None
    kinds: tuple[str, ...] = EXECUTION_KINDS
    remaining: int = 1
    delay_ms: int = 0
    #: How long a lost response is withheld before the connection is severed.
    #: Callers time out first; this only bounds a leaked handler.
    hold_seconds: float = 30.0
    label: str = ""
    applied: int = 0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mode"] = self.mode.value
        data["kinds"] = list(self.kinds)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FaultPlan:
        mode = FaultMode(str(data["mode"]))
        kinds = data.get("kinds")
        if mode is FaultMode.INITIALIZE_FAILURE and not kinds:
            kinds = (KIND_INITIALIZE,)
        return cls(
            mode=mode,
            operation_id=data.get("operation_id"),
            kinds=tuple(kinds) if kinds else EXECUTION_KINDS,
            remaining=int(data.get("remaining", 1)),
            delay_ms=int(data.get("delay_ms", 0)),
            hold_seconds=float(data.get("hold_seconds", 30.0)),
            label=str(data.get("label", "")),
        )


@dataclass(frozen=True)
class Crossing:
    """One execution request observed at the layer."""

    sequence: int
    kind: str
    path: str
    operation_id: str | None
    request_id: str | None
    idempotency_key: str | None
    invocation_id: str | None
    fault: str
    #: The request was handed to the tool (it may still have replayed
    #: natively without a new effect; the ledger decides that).
    reached_tool: bool
    #: Status the tool produced, when it ran.
    tool_status: int | None
    #: A response was forwarded to the caller.
    delivered: bool
    observed_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class FaultInjector:
    """Armed fault plans plus the independent record of every crossing."""

    def __init__(self) -> None:
        self._plans: list[FaultPlan] = []
        self._crossings: list[Crossing] = []
        self._sequence = 0
        self._lock = threading.Lock()
        self.crashed = False

    # -- arming -----------------------------------------------------------

    def arm(self, plan: FaultPlan) -> FaultPlan:
        if plan.mode is FaultMode.INITIALIZE_FAILURE and plan.kinds == EXECUTION_KINDS:
            plan.kinds = (KIND_INITIALIZE,)
        with self._lock:
            self._plans.append(plan)
        return plan

    def clear(self) -> None:
        with self._lock:
            self._plans.clear()

    def restart(self) -> None:
        """Bring a crashed in-process downstream back up."""
        with self._lock:
            self.crashed = False

    def armed(self) -> list[FaultPlan]:
        with self._lock:
            return [plan for plan in self._plans if plan.remaining > 0]

    def _select(self, kind: str, operation_id: str | None) -> FaultPlan | None:
        with self._lock:
            for plan in self._plans:
                if plan.remaining <= 0 or kind not in plan.kinds:
                    continue
                if plan.operation_id is not None and plan.operation_id != operation_id:
                    continue
                plan.remaining -= 1
                plan.applied += 1
                return plan
        return None

    # -- observation ------------------------------------------------------

    def _record(self, **fields: Any) -> Crossing:
        with self._lock:
            self._sequence += 1
            crossing = Crossing(
                sequence=self._sequence,
                observed_at=datetime.now(timezone.utc).isoformat(
                    timespec="microseconds"
                ),
                **fields,
            )
            self._crossings.append(crossing)
        return crossing

    def crossings(
        self,
        *,
        operation_id: str | None = None,
        kinds: tuple[str, ...] = EXECUTION_KINDS,
    ) -> list[Crossing]:
        with self._lock:
            items = list(self._crossings)
        return [
            crossing
            for crossing in items
            if crossing.kind in kinds
            and (operation_id is None or crossing.operation_id == operation_id)
        ]

    def dispatch_count(self, operation_id: str | None = None) -> int:
        """Execution requests that crossed the layer (reached it at all)."""
        return len(self.crossings(operation_id=operation_id))

    def reached_tool_count(self, operation_id: str | None = None) -> int:
        return sum(
            1
            for crossing in self.crossings(operation_id=operation_id)
            if crossing.reached_tool
        )

    def reset_observations(self) -> None:
        with self._lock:
            self._crossings.clear()
            self._sequence = 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "crashed": self.crashed,
                "plans": [plan.as_dict() for plan in self._plans],
                "crossings": [crossing.as_dict() for crossing in self._crossings],
            }


# --------------------------------------------------------------------------- #
# ASGI plumbing                                                                 #
# --------------------------------------------------------------------------- #


async def _read_body(receive: Callable[[], Awaitable[dict[str, Any]]]) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise ValueError("failure_lab: request body exceeds the buffer limit")
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes) -> Callable[[], Awaitable[dict[str, Any]]]:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    return receive


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def classify_request(
    path: str, body: bytes, headers: dict[str, str]
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Return ``(kind, operation_id, request_id, idempotency_key, invocation_id)``."""
    try:
        payload = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeDecodeError):
        return None, None, None, None, None
    normalized = path.rstrip("/") or "/"
    if normalized == MCP_PATH:
        if not isinstance(payload, dict):
            return None, None, None, None, None
        method = payload.get("method")
        request_id = payload.get("id")
        request_id_text = str(request_id) if request_id is not None else None
        if method == "initialize":
            return KIND_INITIALIZE, None, request_id_text, None, None
        if method != "tools/call":
            return None, None, None, None, None
        params = (
            payload.get("params") if isinstance(payload.get("params"), dict) else {}
        )
        arguments = (
            params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        )
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        operation_id = arguments.get("operation_id")
        return (
            KIND_TOOLS_CALL,
            str(operation_id) if operation_id is not None else None,
            request_id_text,
            meta.get(_IDEMPOTENCY_META_KEY),
            meta.get(_INVOCATION_META_KEY),
        )
    if normalized == REST_PATH:
        if not isinstance(payload, dict):
            return None, None, None, None, None
        operation_id = payload.get("operation_id")
        return (
            KIND_REFUND_REST,
            str(operation_id) if operation_id is not None else None,
            headers.get("x-request-id"),
            headers.get("idempotency-key"),
            None,
        )
    return None, None, None, None, None


async def _run_capturing(
    app: Any,
    scope: dict[str, Any],
    receive: Callable[[], Awaitable[dict[str, Any]]],
) -> tuple[int | None, list[dict[str, Any]]]:
    """Run the inner app to completion, buffering its response."""
    messages: list[dict[str, Any]] = []
    status: int | None = None

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
        messages.append(message)

    await app(scope, receive, send)
    return status, messages


async def _forward(
    send: Callable[[dict[str, Any]], Awaitable[None]], messages: list[dict[str, Any]]
) -> None:
    for message in messages:
        await send(message)


async def _send_json(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    status: int,
    payload: dict[str, Any],
) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class FaultInjectionMiddleware:
    """ASGI middleware applying armed faults to execution requests only."""

    def __init__(
        self,
        app: Any,
        injector: FaultInjector,
        *,
        crash_hook: Callable[[], None] | None = None,
    ) -> None:
        self.app = app
        self.injector = injector
        self._crash_hook = crash_hook

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        if method != "POST" or (path.rstrip("/") or "/") not in (MCP_PATH, REST_PATH):
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)
        headers = _headers(scope)
        kind, operation_id, request_id, idempotency_key, invocation_id = (
            classify_request(path, body, headers)
        )
        replay = _replay(body)
        if kind is None:
            await self.app(scope, replay, send)
            return

        record_fields = {
            "kind": kind,
            "path": path,
            "operation_id": operation_id,
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "invocation_id": invocation_id,
        }

        if self.injector.crashed:
            self.injector._record(
                **record_fields,
                fault="downstream_down",
                reached_tool=False,
                tool_status=None,
                delivered=False,
            )
            raise httpx.ConnectError("failure_lab: downstream is down until restarted")

        plan = self.injector._select(kind, operation_id)
        mode = plan.mode if plan is not None else FaultMode.NORMAL

        if mode is FaultMode.NORMAL:
            status, messages = await _run_capturing(self.app, scope, replay)
            self.injector._record(
                **record_fields,
                fault=mode.value,
                reached_tool=True,
                tool_status=status,
                delivered=True,
            )
            await _forward(send, messages)
            return

        if mode in (
            FaultMode.INITIALIZE_FAILURE,
            FaultMode.HTTP_ERROR_BEFORE_EXECUTION,
        ):
            self.injector._record(
                **record_fields,
                fault=mode.value,
                reached_tool=False,
                tool_status=None,
                delivered=True,
            )
            await _send_json(
                send,
                503,
                {"error": "failure_lab_injected", "fault": mode.value},
            )
            return

        if mode is FaultMode.CONNECTION_FAILURE_BEFORE_EXECUTION:
            self.injector._record(
                **record_fields,
                fault=mode.value,
                reached_tool=False,
                tool_status=None,
                delivered=False,
            )
            raise httpx.ConnectError("failure_lab: connection refused before execution")

        # Every remaining mode executes the tool first.
        status, messages = await _run_capturing(self.app, scope, replay)
        assert plan is not None

        if mode is FaultMode.DELAYED:
            await asyncio.sleep(plan.delay_ms / 1000)
            self.injector._record(
                **record_fields,
                fault=mode.value,
                reached_tool=True,
                tool_status=status,
                delivered=True,
            )
            await _forward(send, messages)
            return

        if mode is FaultMode.HTTP_ERROR_AFTER_EXECUTION:
            self.injector._record(
                **record_fields,
                fault=mode.value,
                reached_tool=True,
                tool_status=status,
                delivered=True,
            )
            await _send_json(
                send,
                502,
                {"error": "failure_lab_injected", "fault": mode.value},
            )
            return

        if mode is FaultMode.CONNECTION_FAILURE_AFTER_EXECUTION:
            self.injector._record(
                **record_fields,
                fault=mode.value,
                reached_tool=True,
                tool_status=status,
                delivered=False,
            )
            raise httpx.RemoteProtocolError(
                "failure_lab: connection severed after execution"
            )

        if mode is FaultMode.CRASH_AFTER_EXECUTION:
            self.injector._record(
                **record_fields,
                fault=mode.value,
                reached_tool=True,
                tool_status=status,
                delivered=False,
            )
            if self._crash_hook is not None:
                self._crash_hook()
            self.injector.crashed = True
            raise httpx.RemoteProtocolError("failure_lab: downstream process died")

        if mode is FaultMode.RESPONSE_LOST_AFTER_EXECUTION:
            self.injector._record(
                **record_fields,
                fault=mode.value,
                reached_tool=True,
                tool_status=status,
                delivered=False,
            )
            await asyncio.sleep(plan.hold_seconds)
            raise httpx.RemoteProtocolError(
                "failure_lab: response withheld after execution"
            )

        raise RuntimeError(f"unhandled fault mode {mode!r}")


__all__ = [
    "Crossing",
    "EXECUTION_KINDS",
    "FaultInjectionMiddleware",
    "FaultInjector",
    "FaultMode",
    "FaultPlan",
    "KIND_INITIALIZE",
    "KIND_REFUND_REST",
    "KIND_TOOLS_CALL",
    "MCP_PATH",
    "REST_PATH",
    "classify_request",
]
