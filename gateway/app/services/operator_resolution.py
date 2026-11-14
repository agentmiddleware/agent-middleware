"""Operator adjudication of immutable uncertain receipts, without dispatching."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from app.core.resilience import run_with_write_conflict_retry
from app.db.database import get_session_factory
from app.db.models import (
    LedgerEntryModel,
    McpDispatchAttemptModel,
    ReceiptModel,
    ReceiptResolutionModel,
)
from app.schemas.resolutions import ResolutionRequest
from app.services.agent_money import get_agent_money
from app.services.idempotency import ACTION_MCP_IDEMPOTENCY_ENDPOINT
from app.services.mcp_dispatch_attempts import (
    get_mcp_dispatch_attempt_service,
)
from app.services.mcp_dispatch_reconciliation import (
    STATUS_LOOKUP_TIMEOUT_SECONDS,
    UpstreamDispatchStatusQuery,
    _require_async_lookup,
)
from app.services.ownership import CallerIdentity
from app.services.permits import get_permit_service
from app.services.receipt_resolutions import (
    ReceiptResolutionError,
    append_resolution,
    canonical_evidence,
    get_resolution,
)

ReceiptStatusLookup = Callable[[UpstreamDispatchStatusQuery], Awaitable[str | None]]
_LOOKUPS: dict[str, ReceiptStatusLookup] = {}


def register_receipt_status_lookup(origin: str, lookup: ReceiptStatusLookup) -> None:
    """Explicit startup opt-in only. No adapter is registered by default."""
    _require_async_lookup(lookup)
    _LOOKUPS[origin] = lookup


class RefundFixtureStatusLookup:
    """Read the refund fixture status contract; never retain its raw answer.

    Deployment code supplies an origin and token. Redirects are forbidden and
    only the status enum is exposed, so reflected credentials cannot enter
    evidence, responses, or logs. A transport may be injected for offline tests.
    """

    def __init__(
        self,
        origin: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = httpx.URL(origin)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("fixture origin must be an origin without credentials")
        self._origin = origin.rstrip("/")
        self._token = token
        self._transport = transport

    async def __call__(self, query: UpstreamDispatchStatusQuery) -> str | None:
        if query.upstream_origin.rstrip("/") != self._origin:
            return None
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=STATUS_LOOKUP_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                f"{self._origin}/status",
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "tool": query.upstream_tool_name,
                    "invocation_id": query.invocation_id,
                    "idempotency_key": query.forwarded_idempotency_key,
                },
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 16384:
                        return None
                answer = json.loads(data)
        status = answer.get("status") if isinstance(answer, dict) else None
        return (
            status
            if status in {"executed", "not_executed", "pending", "unknown"}
            else None
        )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(
                word in key.lower()
                for word in (
                    "token",
                    "secret",
                    "authorization",
                    "password",
                    "api_key",
                    "credential",
                )
            )
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


async def resolve_receipt(
    receipt_id: str, request: ResolutionRequest
) -> ReceiptResolutionModel:
    evidence = _redact(request.evidence.model_dump(mode="json", exclude_none=True))
    if request.refund_credits and request.outcome != "confirmed_not_delivered":
        raise ReceiptResolutionError("resolution_refund_outcome_invalid")
    async with get_session_factory()() as session:
        receipt = await session.get(ReceiptModel, receipt_id)
        if receipt is None:
            raise ReceiptResolutionError("receipt_not_found")
        attempt = (
            await session.get(McpDispatchAttemptModel, receipt.dispatch_attempt_id)
            if receipt.dispatch_attempt_id
            else None
        )
        if attempt is None and receipt.idempotency_record_id:
            attempt = (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        cast(
                            ColumnElement[bool],
                            McpDispatchAttemptModel.idempotency_record_id
                            == receipt.idempotency_record_id,
                        )
                    )
                )
            ).scalar_one_or_none()
        if attempt is not None and (
            attempt.wallet_id != receipt.wallet_id
            or attempt.ledger_entry_id != receipt.ledger_entry_id
        ):
            raise ReceiptResolutionError("resolution_dispatch_linkage_invalid")
        if request.refund_credits:
            debit = (
                await session.get(LedgerEntryModel, receipt.ledger_entry_id)
                if receipt.ledger_entry_id
                else None
            )
            if (
                debit is None
                or debit.wallet_id != receipt.wallet_id
                or debit.action != "debit"
                or abs(debit.amount) != receipt.credits_charged
            ):
                raise ReceiptResolutionError("resolution_debit_invalid")
            # A product-derived ledger identity, signed before the refund. Its
            # presence is durable intent; retry finishes an interrupted refund.
            evidence["refund_entry_id"] = f"refund-{debit.entry_id}"
    evidence["use_lookup"] = request.use_lookup
    if request.use_lookup:
        if attempt is None:
            raise ReceiptResolutionError("resolution_inconclusive")
        evidence["lookup_origin"] = attempt.upstream_origin
        evidence["lookup_status"] = (
            "executed" if request.outcome == "confirmed_success" else "not_executed"
        )
    canonical_evidence(evidence)
    existing = await get_resolution(receipt_id)
    if request.use_lookup and existing is None:
        assert attempt is not None
        context = (
            await get_mcp_dispatch_attempt_service().get_context(attempt.attempt_id)
            if attempt
            else None
        )
        lookup = _LOOKUPS.get(attempt.upstream_origin) if attempt else None
        if context is None or lookup is None:
            raise ReceiptResolutionError("resolution_inconclusive")
        query = UpstreamDispatchStatusQuery(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.idempotency_record_id,
            idempotency_key=context.idempotency_key,
            forwarded_idempotency_key=None
            if context.endpoint == ACTION_MCP_IDEMPOTENCY_ENDPOINT
            else context.idempotency_key,
            upstream_origin=attempt.upstream_origin,
            upstream_tool_name=attempt.upstream_tool_name,
            request_hash=attempt.request_hash,
            dispatched_at=attempt.dispatched_at,
        )
        try:
            status = await asyncio.wait_for(
                lookup(query), timeout=STATUS_LOOKUP_TIMEOUT_SECONDS
            )
        except Exception:
            raise ReceiptResolutionError("resolution_inconclusive") from None
        # Terminalizing a claim cannot fence a paused sender. Only attempts
        # with no historical claim or send can prove non-delivery locally.
        negative_safe = (
            attempt.state == "returned_error"
            and attempt.completed_at is not None
            and attempt.dispatch_claim_hash is None
            and attempt.dispatched_at is None
        )
        expected = (
            "confirmed_success"
            if status == "executed"
            else "confirmed_not_delivered"
            if status == "not_executed" and negative_safe
            else None
        )
        if expected is None or expected != request.outcome:
            raise ReceiptResolutionError("resolution_inconclusive")
    model = await append_resolution(receipt_id, request.outcome, evidence)
    if request.refund_credits:
        assert receipt.ledger_entry_id is not None
        charge_entry_id = receipt.ledger_entry_id

        async def compensate() -> None:
            await get_agent_money().refund_charge(
                wallet_id=receipt.wallet_id,
                charge_entry_id=charge_entry_id,
                description=f"Operator resolution {model.resolution_id}",
                caller=CallerIdentity.for_wallet(receipt.wallet_id),
            )

        await run_with_write_conflict_retry(
            compensate,
            on_exhausted=lambda exc: ReceiptResolutionError(
                "resolution_refund_contended"
            ),
        )
        if attempt:

            async def finish_compensation() -> None:
                await get_mcp_dispatch_attempt_service().mark_debit_refunded(
                    attempt_id=attempt.attempt_id,
                    ledger_entry_id=charge_entry_id,
                )
                await get_permit_service().release_dispatch_budget_once(
                    attempt.attempt_id,
                    caller=CallerIdentity.for_wallet(receipt.wallet_id),
                )

            await run_with_write_conflict_retry(
                finish_compensation,
                on_exhausted=lambda exc: ReceiptResolutionError(
                    "resolution_refund_contended"
                ),
            )
    return model
