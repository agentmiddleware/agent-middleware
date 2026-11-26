"""JSON-RPC /mcp/messages must not echo exception text (alerts 2-9).

Each tools/call except branch, plus ``_jsonrpc_error_response``, used to put
``str(exc)`` on ``error.message``. Clients now get a canned public message and
a ``request_id``; the sentinel exception text is logged with that same id.
"""

from __future__ import annotations

import logging
import re
import uuid
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


BRANCH_EXC_TYPE_NAMES: dict[str, str] = {
    "jsonrpc_error_response_invalid_key": "InvalidIdempotencyKeyError",
    "jsonrpc_error_response_governed_invalid": "GovernedRequestInvalid",
    "human_approval_pending": "HumanApprovalPendingSignal",
    "retryable_contention": "LedgerWriteContendedError",
    "tool_permission_denied": "ToolPermissionDenied",
    "governed_tool_error": "GovernedToolError",
    "permission_error": "PermissionError",
    "tool_execution_error": "ToolExecutionError",
    "classified_value_error": "ValueError",
}


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
    assert matched[0].exc_info is None
    assert BRANCH_EXC_TYPE_NAMES[branch] in matched[0].getMessage()
    assert "\n" not in matched[0].getMessage()
    assert "\r" not in matched[0].getMessage()


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
    assert matched[0].exc_info is None
    assert "ToolExecutionError" in matched[0].getMessage()
    assert "\n" not in matched[0].getMessage()
    assert "\r" not in matched[0].getMessage()


# Regression coverage for the REST invoke route and the governed error path.
# Request ids, marker strings, and idempotency keys are generated with uuid4
# at runtime; each test asserts against the values it generated.
TOOL_242 = "exposure-probe-242"


def _fresh_request_id() -> str:
    return uuid.uuid4().hex


def _fresh_sentinel(tag: str = "REQ242") -> str:
    return f"{tag}_{uuid.uuid4().hex}_MUST_NOT_LEAK"


def _fresh_idempotency_key(tag: str = "req-242") -> str:
    return f"{tag}-{uuid.uuid4().hex}"


def _assert_clean_error_log(record: logging.LogRecord, *, exc_type: str) -> str:
    """Shared log-record checks: no traceback, typed, single-line."""
    message = record.getMessage()
    assert record.exc_info is None
    assert exc_type in message
    assert "\n" not in message
    assert "\r" not in message
    return message


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
    request_id = _fresh_request_id()
    sentinel = _fresh_sentinel()
    receipt_id = f"rcpt-{uuid.uuid4().hex}"
    _patch_adapter_invoke(
        monkeypatch,
        mcp_router.ToolPermissionDenied(
            sentinel,
            receipt={"receipt_id": receipt_id},
            details={"reason_code": "fixed-242-probe"},
        ),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            f"/mcp/tools/{TOOL_242}/invoke",
            json=_rest_body(provisioned["agent_wallet_id"], _fresh_idempotency_key()),
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": request_id,
            },
        )

    assert resp.status_code == 403, resp.text
    assert sentinel not in resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "Permission denied"
    assert "request_id" not in detail
    assert resp.headers["X-Request-ID"] == request_id
    assert detail["receipt"]["receipt_id"] == receipt_id
    assert detail["details"] == {"reason_code": "fixed-242-probe"}
    # The invoke route logs only the exception class under the request id.
    matched = [
        r for r in caplog.records if r.name == LOGGER and request_id in r.getMessage()
    ]
    assert matched, caplog.text
    message = _assert_clean_error_log(matched[0], exc_type="ToolPermissionDenied")
    assert sentinel not in message


@pytest.mark.anyio
async def test_rest_invoke_tool_failure_hides_exception_text(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    request_id = _fresh_request_id()
    sentinel = _fresh_sentinel(tag="REQ242B")
    _patch_adapter_invoke(monkeypatch, mcp_router.ToolExecutionError(sentinel))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            f"/mcp/tools/{TOOL_242}/invoke",
            json=_rest_body(provisioned["agent_wallet_id"], _fresh_idempotency_key()),
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": request_id,
            },
        )

    assert resp.status_code == 200, resp.text
    assert sentinel not in resp.text
    body = resp.json()
    assert body["isError"] is True
    assert body["content"] == [{"type": "text", "text": "Error: tool_execution_failed"}]
    assert body["structuredContent"] == {"error": "tool_execution_failed"}
    assert resp.headers["X-Request-ID"] == request_id
    matched = [
        r for r in caplog.records if r.name == LOGGER and request_id in r.getMessage()
    ]
    assert matched, caplog.text
    message = _assert_clean_error_log(matched[0], exc_type="ToolExecutionError")
    assert sentinel not in message


