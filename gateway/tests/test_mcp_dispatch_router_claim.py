"""Focused router tests for the exclusive upstream dispatch boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.routers import mcp as mcp_router
from app.services.mcp_dispatch_attempts import DispatchClaimUnavailableError
from app.services.upstream_mcp import UpstreamMcpPreDispatchError


class _StopAfterSend(RuntimeError):
    pass


class _RefundPathReached(RuntimeError):
    pass


async def _execute(
    *,
    executor: Any,
    dispatch_service: Any,
) -> dict[str, Any]:
    updated_at = datetime(2026, 8, 31, tzinfo=timezone.utc)
    return await mcp_router._execute_upstream_after_charge(
        executor=executor,
        dispatch_service=dispatch_service,
        dispatch_attempt=SimpleNamespace(
            attempt_id="attempt-1",
            updated_at=updated_at,
        ),
        decision=object(),
        money=object(),
        idem=object(),
        permit_model=object(),
        wallet_id="wallet-1",
        key_id="key-1",
        endpoint="/mcp/messages",
        idempotency_endpoint="/mcp/governed-invocations",
        transport="jsonrpc",
        idempotency_key="idem-1",
        idempotency_record_id="record-1",
        tool_name="partner.write",
        request_payload={"method": "tools/call"},
        arguments={"message": "hello"},
        registered_cost=Decimal("2"),
        credits_charged=Decimal("2"),
        ledger_entry_id="ledger-1",
        description="MCP jsonrpc invoke partner.write",
        policy_metadata={},
        approval_check=None,
    )


@pytest.mark.anyio
async def test_claim_is_durable_before_the_upstream_send() -> None:
    events: list[str] = []

    class DispatchService:
        async def claim_dispatch(self, attempt_id: str) -> None:
            assert attempt_id == "attempt-1"
            events.append("claim")

    class Executor:
        async def call_tool(
            self,
            _arguments: dict[str, Any],
            *,
            invocation_id: str,
            idempotency_key: str,
            before_dispatch: Callable[[], Awaitable[None]],
        ) -> None:
            assert invocation_id == "record-1"
            assert idempotency_key == "idem-1"
            await before_dispatch()
            events.append("send")
            raise _StopAfterSend

    with pytest.raises(_StopAfterSend):
        await _execute(executor=Executor(), dispatch_service=DispatchService())

    assert events == ["claim", "send"]


@pytest.mark.anyio
async def test_losing_claim_preserves_contract_without_compensation() -> None:
    class DispatchService:
        async def claim_dispatch(self, _attempt_id: str) -> None:
            raise DispatchClaimUnavailableError("dispatch_claim_unavailable")

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"losing claim called compensation method: {name}")

    class Executor:
        sent = False

        async def call_tool(
            self,
            _arguments: dict[str, Any],
            *,
            invocation_id: str,
            idempotency_key: str,
            before_dispatch: Callable[[], Awaitable[None]],
        ) -> None:
            del invocation_id, idempotency_key
            await before_dispatch()
            self.sent = True

    executor = Executor()
    with pytest.raises(
        mcp_router.IdempotencyInProgressError,
        match="^idempotency_in_progress$",
    ):
        await _execute(executor=executor, dispatch_service=DispatchService())

    assert executor.sent is False


@pytest.mark.anyio
async def test_pre_dispatch_failure_uses_guarded_completion_before_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed: dict[str, Any] = {}
    terminal = SimpleNamespace(attempt_id="attempt-1")

    class DispatchService:
        async def claim_dispatch(self, _attempt_id: str) -> None:
            raise AssertionError("connection failure must happen before claim")

        async def complete_pre_dispatch_failure(self, **kwargs: Any) -> Any:
            completed.update(kwargs)
            return terminal

        async def complete(self, **_kwargs: Any) -> None:
            raise AssertionError("unguarded terminal completion was used")

    class Executor:
        async def call_tool(self, *_args: Any, **_kwargs: Any) -> None:
            raise UpstreamMcpPreDispatchError("upstream_connection_failed")

    async def observe_refund(**kwargs: Any) -> None:
        assert kwargs["dispatch_attempt"] is terminal
        raise _RefundPathReached

    monkeypatch.setattr(
        mcp_router,
        "_raise_refunded_upstream_failure",
        observe_refund,
    )

    with pytest.raises(_RefundPathReached):
        await _execute(executor=Executor(), dispatch_service=DispatchService())

    assert completed == {
        "attempt_id": "attempt-1",
        "expected_updated_at": datetime(2026, 8, 31, tzinfo=timezone.utc),
        "ledger_entry_id": "ledger-1",
        "credits_charged": Decimal("2"),
        "result_payload": {
            "error": "failed_refunded",
            "error_code": "upstream_connection_failed",
        },
        "error_code": "upstream_connection_failed",
        "max_result_bytes": mcp_router.settings.MCP_UPSTREAM_MAX_RESPONSE_BYTES,
    }


@pytest.mark.anyio
async def test_pre_dispatch_cas_loser_does_not_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DispatchService:
        async def complete_pre_dispatch_failure(self, **_kwargs: Any) -> None:
            raise DispatchClaimUnavailableError("dispatch_claim_unavailable")

    class Executor:
        async def call_tool(self, *_args: Any, **_kwargs: Any) -> None:
            raise UpstreamMcpPreDispatchError("upstream_connection_failed")

    async def unexpected_refund(**_kwargs: Any) -> None:
        raise AssertionError("CAS loser entered refund path")

    monkeypatch.setattr(
        mcp_router,
        "_raise_refunded_upstream_failure",
        unexpected_refund,
    )

    with pytest.raises(
        mcp_router.IdempotencyInProgressError,
        match="^idempotency_in_progress$",
    ):
        await _execute(executor=Executor(), dispatch_service=DispatchService())
