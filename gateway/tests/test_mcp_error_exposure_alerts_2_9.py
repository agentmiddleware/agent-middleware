"""JSON-RPC /mcp/messages must not echo exception text (alerts 2-9).

Each tools/call except branch, plus ``_jsonrpc_error_response``, used to put
``str(exc)`` on ``error.message``. Clients now get a canned public message and
a ``request_id``; the sentinel exception text is logged with that same id.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import mcp as mcp_router
from app.services.billing_engine import LedgerWriteContendedError
from app.services.idempotency import InvalidIdempotencyKeyError
from app.trust.adapters import GovernedRequestInvalid
from tests.test_trust_helpers import provision_agent_wallet

LOGGER = "app.routers.mcp"
REQUEST_ID = "alert29-req-1"
SENTINEL = "ALERT29_SENTINEL_stack_trace_leak"
GENERATED_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
MCP_CALL = {
    "jsonrpc": "2.0",
    "id": "alert-29",
    "method": "tools/call",
    "params": {
        "name": "exposure-probe",
        "arguments": {},
        "mcpContext": {"wallet_id": "unused", "idempotency_key": "alert-29-key"},
    },
}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _patch_handle(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    async def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(mcp_router, "_handle_tools_call", explode)


def _patch_validate(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(mcp_router, "validate_tools_call_params", explode)


def _patch_resolve_key(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(mcp_router, "resolve_client_idempotency_key", explode)


BRANCHES: list[tuple[str, Callable[[pytest.MonkeyPatch], None]]] = [
    (
        "jsonrpc_error_response_invalid_key",
        lambda monkeypatch: _patch_resolve_key(
            monkeypatch,
            InvalidIdempotencyKeyError(
                "idempotency_key_blank", SENTINEL, source="test"
            ),
        ),
    ),
    (
        "jsonrpc_error_response_governed_invalid",
        lambda monkeypatch: _patch_validate(
            monkeypatch, GovernedRequestInvalid(SENTINEL)
        ),
    ),
    (
        "human_approval_pending",
        lambda monkeypatch: _patch_handle(
            monkeypatch,
            mcp_router.HumanApprovalPendingSignal(
                SENTINEL, data={"approval_id": "apr-1"}
            ),
        ),
    ),
    (
        "retryable_contention",
        lambda monkeypatch: _patch_handle(
            monkeypatch, LedgerWriteContendedError(SENTINEL)
        ),
    ),
    (
        "tool_permission_denied",
        lambda monkeypatch: _patch_handle(
            monkeypatch,
            mcp_router.ToolPermissionDenied(
                SENTINEL, receipt={"receipt_id": "rcpt-1"}, details={"limit": 1}
            ),
        ),
    ),
    (
        "governed_tool_error",
        lambda monkeypatch: _patch_handle(
            monkeypatch,
            mcp_router.GovernedToolError(
                SENTINEL, extra_data={"dispatch": {"status": "failed"}}
            ),
        ),
    ),
    (
        "permission_error",
        lambda monkeypatch: _patch_handle(monkeypatch, PermissionError(SENTINEL)),
    ),
    (
        "tool_execution_error",
        lambda monkeypatch: _patch_handle(
            monkeypatch, mcp_router.ToolExecutionError(SENTINEL)
        ),
    ),
    (
        "classified_value_error",
        lambda monkeypatch: _patch_handle(
            monkeypatch, ValueError(f"Tool not found: {SENTINEL}")
        ),
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("branch", "patch"), BRANCHES, ids=[row[0] for row in BRANCHES])
async def test_tools_call_except_branch_hides_exception_text(
    client, clean_database, monkeypatch, caplog, branch, patch
):
    provisioned = await provision_agent_wallet(client)
    patch(monkeypatch)

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={**provisioned["agent_headers"], "X-Request-ID": REQUEST_ID},
        )

    assert resp.status_code == 200, resp.text
    assert SENTINEL not in resp.text
    payload = resp.json()
    assert "error" in payload, payload
    error = payload["error"]
    assert error["data"]["request_id"] == REQUEST_ID
    assert SENTINEL not in error["message"]
    records = [r for r in caplog.records if r.name == LOGGER]
    assert records, caplog.text
    matched = [
        r
        for r in records
        if REQUEST_ID in r.getMessage() and SENTINEL in r.getMessage()
    ]
    assert matched, f"{branch}: {caplog.text}"


@pytest.mark.anyio
async def test_malformed_request_id_header_is_replaced_with_generated_hex(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    _patch_handle(monkeypatch, mcp_router.ToolExecutionError(SENTINEL))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": "not a valid id / has spaces",
            },
        )

    assert resp.status_code == 200, resp.text
    assert SENTINEL not in resp.text
    request_id = resp.json()["error"]["data"]["request_id"]
    assert GENERATED_REQUEST_ID.fullmatch(request_id)
    assert "not a valid id" not in resp.text
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and request_id in r.getMessage()
        and SENTINEL in r.getMessage()
    ]
    assert matched, caplog.text
