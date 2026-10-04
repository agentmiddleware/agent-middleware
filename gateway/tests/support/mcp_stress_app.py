"""Two-process PostgreSQL fault-injection app for governed MCP acceptance tests.

This module is loaded only by the opt-in multiprocess stress harness.  It
registers two durable test tools -- one local, one upstream-MCP-backed -- and
installs process-local gates around the production commit boundaries.  No
test-only hooks are imported by the normal application.

The upstream tool exists so the remote governed dispatch state machine
(``prepared -> dispatch_claimed -> delivery_uncertain``) is exercised by a real
process kill rather than by seeding its durable states in-process.  Its
side-effect table is deliberately duplicate-tolerant: a redispatch after
ambiguity must be observable as a second row, never hidden by a constraint.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import Header, HTTPException, Query, status
from sqlalchemy import text

from app.main import app
from app.routers import mcp as mcp_router
from app.schemas.billing import ServiceCategory
from app.services.agent_money import AgentMoney
from app.services.idempotency import IdempotencyService, get_idempotency_service
from app.services.mcp_dispatch_attempts import McpDispatchAttemptService
from app.services.mcp_dispatch_reconciliation import (
    get_mcp_dispatch_reconciliation_service,
)
from app.services.permits import PermitService, get_permit_service
from app.services.receipts import ReceiptService
from app.services.service_registry import get_service_registry
from app.services.upstream_mcp import UpstreamMcpResult


_FAULT_POINT = os.environ.get("MCP_STRESS_FAULT_POINT", "")
_FAULT_ACTION = os.environ.get("MCP_STRESS_FAULT_ACTION", "pause")
_FAULT_REPEAT = os.environ.get("MCP_STRESS_FAULT_REPEAT", "once")
_MARKER_PATH = Path(os.environ.get("MCP_STRESS_MARKER_PATH", "/tmp/mcp-stress.marker"))
_RELEASE_PATH = Path(
    os.environ.get("MCP_STRESS_RELEASE_PATH", "/tmp/mcp-stress.release")
)
_CONTROL_TOKEN = os.environ.get("MCP_STRESS_CONTROL_TOKEN", "")
_fault_count = 0


def _fault(point: str, **context: Any) -> None:
    """Trigger the configured gate after a production method returns.

    ``pause`` writes a durable marker and blocks until the parent creates the
    release file (or kills this process). ``raise`` simulates a commit whose
    acknowledgement was lost after the durable write landed.
    """

    global _fault_count
    if _FAULT_POINT != point:
        return
    if _FAULT_REPEAT != "always" and _fault_count:
        return

    _fault_count += 1
    _MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "point": point,
        "action": _FAULT_ACTION,
        "count": _fault_count,
        "pid": os.getpid(),
        "context": context,
        "written_at_unix": time.time(),
    }
    _MARKER_PATH.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")

    if _FAULT_ACTION == "raise":
        raise RuntimeError(f"mcp_stress_lost_commit_ack:{point}:{_fault_count}")
    if _FAULT_ACTION != "pause":
        raise RuntimeError(f"Unsupported MCP_STRESS_FAULT_ACTION={_FAULT_ACTION!r}")

    while not _RELEASE_PATH.exists():
        time.sleep(0.01)


def _install_fault_wrappers() -> None:
    if _FAULT_POINT == "before_owner_commit":
        from sqlalchemy.ext.asyncio import AsyncSession
        from app.db.models import IdempotencyRecordModel

        original_commit = AsyncSession.commit

        async def owner_commit(self: AsyncSession) -> None:
            owners = [
                r
                for r in self.new
                if isinstance(r, IdempotencyRecordModel)
                and r.endpoint == "/mcp/action/v1"
            ]
            if owners:
                await self.flush()
                _fault(
                    "before_owner_commit", record_id=owners[0].record_id, flushed=True
                )
            await original_commit(self)

        AsyncSession.commit = owner_commit

    original_begin_with_record = IdempotencyService.begin_with_record
    original_complete = IdempotencyService.complete
    original_mark_charged = IdempotencyService.mark_charged
    original_claim_dispatch = McpDispatchAttemptService.claim_dispatch
    original_prepare = McpDispatchAttemptService.authorize_reserve_and_prepare
    original_complete_dispatch = McpDispatchAttemptService.complete
    original_authorize_and_reserve = PermitService.authorize_and_reserve
    original_charge = AgentMoney.charge
    original_create_receipt = ReceiptService.create_receipt
    original_audit = mcp_router._audit_mcp_invocation

    async def begin_with_record(
        self: IdempotencyService, *args: Any, **kwargs: Any
    ) -> Any:
        result = await original_begin_with_record(self, *args, **kwargs)
        if result.replay is None:
            _fault(
                "after_idempotency_begin",
                idempotency_key=kwargs.get("idempotency_key"),
                record_id=result.record_id,
            )
        return result

    async def complete(self: IdempotencyService, *args: Any, **kwargs: Any) -> Any:
        result = await original_complete(self, *args, **kwargs)
        _fault(
            "after_idempotency_complete", idempotency_key=kwargs.get("idempotency_key")
        )
        return result

    async def mark_charged(self: IdempotencyService, *args: Any, **kwargs: Any) -> Any:
        result = await original_mark_charged(self, *args, **kwargs)
        _fault(
            "after_mark_charged",
            idempotency_key=kwargs.get("idempotency_key"),
            ledger_entry_id=kwargs.get("ledger_entry_id"),
        )
        return result

    async def claim_dispatch(
        self: McpDispatchAttemptService, *args: Any, **kwargs: Any
    ) -> Any:
        result = await original_claim_dispatch(self, *args, **kwargs)
        _fault(
            "after_dispatch_claim",
            attempt_id=getattr(result, "attempt_id", None),
        )
        return result

    async def prepare(
        self: McpDispatchAttemptService, *args: Any, **kwargs: Any
    ) -> Any:
        result = await original_prepare(self, *args, **kwargs)
        if result[1] is not None:
            _fault("after_action_prepare", attempt_id=result[1].attempt_id)
        return result

    async def complete_dispatch(
        self: McpDispatchAttemptService, *args: Any, **kwargs: Any
    ) -> Any:
        if kwargs.get("state") == "succeeded":
            _fault(
                "after_upstream_ack_before_terminal",
                attempt_id=kwargs.get("attempt_id"),
            )
        return await original_complete_dispatch(self, *args, **kwargs)

    async def authorize_and_reserve(
        self: PermitService, *args: Any, **kwargs: Any
    ) -> Any:
        _fault("before_permit_reserve", permit_id=kwargs.get("permit_id"))
        result = await original_authorize_and_reserve(self, *args, **kwargs)
        if getattr(result, "allowed", False):
            _fault("after_permit_reserve", permit_id=kwargs.get("permit_id"))
        return result

    async def charge(self: AgentMoney, *args: Any, **kwargs: Any) -> Any:
        result = await original_charge(self, *args, **kwargs)
        entry_id = getattr(result, "entry_id", None)
        if entry_id:
            _fault(
                "after_debit_commit",
                wallet_id=kwargs.get("wallet_id"),
                ledger_entry_id=entry_id,
            )
        return result

    async def create_receipt(self: ReceiptService, *args: Any, **kwargs: Any) -> Any:
        _fault(
            "before_receipt_commit",
            permit_id=kwargs.get("permit_id"),
            ledger_entry_id=kwargs.get("ledger_entry_id"),
        )
        result = await original_create_receipt(self, *args, **kwargs)
        _fault(
            "after_receipt_commit",
            permit_id=kwargs.get("permit_id"),
            ledger_entry_id=kwargs.get("ledger_entry_id"),
            receipt_id=getattr(result, "receipt_id", None),
        )
        return result

    async def audit(*args: Any, **kwargs: Any) -> Any:
        result = await original_audit(*args, **kwargs)
        _fault("after_audit_commit", audit_event_id=getattr(result, "event_id", None))
        return result

    IdempotencyService.begin_with_record = begin_with_record
    IdempotencyService.complete = complete
    IdempotencyService.mark_charged = mark_charged
    McpDispatchAttemptService.claim_dispatch = claim_dispatch
    McpDispatchAttemptService.authorize_reserve_and_prepare = prepare
    McpDispatchAttemptService.complete = complete_dispatch
    PermitService.authorize_and_reserve = authorize_and_reserve
    AgentMoney.charge = charge
    ReceiptService.create_receipt = create_receipt
    mcp_router._audit_mcp_invocation = audit


async def _stress_tool(
    call_token: str,
    mode: str = "success",
    delay_ms: int = 0,
) -> dict[str, Any]:
    """Persist a side effect before returning or raising.

    The table intentionally permits duplicate ``call_token`` values: duplicate
    execution must be observed and counted, not hidden behind another unique
    constraint.
    """

    from app.db.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        row = await session.execute(
            text(
                """
                INSERT INTO mcp_stress_tool_executions
                    (call_token, mode, worker_pid)
                VALUES (:call_token, :mode, :worker_pid)
                RETURNING execution_id
                """
            ),
            {
                "call_token": call_token,
                "mode": mode,
                "worker_pid": os.getpid(),
            },
        )
        execution_id = int(row.scalar_one())
        await session.commit()

    _fault(
        "after_tool_side_effect",
        call_token=call_token,
        execution_id=execution_id,
    )
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)
    if mode == "fail":
        raise RuntimeError("stress_tool_requested_failure")
    return {
        "call_token": call_token,
        "execution_id": execution_id,
        "worker_pid": os.getpid(),
    }


async def _record_upstream_effect(call_token: str) -> int:
    """Persist the simulated remote side effect and return its row id.

    Duplicates are permitted on purpose. If the trust plane ever redispatched
    an ambiguous invocation, this table is where the second effect would show
    up, so the invariant is measured rather than assumed.
    """

    from app.db.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        row = await session.execute(
            text(
                """
                INSERT INTO mcp_stress_upstream_effects
                    (call_token, worker_pid)
                VALUES (:call_token, :worker_pid)
                RETURNING effect_id
                """
            ),
            {"call_token": call_token, "worker_pid": os.getpid()},
        )
        effect_id = int(row.scalar_one())
        await session.commit()
    return effect_id


class _StressUpstreamExecutor:
    """Stand-in for the upstream MCP adapter with gates at the dispatch boundary.

    ``before_dispatch`` is the production checkpoint that moves the durable
    attempt to ``dispatch_claimed``; the gates around it are what let the harness
    kill a worker on either side of the point where an external effect
    becomes possible.
    """

    async def call_tool(
        self,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
        idempotency_key: str,
        before_dispatch: Callable[[], Awaitable[None]],
    ) -> UpstreamMcpResult:
        call_token = str(arguments.get("call_token", ""))

        # Durable pre-dispatch checkpoint: prepared -> dispatch_claimed.
        await before_dispatch()
        # The legacy fault-point label remains stable for existing harness users.
        _fault(
            "after_mark_dispatched",
            call_token=call_token,
            invocation_id=invocation_id,
        )

        effect_id = await _record_upstream_effect(call_token)
        _fault(
            "after_upstream_effect",
            call_token=call_token,
            invocation_id=invocation_id,
            effect_id=effect_id,
        )

        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": f"upstream effect {effect_id}"}],
            "structuredContent": {
                "call_token": call_token,
                "effect_id": effect_id,
                "forwarded_invocation_id": invocation_id,
                "forwarded_idempotency_key": idempotency_key,
            },
            "isError": False,
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        encoded = canonical.encode()
        return UpstreamMcpResult(
            payload=payload,
            canonical_json=canonical,
            response_hash=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
            is_error=False,
        )


_STRESS_UPSTREAM_TOOL = "stress-upstream-tool"
_STRESS_UPSTREAM_EXECUTOR = _StressUpstreamExecutor()


def _ensure_stress_upstream_registered() -> None:
    """(Re-)register the stress upstream tool if startup cleared it.

    ``register_configured_upstream_mcp`` unregisters every ``upstream_mcp``
    backend during app startup, which runs after this module is imported. The
    router calls ``_ensure_local_mcp_tools_registered`` on each invocation, so
    hooking it is what keeps the tool resolvable without weakening that
    production cleanup.
    """
    registry = get_service_registry()
    if registry.get_executor(_STRESS_UPSTREAM_TOOL) is not None:
        return
    registry.register_upstream(
        service_id=_STRESS_UPSTREAM_TOOL,
        name="Stress Upstream Tool",
        description="Remote-backed side-effect tool used only by the opt-in PG harness",
        category=ServiceCategory.AGENT_COMMS,
        executor=_STRESS_UPSTREAM_EXECUTOR,
        input_schema={
            "type": "object",
            "properties": {"call_token": {"type": "string"}},
            "required": ["call_token"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        credits_per_unit=2.0,
        upstream_tool_name="partner.stress_effect",
        upstream_origin="https://stress-partner.example",
    )


def _install_upstream_registration_hook() -> None:
    original_ensure = mcp_router._ensure_local_mcp_tools_registered

    def ensure_local_mcp_tools_registered() -> None:
        original_ensure()
        _ensure_stress_upstream_registered()

    mcp_router._ensure_local_mcp_tools_registered = ensure_local_mcp_tools_registered


_install_fault_wrappers()
_install_upstream_registration_hook()
get_service_registry().register_local(
    service_id="stress-governed-tool",
    name="Stress Governed Tool",
    description="Durable side-effect tool used only by the opt-in PG harness",
    category=ServiceCategory.AGENT_COMMS,
    func=_stress_tool,
    credits_per_unit=2.0,
    unit_name="call",
    require_permit=True,
)
_ensure_stress_upstream_registered()


def _authorize_control_request(provided_token: str | None) -> None:
    """Keep test-only process controls inaccessible without the run token."""
    if not _CONTROL_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="mcp_stress_control_not_configured",
        )
    if provided_token is None or not hmac.compare_digest(
        provided_token, _CONTROL_TOKEN
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="mcp_stress_control_denied",
        )


@app.get("/__stress/pid", include_in_schema=False)
async def stress_pid(
    control_token: str | None = Header(
        default=None,
        alias="X-MCP-Stress-Control",
    ),
) -> dict[str, Any]:
    _authorize_control_request(control_token)
    return {
        "pid": os.getpid(),
        "fault_point": _FAULT_POINT or None,
        "fault_action": _FAULT_ACTION,
    }


@app.post("/__stress/reconcile", include_in_schema=False)
async def stress_reconcile(
    idle_seconds: int = Query(default=0, ge=0),
    control_token: str | None = Header(
        default=None,
        alias="X-MCP-Stress-Control",
    ),
) -> dict[str, Any]:
    _authorize_control_request(control_token)
    # Governed remote dispatch is reconciled first: it terminalizes its own
    # crash-orphaned attempts and repairs their idempotency records, so the
    # generic sweep below must not see those records as unexplained.
    dispatch = await get_mcp_dispatch_reconciliation_service().reconcile(
        idle_seconds=idle_seconds
    )
    repaired, needs_review = await get_idempotency_service().reconcile_stuck_records(
        idle_seconds=idle_seconds
    )
    permits_corrected = await get_permit_service().reconcile_budgets(
        idle_seconds=idle_seconds
    )
    return {
        "idempotency_repaired": repaired,
        "idempotency_needs_review": needs_review,
        "permit_budgets_corrected": permits_corrected,
        "dispatch_prepared_finalized": dispatch.prepared_finalized,
        "dispatch_uncertain": dispatch.dispatched_uncertain,
        "dispatch_dispatched_uncertain": dispatch.dispatched_uncertain,
        "dispatch_terminal_recovered": dispatch.terminal_recovered,
        "dispatch_idempotency_recovered": dispatch.idempotency_recovered,
        "dispatch_failed_attempt_ids": list(dispatch.failed_attempt_ids),
        "dispatch_failed_attempts": len(dispatch.failed_attempt_ids),
    }


if os.environ.get("MCP_STRESS_ACTION_MODE") == "1":
    from app.routers.permits import action_router
    from app.services.action_permits import (
        ActionToolBinding,
        upstream_action_binding_hash,
    )

    app.include_router(action_router)

    @app.middleware("http")
    async def bind_action_fixture(request, call_next):
        registry = get_service_registry()
        service_id = "remote-stress-governed-tool"
        record = await registry.get(service_id)
        if record and registry.get_action_binding(record) is None:
            schema = record["input_schema"]
            binding = ActionToolBinding(
                "disposable-action-proof-v1",
                service_id,
                upstream_action_binding_hash(
                    deployment_authority="disposable-action-proof-v1",
                    public_tool_id=service_id,
                    upstream_origin=record["upstream_origin"],
                    upstream_tool_name=record["upstream_tool_name"],
                    schema_id="partner-write",
                    schema_version="1",
                    input_schema=schema,
                ),
                "partner-write",
                "1",
                schema,
            )
            registry.register_upstream(
                service_id=service_id,
                name=record["name"],
                description=record["description"],
                category=ServiceCategory.AGENT_COMMS,
                executor=registry.get_executor(service_id),
                input_schema=schema,
                output_schema=record["output_schema"],
                credits_per_unit=2,
                upstream_tool_name=record["upstream_tool_name"],
                upstream_origin=record["upstream_origin"],
                action_binding=binding,
            )
        return await call_next(request)