@pytest.mark.anyio
async def test_messages_governed_tool_error_hides_exception_text(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    request_id = _fresh_request_id()
    sentinel = _fresh_sentinel()
    _patch_handle(monkeypatch, mcp_router.GovernedToolError(sentinel))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": request_id,
            },
        )

    assert resp.status_code == 200, resp.text
    assert sentinel not in resp.text
    error = resp.json()["error"]
    assert error["message"] == "internal_error"
    assert error["data"]["request_id"] == request_id
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and request_id in r.getMessage()
        and sentinel in r.getMessage()
    ]
    assert matched, caplog.text
    _assert_clean_error_log(matched[0], exc_type="GovernedToolError")


# Coverage for the refusal-code sanitizer: a wrapped tool failure is
# classified by type before any text matching, every refusal suffix is
# stripped to the bare code, and the fixed quote/refund denial reasons pass
# through unchanged.


@pytest.mark.anyio
async def test_messages_tool_execution_error_mimicking_refusal_is_classified(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    request_id = _fresh_request_id()
    sentinel = _fresh_sentinel()
    _patch_handle(
        monkeypatch,
        mcp_router.ToolExecutionError(f"wallet_frozen: {sentinel}"),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": request_id,
            },
        )

    assert resp.status_code == 200, resp.text
    assert sentinel not in resp.text
    error = resp.json()["error"]
    assert error["message"] == "tool_execution_failed"
    assert error["data"]["request_id"] == request_id
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and request_id in r.getMessage()
        and sentinel in r.getMessage()
    ]
    assert matched, caplog.text
    _assert_clean_error_log(matched[0], exc_type="ToolExecutionError")


@pytest.mark.anyio
async def test_messages_refusal_suffix_is_stripped_to_bare_code(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    request_id = _fresh_request_id()
    sentinel = _fresh_sentinel()
    _patch_handle(
        monkeypatch,
        mcp_router.ToolPermissionDenied(f"wallet_frozen: {sentinel}"),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": request_id,
            },
        )

    assert resp.status_code == 200, resp.text
    assert sentinel not in resp.text
    error = resp.json()["error"]
    assert error["message"] == "wallet_frozen"
    assert error["data"]["request_id"] == request_id
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and request_id in r.getMessage()
        and sentinel in r.getMessage()
    ]
    assert matched, caplog.text
    _assert_clean_error_log(matched[0], exc_type="ToolPermissionDenied")


@pytest.mark.anyio
async def test_messages_allowlisted_forbidden_field_detail_is_stripped_to_bare_code(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    request_id = _fresh_request_id()
    field_name = f"field_{uuid.uuid4().hex}"
    _patch_handle(
        monkeypatch,
        mcp_router.ToolPermissionDenied(f"permit_forbidden_field:{field_name}"),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": request_id,
            },
        )

    assert resp.status_code == 200, resp.text
    assert field_name not in resp.text
    error = resp.json()["error"]
    assert error["message"] == "permit_forbidden_field"
    assert error["data"]["request_id"] == request_id
    matched = [
        r for r in caplog.records if r.name == LOGGER and request_id in r.getMessage()
    ]
    assert matched, caplog.text
    message = _assert_clean_error_log(matched[0], exc_type="ToolPermissionDenied")
    assert field_name in message


