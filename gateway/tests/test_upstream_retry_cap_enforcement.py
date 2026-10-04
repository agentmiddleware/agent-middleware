"""Tests for upstream retry cap enforcement and duplicate detection.

Proves that per-tool call caps work on remote tools, concurrent retries are
handled atomically, duplicate detection works in observe and enforce modes,
and call slots are released correctly.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.config import get_settings
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import (
    IdempotencyRecordModel,
    McpDispatchAttemptModel,
    PermitModel,
)
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.idempotency import (
    GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    get_idempotency_service,
)
from app.services.mcp_dispatch_attempts import (
    McpDispatchAttemptService,
    get_mcp_dispatch_attempt_service,
)
from app.services.permits import get_permit_service
from app.services.service_registry import get_service_registry
from app.services.upstream_mcp import (
    UpstreamMcpDeliveryUncertainError,
    UpstreamMcpPreDispatchError,
    UpstreamMcpResult,
    UpstreamMcpReturnedError,
)
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _upstream_result(payload: dict[str, Any]) -> UpstreamMcpResult:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return UpstreamMcpResult(
        payload=payload,
        canonical_json=canonical,
        response_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        size_bytes=len(canonical.encode()),
        is_error=bool(payload.get("isError")),
    )


@dataclass
class FakeUpstreamExecutor:
    mode: str = "success"
    calls: list[dict[str, Any]] = field(default_factory=list)
    dispatch_count: int = 0
    result_payload: dict[str, Any] | None = None

    async def call_tool(
        self,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
        idempotency_key: str,
        before_dispatch: Callable[[], Awaitable[None]],
    ) -> UpstreamMcpResult:
        self.calls.append(
            {
                "arguments": arguments,
                "invocation_id": invocation_id,
                "idempotency_key": idempotency_key,
            }
        )

        if self.mode == "pre_dispatch_failure":
            raise UpstreamMcpPreDispatchError("upstream_connection_failed")

        await before_dispatch()
        self.dispatch_count += 1

        if self.mode == "returned_error":
            raise UpstreamMcpReturnedError(
                _upstream_result(
                    self.result_payload
                    if self.result_payload is not None
                    else {
                        "content": [{"type": "text", "text": "error"}],
                        "isError": True,
                    }
                )
            )
        if self.mode == "delivery_uncertain":
            raise UpstreamMcpDeliveryUncertainError()

        return _upstream_result(
            self.result_payload
            if self.result_payload is not None
            else {"content": [{"type": "text", "text": "success"}], "isError": False}
        )


def _register_upstream(
    tool_name: str,
    executor: FakeUpstreamExecutor,
    *,
    credits_per_call: float = 2.0,
    category: ServiceCategory = ServiceCategory.AGENT_COMMS,
) -> None:
    get_service_registry().register_upstream(
        service_id=tool_name,
        name="Test Upstream Tool",
        description="Test upstream tool for retry cap enforcement",
        category=category,
        executor=executor,
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
        },
        output_schema={"type": "object"},
        credits_per_unit=credits_per_call,
        upstream_tool_name=tool_name,
        upstream_origin="https://test.example",
    )


def _call_body(
    *,
    tool_name: str,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
    message: str = "test",
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"message": message},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idempotency_key,
            },
        },
    }


async def _create_permit_with_cap(
    client: AsyncClient,
    wallet_id: str,
    key_id: str,
    tool_name: str,
    max_calls: int,
    allow_repeats: bool = False,
) -> str:
    """Create a permit with max_calls_per_tool set."""
    permit_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": key_id,
            "allowed_tools": [tool_name],
            "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
            "max_credits": 100,
            "max_calls_per_tool": {tool_name: max_calls},
            "allow_identical_repeats": allow_repeats,
            "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
        },
        headers={
            **BOOTSTRAP_HEADERS,
            "Idempotency-Key": f"permit-cap-{tool_name}-{max_calls}",
        },
    )
    assert permit_resp.status_code == 201
    return permit_resp.json()["permit_id"]


@pytest.mark.anyio
async def test_remote_cap_1_success_then_new_key_refused(client, clean_database):
    """Remote tool with cap=1: success → new key → permit_max_calls_exceeded.

    This is the primary fix: per-tool call caps now work on remote tools.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.cap.one"

    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)

    try:
        permit_id = await _create_permit_with_cap(
            client, wallet_id, key_id, tool_name, max_calls=1
        )

        # First call succeeds
        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="key-1",
                message="hello",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        body1 = r1.json()
        assert "result" in body1
        assert body1["result"]["receipt"]["outcome"] == "success"
        assert executor.dispatch_count == 1

        # Second call with different key is refused
        r2 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="key-2",
                message="hello",
            ),
            headers=agent_headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert "error" in body2
        assert body2["error"]["code"] == -32003
        assert body2["error"]["message"] == "permit_max_calls_exceeded"
        assert executor.dispatch_count == 1  # Still 1, no second dispatch
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_remote_cap_1_delivery_uncertain_then_refused(client, clean_database):
    """Remote tool with cap=1: delivery_uncertain → new key → refused.

    Delivery uncertain calls hold their slot to prevent duplicate effects.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.cap.uncertain"

    executor = FakeUpstreamExecutor("delivery_uncertain")
    _register_upstream(tool_name, executor)

    try:
        permit_id = await _create_permit_with_cap(
            client, wallet_id, key_id, tool_name, max_calls=1
        )

        # First call ends delivery_uncertain
        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="uncertain-1",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        body1 = r1.json()
        assert "error" in body1
        assert body1["error"]["message"] == "delivery_uncertain"
        assert body1["error"]["data"]["receipt"]["outcome"] == "delivery_uncertain"
        assert executor.dispatch_count == 1

        # Second call is refused (slot still held by uncertain call)
        r2 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="uncertain-2",
            ),
            headers=agent_headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert "error" in body2
        assert body2["error"]["message"] == "permit_max_calls_exceeded"
        assert executor.dispatch_count == 1  # No second dispatch
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_remote_different_tool_caps_independent(client, clean_database):
    """A cap on tool A doesn't block remote tool B.

    Fixes the regression where max_calls_per_tool_json is not None blocked
    all remote tools, not just the capped ones.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_a = "test.cap.a"
    tool_b = "test.cap.b"

    executor_a = FakeUpstreamExecutor("success")
    executor_b = FakeUpstreamExecutor("success")
    _register_upstream(tool_a, executor_a)
    _register_upstream(tool_b, executor_b)

    try:
        # Permit with cap on tool A only
        permit_resp = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": key_id,
                "allowed_tools": [tool_a, tool_b],
                "scopes": [
                    f"tool:{tool_a}:invoke",
                    f"tool:{tool_b}:invoke",
                    "billing:charge",
                ],
                "max_credits": 100,
                "max_calls_per_tool": {tool_a: 1},
                "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
            },
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "permit-mixed-caps"},
        )
        assert permit_resp.status_code == 201
        permit_id = permit_resp.json()["permit_id"]

        # Call tool A once (uses up its cap)
        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_a,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="tool-a-1",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        assert executor_a.dispatch_count == 1

        # Call tool B (should succeed, not blocked by tool A's cap)
        r2 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_b,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="tool-b-1",
            ),
            headers=agent_headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["result"]["receipt"]["outcome"] == "success"
        assert executor_b.dispatch_count == 1
    finally:
        get_service_registry().unregister_local(tool_a)
        get_service_registry().unregister_local(tool_b)


