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


def _patch_adapter_invoke(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    async def explode(_request: Any) -> Any:
        raise exc

    monkeypatch.setattr(mcp_router._mcp_adapter, "invoke", explode)


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
@pytest.mark.parametrize(
    ("branch", "patch"), BRANCHES, ids=[row[0] for row in BRANCHES]
)
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


# Fixed-value regression coverage for the REST invoke route and the governed
# error path: fixed X-Request-ID headers, fixed marker strings, no randomness.
REQUEST_ID_242_A = "req-fixed-242-0001"
REQUEST_ID_242_B = "req-fixed-242-0002"
REQUEST_ID_242_C = "req-fixed-242-0003"
REQUEST_ID_242_D = "req-fixed-242-0004"
REQUEST_ID_242_E = "req-fixed-242-0005"
REQUEST_ID_242_F = "req-fixed-242-0006"
REQUEST_ID_242_G = "req-fixed-242-0007"
REQUEST_ID_242_H = "req-fixed-242-0008"
SENTINEL_242 = "REQ242_FIXED_SENTINEL_MUST_NOT_LEAK"
SENTINEL_242_B = "REQ242_FIXED_SENTINEL_B_MUST_NOT_LEAK"
TOOL_242 = "exposure-probe-242"


def _rest_body(wallet_id: str, idempotency_key: str) -> dict[str, Any]:
    return {
        "name": TOOL_242,
        "arguments": {},
        "mcp_context": {
            "wallet_id": wallet_id,
            "idempotency_key": idempotency_key,
        },
    }


@pytest.mark.anyio
async def test_rest_invoke_permission_denied_hides_exception_text(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    _patch_adapter_invoke(
        monkeypatch,
        mcp_router.ToolPermissionDenied(
            SENTINEL_242,
            receipt={"receipt_id": "rcpt-fixed-242-0001"},
            details={"reason_code": "fixed-242-probe"},
        ),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            f"/mcp/tools/{TOOL_242}/invoke",
            json=_rest_body(provisioned["agent_wallet_id"], "req-242-fixed-key-0001"),
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": REQUEST_ID_242_A,
            },
        )

    assert resp.status_code == 403, resp.text
    assert SENTINEL_242 not in resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "Permission denied"
    assert detail["request_id"] == REQUEST_ID_242_A
    assert detail["receipt"]["receipt_id"] == "rcpt-fixed-242-0001"
    assert detail["details"] == {"reason_code": "fixed-242-probe"}
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and REQUEST_ID_242_A in r.getMessage()
        and SENTINEL_242 in r.getMessage()
    ]
    assert matched, caplog.text
    assert matched[0].exc_info is not None


@pytest.mark.anyio
async def test_rest_invoke_tool_failure_hides_exception_text(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    _patch_adapter_invoke(monkeypatch, mcp_router.ToolExecutionError(SENTINEL_242_B))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            f"/mcp/tools/{TOOL_242}/invoke",
            json=_rest_body(provisioned["agent_wallet_id"], "req-242-fixed-key-0002"),
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": REQUEST_ID_242_B,
            },
        )

    assert resp.status_code == 200, resp.text
    assert SENTINEL_242_B not in resp.text
    body = resp.json()
    assert body["isError"] is True
    assert body["content"] == [{"type": "text", "text": "Error: tool_execution_failed"}]
    assert body["structuredContent"]["error"] == "tool_execution_failed"
    assert body["structuredContent"]["request_id"] == REQUEST_ID_242_B
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and REQUEST_ID_242_B in r.getMessage()
        and SENTINEL_242_B in r.getMessage()
    ]
    assert matched, caplog.text
    assert matched[0].exc_info is not None


@pytest.mark.anyio
async def test_messages_governed_tool_error_hides_exception_text_fixed_id(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    _patch_handle(monkeypatch, mcp_router.GovernedToolError(SENTINEL_242))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": REQUEST_ID_242_C,
            },
        )

    assert resp.status_code == 200, resp.text
    assert SENTINEL_242 not in resp.text
    error = resp.json()["error"]
    assert error["message"] == "internal_error"
    assert error["data"]["request_id"] == REQUEST_ID_242_C
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and REQUEST_ID_242_C in r.getMessage()
        and SENTINEL_242 in r.getMessage()
    ]
    assert matched, caplog.text
    assert matched[0].exc_info is not None


