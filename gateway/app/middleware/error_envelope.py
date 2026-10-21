"""Add actionable error fields without changing existing transport codes."""

from __future__ import annotations

import json
import re
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send


# Copy for common gateway errors. Reconcile the other nine entries with the
# ux-spec Part B table before merge.
ERROR_COPY: dict[str, tuple[str, str]] = {
    "missing_wallet_id": (
        "This call needs a wallet. Pass wallet_id from your demo key response, or use a key with one wallet.",
        "Pass wallet_id from the demo key response or use a key with one wallet.",
    ),
    "invalid_api_key": (
        "The API key is invalid.",
        "Check the API key and send the request again.",
    ),
    "wallet_scoped_key_required": (
        "This call needs a key for one wallet.",
        "Use a key linked to the wallet for this call.",
    ),
    "rate_limited": (
        "Too many requests.",
        "Wait for the limit to reset before sending another request.",
    ),
    "idempotency_key_conflict": (
        "This key was used with a different request.",
        "Use a new key for a different request.",
    ),
    "idempotency_in_progress": (
        "A call with this key is still in progress.",
        "Wait for it to finish before checking the receipt.",
    ),
    "insufficient_funds": (
        "This wallet does not have enough funds.",
        "Add funds or use a wallet with enough funds.",
    ),
    "permit_expired": (
        "The permit has expired.",
        "Request a new permit before calling the tool.",
    ),
    "delivery_uncertain": (
        "The outcome of the dispatched call is uncertain.",
        "Reconcile the outcome with the tool before taking another action.",
    ),
    "internal_error": (
        "The request could not be completed.",
        "Try again later.",
    ),
}

_DEFAULT_COPY = (
    "The request could not be completed.",
    "Check the error code and request details.",
)
_STATUS_CODES = {
    400: "invalid_request",
    401: "invalid_api_key",
    403: "access_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "request_too_large",
    421: "host_not_allowed",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}
_JSONRPC_CODES = {
    -32600: "invalid_request",
    -32601: "method_not_found",
    -32602: "invalid_params",
    -32603: "internal_error",
    -32003: "access_denied",
    -32004: "insufficient_funds",
    -32005: "idempotency_in_progress",
}
_CODE = re.compile(r"[a-z][a-z0-9_]*\Z")
_MCP_PATHS = frozenset({"/mcp", "/mcp/messages", "/mcp/public"})


def _is_rest_invoke(path: str) -> bool:
    return path.startswith("/mcp/tools/") and path.endswith("/invoke")


def error_envelope(code: str) -> dict[str, str]:
    message, fix = ERROR_COPY.get(code, _DEFAULT_COPY)
    return {
        "code": code,
        "message": message,
        "fix": fix,
        "docs_url": f"https://www.agentmiddleware.dev/docs/errors#{code}",
    }


def _existing_code(payload: dict[str, Any]) -> str | None:
    detail = payload.get("detail")
    sources = (payload, detail) if isinstance(detail, dict) else (payload,)
    for source in sources:
        for field in ("reason_code", "code", "error"):
            value = source.get(field)
            if isinstance(value, str) and _CODE.fullmatch(value):
                return value
    if isinstance(detail, str) and _CODE.fullmatch(detail):
        return detail
    if detail == "Missing wallet_id":
        return "missing_wallet_id"
    return None


def _jsonrpc_error_code(error: dict[str, Any]) -> str:
    if (
        error.get("code") == -32602
        and error.get("message") == "Missing wallet_id in mcpContext"
    ):
        return "missing_wallet_id"
    data = error.get("data")
    if isinstance(data, dict):
        code = _existing_code(data)
        if code:
            return code
    message = error.get("message")
    if isinstance(message, str):
        prefix = message.split(":", 1)[0]
        if _CODE.fullmatch(prefix):
            return prefix
    numeric_code = error.get("code")
    return (
        _JSONRPC_CODES.get(numeric_code, "request_failed")
        if isinstance(numeric_code, int)
        else "request_failed"
    )


def _rewrite(body: bytes, status: int) -> bytes | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
        error = payload.get("error")
        if isinstance(error, dict):
            data = error.get("data")
            data = (
                dict(data)
                if isinstance(data, dict)
                else {"detail": data}
                if data is not None
                else {}
            )
            error["data"] = {**data, **error_envelope(_jsonrpc_error_code(error))}
            return json.dumps(payload).encode("utf-8")
    if isinstance(payload, dict) and status == 200 and payload.get("isError") is True:
        structured = payload.get("structuredContent")
        code = _existing_code(structured) if isinstance(structured, dict) else None
        return json.dumps(
            {**payload, **error_envelope(code or "tool_execution_failed")}
        ).encode("utf-8")
    if status == 202 and isinstance(payload, dict):
        if _existing_code(payload) == "human_approval_pending":
            return json.dumps(
                {**payload, **error_envelope("human_approval_pending")}
            ).encode("utf-8")
    if status < 400:
        return None
    if not isinstance(payload, dict):
        payload = {}
    code = _existing_code(payload) or _STATUS_CODES.get(status, "request_failed")
    return json.dumps({**payload, **error_envelope(code)}).encode("utf-8")


class ErrorEnvelopeMiddleware:
    """Normalize completed HTTP errors and JSON RPC errors at the ASGI boundary."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start: Message | None = None
        chunks: list[bytes] = []

        async def send_enveloped(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                status = message["status"]
                content_type = next(
                    (
                        value.lower()
                        for key, value in message.get("headers", [])
                        if key.lower() == b"content-type"
                    ),
                    b"",
                )
                if status >= 400 or (
                    (
                        (scope.get("path") in _MCP_PATHS and status == 200)
                        or (
                            _is_rest_invoke(scope.get("path", ""))
                            and status in {200, 202}
                        )
                    )
                    and b"json" in content_type
                ):
                    start = message
                else:
                    await send(message)
                return
            if start is None or message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            original = b"".join(chunks)
            replacement = _rewrite(original, start["status"])
            if replacement is not None:
                headers = [
                    (key, value)
                    for key, value in start.get("headers", [])
                    if key.lower() not in {b"content-length", b"content-type"}
                ]
                headers.extend(
                    [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(replacement)).encode()),
                    ]
                )
                start = {**start, "headers": headers}
            await send(start)
            await send(
                {
                    "type": "http.response.body",
                    "body": replacement if replacement is not None else original,
                }
            )
            start = None
            chunks.clear()

        await self.app(scope, receive, send_enveloped)