@pytest.mark.anyio
async def test_duplicate_detection_enforce_mode(client, clean_database, monkeypatch):
    """Cross-key duplicate detection in enforce mode refuses duplicates.

    Identical request hash under different key is detected and refused.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "MCP_UPSTREAM_DUPLICATE_GUARD", "enforce")

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.dup.enforce"

    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)

    try:
        # Permit WITHOUT cap (testing pure duplicate detection)
        permit_resp = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": key_id,
                "allowed_tools": [tool_name],
                "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                "max_credits": 100,
                "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
            },
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "permit-dup-test"},
        )
        assert permit_resp.status_code == 201
        permit_id = permit_resp.json()["permit_id"]

        # First call with identical arguments
        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="dup-key-1",
                message="duplicate-me",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        assert executor.dispatch_count == 1

        # Second call with SAME arguments but DIFFERENT key
        r2 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="dup-key-2",
                message="duplicate-me",
            ),
            headers=agent_headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert "error" in body2
        assert body2["error"]["message"] == "duplicate_request_new_key"
        assert executor.dispatch_count == 1  # No second dispatch

        # The refusal is a denial receipt, so the durable count on the admin
        # observability endpoint reflects it without depending on this
        # process's in-memory counter.
        metrics = await client.get("/health/duplicate-guard", headers=BOOTSTRAP_HEADERS)
        assert metrics.status_code == 200
        metrics_body = metrics.json()
        assert metrics_body["mode"] == "enforce"
        assert metrics_body["enforce_mode_denials_durable"] == 1
        assert metrics_body["enforce_mode_blocks"] >= 1
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_duplicate_detection_log_mode_allows(client, clean_database, monkeypatch):
    """Cross-key duplicate detection in log mode allows duplicates (observe only)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "MCP_UPSTREAM_DUPLICATE_GUARD", "log")

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.dup.log"

    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)

    try:
        permit_resp = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": key_id,
                "allowed_tools": [tool_name],
                "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                "max_credits": 100,
                "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
            },
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "permit-log-mode"},
        )
        assert permit_resp.status_code == 201
        permit_id = permit_resp.json()["permit_id"]

        # First call
        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="log-key-1",
                message="log-me",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        assert executor.dispatch_count == 1

        # Duplicate with different key should still succeed in log mode
        r2 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="log-key-2",
                message="log-me",
            ),
            headers=agent_headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["result"]["receipt"]["outcome"] == "success"
        assert executor.dispatch_count == 2  # Both dispatched in log mode
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_duplicate_detection_opt_out(client, clean_database, monkeypatch):
    """Permit with allow_identical_repeats=true bypasses duplicate detection."""
    settings = get_settings()
    monkeypatch.setattr(settings, "MCP_UPSTREAM_DUPLICATE_GUARD", "enforce")

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.dup.optout"

    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)

    try:
        # Permit with opt-out enabled
        permit_resp = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": key_id,
                "allowed_tools": [tool_name],
                "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                "max_credits": 100,
                "allow_identical_repeats": True,
                "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
            },
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "permit-opt-out"},
        )
        assert permit_resp.status_code == 201
        permit_id = permit_resp.json()["permit_id"]

        # First call
        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="opt-out-1",
                message="repeat-me",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        assert executor.dispatch_count == 1

        # Duplicate should succeed because opt-out is enabled
        r2 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="opt-out-2",
                message="repeat-me",
            ),
            headers=agent_headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["result"]["receipt"]["outcome"] == "success"
        assert executor.dispatch_count == 2  # Both dispatched with opt-out
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_same_key_replay_unchanged(client, clean_database):
    """Same-key replay returns original receipt with no second call or charge.

    This behavior must not regress.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.replay.same"

    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)

    try:
        permit_id = await _create_permit_with_cap(
            client, wallet_id, key_id, tool_name, max_calls=1
        )

        # First call
        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="same-key",
                message="once",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        body1 = r1.json()
        receipt_id_1 = body1["result"]["receipt"]["receipt_id"]
        assert executor.dispatch_count == 1

        # Replay with same key
        r2 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="same-key",
                message="once",
            ),
            headers=agent_headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        receipt_id_2 = body2["result"]["receipt"]["receipt_id"]

        # Same receipt, no second call
        assert receipt_id_2 == receipt_id_1
        assert executor.dispatch_count == 1  # Still 1, not 2
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_aggregate_value_cap_still_unsupported(client, clean_database):
    """Permit with aggregate_value_cap is still refused on upstream tools.

    This constraint requires folding in-flight reservations and remains
    unsupported until that's implemented.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.agg.unsupported"

    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)

    try:
        # Permit with aggregate_value_cap
        permit_resp = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": key_id,
                "allowed_tools": [tool_name],
                "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                "max_credits": 100,
                "aggregate_value_cap": 50,
                "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
            },
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "permit-agg-cap"},
        )
        assert permit_resp.status_code == 201
        permit_id = permit_resp.json()["permit_id"]

        # Call is refused
        r = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="agg-1",
            ),
            headers=agent_headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert "error" in body
        assert body["error"]["message"] == "permit_constraint_unsupported_for_upstream"
        assert "aggregate_value_cap" in str(body["error"]["data"]["details"])
        assert executor.dispatch_count == 0  # Never dispatched
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_duplicate_detection_checks_all_attempts_not_just_newest(
    client, clean_database, monkeypatch
):
    """Regression for Issue 3: the guard must scan every prior in the window.

    The original query took only the newest matching attempt (``LIMIT 1``)
    and then ignored it when it was a never-dispatched ``returned_error``, so
    an older succeeded attempt with the same request hash was never consulted.
    Here the newest prior is exactly such a non-effectful row, inserted after
    the effectful one, and the third call must still be refused because of
    the older success.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "MCP_UPSTREAM_DUPLICATE_GUARD", "enforce")

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.dup.all"

    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)

    try:
        permit_resp = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": key_id,
                "allowed_tools": [tool_name],
                "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                "max_credits": 100,
                "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
            },
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "permit-dup-all"},
        )
        assert permit_resp.status_code == 201
        permit_id = permit_resp.json()["permit_id"]

        # Call 1: dispatched and succeeded, so it is the effectful prior.
        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="dup-all-1",
                message="original",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        assert r1.json()["result"]["receipt"]["outcome"] == "success"
        assert executor.dispatch_count == 1

        factory = get_session_factory()
        async with factory() as session:
            effectful = (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        McpDispatchAttemptModel.permit_id == permit_id
                    )
                )
            ).scalar_one()
            assert effectful.state == "succeeded"
            assert effectful.dispatched_at is not None

            # Call 2: a NEWER pre-dispatch failure with the same request hash.
            # It never dispatched, so it must not block on its own. It is
            # inserted directly because a live call with this hash would be
            # refused by the very guard under test.
            later = effectful.created_at + timedelta(seconds=1)
            newer_record_id = f"idm-dup-all-{uuid.uuid4().hex[:12]}"
            session.add(
                IdempotencyRecordModel(
                    record_id=newer_record_id,
                    wallet_id=wallet_id,
                    endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                    idempotency_key="dup-all-2",
                    request_hash=effectful.request_hash,
                    operation_kind="upstream_mcp",
                )
            )
            await session.flush()
            newer = McpDispatchAttemptModel(
                attempt_id=f"att-dup-all-{uuid.uuid4().hex[:12]}",
                idempotency_record_id=newer_record_id,
                wallet_id=wallet_id,
                permit_id=permit_id,
                key_id=effectful.key_id,
                public_tool_id=tool_name,
                upstream_tool_name=effectful.upstream_tool_name,
                upstream_origin=effectful.upstream_origin,
                request_hash=effectful.request_hash,
                credits_authorized=effectful.credits_authorized,
                state="returned_error",
                error_code="upstream_connection_failed",
                dispatched_at=None,
                created_at=later,
                updated_at=later,
                completed_at=later,
            )
            session.add(newer)
            await session.commit()
        assert newer.created_at > effectful.created_at

        # Call 3: same arguments under a new key. LIMIT 1 would have seen only
        # the newer non-effectful row and let this dispatch again.
        r3 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="dup-all-3",
                message="original",
            ),
            headers=agent_headers,
        )
        assert r3.status_code == 200
        body3 = r3.json()
        assert "error" in body3, body3
        assert body3["error"]["message"] == "duplicate_request_new_key"
        details = body3["error"]["data"]["details"]
        assert details["prior_attempt_id"] == effectful.attempt_id
        assert details["prior_state"] == "succeeded"
        assert executor.dispatch_count == 1
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_remote_cap_1_post_dispatch_error_keeps_slot(client, clean_database):
    """An error returned after a real send keeps its slot on a cap-of-one permit.

    ``release_dispatch_budget_once`` refunds the credits of a refunded
    ``returned_error`` attempt but returns the call slot only when the attempt
    never dispatched. The upstream may have acted on this call, so the next key
    must be refused rather than dispatched again.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.cap.post.dispatch.error"

    executor = FakeUpstreamExecutor("returned_error")
    _register_upstream(tool_name, executor)

    try:
        permit_id = await _create_permit_with_cap(
            client, wallet_id, key_id, tool_name, max_calls=1
        )

        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="post-error-1",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        body1 = r1.json()
        assert "error" in body1, body1
        assert body1["error"]["message"] == "upstream_returned_error"
        assert executor.dispatch_count == 1
        counts, spent = await _permit_slot_state(permit_id)
        assert counts == {tool_name: 1}
        assert spent == Decimal("0")

        r2 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="post-error-2",
            ),
            headers=agent_headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert "error" in body2, body2
        assert body2["error"]["message"] == "permit_max_calls_exceeded"
        assert executor.dispatch_count == 1
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_remote_cap_1_pre_dispatch_failure_releases_slot(client, clean_database):
    """A pre-dispatch failure returns both the budget and the call slot.

    Exercises the live route: the executor fails before ``before_dispatch``,
    the attempt terminalises as a never-dispatched ``returned_error`` and
    ``release_dispatch_budget_once`` gives the slot back, so the next key on
    the same cap-of-one permit dispatches. The abandon path (lost quote or
    ledger contention) is covered by the service-level tests below.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.cap.abandon"

    # Use pre_dispatch_failure mode to trigger abandon path
    executor = FakeUpstreamExecutor("pre_dispatch_failure")
    _register_upstream(tool_name, executor)

    try:
        permit_id = await _create_permit_with_cap(
            client, wallet_id, key_id, tool_name, max_calls=1
        )

        # First call fails pre-dispatch (abandoned, slot released)
        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="abandon-1",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        body1 = r1.json()
        assert "error" in body1
        assert body1["error"]["message"] == "upstream_pre_dispatch_failed"
        assert body1["error"]["data"]["receipt"]["outcome"] == "failed_refunded"
        assert executor.dispatch_count == 0

        # Second call with different key should succeed (slot was released)
        executor.mode = "success"
        r2 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="abandon-2",
            ),
            headers=agent_headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert "result" in body2, "Second call should succeed after slot release"
        assert body2["result"]["receipt"]["outcome"] == "success"
        assert executor.dispatch_count == 1
    finally:
        get_service_registry().unregister_local(tool_name)


# ---------------------------------------------------------------------------
# Service-level slot lifecycle: abandon path, lost-CAS replay, lost-CAS reason
# ---------------------------------------------------------------------------


async def _reserve_capped_attempt(
    client: AsyncClient,
    *,
    suffix: str,
) -> tuple[McpDispatchAttemptService, McpDispatchAttemptModel, str, str]:
    """Reserve one prepared attempt holding the only slot of a cap=1 permit."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    tool_name = f"test.cap.reserved.{suffix}"
    permit_id = await _create_permit_with_cap(
        client, wallet_id, provisioned["key_id"], tool_name, max_calls=1
    )
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=wallet_id,
        endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        idempotency_key=f"reserved-{suffix}",
        request_payload={"tool": tool_name, "arguments": {"message": suffix}},
        operation_kind="upstream_mcp",
    )
    service = get_mcp_dispatch_attempt_service()
    validation, attempt = await service.authorize_reserve_and_prepare(
        idempotency_record_id=begun.record_id,
        wallet_id=wallet_id,
        permit_id=permit_id,
        key_id=provisioned["key_id"],
        public_tool_id=tool_name,
        upstream_tool_name=tool_name,
        upstream_origin="https://test.example",
        request_hash=begun.request_hash,
        credits_authorized=Decimal("2"),
        arguments={"message": suffix},
    )
    assert validation.allowed is True, validation.reason
    assert attempt is not None
    assert attempt.call_slot_reserved is True
    return service, attempt, permit_id, tool_name


