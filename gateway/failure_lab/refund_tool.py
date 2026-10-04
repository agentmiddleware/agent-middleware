"""The simulated business tool: a refund API with a durable ledger.

One consequential operation, modelled two ways:

* **naive** -- every request executes. This is what an integration looks like
  when nobody has thought about retries yet, and it is the baseline the PRD
  forbids treating as a strawman: it is measured, not assumed.
* **native idempotency** -- the tool honors the business ``operation_id`` as a
  durable idempotency key (the way payment processors do): a repeat with the
  same payload returns the stored result without executing again, and a
  repeat with a different payload is refused as a conflict. This is the
  "correct native baseline" every gateway result is compared against.

The tool is served two ways from one ASGI app: as a Streamable HTTP MCP server
(``POST /mcp``, tool ``refund.create``) for the gateway path, and as a plain
REST endpoint (``POST /refunds``) for the direct, no-gateway configurations.
Both go through the same :class:`~failure_lab.faults.FaultInjectionMiddleware`
and the same :class:`~failure_lab.effect_ledger.EffectLedger`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import StrictInt, StrictStr
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from failure_lab.effect_ledger import EffectLedger, EffectRecord, NativeConflictError
from failure_lab.faults import (
    MCP_PATH,
    REST_PATH,
    FaultInjectionMiddleware,
    FaultInjector,
    FaultPlan,
)

REFUND_TOOL_NAME = "refund.create"
CONTROL_HEADER = "X-Failure-Lab-Control"
_IDEMPOTENCY_META_KEY = "io.agentmiddleware/idempotency_key"
_INVOCATION_META_KEY = "io.agentmiddleware/invocation_id"
_SUPPORTED_CURRENCIES = frozenset({"USD", "EUR", "GBP"})
_MAX_AMOUNT_MINOR_UNITS = 10_000_000


class RefundValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RefundRequest:
    operation_id: str
    customer_id: str
    payment_id: str
    amount: int
    currency: str = "USD"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> RefundRequest:
        """Strict parse: the *type* of every field is part of the contract.

        ``"5000"`` is not ``5000`` here. A downstream that silently coerced
        would make the alternate-representation probe in Test 9 unobservable.
        """
        for name in ("operation_id", "customer_id", "payment_id"):
            value = data.get(name)
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise RefundValidationError(f"{name} must be a non-empty string")
        amount = data.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise RefundValidationError(
                "amount must be an integer number of minor units"
            )
        if amount <= 0 or amount > _MAX_AMOUNT_MINOR_UNITS:
            raise RefundValidationError("amount is out of range")
        currency = data.get("currency", "USD")
        if not isinstance(currency, str) or currency not in _SUPPORTED_CURRENCIES:
            raise RefundValidationError("currency is not supported")
        unknown = set(data) - {
            "operation_id",
            "customer_id",
            "payment_id",
            "amount",
            "currency",
        }
        if unknown:
            raise RefundValidationError(
                "unknown fields: " + ", ".join(sorted(str(name) for name in unknown))
            )
        return cls(
            operation_id=str(data["operation_id"]),
            customer_id=str(data["customer_id"]),
            payment_id=str(data["payment_id"]),
            amount=int(amount),
            currency=str(currency),
        )

    def fingerprint(self) -> str:
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RefundResult:
    refund_id: str
    operation_id: str
    amount: int
    currency: str
    status: str
    replayed: bool
    effect_id: str
    execution_ordinal: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _result_from_record(record: EffectRecord) -> dict[str, Any]:
    return RefundResult(
        refund_id="re_" + record.effect_id.removeprefix("effect_"),
        operation_id=record.operation_id,
        amount=record.amount,
        currency=record.currency,
        status="succeeded",
        replayed=False,
        effect_id=record.effect_id,
        execution_ordinal=record.execution_ordinal,
    ).as_dict()


class SimulatedRefundService:
    """The tool body. Every non-replayed call is a committed downstream effect."""

    def __init__(
        self,
        ledger: EffectLedger,
        *,
        native_idempotency: bool,
        configuration: str = "",
    ) -> None:
        self.ledger = ledger
        self.native_idempotency = native_idempotency
        self.configuration = configuration

    def execute(
        self,
        request: RefundRequest,
        *,
        request_id: str,
        idempotency_key: str,
    ) -> RefundResult:
        outcome = self.ledger.execute(
            operation_id=request.operation_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            amount=request.amount,
            currency=request.currency,
            customer_id=request.customer_id,
            payment_id=request.payment_id,
            configuration=self.configuration,
            native_fingerprint=request.fingerprint()
            if self.native_idempotency
            else None,
            make_result=_result_from_record,
        )
        result = dict(outcome.result)
        result["replayed"] = outcome.replayed
        return RefundResult(**result)


# --------------------------------------------------------------------------- #
# ASGI app                                                                      #
# --------------------------------------------------------------------------- #


class _BearerAuth:
    """Require the tool credential on the execution paths only."""

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            path = str(scope.get("path", "")).rstrip("/") or "/"
            if path in (MCP_PATH, REST_PATH):
                values = [
                    value
                    for key, value in scope.get("headers", [])
                    if key.lower() == b"authorization"
                ]
                if len(values) != 1 or not hmac.compare_digest(
                    values[0], self._expected
                ):
                    response = JSONResponse(
                        {"detail": "refund_tool_unauthorized"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    await response(scope, receive, send)
                    return
        await self._app(scope, receive, send)


@dataclass
class DownstreamApp:
    """The assembled downstream: outer ASGI callable plus its parts."""

    asgi: Any
    starlette: Any
    service: SimulatedRefundService
    injector: FaultInjector
    bearer_token: str
    control_token: str

    @asynccontextmanager
    async def lifespan(self):  # type: ignore[no-untyped-def]
        """Run the MCP session manager, required even in stateless mode."""
        async with self.starlette.router.lifespan_context(self.starlette):
            yield self


def build_downstream_app(
    service: SimulatedRefundService,
    injector: FaultInjector,
    *,
    bearer_token: str,
    control_token: str,
    allowed_hosts: tuple[str, ...] = (
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
    ),
    crash_hook: Callable[[], None] | None = None,
) -> DownstreamApp:
    server = FastMCP(
        "failure-lab-simulated-refund",
        instructions=(
            "Simulated refund processor for the Agent Gateway Failure Lab. "
            "Every call may move (simulated) money."
        ),
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=list(allowed_hosts)),
    )

    @server.tool(
        name=REFUND_TOOL_NAME,
        description=(
            "Refund a payment. amount is an integer in minor units. "
            "operation_id identifies the business operation; the tool "
            "may or may not honor it as an idempotency key."
        ),
    )
    async def refund_create(
        ctx: Context,
        operation_id: StrictStr,
        customer_id: StrictStr,
        payment_id: StrictStr,
        amount: StrictInt,
        currency: StrictStr = "USD",
    ) -> dict[str, Any]:
        meta = ctx.request_context.meta
        meta_payload = (
            meta.model_dump(mode="json", by_alias=True, exclude_none=True)
            if meta is not None
            else {}
        )
        request = RefundRequest.from_mapping(
            {
                "operation_id": operation_id,
                "customer_id": customer_id,
                "payment_id": payment_id,
                "amount": amount,
                "currency": currency,
            }
        )
        request_id = str(ctx.request_id)
        idempotency_key = str(meta_payload.get(_IDEMPOTENCY_META_KEY) or "")
        try:
            result = await asyncio.to_thread(
                service.execute,
                request,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )
        except NativeConflictError as exc:
            raise ValueError(exc.code) from exc
        payload = result.as_dict()
        payload["forwarded_invocation_id"] = meta_payload.get(_INVOCATION_META_KEY)
        payload["forwarded_idempotency_key"] = idempotency_key or None
        return payload

    def _control_ok(request: Request) -> bool:
        provided = request.headers.get(CONTROL_HEADER)
        return provided is not None and hmac.compare_digest(provided, control_token)

    def _denied() -> JSONResponse:
        return JSONResponse({"detail": "failure_lab_control_denied"}, status_code=403)

    @server.custom_route(REST_PATH, methods=["POST"], include_in_schema=False)
    async def refund_rest(request: Request) -> JSONResponse:
        try:
            data = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(data, dict):
            return JSONResponse({"error": "invalid_body"}, status_code=400)
        try:
            refund_request = RefundRequest.from_mapping(data)
        except RefundValidationError as exc:
            return JSONResponse(
                {"error": "validation_failed", "detail": str(exc)}, status_code=400
            )
        try:
            result = await asyncio.to_thread(
                service.execute,
                refund_request,
                request_id=request.headers.get("x-request-id", ""),
                idempotency_key=request.headers.get("idempotency-key", ""),
            )
        except NativeConflictError as exc:
            return JSONResponse({"error": exc.code}, status_code=409)
        return JSONResponse(result.as_dict(), status_code=201)

    @server.custom_route("/__lab/health", methods=["GET"], include_in_schema=False)
    async def lab_health(request: Request) -> JSONResponse:
        if not _control_ok(request):
            return _denied()
        return JSONResponse(
            {
                "status": "ok",
                "tool": REFUND_TOOL_NAME,
                "native_idempotency": service.native_idempotency,
                "execution_count": await asyncio.to_thread(
                    service.ledger.execution_count
                ),
                "crashed": injector.crashed,
            }
        )

    @server.custom_route("/__lab/effects", methods=["GET"], include_in_schema=False)
    async def lab_effects(request: Request) -> JSONResponse:
        if not _control_ok(request):
            return _denied()
        operation_id = request.query_params.get("operation_id")
        effects = await asyncio.to_thread(service.ledger.effects, operation_id)
        return JSONResponse(
            {"count": len(effects), "effects": [e.as_dict() for e in effects]}
        )

    @server.custom_route("/__lab/crossings", methods=["GET"], include_in_schema=False)
    async def lab_crossings(request: Request) -> JSONResponse:
        if not _control_ok(request):
            return _denied()
        return JSONResponse(injector.snapshot())

    @server.custom_route(
        "/__lab/faults", methods=["POST", "DELETE"], include_in_schema=False
    )
    async def lab_faults(request: Request) -> JSONResponse:
        if not _control_ok(request):
            return _denied()
        if request.method == "DELETE":
            injector.clear()
            return JSONResponse({"armed": []})
        try:
            data = await request.json()
            plan = FaultPlan.from_dict(data)
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse(
                {"error": "invalid_plan", "detail": str(exc)}, status_code=400
            )
        injector.arm(plan)
        return JSONResponse({"armed": [p.as_dict() for p in injector.armed()]})

    @server.custom_route("/__lab/restart", methods=["POST"], include_in_schema=False)
    async def lab_restart(request: Request) -> JSONResponse:
        if not _control_ok(request):
            return _denied()
        injector.restart()
        return JSONResponse({"crashed": injector.crashed})

    @server.custom_route("/__lab/reset", methods=["POST"], include_in_schema=False)
    async def lab_reset(request: Request) -> JSONResponse:
        if not _control_ok(request):
            return _denied()
        await asyncio.to_thread(service.ledger.reset)
        injector.clear()
        injector.reset_observations()
        injector.restart()
        return JSONResponse({"status": "reset"})

    starlette_app = server.streamable_http_app()
    outer = FaultInjectionMiddleware(
        _BearerAuth(starlette_app, bearer_token),
        injector,
        crash_hook=crash_hook,
    )
    return DownstreamApp(
        asgi=outer,
        starlette=starlette_app,
        service=service,
        injector=injector,
        bearer_token=bearer_token,
        control_token=control_token,
    )


__all__ = [
    "CONTROL_HEADER",
    "DownstreamApp",
    "REFUND_TOOL_NAME",
    "RefundRequest",
    "RefundResult",
    "RefundValidationError",
    "SimulatedRefundService",
    "build_downstream_app",
]
