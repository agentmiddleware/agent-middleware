"""Browser Origin and Host checks for routes a page must not read.

A browser origin is the scheme, the host, and the port together. Comparing
hostnames alone treats a page on another port, or on https, as the same
origin. Under wildcard CORS that page can read the response.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.core.config import get_settings


def validate_browser_origin(request: Request) -> None:
    """Refuse a cross-origin browser call, and a Host that is not PUBLIC_URL.

    Clients that send no Origin (CLIs, SDKs, curl) pass through. When
    PUBLIC_URL is set, the request Host must match it and a present Origin
    must match it exactly. Without PUBLIC_URL, browser requests must target
    a loopback Host and send its matching Origin.
    """
    public_url = get_settings().PUBLIC_URL.rstrip("/")
    request_origin = f"{request.url.scheme}://{request.url.netloc}"
    if public_url and request.url.netloc != urlsplit(public_url).netloc:
        raise HTTPException(status_code=421, detail={"error": "host_not_allowed"})

    origin = request.headers.get("origin")
    if not origin:
        return
    # A reverse proxy may expose its internal HTTP scheme through the ASGI
    # request even though the canonical browser origin is HTTPS. Once an
    # operator configures PUBLIC_URL, it is the only origin authority.
    # Request-derived origin only rejects mismatches until Host is checked.
    allowed = {public_url} if public_url else {request_origin}
    if origin.rstrip("/") not in allowed:
        raise HTTPException(status_code=403, detail={"error": "origin_not_allowed"})
    if not public_url and request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        server = request.scope.get("server")
        # HTTPX's in-process test transport has no listening port. Only its
        # reserved test host may match that server; a forged Host is refused.
        in_process = (
            request.url.hostname == "test"
            and server
            and server[1] is None
            and request.url.hostname == server[0]
            and request.client
            and request.client.host in {"127.0.0.1", "::1"}
        )
        if not in_process:
            raise HTTPException(status_code=421, detail={"error": "host_not_allowed"})