async def _permit_slot_state(permit_id: str) -> tuple[dict[str, Any], Decimal]:
    """Return the permit's per-tool call counters and its reserved credits."""
    factory = get_session_factory()
    async with factory() as session:
        permit = await session.get(PermitModel, permit_id)
        assert permit is not None
        return json.loads(permit.tool_call_counts_json or "{}"), permit.spent_credits


def _inject_one_lost_counter_cas(
    monkeypatch: pytest.MonkeyPatch,
    *,
    budget_spent_on_refresh: bool = False,
) -> dict[str, int]:
    """Make the first permit UPDATE that carries the counter CAS predicate miss.

    Stands in for a concurrent writer between the read and the guarded UPDATE:
    the statement is not executed and reports ``rowcount`` 0, exactly what the
    database returns when the predicate no longer matches. Every later
    statement runs normally, so a replayed transaction succeeds and a
    non-replayed one surfaces the miss.

    With ``budget_spent_on_refresh`` the stand-in is a concurrent spend rather
    than a counter move: the permit row re-read after the miss shows its
    credits exhausted. The value is set as committed state on the loaded
    instance (no dirty attribute, nothing is flushed), which is what the
    classification sees when the row was changed by another transaction on an
    engine that does not honour the row lock.
    """
    misses = {"count": 0, "refreshes": 0}
    original_execute = AsyncSession.execute
    original_refresh = AsyncSession.refresh

    async def execute(
        self: AsyncSession, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        rendered = str(statement) if misses["count"] == 0 else ""
        if (
            rendered.startswith("UPDATE permits")
            and "tool_call_counts_json" in rendered
        ):
            misses["count"] += 1
            return SimpleNamespace(rowcount=0)
        return await original_execute(self, statement, *args, **kwargs)

    async def refresh(
        self: AsyncSession, instance: Any, *args: Any, **kwargs: Any
    ) -> Any:
        result = await original_refresh(self, instance, *args, **kwargs)
        if (
            budget_spent_on_refresh
            and misses["count"] == 1
            and misses["refreshes"] == 0
            and isinstance(instance, PermitModel)
        ):
            misses["refreshes"] += 1
            set_committed_value(instance, "spent_credits", instance.max_credits)
        return result

    monkeypatch.setattr(AsyncSession, "execute", execute)
    monkeypatch.setattr(AsyncSession, "refresh", refresh)
    return misses


@pytest.mark.anyio
async def test_abandon_effect_free_prepared_attempt_releases_call_slot(
    client, clean_database
):
    """Abandoning a prepared attempt returns its budget and its call slot.

    Regression for the slot leak: the lost-quote and ledger-contention
    cleanups abandon the prepared attempt, and only ``spent_credits`` used to
    come back, so a cap-of-one permit stayed exhausted although nothing was
    dispatched or charged.
    """
    service, attempt, permit_id, tool_name = await _reserve_capped_attempt(
        client, suffix="abandon-slot"
    )
    counts, spent = await _permit_slot_state(permit_id)
    assert counts == {tool_name: 1}
    assert spent == Decimal("2")

    await service.abandon_effect_free_prepared_attempt(
        attempt_id=attempt.attempt_id,
        expected_updated_at=attempt.updated_at,
    )

    counts, spent = await _permit_slot_state(permit_id)
    assert counts == {tool_name: 0}
    assert spent == Decimal("0")
    factory = get_session_factory()
    async with factory() as session:
        assert await session.get(McpDispatchAttemptModel, attempt.attempt_id) is None

    # The freed slot is usable again by a fresh reservation on the same permit.
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=attempt.wallet_id,
        endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        idempotency_key="reserved-abandon-slot-again",
        request_payload={"tool": tool_name, "arguments": {"message": "again"}},
        operation_kind="upstream_mcp",
    )
    validation, again = await service.authorize_reserve_and_prepare(
        idempotency_record_id=begun.record_id,
        wallet_id=attempt.wallet_id,
        permit_id=permit_id,
        key_id=attempt.key_id,
        public_tool_id=tool_name,
        upstream_tool_name=tool_name,
        upstream_origin="https://test.example",
        request_hash=begun.request_hash,
        credits_authorized=Decimal("2"),
        arguments={"message": "again"},
    )
    assert validation.allowed is True, validation.reason
    assert again is not None
    assert again.call_slot_reserved is True
    counts, spent = await _permit_slot_state(permit_id)
    assert counts == {tool_name: 1}
    assert spent == Decimal("2")