@pytest.mark.anyio
async def test_messages_wrapped_tool_mimic_of_allowlisted_code_is_bare(
    client, clean_database, monkeypatch, caplog
):
    provisioned = await provision_agent_wallet(client)
    request_id = _fresh_request_id()
    sentinel = _fresh_sentinel()
    _patch_handle(
        monkeypatch,
        mcp_router.GovernedToolError(f"permit_forbidden_field:{sentinel}"),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": request_id,
            },
        )

    assert resp.status_code == 200, resp.text
    assert sentinel not in resp.text
    error = resp.json()["error"]
    assert error["message"] == "permit_forbidden_field"
    assert error["data"]["request_id"] == request_id
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and request_id in r.getMessage()
        and sentinel in r.getMessage()
    ]
    assert matched, caplog.text
    _assert_clean_error_log(matched[0], exc_type="GovernedToolError")


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
    request_id = _fresh_request_id()
    _patch_handle(monkeypatch, make_exc(reason))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": request_id,
            },
        )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["message"] == reason
    assert error["data"]["request_id"] == request_id
    matched = [
        r
        for r in caplog.records
        if r.name == LOGGER
        and request_id in r.getMessage()
        and reason in r.getMessage()
    ]
    assert matched, caplog.text
    _assert_clean_error_log(matched[0], exc_type=type(make_exc(reason)).__name__)


def test_sanitize_log_message_escapes_controls_and_caps() -> None:
    sanitize = mcp_router._sanitize_log_message
    assert sanitize("plain") == "plain"
    assert sanitize("a\nb\rc\td") == "a\\nb\\rc\\td"
    assert sanitize("a\x00b\x1bb") == "a\\x00b\\x1bb"
    assert sanitize("a\u2028b\u2029c") == "a b c"
    long_text = "A" * 1000
    capped = sanitize(long_text)
    assert capped == "A" * 500 + "...[truncated]"
    assert len(capped) == 514
    short = sanitize("B" * 500)
    assert short == "B" * 500
    assert "...[truncated]" not in short


@pytest.mark.anyio
async def test_messages_log_injection_is_escaped_capped_and_typed(
    client, clean_database, monkeypatch, caplog
):
    """CR/LF/control bytes in exception text must not reach the log raw."""
    provisioned = await provision_agent_wallet(client)
    request_id = _fresh_request_id()
    core = _fresh_sentinel(tag="INJECT")
    payload = core + "\nINJECTED_LINE\r\nTAIL\x00\x1b\u2028" + "P" * 1000
    _patch_handle(monkeypatch, mcp_router.ToolExecutionError(payload))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json=MCP_CALL,
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": request_id,
            },
        )

    assert resp.status_code == 200, resp.text
    assert core not in resp.text
    assert "INJECTED_LINE" not in resp.text
    error = resp.json()["error"]
    assert error["message"] == "tool_execution_failed"
    assert error["data"]["request_id"] == request_id
    matched = [
        r for r in caplog.records if r.name == LOGGER and request_id in r.getMessage()
    ]
    assert matched, caplog.text
    message = _assert_clean_error_log(matched[0], exc_type="ToolExecutionError")
    assert core in message
    assert "\x00" not in message
    assert "\x1b" not in message
    assert "\u2028" not in message
    assert "\\n" in message
    assert "\\r" in message
    assert "\\x00" in message
    assert "...[truncated]" in message
    assert "P" * 600 not in message
    assert len(message) < 800


@pytest.mark.anyio
async def test_rest_invoke_log_injection_is_escaped_capped_and_typed(
    client, clean_database, monkeypatch, caplog
):
    """The REST invoke log line carries only the class, never injected text."""
    provisioned = await provision_agent_wallet(client)
    request_id = _fresh_request_id()
    core = _fresh_sentinel(tag="RINJECT")
    payload = core + "\r\nFORGED: entry\x00" + "Q" * 1000
    _patch_adapter_invoke(monkeypatch, mcp_router.ToolPermissionDenied(payload))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            f"/mcp/tools/{TOOL_242}/invoke",
            json=_rest_body(provisioned["agent_wallet_id"], _fresh_idempotency_key()),
            headers={
                **provisioned["agent_headers"],
                "X-Request-ID": request_id,
            },
        )

    assert resp.status_code == 403, resp.text
    assert core not in resp.text
    assert "FORGED" not in resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "Permission denied"
    assert resp.headers["X-Request-ID"] == request_id
    matched = [
        r for r in caplog.records if r.name == LOGGER and request_id in r.getMessage()
    ]
    assert matched, caplog.text
    message = _assert_clean_error_log(matched[0], exc_type="ToolPermissionDenied")
    # The invoke route never logs exception text, so nothing can be injected.
    assert core not in message
    assert "FORGED" not in message
    assert "\x00" not in message
    assert len(message) < 800
