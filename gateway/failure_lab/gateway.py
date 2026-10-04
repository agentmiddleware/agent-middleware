"""The gateway under test, driven in-process through its real routers.

The gateway is the actual FastAPI application (``app.main.app``) served over
an ASGI transport, with a throwaway database and an ephemeral signing key. Its
one governed tool is the simulated refund tool, registered through the same
``register_configured_upstream_mcp`` path an operator uses -- real discovery,
the real :class:`~app.services.upstream_mcp.UpstreamMcpAdapter`, the real
dispatch state machine -- with the adapter's HTTP client pointed at the
downstream ASGI app so the fault layer sits exactly where a network would.

What this module observes about the gateway is read from the gateway's own
tables and is labelled **gateway-reported** in every report. The independent
numbers (downstream executions, requests that crossed the fault layer) come
from :mod:`failure_lab.effect_ledger` and :mod:`failure_lab.faults`.

Crash injection here is **boundary-instrumented**: a scenario asks for the
gateway process to "die" immediately after one named durable commit, and the
request handler is torn down at that point by a :class:`BaseException` that
no ``except Exception`` in the pipeline can catch or compensate for. Committed
state stays committed; uncommitted state rolls back -- the same footprint a
``SIGKILL`` leaves. A real two-process kill proof exists separately in
``tests/test_mcp_postgres_multiprocess.py``; the lab labels its own crashes as
simulated and never claims otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from failure_lab.identity import OperationIdentity
from failure_lab.refund_tool import REFUND_TOOL_NAME, DownstreamApp

GATEWAY_TOOL_ID = REFUND_TOOL_NAME
#: Loopback-by-name so the adapter's URL guard admits plain HTTP; the name is
#: resolved to the in-process ASGI transport, never to a socket.
DOWNSTREAM_URL = "http://localhost:9000/mcp"
DOWNSTREAM_BASE_URL = "http://localhost:9000"
GATEWAY_BASE_URL = "http://failure-lab.gateway"

CRASH_BOUNDARIES = (
    "after_idempotency_begin",
    "after_prepare",
    "after_debit",
    "after_attach_charge",
    "after_claim",
    "after_upstream_response",
    "after_terminal_commit",
)

CRASH_BOUNDARY_DESCRIPTIONS = {
    "after_idempotency_begin": "idempotency record created; no reservation, debit, attempt or receipt",
    "after_prepare": "budget reserved and attempt row prepared; no debit yet",
    "after_debit": "wallet debit committed; not yet linked to the attempt",
    "after_attach_charge": "debit linked to the attempt; dispatch not yet claimed",
    "after_claim": "one-shot dispatch claim committed; network send not started",
    "after_upstream_response": "downstream executed and answered; terminal state never recorded",
    "after_terminal_commit": "terminal state recorded; receipt never written",
}

CLIENT_TERMINAL_OUTCOMES = frozenset(
    {
        "success",
        "denied",
        "insufficient_funds",
        "failed_refunded",
        "response_rejected",
        "failed_unrefunded",
        "key_conflict",
        "invalid_params",
    }
)


class SimulatedProcessDeath(BaseException):
    """The gateway process 'died' at a durable boundary. Not an Exception on purpose."""

    def __init__(self, boundary: str) -> None:
        super().__init__(f"simulated_process_death:{boundary}")
        self.boundary = boundary


def is_process_death(exc: BaseException) -> bool:
    if isinstance(exc, SimulatedProcessDeath):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(is_process_death(inner) for inner in exc.exceptions)
    return False


@dataclass
class BoundaryHold:
    boundary: str
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    fired: bool = False


@dataclass(frozen=True)
class Tenant:
    sponsor_wallet_id: str
    wallet_id: str
    key_id: str
    api_key: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    def redacted(self) -> dict[str, str]:
        return {
            "sponsor_wallet_id": self.sponsor_wallet_id,
            "wallet_id": self.wallet_id,
            "key_id": self.key_id,
            "api_key": self.api_key[:8] + "…",
        }


@dataclass(frozen=True)
class GatewayOutcome:
    """One gateway response, normalized."""

    #: ``success`` | ``denied`` | ``insufficient_funds`` | ``failed_refunded`` |
    #: ``delivery_uncertain`` | ``response_rejected`` | ``failed_unrefunded`` |
    #: ``in_progress`` | ``key_conflict`` | ``invalid_params`` |
    #: ``internal_error`` | ``gateway_process_died`` | ``http_error``
    status: str
    http_status: int | None
    jsonrpc_code: int | None
    reason: str | None
    receipt: dict[str, Any] | None
    structured: dict[str, Any] | None
    latency_ms: float
    details: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None

    @property
    def receipt_id(self) -> str | None:
        return self.receipt.get("receipt_id") if self.receipt else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "http_status": self.http_status,
            "jsonrpc_code": self.jsonrpc_code,
            "reason": self.reason,
            "receipt_id": self.receipt_id,
            "receipt_outcome": self.receipt.get("outcome") if self.receipt else None,
            "credits_charged": (
                str(self.receipt.get("credits_charged")) if self.receipt else None
            ),
            "structured": self.structured,
            "latency_ms": round(self.latency_ms, 3),
            "details": self.details,
        }


@dataclass(frozen=True)
class GatewaySnapshot:
    """Gateway-reported state for one wallet, read straight from its tables."""

    wallet_id: str
    attempts: list[dict[str, Any]]
    debits: list[dict[str, Any]]
    refunds: list[dict[str, Any]]
    receipts: list[dict[str, Any]]
    idempotency_records: list[dict[str, Any]]
    permits: list[dict[str, Any]]
    wallet_balance: str | None

    @property
    def sent_attempt_count(self) -> int:
        """Attempts that crossed the send boundary (claimed or later)."""
        return sum(1 for attempt in self.attempts if attempt["sent"])

    @property
    def debit_count(self) -> int:
        return len(self.debits)

    @property
    def refund_count(self) -> int:
        return len(self.refunds)

    @property
    def net_debit_count(self) -> int:
        return self.debit_count - self.refund_count

    @property
    def receipt_count(self) -> int:
        return len(self.receipts)

    def receipt_outcomes(self) -> list[str]:
        return [receipt["outcome"] for receipt in self.receipts]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "gateway-reported",
            "wallet_id": self.wallet_id,
            "attempts": self.attempts,
            "debits": self.debits,
            "refunds": self.refunds,
            "receipts": self.receipts,
            "idempotency_records": self.idempotency_records,
            "permits": self.permits,
            "wallet_balance": self.wallet_balance,
            "sent_attempt_count": self.sent_attempt_count,
            "debit_count": self.debit_count,
            "refund_count": self.refund_count,
            "receipt_count": self.receipt_count,
        }


def _classify(
    message: str | None, code: int | None, receipt: dict[str, Any] | None
) -> str:
    if receipt and isinstance(receipt.get("outcome"), str):
        return str(receipt["outcome"])
    if message == "idempotency_in_progress":
        return "in_progress"
    if message == "idempotency_key_reused":
        return "key_conflict"
    if message in (
        "delivery_uncertain",
        "failed_refunded",
        "response_rejected",
        "failed_unrefunded",
    ):
        return message
    if code == -32005:
        return "in_progress"
    if code == -32003:
        return "denied"
    if code == -32004:
        return "insufficient_funds"
    if code == -32006:
        return "response_rejected"
    if code == -32602:
        return "invalid_params"
    if code == -32603:
        return "internal_error"
    return "error"


def _isoformat(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def boot_standalone_environment(
    run_dir: Path, *, admin_api_key: str | None = None
) -> str:
    """Pin a sandbox posture into the environment before the app is imported.

    Returns the admin API key. Refuses to run if the application is already
    imported, because settings would then be cached with a different posture.
    """
    import sys

    if "app.main" in sys.modules:
        raise RuntimeError(
            "boot_standalone_environment must run before app.main is imported"
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    key = admin_api_key or ("lab-admin-" + secrets.token_urlsafe(24))
    seed = base64.b64encode(secrets.token_bytes(32)).decode()
    os.environ.update(
        {
            "ENVIRONMENT": "local",
            "DEBUG": "false",
            "DATABASE_URL": f"sqlite+aiosqlite:///{run_dir / 'gateway.db'}",
            "STATE_BACKEND": "memory",
            "ALLOW_METADATA_CREATE_ALL": "true",
            "VALID_API_KEYS": key,
            "STATIC_DEV_API_KEYS": "",
            "TRUST_MODE_ENABLED": "true",
            "ALLOW_LEGACY_UNPERMITTED_MCP": "false",
            "ENABLE_PROOF_SURFACES": "false",
            "ENABLE_DOGFOOD_TOOL": "false",
            "ENABLE_DEV_KEY_SELF_PROVISION": "false",
            "ENABLE_STANDARD_MCP_ENDPOINT": "true",
            "MCP_UPSTREAM_ENABLED": "false",
            "TRUST_SIGNING_KEY_ID": "failure-lab-ephemeral-ed25519",
            "TRUST_SIGNING_PRIVATE_KEY_B64": seed,
            "RATE_LIMIT_PER_MINUTE": "1000000",
            "REDIS_URL": "",
            "STRIPE_SECRET_KEY": "",
            "SENTINEL_API_KEY": "",
            "SENTINEL_API_URL": "",
            "SIMULATION_MODE_HUMAN_APPROVAL": "true",
            "PUBLIC_URL": "",
        }
    )
    return key


class GatewayUnderTest:
    """The real gateway, one governed upstream tool, throwaway state."""

    def __init__(
        self,
        *,
        downstream: DownstreamApp,
        app: Any,
        admin_api_key: str,
        credits_per_call: Decimal = Decimal("5"),
        call_timeout_seconds: float = 2.0,
        connect_timeout_seconds: float = 2.0,
    ) -> None:
        self.downstream = downstream
        self.app = app
        self.admin_api_key = admin_api_key
        self.credits_per_call = credits_per_call
        self.call_timeout_seconds = call_timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self._downstream_client: httpx.AsyncClient | None = None
        self._client: httpx.AsyncClient | None = None
        self.registration: dict[str, Any] | None = None

    @property
    def admin_headers(self) -> dict[str, str]:
        return {"X-API-Key": self.admin_api_key}

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("GatewayUnderTest is not entered")
        return self._client

    # -- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> GatewayUnderTest:
        from app.core.config import Settings, get_settings
        from app.services.service_registry import get_service_registry
        from app.services.upstream_mcp import register_configured_upstream_mcp

        base = get_settings()
        lab_settings = Settings(
            _env_file=None,
            ENVIRONMENT=base.ENVIRONMENT,
            MCP_UPSTREAM_ENABLED=True,
            MCP_UPSTREAM_URL=DOWNSTREAM_URL,
            MCP_UPSTREAM_TOOL_NAME=REFUND_TOOL_NAME,
            MCP_UPSTREAM_PUBLIC_TOOL_ID=GATEWAY_TOOL_ID,
            MCP_UPSTREAM_BEARER_TOKEN=self.downstream.bearer_token,
            MCP_UPSTREAM_CREDITS_PER_CALL=str(self.credits_per_call),
            MCP_UPSTREAM_CALL_TIMEOUT_SECONDS=self.call_timeout_seconds,
            MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS=self.connect_timeout_seconds,
        )
        self._downstream_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.downstream.asgi),
            base_url=DOWNSTREAM_BASE_URL,
            headers={"Authorization": f"Bearer {self.downstream.bearer_token}"},
            follow_redirects=False,
        )
        self.registration = await register_configured_upstream_mcp(
            settings=lab_settings,
            registry=get_service_registry(),
            http_client=self._downstream_client,
        )
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=GATEWAY_BASE_URL,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        from app.services.service_registry import get_service_registry

        get_service_registry().unregister_execution_backend("upstream_mcp")
        if self._client is not None:
            await self._client.aclose()
        if self._downstream_client is not None:
            await self._downstream_client.aclose()
        self._client = None
        self._downstream_client = None

    # -- provisioning -----------------------------------------------------

    async def provision(
        self,
        *,
        label: str = "lab-agent",
        initial_credits: int = 10_000,
        budget_credits: int = 1_000,
        daily_limit: int = 1_000,
    ) -> Tenant:
        sponsor = await self._post(
            "/v1/billing/wallets/sponsor",
            json={
                "sponsor_name": f"Failure Lab {label}",
                "email": "lab@example.com",
                "initial_credits": initial_credits,
                "require_kyc": False,
            },
            headers=self.admin_headers,
            expected=201,
        )
        agent = await self._post(
            "/v1/billing/wallets/agent",
            json={
                "sponsor_wallet_id": sponsor["wallet_id"],
                "agent_id": f"{label}-{uuid.uuid4().hex[:6]}",
                "budget_credits": budget_credits,
                "daily_limit": daily_limit,
            },
            headers=self.admin_headers,
            expected=201,
        )
        key = await self._post(
            "/v1/api-keys",
            json={
                "wallet_id": agent["wallet_id"],
                "key_name": f"{label}-runtime",
                "expires_in_days": 1,
            },
            headers=self.admin_headers,
            expected=201,
        )
        return Tenant(
            sponsor_wallet_id=sponsor["wallet_id"],
            wallet_id=agent["wallet_id"],
            key_id=key["key_id"],
            api_key=key["api_key"],
        )

    async def issue_permit(
        self,
        tenant: Tenant,
        *,
        max_credits: Decimal | int = Decimal("50"),
        expires_in_minutes: int = 30,
        tool: str = GATEWAY_TOOL_ID,
        forbidden_fields: tuple[str, ...] = (),
        max_calls_per_tool: dict[str, int] | None = None,
        aggregate_value_cap: Decimal | int | None = None,
        recipient_domain: str | None = None,
        requires_human_approval: bool = False,
        bind_key: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "issuer_wallet_id": tenant.wallet_id,
            "subject_wallet_id": tenant.wallet_id,
            "subject_key_id": tenant.key_id if bind_key else None,
            "allowed_tools": [tool],
            "scopes": [f"tool:{tool}:invoke", "billing:charge"],
            "max_credits": str(max_credits),
            "requires_human_approval": requires_human_approval,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)
            ).isoformat(),
        }
        if forbidden_fields:
            body["forbidden_fields"] = list(forbidden_fields)
        if max_calls_per_tool:
            body["max_calls_per_tool"] = dict(max_calls_per_tool)
        if aggregate_value_cap is not None:
            body["aggregate_value_cap"] = str(aggregate_value_cap)
        if recipient_domain is not None:
            body["recipient_domain"] = recipient_domain
        return await self._post(
            "/v1/permits",
            json=body,
            headers={
                **self.admin_headers,
                "Idempotency-Key": f"permit-{uuid.uuid4().hex}",
            },
            expected=201,
        )

    async def revoke_permit(self, tenant: Tenant, permit_id: str) -> dict[str, Any]:
        return await self._post(
            f"/v1/permits/{permit_id}/revoke",
            json=None,
            headers=tenant.headers,
            expected=200,
        )

    async def get_permit(self, tenant: Tenant, permit_id: str) -> dict[str, Any]:
        response = await self.client.get(
            f"/v1/permits/{permit_id}", headers=tenant.headers
        )
        response.raise_for_status()
        return response.json()

    # -- invocation -------------------------------------------------------

    def jsonrpc_body(
        self,
        tenant: Tenant,
        *,
        permit_id: str | None,
        identity: OperationIdentity,
        arguments: dict[str, Any],
        tool: str = GATEWAY_TOOL_ID,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "wallet_id": tenant.wallet_id,
            "idempotency_key": identity.idempotency_key,
        }
        if permit_id is not None:
            context["permit_id"] = permit_id
        return {
            "jsonrpc": "2.0",
            "id": identity.request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments, "mcpContext": context},
        }

    async def invoke(
        self,
        tenant: Tenant,
        *,
        permit_id: str | None,
        identity: OperationIdentity,
        arguments: dict[str, Any],
        tool: str = GATEWAY_TOOL_ID,
    ) -> GatewayOutcome:
        body = self.jsonrpc_body(
            tenant,
            permit_id=permit_id,
            identity=identity,
            arguments=arguments,
            tool=tool,
        )
        started = time.perf_counter()
        try:
            response = await self.client.post(
                "/mcp/messages", json=body, headers=tenant.headers
            )
        except BaseException as exc:  # noqa: BLE001 - a simulated death is a BaseException
            if is_process_death(exc):
                return GatewayOutcome(
                    status="gateway_process_died",
                    http_status=None,
                    jsonrpc_code=None,
                    reason=str(exc),
                    receipt=None,
                    structured=None,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    details={"client_view": "connection dropped; no response"},
                )
            if isinstance(exc, Exception):
                # An unhandled exception inside the application. Over a real
                # socket uvicorn would turn this into a 500 and the client
                # would see a failed request; the in-process ASGI transport
                # re-raises it into the caller instead. Classifying it as an
                # outcome rather than letting it escape is what makes an
                # induced infrastructure failure measurable at all -- a
                # scenario that takes the database away needs the resulting
                # request failure to be a row in its table, not a crash of
                # the harness that was measuring it.
                #
                # SimulatedProcessDeath is deliberately a BaseException and is
                # handled above, so this never swallows a crash injection.
                return GatewayOutcome(
                    status="gateway_error",
                    http_status=None,
                    jsonrpc_code=None,
                    reason=f"{type(exc).__name__}: {exc}"[:300],
                    receipt=None,
                    structured=None,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    details={
                        "client_view": "the gateway failed to answer",
                        "exception_type": type(exc).__name__,
                    },
                )
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            detail: Any
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            return GatewayOutcome(
                status="http_error",
                http_status=response.status_code,
                jsonrpc_code=None,
                reason=str(detail)[:200],
                receipt=None,
                structured=None,
                latency_ms=latency_ms,
                raw=detail if isinstance(detail, dict) else None,
            )
        data = response.json()
        if "result" in data:
            result = data["result"] or {}
            receipt = result.get("receipt")
            return GatewayOutcome(
                status="success",
                http_status=200,
                jsonrpc_code=None,
                reason=None,
                receipt=receipt,
                structured=result.get("structuredContent"),
                latency_ms=latency_ms,
                raw=data,
            )
        error = data.get("error") or {}
        error_data = error.get("data") or {}
        receipt = error_data.get("receipt") if isinstance(error_data, dict) else None
        message = error.get("message")
        code = error.get("code")
        details = (
            {k: v for k, v in error_data.items() if k != "receipt"}
            if isinstance(error_data, dict)
            else {}
        )
        return GatewayOutcome(
            status=_classify(message, code, receipt),
            http_status=200,
            jsonrpc_code=code,
            reason=message,
            receipt=receipt,
            structured=None,
            latency_ms=latency_ms,
            details=details,
            raw=data,
        )

    async def invoke_rest(
        self,
        tenant: Tenant,
        *,
        permit_id: str | None,
        identity: OperationIdentity,
        arguments: dict[str, Any],
        tool: str = GATEWAY_TOOL_ID,
    ) -> GatewayOutcome:
        """The REST transport of the same governed call."""
        body = {
            "name": tool,
            "arguments": arguments,
            "mcp_context": {
                "wallet_id": tenant.wallet_id,
                "permit_id": permit_id,
                "idempotency_key": identity.idempotency_key,
            },
        }
        started = time.perf_counter()
        response = await self.client.post(
            f"/mcp/tools/{tool}/invoke", json=body, headers=tenant.headers
        )
        latency_ms = (time.perf_counter() - started) * 1000
        data = response.json()
        if response.status_code == 200:
            return GatewayOutcome(
                status="success",
                http_status=200,
                jsonrpc_code=None,
                reason=None,
                receipt=data.get("receipt"),
                structured=data.get("structuredContent"),
                latency_ms=latency_ms,
                raw=data,
            )
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            receipt = detail.get("receipt")
            reason = detail.get("error") or detail.get("message")
        else:
            receipt = None
            reason = str(detail)
        return GatewayOutcome(
            status=_classify(reason, None, receipt)
            if receipt
            else _rest_status(response.status_code, reason),
            http_status=response.status_code,
            jsonrpc_code=None,
            reason=reason,
            receipt=receipt,
            structured=None,
            latency_ms=latency_ms,
            raw=data if isinstance(data, dict) else None,
        )

    # -- observation (gateway-reported) ----------------------------------

    async def snapshot(self, tenant: Tenant) -> GatewaySnapshot:
        from sqlalchemy import select

        from app.db.database import get_session_factory
        from app.db.models import (
            IdempotencyRecordModel,
            LedgerEntryModel,
            McpDispatchAttemptModel,
            PermitModel,
            ReceiptModel,
            WalletModel,
        )
        from app.services.idempotency import GOVERNED_MCP_IDEMPOTENCY_ENDPOINT
        from app.services.mcp_dispatch_attempts import (
            DISPATCH_SENT_STATES,
            DISPATCH_TERMINAL_STATES,
        )

        factory = get_session_factory()
        async with factory() as session:
            attempts = (
                (
                    await session.execute(
                        select(McpDispatchAttemptModel)
                        .where(McpDispatchAttemptModel.wallet_id == tenant.wallet_id)
                        .order_by(McpDispatchAttemptModel.created_at)
                    )
                )
                .scalars()
                .all()
            )
            receipts = (
                (
                    await session.execute(
                        select(ReceiptModel)
                        .where(ReceiptModel.wallet_id == tenant.wallet_id)
                        .order_by(ReceiptModel.created_at)
                    )
                )
                .scalars()
                .all()
            )
            entries = (
                (
                    await session.execute(
                        select(LedgerEntryModel)
                        .where(LedgerEntryModel.wallet_id == tenant.wallet_id)
                        .order_by(LedgerEntryModel.timestamp)
                    )
                )
                .scalars()
                .all()
            )
            records = (
                (
                    await session.execute(
                        select(IdempotencyRecordModel)
                        .where(
                            IdempotencyRecordModel.wallet_id == tenant.wallet_id,
                            IdempotencyRecordModel.endpoint
                            == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                        )
                        .order_by(IdempotencyRecordModel.created_at)
                    )
                )
                .scalars()
                .all()
            )
            permits = (
                (
                    await session.execute(
                        select(PermitModel).where(
                            PermitModel.subject_wallet_id == tenant.wallet_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            wallet = await session.get(WalletModel, tenant.wallet_id)

        sent_states = set(DISPATCH_SENT_STATES) | set(DISPATCH_TERMINAL_STATES)
        attempt_rows = [
            {
                "attempt_id": a.attempt_id,
                "idempotency_record_id": a.idempotency_record_id,
                "state": a.state,
                "sent": (a.state in sent_states and a.dispatched_at is not None)
                or a.state in DISPATCH_SENT_STATES,
                "claim_hash_present": a.dispatch_claim_hash is not None,
                "ledger_entry_id": a.ledger_entry_id,
                "credits_authorized": _decimal_text(a.credits_authorized),
                "credits_charged": _decimal_text(a.credits_charged),
                "error_code": a.error_code,
                "response_hash": a.response_hash,
                "debit_refunded_at": _isoformat(a.debit_refunded_at),
                "budget_released_at": _isoformat(a.budget_released_at),
                "created_at": _isoformat(a.created_at),
                "dispatched_at": _isoformat(a.dispatched_at),
                "completed_at": _isoformat(a.completed_at),
            }
            for a in attempts
        ]
        debit_rows = []
        refund_rows = []
        for entry in entries:
            row = {
                "entry_id": entry.entry_id,
                "action": entry.action,
                "amount": _decimal_text(entry.amount),
                "balance_after": _decimal_text(entry.balance_after),
                "description": entry.description,
                "operation_key": entry.operation_key,
                "created_at": _isoformat(entry.timestamp),
            }
            if entry.amount is not None and entry.amount < 0:
                debit_rows.append(row)
            elif str(entry.action).lower() == "refund" or str(
                entry.entry_id
            ).startswith("refund-"):
                refund_rows.append(row)
        receipt_rows = [
            {
                "receipt_id": r.receipt_id,
                "outcome": r.outcome,
                "reason_code": r.reason_code,
                "credits_authorized": _decimal_text(r.credits_authorized),
                "credits_charged": _decimal_text(r.credits_charged),
                "ledger_entry_id": r.ledger_entry_id,
                "dispatch_attempt_id": r.dispatch_attempt_id,
                "idempotency_record_id": r.idempotency_record_id,
                "permit_id": r.permit_id,
                "tool": r.tool,
                "request_hash": r.request_hash,
                "response_hash": r.response_hash,
                "signature_key_id": r.signature_key_id,
                "created_at": _isoformat(r.created_at),
            }
            for r in receipts
        ]
        record_rows = [
            {
                "record_id": rec.record_id,
                "idempotency_key": rec.idempotency_key,
                "operation_kind": rec.operation_kind,
                "completed": rec.response_json is not None,
                "status_code": rec.status_code,
                "created_at": _isoformat(rec.created_at),
            }
            for rec in records
        ]
        permit_rows = [
            {
                "permit_id": p.permit_id,
                "status": p.status,
                "max_credits": _decimal_text(p.max_credits),
                "spent_credits": _decimal_text(p.spent_credits),
                "revoked_at": _isoformat(p.revoked_at),
            }
            for p in permits
        ]
        return GatewaySnapshot(
            wallet_id=tenant.wallet_id,
            attempts=attempt_rows,
            debits=debit_rows,
            refunds=refund_rows,
            receipts=receipt_rows,
            idempotency_records=record_rows,
            permits=permit_rows,
            wallet_balance=_decimal_text(wallet.balance)
            if wallet is not None
            else None,
        )

    async def reconcile(self, *, idle_seconds: int = 0) -> dict[str, Any]:
        """The operator sweep a restarted gateway runs."""
        from app.services.idempotency import get_idempotency_service
        from app.services.mcp_dispatch_reconciliation import (
            get_mcp_dispatch_reconciliation_service,
        )
        from app.services.permits import get_permit_service

        dispatch = await get_mcp_dispatch_reconciliation_service().reconcile(
            idle_seconds=idle_seconds,
            terminal_idle_seconds=idle_seconds,
        )
        (
            repaired,
            needs_review,
        ) = await get_idempotency_service().reconcile_stuck_records(
            idle_seconds=idle_seconds
        )
        budgets = await get_permit_service().reconcile_budgets(
            idle_seconds=idle_seconds
        )
        return {
            "dispatch_prepared_finalized": dispatch.prepared_finalized,
            "dispatch_uncertain": dispatch.dispatched_uncertain,
            "dispatch_terminal_recovered": dispatch.terminal_recovered,
            "dispatch_idempotency_recovered": dispatch.idempotency_recovered,
            "dispatch_failed_attempt_ids": list(dispatch.failed_attempt_ids),
            "idempotency_repaired": repaired,
            "idempotency_needs_review": needs_review,
            "permit_budgets_corrected": budgets,
        }

    async def backdate_attempts(self, tenant: Tenant, *, seconds: int) -> int:
        """Age this wallet's active attempts past the reconciler's idle window.

        The lab cannot wait the fixed 11,430-second window a live claim gets
        before being declared abandoned; this moves the clock instead and is
        reported as such.
        """
        from sqlalchemy import update as sa_update

        from app.core.time import utc_now
        from app.db.database import get_session_factory
        from app.db.models import McpDispatchAttemptModel

        stale = utc_now() - timedelta(seconds=seconds)
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                sa_update(McpDispatchAttemptModel)
                .where(McpDispatchAttemptModel.wallet_id == tenant.wallet_id)
                .values(updated_at=stale, dispatched_at=stale, created_at=stale)
            )
            await session.commit()
            return int(result.rowcount or 0)

    # -- evidence surfaces --------------------------------------------------

    async def portable_receipt(self, tenant: Tenant, receipt_id: str) -> dict[str, Any]:
        response = await self.client.get(
            f"/v1/receipts/{receipt_id}/portable", headers=tenant.headers
        )
        response.raise_for_status()
        return response.json()

    async def receipt_evidence(self, tenant: Tenant, receipt_id: str) -> dict[str, Any]:
        response = await self.client.get(
            f"/v1/receipts/{receipt_id}/evidence", headers=tenant.headers
        )
        response.raise_for_status()
        return response.json()

    async def trust_keys(self) -> dict[str, Any]:
        response = await self.client.get("/.well-known/trust-keys.json")
        response.raise_for_status()
        return response.json()

    async def ledger(self, tenant: Tenant) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"/v1/billing/ledger/{tenant.wallet_id}", headers=tenant.headers
        )
        response.raise_for_status()
        return list(response.json()["entries"])

    async def version(self) -> dict[str, Any]:
        response = await self.client.get("/health")
        try:
            data = response.json()
        except ValueError:
            data = {}
        return {"http_status": response.status_code, "health": data}

    # -- crash injection ----------------------------------------------------

    @contextlib.contextmanager
    def crash_at(self, boundary: str) -> Iterator[dict[str, Any]]:
        """Make the next governed call die right after ``boundary`` commits."""
        if boundary not in CRASH_BOUNDARIES:
            raise ValueError(f"unknown crash boundary {boundary!r}")
        from app.services.agent_money import AgentMoney
        from app.services.mcp_dispatch_attempts import McpDispatchAttemptService

        state: dict[str, Any] = {"boundary": boundary, "fired": False, "fired_at": None}

        def die() -> None:
            state["fired"] = True
            state["fired_at"] = datetime.now(timezone.utc).isoformat()
            raise SimulatedProcessDeath(boundary)

        originals: list[tuple[Any, str, Any]] = []

        def patch(target: Any, name: str, replacement: Any) -> None:
            originals.append((target, name, getattr(target, name)))
            setattr(target, name, replacement)

        if boundary == "after_idempotency_begin":
            from app.services.idempotency import IdempotencyService

            original_begin = IdempotencyService.begin_with_record

            async def begin(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = await original_begin(self, *args, **kwargs)
                if (
                    getattr(result, "replay", None) is None
                    and kwargs.get("operation_kind") == "upstream_mcp"
                    and not state["fired"]
                ):
                    die()
                return result

            patch(IdempotencyService, "begin_with_record", begin)
        elif boundary == "after_prepare":
            original = McpDispatchAttemptService.authorize_reserve_and_prepare

            async def prepare(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = await original(self, *args, **kwargs)
                validation, attempt = result
                if attempt is not None and not state["fired"]:
                    die()
                return result

            patch(McpDispatchAttemptService, "authorize_reserve_and_prepare", prepare)
        elif boundary == "after_debit":
            original_charge = AgentMoney.charge

            async def charge(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = await original_charge(self, *args, **kwargs)
                if getattr(result, "entry_id", None) and not state["fired"]:
                    die()
                return result

            patch(AgentMoney, "charge", charge)
        elif boundary == "after_attach_charge":
            original_attach = McpDispatchAttemptService.attach_charge

            async def attach(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = await original_attach(self, *args, **kwargs)
                if not state["fired"]:
                    die()
                return result

            patch(McpDispatchAttemptService, "attach_charge", attach)
        elif boundary == "after_claim":
            original_claim = McpDispatchAttemptService.claim_dispatch

            async def claim(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = await original_claim(self, *args, **kwargs)
                if not state["fired"]:
                    die()
                return result

            patch(McpDispatchAttemptService, "claim_dispatch", claim)
        elif boundary in ("after_upstream_response", "after_terminal_commit"):
            original_complete = McpDispatchAttemptService.complete

            async def complete(self: Any, *args: Any, **kwargs: Any) -> Any:
                if kwargs.get("state") == "succeeded" and not state["fired"]:
                    if boundary == "after_upstream_response":
                        die()
                    await original_complete(self, *args, **kwargs)
                    die()
                return await original_complete(self, *args, **kwargs)

            patch(McpDispatchAttemptService, "complete", complete)

        try:
            yield state
        finally:
            for target, name, value in reversed(originals):
                setattr(target, name, value)

    @contextlib.contextmanager
    def hold_at(self, boundary: str) -> Iterator[BoundaryHold]:
        """Pause the next governed call right after ``boundary`` commits.

        The scenario waits on ``hold.reached``, does something concurrent
        (revoke the permit, take the database away), then sets
        ``hold.release`` and observes how the paused call proceeds.
        """
        if boundary not in ("after_prepare", "after_attach_charge", "after_claim"):
            raise ValueError(f"hold_at does not support boundary {boundary!r}")
        from app.services.mcp_dispatch_attempts import McpDispatchAttemptService

        hold = BoundaryHold(boundary=boundary)
        originals: list[tuple[Any, str, Any]] = []

        def patch(target: Any, name: str, replacement: Any) -> None:
            originals.append((target, name, getattr(target, name)))
            setattr(target, name, replacement)

        async def pause() -> None:
            if hold.fired:
                return
            hold.fired = True
            hold.reached.set()
            await hold.release.wait()

        if boundary == "after_prepare":
            original = McpDispatchAttemptService.authorize_reserve_and_prepare

            async def prepare(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = await original(self, *args, **kwargs)
                if result[1] is not None:
                    await pause()
                return result

            patch(McpDispatchAttemptService, "authorize_reserve_and_prepare", prepare)
        elif boundary == "after_attach_charge":
            original_attach = McpDispatchAttemptService.attach_charge

            async def attach(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = await original_attach(self, *args, **kwargs)
                await pause()
                return result

            patch(McpDispatchAttemptService, "attach_charge", attach)
        else:
            original_claim = McpDispatchAttemptService.claim_dispatch

            async def claim(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = await original_claim(self, *args, **kwargs)
                await pause()
                return result

            patch(McpDispatchAttemptService, "claim_dispatch", claim)
        try:
            yield hold
        finally:
            hold.release.set()
            for target, name, value in reversed(originals):
                setattr(target, name, value)

    @contextlib.asynccontextmanager
    async def database_outage(self) -> AsyncIterator[dict[str, Any]]:
        """Take the gateway's database away until the block exits.

        Simulated at the engine: pooled connections are disposed and every
        new connection attempt is refused, so any statement issued inside the
        block fails as it would against a database that is down. This is not
        a real server restart; the label travels with every result.
        """
        from sqlalchemy import event
        from sqlalchemy.exc import OperationalError

        from app.db.database import get_engine

        engine = get_engine()
        if engine is None:
            raise RuntimeError("gateway database is not configured")
        state: dict[str, Any] = {"refused_connections": 0}

        def refuse(dialect: Any, conn_rec: Any, cargs: Any, cparams: Any) -> Any:
            state["refused_connections"] += 1
            raise OperationalError(
                "failure_lab_database_outage", None, RuntimeError("simulated outage")
            )

        event.listen(engine.sync_engine, "do_connect", refuse)
        await engine.dispose()
        try:
            yield state
        finally:
            event.remove(engine.sync_engine, "do_connect", refuse)
            await engine.dispose()

    # -- standard MCP endpoint ---------------------------------------------

    @contextlib.contextmanager
    def standard_endpoint_enabled(self) -> Iterator[None]:
        from app.core.config import get_settings

        settings = get_settings()
        previous = settings.ENABLE_STANDARD_MCP_ENDPOINT
        settings.ENABLE_STANDARD_MCP_ENDPOINT = True
        try:
            yield
        finally:
            settings.ENABLE_STANDARD_MCP_ENDPOINT = previous

    @contextlib.asynccontextmanager
    async def mcp_client_session(
        self,
        tenant: Tenant,
        *,
        idempotency_key: str | None,
    ) -> AsyncIterator[Any]:
        """An official MCP SDK client session against ``POST /mcp``.

        This is an independent client implementation relative to the
        product's own SDK: the transport, framing, negotiation and error
        handling are the reference SDK's, not this repository's.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers = dict(tenant.headers)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=GATEWAY_BASE_URL,
            headers=headers,
            follow_redirects=False,
        )
        try:
            async with streamable_http_client(
                f"{GATEWAY_BASE_URL}/mcp", http_client=http_client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    yield session
        finally:
            await http_client.aclose()

    # -- helpers --------------------------------------------------------------

    async def _post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None,
        headers: dict[str, str],
        expected: int,
    ) -> dict[str, Any]:
        response = await self.client.post(path, json=json, headers=headers)
        if response.status_code != expected:
            raise RuntimeError(
                f"POST {path} -> {response.status_code}: {response.text[:300]}"
            )
        return response.json()


def _rest_status(status_code: int, reason: str | None) -> str:
    if reason == "idempotency_key_reused":
        return "key_conflict"
    if reason == "idempotency_in_progress" or status_code == 409:
        return "in_progress"
    if status_code == 403:
        return "denied"
    if status_code == 402:
        return "insufficient_funds"
    if status_code == 504:
        return "delivery_uncertain"
    if status_code == 400:
        return "invalid_params"
    return "http_error"


__all__ = [
    "BoundaryHold",
    "CLIENT_TERMINAL_OUTCOMES",
    "CRASH_BOUNDARIES",
    "CRASH_BOUNDARY_DESCRIPTIONS",
    "DOWNSTREAM_URL",
    "GATEWAY_TOOL_ID",
    "GatewayOutcome",
    "GatewaySnapshot",
    "GatewayUnderTest",
    "SimulatedProcessDeath",
    "Tenant",
    "boot_standalone_environment",
    "is_process_death",
]