@pytest.mark.anyio
async def test_abandon_replays_a_lost_slot_cas(client, clean_database, monkeypatch):
    """A lost slot CAS replays the abandon instead of failing it.

    The CAS miss is raised inside the transaction as
    ``PermitWriteContendedError``. Without a ``restart_on`` predicate it
    escaped ``run_with_write_conflict_retry`` on the first attempt and the
    recovery read reported the abandon as commit-uncertain, leaving the
    reservation and the slot in place.
    """
    service, attempt, permit_id, tool_name = await _reserve_capped_attempt(
        client, suffix="abandon-cas"
    )
    misses = _inject_one_lost_counter_cas(monkeypatch)

    await service.abandon_effect_free_prepared_attempt(
        attempt_id=attempt.attempt_id,
        expected_updated_at=attempt.updated_at,
    )

    assert misses["count"] == 1
    counts, spent = await _permit_slot_state(permit_id)
    assert counts == {tool_name: 0}
    assert spent == Decimal("0")
    factory = get_session_factory()
    async with factory() as session:
        assert await session.get(McpDispatchAttemptModel, attempt.attempt_id) is None


@pytest.mark.anyio
async def test_pre_dispatch_release_replays_a_lost_slot_cas(
    client, clean_database, monkeypatch
):
    """A lost slot CAS replays ``release_dispatch_budget_once``.

    The permit read in that release is not row-locked, so a concurrent
    reservation can move ``tool_call_counts_json`` between the read and the
    guarded UPDATE. The miss must replay the whole transaction (it used to
    escape ``_run_with_write_retry`` on the first attempt), so the budget and
    the slot still come back, and still exactly once.
    """
    service, attempt, permit_id, tool_name = await _reserve_capped_attempt(
        client, suffix="release-cas"
    )
    terminal = await service.complete_pre_dispatch_failure(
        attempt_id=attempt.attempt_id,
        expected_updated_at=attempt.updated_at,
        result_payload={
            "error": "failed_refunded",
            "error_code": "upstream_connection_failed",
        },
        error_code="upstream_connection_failed",
        max_result_bytes=get_settings().MCP_UPSTREAM_MAX_RESPONSE_BYTES,
    )
    assert terminal.state == "returned_error"
    assert terminal.dispatched_at is None
    counts, spent = await _permit_slot_state(permit_id)
    assert counts == {tool_name: 1}
    assert spent == Decimal("2")

    misses = _inject_one_lost_counter_cas(monkeypatch)
    permits = get_permit_service()
    assert await permits.release_dispatch_budget_once(attempt.attempt_id) is True

    assert misses["count"] == 1
    counts, spent = await _permit_slot_state(permit_id)
    assert counts == {tool_name: 0}
    assert spent == Decimal("0")
    # The guarded claim keeps the release once-only across replays too.
    assert await permits.release_dispatch_budget_once(attempt.attempt_id) is False