# Fixed-value coverage for the refusal-code sanitizer: a wrapped tool failure
# is classified by type before any text matching, a refusal suffix is stripped
# to the bare code unless allowlisted on the denial path, and the fixed
# quote/refund denial reasons pass through unchanged.


@pytest.mark.anyio
async def test_messages_tool_execution_error_mimicking_refusal_is_classified(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    _patch_handle(
        monkeypatch,
        mcp_router.ToolExecutionError(f"wallet_frozen: {SENTINEL_242}"),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": REQUEST_ID_242_D,
            },
        )

    assert resp.status_code == 200, resp.text
    assert SENTINEL_242 not in resp.text
    error = resp.json()["error"]
    assert error["message"] == "tool_execution_failed"
    assert error["data"]["request_id"] == REQUEST_ID_242_D
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and REQUEST_ID_242_D in r.getMessage()
        and SENTINEL_242 in r.getMessage()
    ]
    assert matched, caplog.text
    assert matched[0].exc_info is not None


@pytest.mark.anyio
async def test_messages_refusal_suffix_is_stripped_to_bare_code(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    _patch_handle(
        monkeypatch,
        mcp_router.ToolPermissionDenied(f"wallet_frozen: {SENTINEL_242}"),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": REQUEST_ID_242_E,
            },
        )

    assert resp.status_code == 200, resp.text
    assert SENTINEL_242 not in resp.text
    error = resp.json()["error"]
    assert error["message"] == "wallet_frozen"
    assert error["data"]["request_id"] == REQUEST_ID_242_E
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and REQUEST_ID_242_E in r.getMessage()
        and SENTINEL_242 in r.getMessage()
    ]
    assert matched, caplog.text


@pytest.mark.anyio
async def test_messages_allowlisted_forbidden_field_detail_passes_through(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    _patch_handle(
        monkeypatch,
        mcp_router.ToolPermissionDenied("permit_forbidden_field:secret_token"),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": REQUEST_ID_242_F,
            },
        )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["message"] == "permit_forbidden_field:secret_token"
    assert error["data"]["request_id"] == REQUEST_ID_242_F
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER and REQUEST_ID_242_F in r.getMessage()
    ]
    assert matched, caplog.text


@pytest.mark.anyio
async def test_messages_wrapped_tool_mimic_of_allowlisted_code_is_bare(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    _patch_handle(
        monkeypatch,
        mcp_router.GovernedToolError(f"permit_forbidden_field:{SENTINEL_242}"),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": REQUEST_ID_242_G,
            },
        )

    assert resp.status_code == 200, resp.text
    assert SENTINEL_242 not in resp.text
    error = resp.json()["error"]
    assert error["message"] == "permit_forbidden_field"
    assert error["data"]["request_id"] == REQUEST_ID_242_G
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and REQUEST_ID_242_G in r.getMessage()
        and SENTINEL_242 in r.getMessage()
    ]
    assert matched, caplog.text


def _permission_error(reason: str) -> PermissionError:
    return PermissionError(reason)


def _governed_error(reason: str) -> mcp_router.GovernedToolError:
    return mcp_router.GovernedToolError(reason)


FIXED_DENIAL_REASONS: list[tuple[Callable[[str], BaseException], str]] = [
    (_permission_error, "quote_expired"),
    (_permission_error, "quote_invalid"),
    (_permission_error, "quote_not_found"),
    (_permission_error, "quote_wallet_mismatch"),
    (_permission_error, "quote_tool_mismatch"),
    (_governed_error, "refund_reconciliation_resolution_invalid"),
]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("make_exc", "reason"),
    FIXED_DENIAL_REASONS,
    ids=[row[1] for row in FIXED_DENIAL_REASONS],
)
async def test_messages_fixed_denial_reasons_are_preserved(
    client, clean_database, monkeypatch, caplog, make_exc, reason
):
    provisioned = await provision_agent_wallet(client)
    _patch_handle(monkeypatch, make_exc(reason))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": REQUEST_ID_242_H,
            },
        )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["message"] == reason
    assert error["data"]["request_id"] == REQUEST_ID_242_H
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and REQUEST_ID_242_H in r.getMessage()
        and reason in r.getMessage()
    ]
    assert matched, caplog.text
