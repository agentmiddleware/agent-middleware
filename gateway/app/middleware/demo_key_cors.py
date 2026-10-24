"""Browser CORS for the anonymous demo-key route only."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings


class DemoKeyCORSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path != "/v1/demo/keys":
            return await call_next(request)

        origin = request.headers.get("origin")
        allowed = {
            (entry.strip() if "://" in entry else "https://" + entry.strip()).rstrip(
                "/"
            )
            for entry in get_settings().DEMO_ALLOWED_ORIGINS.split(",")
            if entry.strip()
        }
        if not origin or origin not in allowed:
            return await call_next(request)

        if request.method == "OPTIONS" and request.headers.get(
            "access-control-request-method"
        ) in {"GET", "POST"}:
            response = Response(status_code=200)
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Max-Age"] = "600"
        else:
            response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers.add_vary_header("Origin")
        return response
