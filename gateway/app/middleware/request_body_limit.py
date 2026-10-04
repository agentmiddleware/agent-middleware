"""Bound the size of inbound HTTP request bodies.

The governed MCP invoke path accepts caller-supplied tool arguments, which are
parsed, hashed, and persisted. Without a ceiling, one request can force the
gateway to buffer and process an arbitrarily large payload before any permit,
wallet, or rate-limit decision has a chance to reject it — the work is done on
the attacker's terms and charged to nobody.

Two MCP transports already cap their own bodies (``mcp_public`` at 256 KiB,
``partner_mcp`` at 64 KiB), but both are opt-in surfaces. This middleware puts
the same floor under every route, including ``/mcp/messages``, which is always
mounted and is the product.

Enforcement is two-stage. A declared ``Content-Length`` over the limit is
refused without reading a byte, so the common case costs nothing. Otherwise the
body is drained up to the limit and replayed to the application; a stream that
exceeds the limit mid-flight is refused at the moment it does. Buffering is
bounded by the limit itself, so the check cannot become the exhaustion it
prevents.
"""

from __future__ import annotations

from collections import deque

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Methods that carry a body. A GET has none to bound, and passing it straight
# through keeps the read side of the API free of buffering.
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _too_large(limit: int) -> JSONResponse:
    """413 in the API's error shape, naming the limit so a caller can adapt."""
    return JSONResponse(
        status_code=413,
        content={
            "detail": f"Request body exceeds the {limit}-byte limit.",
            "max_request_body_bytes": limit,
        },
    )


class RequestBodyLimitMiddleware:
    """Reject request bodies larger than ``max_body_size`` with a 413."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in BODY_METHODS:
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                # A malformed header is not proof of an oversized body; fall
                # through to the streaming check, which measures the real one.
                pass
            else:
                if declared_size > self.max_body_size:
                    await _too_large(self.max_body_size)(scope, receive, send)
                    return

        # A truthful Content-Length is not guaranteed (chunked encoding omits
        # it, a hostile client can understate it), so measure what arrives.
        buffered = bytearray()
        saw_body = False
        complete = False
        trailing: Message | None = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # A disconnect (or anything else) ends the body; hand it on so
                # the application observes the same event we did.
                trailing = message
                break

            saw_body = True
            buffered.extend(message.get("body", b""))
            if len(buffered) > self.max_body_size:
                await _too_large(self.max_body_size)(scope, receive, send)
                return
            if not message.get("more_body", False):
                complete = True
                break

        replayed: deque[Message] = deque()
        if saw_body:
            replayed.append(
                {
                    "type": "http.request",
                    "body": bytes(buffered),
                    "more_body": not complete,
                }
            )
        if trailing is not None:
            replayed.append(trailing)

        async def replay() -> Message:
            if replayed:
                return replayed.popleft()
            return await receive()

        await self.app(scope, replay, send)