@pytest.mark.anyio
async def test_reserve_contention_is_retryable_and_frees_the_key(
    client, clean_database, monkeypatch
):
    """A lost reservation CAS with capacity remaining is retryable, not a verdict.

    The guarded reservation UPDATE carries the counter predicate. When it
    matches no row although the permit is active, in date, under its cap and
    within budget, the counter moved between the read and the write. That used
    to be completed against the idempotency key as a ``permit_write_contended``
    denial, so a same-key retry replayed the denial forever. It is now the
    retryable envelope (-32005) with nothing consumed and the in-progress
    record released, so the same key retried dispatches.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.cap.contended"

    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)

    try:
        permit_id = await _create_permit_with_cap(
            client, wallet_id, key_id, tool_name, max_calls=2
        )
        misses = _inject_one_lost_counter_cas(monkeypatch)
        body = _call_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key="contended-1",
        )

        r1 = await client.post("/mcp/messages", json=body, headers=agent_headers)
        assert r1.status_code == 200
        body1 = r1.json()
        assert "error" in body1, body1
        assert body1["error"]["code"] == -32005
        assert body1["error"]["message"] == "permit_write_contended"
        assert "data" not in body1["error"]
        assert misses["count"] == 1
        assert executor.dispatch_count == 0
        counts, spent = await _permit_slot_state(permit_id)
        assert counts == {}
        assert spent == Decimal("0")

        # The same key, retried once the contention is gone: the released
        # record lets the retry run instead of replaying a frozen denial.
        r2 = await client.post("/mcp/messages", json=body, headers=agent_headers)
        assert r2.status_code == 200
        body2 = r2.json()
        assert "result" in body2, body2
        assert body2["result"]["receipt"]["outcome"] == "success"
        assert executor.dispatch_count == 1
        counts, spent = await _permit_slot_state(permit_id)
        assert counts == {tool_name: 1}
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_reserve_contention_returns_409_on_rest_invoke(
    client, clean_database, monkeypatch
):
    """The REST invoke endpoint returns HTTP 409 for permit_write_contended.

    When a permit reservation's CAS loses to a concurrent writer, the JSON-RPC
    endpoint returns error code -32005. The REST /mcp/tools/{id}/invoke
    endpoint must return HTTP 409 for the same error, allowing the caller to
    retry the same idempotency key.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.cap.contended.rest"

    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)

    try:
        permit_id = await _create_permit_with_cap(
            client, wallet_id, key_id, tool_name, max_calls=2
        )
        misses = _inject_one_lost_counter_cas(monkeypatch)

        # REST endpoint: /mcp/tools/{id}/invoke
        invoke_body = {
            "name": tool_name,
            "arguments": {"message": "test"},
            "mcp_context": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": "rest-contended-1",
            },
        }

        r1 = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json=invoke_body,
            headers=agent_headers,
        )
        assert r1.status_code == 409, r1.json()
        body1 = r1.json()
        assert body1["detail"]["error"] == "permit_write_contended"
        assert misses["count"] == 1
        assert executor.dispatch_count == 0
        counts, spent = await _permit_slot_state(permit_id)
        assert counts == {}
        assert spent == Decimal("0")

        # Retry the same key once contention is gone
        r2 = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json=invoke_body,
            headers=agent_headers,
        )
        assert r2.status_code == 200, r2.json()
        body2 = r2.json()
        assert body2["isError"] is False
        assert body2["receipt"]["outcome"] == "success"
        assert executor.dispatch_count == 1
        counts, spent = await _permit_slot_state(permit_id)
        assert counts == {tool_name: 1}
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_reserve_reports_budget_not_contention_when_credits_ran_out(
    client, clean_database, monkeypatch
):
    """A lost reservation on an exhausted budget is a budget denial.

    With a cap configured the guarded UPDATE carries both the counter and the
    budget predicate. When it matches no row and the re-read permit shows the
    credits gone, the reason is ``permit_budget_exceeded`` with its details,
    never the retryable ``permit_write_contended`` the cap branch used to
    return first. Nothing is consumed by the denial.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "test.cap.budget.gone"

    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)

    try:
        permit_id = await _create_permit_with_cap(
            client, wallet_id, key_id, tool_name, max_calls=2
        )
        misses = _inject_one_lost_counter_cas(monkeypatch, budget_spent_on_refresh=True)

        r1 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="budget-gone-1",
            ),
            headers=agent_headers,
        )
        assert r1.status_code == 200
        body1 = r1.json()
        assert "error" in body1, body1
        assert body1["error"]["message"] == "permit_budget_exceeded"
        details = body1["error"]["data"]["details"]
        assert Decimal(details["required_credits"]) == Decimal("2")
        assert Decimal(details["remaining_credits"]) == Decimal("0")
        assert Decimal(details["spent_credits"]) == Decimal(details["max_credits"])
        assert misses == {"count": 1, "refreshes": 1}
        assert executor.dispatch_count == 0

        # The denial consumed nothing: the stored row is untouched and the
        # next key reserves and dispatches.
        counts, spent = await _permit_slot_state(permit_id)
        assert counts == {}
        assert spent == Decimal("0")
        r2 = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key="budget-gone-2",
            ),
            headers=agent_headers,
        )
        assert r2.status_code == 200
        assert r2.json()["result"]["receipt"]["outcome"] == "success"
        assert executor.dispatch_count == 1
    finally:
        get_service_registry().unregister_local(tool_name)
