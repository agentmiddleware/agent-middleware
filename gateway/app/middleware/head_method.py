"""HEAD support for every GET route (RFC 9110 §9.3.2).

FastAPI's ``APIRoute`` registers only the methods a route declares, unlike
plain Starlette ``Route`` objects which auto-add HEAD to GET routes. The
result is that ``HEAD /health`` answered 405 while ``HEAD /openapi.json``
(a plain Starlette route) answered 200 — an inconsistency monitoring
probes and link checkers trip over.

Fixed here at the ASGI boundary rather than per-route: a HEAD request is
presented to the inner application as GET, and every response body byte is
suppressed on the way out. Headers — status, ``Content-Length``,
``Content-Type``, rate-limit and security headers — are exactly what the
GET would have produced, which is what RFC 9110 asks of HEAD. The server
(uvicorn/h11) still knows the socket-level request was HEAD, so an empty
body is the correct wire framing.

Pure ASGI (no ``BaseHTTPMiddleware``) so streaming responses are dropped
chunk-by-chunk instead of being buffered.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable


class HeadMethodMiddleware:
    """Answer HEAD for any route that answers GET, with an empty body."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http" or scope.get("method") != "HEAD":
            await self.app(scope, receive, send)
            return

        # Copy before mutating: the original scope belongs to the server.
        get_scope = dict(scope)
        get_scope["method"] = "GET"

        async def send_without_body(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                message = {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": message.get("more_body", False),
                }
            await send(message)

        await self.app(get_scope, receive, send_without_body)
