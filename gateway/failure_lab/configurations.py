"""The configurations every scenario is run against.

The PRD's product principle is that the diagnostic must be able to prove the
customer does not need the gateway. That only means something if the
comparison includes a *correct* integration built with native controls, so
the lab runs each workload against:

``A_direct_naive``
    The agent calls the refund tool directly; the tool executes every request.
    This is what an integration looks like before anyone thought about
    retries. It is measured, not assumed.

``B_direct_native_idempotency``
    The agent calls the tool directly; the tool honors the business
    ``operation_id`` as a durable idempotency key, the way payment processors
    do. This is the correct native baseline. When it handles a scenario, the
    report says the gateway added nothing for that scenario.

``C_gateway_with_native_idempotency``
    The same native-idempotent tool behind the gateway. Measures what the
    gateway adds *on top of* correct native controls.

``D_gateway_naive_downstream``
    The naive tool behind the gateway. Not one of the PRD's three mandatory
    configurations; included because it isolates the gateway's own guarantee
    from the downstream's, which is what the P0 gateway tests are about.

All four share one workload driver interface (:class:`Agent`) so a scenario
never special-cases a configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from failure_lab.effect_ledger import EffectLedger
from failure_lab.faults import FaultInjector
from failure_lab.gateway import (
    CLIENT_TERMINAL_OUTCOMES,
    DOWNSTREAM_BASE_URL,
    GatewayOutcome,
    GatewayUnderTest,
    Tenant,
)
from failure_lab.identity import OperationIdentity
from failure_lab.refund_tool import (
    DownstreamApp,
    RefundRequest,
    SimulatedRefundService,
    build_downstream_app,
)


class Configuration(str, Enum):
    DIRECT_NAIVE = "A_direct_naive"
    DIRECT_NATIVE = "B_direct_native_idempotency"
    GATEWAY_NATIVE = "C_gateway_with_native_idempotency"
    GATEWAY_NAIVE = "D_gateway_naive_downstream"

    @property
    def uses_gateway(self) -> bool:
        return self in (Configuration.GATEWAY_NATIVE, Configuration.GATEWAY_NAIVE)

    @property
    def native_idempotency(self) -> bool:
        return self in (Configuration.DIRECT_NATIVE, Configuration.GATEWAY_NATIVE)

    @property
    def label(self) -> str:
        return CONFIGURATION_LABELS[self]


CONFIGURATION_LABELS: dict[Configuration, str] = {
    Configuration.DIRECT_NAIVE: "Existing integration (direct, no native idempotency)",
    Configuration.DIRECT_NATIVE: "Correct native baseline (direct, downstream honors operation_id)",
    Configuration.GATEWAY_NATIVE: "Native controls + Agent Middleware",
    Configuration.GATEWAY_NAIVE: "Agent Middleware only (naive downstream)",
}

MANDATORY_CONFIGURATIONS = (
    Configuration.DIRECT_NAIVE,
    Configuration.DIRECT_NATIVE,
    Configuration.GATEWAY_NATIVE,
)
ALL_CONFIGURATIONS = tuple(Configuration)
GATEWAY_CONFIGURATIONS = (Configuration.GATEWAY_NATIVE, Configuration.GATEWAY_NAIVE)


@dataclass(frozen=True)
class AttemptOutcome:
    """What the agent saw for one attempt, normalized across configurations."""

    configuration: str
    identity: dict[str, Any]
    #: ``succeeded`` | ``replayed`` | ``conflict`` | ``rejected`` | ``denied`` |
    #: ``timeout`` | ``transport_error`` | ``http_error`` |
    #: ``delivery_uncertain`` | ``failed_refunded`` | ``in_progress`` |
    #: ``gateway_process_died`` | ``insufficient_funds`` | ``error``
    status: str
    #: What the client can truthfully say about the business operation:
    #: ``confirmed_success`` | ``confirmed_replay`` | ``confirmed_rejected`` |
    #: ``explicit_uncertain`` | ``no_information``
    client_visible_state: str
    http_status: int | None
    latency_ms: float
    reason: str | None = None
    refund: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def receipt_id(self) -> str | None:
        return self.receipt.get("receipt_id") if self.receipt else None

    @property
    def knows_outcome(self) -> bool:
        return self.client_visible_state.startswith("confirmed_")

    def as_dict(self) -> dict[str, Any]:
        return {
            "configuration": self.configuration,
            "identity": self.identity,
            "status": self.status,
            "client_visible_state": self.client_visible_state,
            "http_status": self.http_status,
            "latency_ms": round(self.latency_ms, 3),
            "reason": self.reason,
            "refund": self.refund,
            "receipt_id": self.receipt_id,
            "receipt_outcome": self.receipt.get("outcome") if self.receipt else None,
            "details": self.details,
        }


class Agent:
    """The workload driver: submit one refund attempt, report what came back."""

    configuration: Configuration

    async def submit(
        self,
        identity: OperationIdentity,
        refund: RefundRequest | dict[str, Any],
        *,
        timeout_seconds: float = 5.0,
    ) -> AttemptOutcome:
        raise NotImplementedError


def _refund_arguments(refund: RefundRequest | dict[str, Any]) -> dict[str, Any]:
    return refund.as_dict() if isinstance(refund, RefundRequest) else dict(refund)


class DirectAgent(Agent):
    """Calls the downstream REST endpoint itself, holding the tool credential."""

    def __init__(self, configuration: Configuration, downstream: DownstreamApp) -> None:
        self.configuration = configuration
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=downstream.asgi),
            base_url=DOWNSTREAM_BASE_URL,
            headers={"Authorization": f"Bearer {downstream.bearer_token}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def submit(
        self,
        identity: OperationIdentity,
        refund: RefundRequest | dict[str, Any],
        *,
        timeout_seconds: float = 5.0,
    ) -> AttemptOutcome:
        arguments = _refund_arguments(refund)
        headers = {
            "Idempotency-Key": identity.idempotency_key,
            "X-Request-Id": identity.request_id,
        }
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_seconds):
                response = await self._client.post(
                    "/refunds", json=arguments, headers=headers
                )
        except TimeoutError:
            return AttemptOutcome(
                configuration=self.configuration.value,
                identity=identity.as_dict(),
                status="timeout",
                client_visible_state="no_information",
                http_status=None,
                latency_ms=(time.perf_counter() - started) * 1000,
                reason="client timed out waiting for the downstream response",
            )
        except httpx.TransportError as exc:
            return AttemptOutcome(
                configuration=self.configuration.value,
                identity=identity.as_dict(),
                status="transport_error",
                client_visible_state="no_information",
                http_status=None,
                latency_ms=(time.perf_counter() - started) * 1000,
                reason=f"{type(exc).__name__}: {exc}",
            )
        latency_ms = (time.perf_counter() - started) * 1000
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 201:
            replayed = bool(body.get("replayed"))
            return AttemptOutcome(
                configuration=self.configuration.value,
                identity=identity.as_dict(),
                status="replayed" if replayed else "succeeded",
                client_visible_state="confirmed_replay"
                if replayed
                else "confirmed_success",
                http_status=201,
                latency_ms=latency_ms,
                refund=body,
            )
        if response.status_code == 409:
            return AttemptOutcome(
                configuration=self.configuration.value,
                identity=identity.as_dict(),
                status="conflict",
                client_visible_state="confirmed_rejected",
                http_status=409,
                latency_ms=latency_ms,
                reason=body.get("error"),
            )
        if 400 <= response.status_code < 500:
            return AttemptOutcome(
                configuration=self.configuration.value,
                identity=identity.as_dict(),
                status="rejected",
                client_visible_state="confirmed_rejected",
                http_status=response.status_code,
                latency_ms=latency_ms,
                reason=body.get("error") or body.get("detail"),
                details={"detail": body.get("detail")},
            )
        return AttemptOutcome(
            configuration=self.configuration.value,
            identity=identity.as_dict(),
            status="http_error",
            client_visible_state="no_information",
            http_status=response.status_code,
            latency_ms=latency_ms,
            reason=body.get("error") if isinstance(body, dict) else None,
        )


def _gateway_visible_state(outcome: GatewayOutcome) -> str:
    if outcome.status == "success":
        return "confirmed_success"
    if outcome.status == "delivery_uncertain":
        return "explicit_uncertain"
    if outcome.status in CLIENT_TERMINAL_OUTCOMES:
        return "confirmed_rejected"
    return "no_information"


class GatewayAgent(Agent):
    """Calls the gateway with a wallet-scoped key and a permit; never holds the tool credential."""

    def __init__(
        self,
        configuration: Configuration,
        gateway: GatewayUnderTest,
        tenant: Tenant,
        permit_id: str | None,
        *,
        transport: str = "jsonrpc",
    ) -> None:
        self.configuration = configuration
        self.gateway = gateway
        self.tenant = tenant
        self.permit_id = permit_id
        self.transport = transport

    def with_permit(self, permit_id: str | None) -> GatewayAgent:
        return GatewayAgent(
            self.configuration,
            self.gateway,
            self.tenant,
            permit_id,
            transport=self.transport,
        )

    async def submit(
        self,
        identity: OperationIdentity,
        refund: RefundRequest | dict[str, Any],
        *,
        timeout_seconds: float = 5.0,
    ) -> AttemptOutcome:
        arguments = _refund_arguments(refund)
        invoke = (
            self.gateway.invoke_rest
            if self.transport == "rest"
            else self.gateway.invoke
        )
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_seconds):
                outcome = await invoke(
                    self.tenant,
                    permit_id=self.permit_id,
                    identity=identity,
                    arguments=arguments,
                )
        except TimeoutError:
            return AttemptOutcome(
                configuration=self.configuration.value,
                identity=identity.as_dict(),
                status="timeout",
                client_visible_state="no_information",
                http_status=None,
                latency_ms=(time.perf_counter() - started) * 1000,
                reason="client timed out waiting for the gateway response",
            )
        refund_result: dict[str, Any] | None = None
        if outcome.structured and "refund_id" in outcome.structured:
            refund_result = dict(outcome.structured)
        status = outcome.status
        visible = _gateway_visible_state(outcome)
        if (
            status == "success"
            and refund_result is not None
            and refund_result.get("replayed")
        ):
            visible = "confirmed_replay"
        return AttemptOutcome(
            configuration=self.configuration.value,
            identity=identity.as_dict(),
            status=status,
            client_visible_state=visible,
            http_status=outcome.http_status,
            latency_ms=outcome.latency_ms,
            reason=outcome.reason,
            refund=refund_result,
            receipt=outcome.receipt,
            details=outcome.details,
        )


@dataclass
class Target:
    """Everything a scenario needs to drive one configuration and observe it."""

    configuration: Configuration
    ledger: EffectLedger
    injector: FaultInjector
    downstream: DownstreamApp
    agent: Agent
    gateway: GatewayUnderTest | None = None
    tenant: Tenant | None = None
    permit: dict[str, Any] | None = None

    @property
    def uses_gateway(self) -> bool:
        return self.gateway is not None

    def require_gateway(self) -> tuple[GatewayUnderTest, Tenant, dict[str, Any]]:
        if self.gateway is None or self.tenant is None or self.permit is None:
            raise RuntimeError(f"{self.configuration.value} has no gateway")
        return self.gateway, self.tenant, self.permit

    def gateway_agent(
        self, permit_id: str | None, *, transport: str = "jsonrpc"
    ) -> GatewayAgent:
        gateway, tenant, _ = self.require_gateway()
        return GatewayAgent(
            self.configuration, gateway, tenant, permit_id, transport=transport
        )


@dataclass
class LabEnvironment:
    """Process-wide handles a run shares across configurations."""

    run_dir: Path
    app: Any
    admin_api_key: str
    downstream_bearer_token: str = field(
        default_factory=lambda: secrets.token_urlsafe(24)
    )
    control_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    credits_per_call: str = "5"
    call_timeout_seconds: float = 2.0
    permit_max_credits: str = "1000"


@contextlib.asynccontextmanager
async def configured_target(
    env: LabEnvironment,
    configuration: Configuration,
    *,
    ledger_suffix: str = "",
) -> AsyncIterator[Target]:
    """Build the downstream for ``configuration`` and, if needed, the gateway in front of it."""
    from decimal import Decimal

    ledger_name = f"effects-{configuration.value}{ledger_suffix}.db"
    ledger = EffectLedger(env.run_dir / ledger_name)
    injector = FaultInjector()
    service = SimulatedRefundService(
        ledger,
        native_idempotency=configuration.native_idempotency,
        configuration=configuration.value,
    )
    downstream = build_downstream_app(
        service,
        injector,
        bearer_token=env.downstream_bearer_token,
        control_token=env.control_token,
    )
    async with downstream.lifespan():
        if not configuration.uses_gateway:
            agent = DirectAgent(configuration, downstream)
            try:
                yield Target(
                    configuration=configuration,
                    ledger=ledger,
                    injector=injector,
                    downstream=downstream,
                    agent=agent,
                )
            finally:
                await agent.aclose()
            return
        async with GatewayUnderTest(
            downstream=downstream,
            app=env.app,
            admin_api_key=env.admin_api_key,
            credits_per_call=Decimal(env.credits_per_call),
            call_timeout_seconds=env.call_timeout_seconds,
        ) as gateway:
            tenant = await gateway.provision(label=configuration.value)
            permit = await gateway.issue_permit(
                tenant, max_credits=Decimal(env.permit_max_credits)
            )
            yield Target(
                configuration=configuration,
                ledger=ledger,
                injector=injector,
                downstream=downstream,
                agent=GatewayAgent(configuration, gateway, tenant, permit["permit_id"]),
                gateway=gateway,
                tenant=tenant,
                permit=permit,
            )


__all__ = [
    "ALL_CONFIGURATIONS",
    "Agent",
    "AttemptOutcome",
    "CONFIGURATION_LABELS",
    "Configuration",
    "DirectAgent",
    "GATEWAY_CONFIGURATIONS",
    "GatewayAgent",
    "LabEnvironment",
    "MANDATORY_CONFIGURATIONS",
    "Target",
    "configured_target",
]
