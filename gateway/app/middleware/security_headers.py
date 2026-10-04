"""Baseline security response headers for the API origin.

The API answers with JSON, a Swagger UI, and a few first-party HTML pages.
None of those need to be sniffed, framed, or referred with a full URL, so the
same three headers the marketing origin already sends belong here too. HSTS is
only emitted for requests that actually arrived over TLS, so plain-HTTP local
development is unaffected and the header is never sent where a browser must
ignore it.

Content-Security-Policy is path-aware: JSON has nothing to execute, first-party
HTML (dashboard, approval cards) is inline-CSS only, and FastAPI's stock
Swagger UI / ReDoc need jsDelivr plus an inline boot script, a blob: Web Worker,
and Google Fonts. Cache-Control: no-store is the default on tenant-sensitive
paths; public discovery stays cacheable so agents can keep OpenAPI and
well-known documents.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Two years, matching the marketing origin's vercel.json. "preload" is
# deliberately omitted: submitting to the preload list is a slow-to-reverse
# commitment for every subdomain and is an operator decision, not a default.
HSTS_VALUE = "max-age=63072000; includeSubDomains"

STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

# JSON and every non-HTML response: browsers should not execute anything here.
# frame-ancestors 'self' matches X-Frame-Options: SAMEORIGIN.
API_CSP = (
    "default-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
)

# Dashboard and permit-request cards: inline CSS, data: icons, no scripts.
FIRST_PARTY_HTML_CSP = (
    "default-src 'none'; "
    "script-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src 'self' data:; "
    "frame-ancestors 'self'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

# FastAPI ships Swagger UI / ReDoc from jsDelivr with an inline window.onload
# boot script and fetches /openapi.json from this origin. Restricted to the
# two documentation HTML paths so the JSON API stays locked down.
#
# Two entries exist only because ReDoc's stock bundle needs them, and both are
# load-bearing for /redoc rendering at all:
#   - worker-src blob: — redoc.standalone.js starts its parser in a Web Worker
#     built with `new Worker(URL.createObjectURL(new Blob([...])))`. worker-src
#     falls back to script-src, which does not allow blob:, so without this the
#     worker is blocked and the page never renders.
#   - fonts.googleapis.com / fonts.gstatic.com — FastAPI's get_redoc_html emits
#     a Google Fonts stylesheet (with_google_fonts defaults to True), and the
#     stylesheet pulls its font files from gstatic.
# Allowing blob: workers here costs nothing this policy was still holding back:
# script-src on these two paths already allows 'unsafe-inline'.
DOCS_HTML_CSP = (
    "default-src 'none'; "
    "script-src https://cdn.jsdelivr.net 'unsafe-inline'; "
    "worker-src blob:; "
    "style-src https://cdn.jsdelivr.net https://fonts.googleapis.com "
    "'unsafe-inline'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src https://cdn.jsdelivr.net https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "frame-ancestors 'self'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

_DOCS_HTML_PATHS = frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"})

# Public discovery is intentionally credential-less and agent-cacheable.
# Tool catalogs are not: on production-like boots they require a key, so they
# must not be CDN-cached as anonymous documents. Receipt verification keys
# stay cacheable.
_PUBLIC_DISCOVERY_PATHS = frozenset(
    {
        "/",
        "/docs",
        "/redoc",
        "/docs/oauth2-redirect",
        "/docs/index",
        "/openapi.json",
        "/health",
        "/health/ready",
        "/health/dependencies",
        "/llm.txt",
        "/llms.txt",
        "/WEDGE.md",
        "/SECURITY_LIMITATIONS.md",
        "/DESIGN_PARTNER_GUIDE.md",
        "/docs/partner-api-key-bootstrap.md",
        "/docs/agent-accountability.md",
        "/dashboard",
    }
)
_PUBLIC_DISCOVERY_PREFIXES = ("/.well-known/",)
_NON_CACHEABLE_WELL_KNOWN_PATHS = frozenset(
    {
        "/.well-known/mcp/tools.json",
    }
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach baseline security headers without overriding explicit values."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for header, value in STATIC_HEADERS.items():
            response.headers.setdefault(header, value)
        response.headers.setdefault(
            "Content-Security-Policy",
            _content_security_policy(request, response),
        )
        if not _is_public_discovery(request.url.path):
            response.headers.setdefault("Cache-Control", "no-store")
        if _is_secure(request):
            response.headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
        return response


def _normalized_path(path: str) -> str:
    if path != "/":
        return path.rstrip("/") or "/"
    return path


def _content_security_policy(request: Request, response: Response) -> str:
    path = _normalized_path(request.url.path)
    if path in _DOCS_HTML_PATHS:
        return DOCS_HTML_CSP
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type == "text/html":
        return FIRST_PARTY_HTML_CSP
    return API_CSP


def _is_public_discovery(path: str) -> bool:
    normalized = _normalized_path(path)
    if normalized in _NON_CACHEABLE_WELL_KNOWN_PATHS:
        return False
    if normalized in _PUBLIC_DISCOVERY_PATHS:
        return True
    return normalized.startswith(_PUBLIC_DISCOVERY_PREFIXES)


def _is_secure(request: Request) -> bool:
    """Return whether the client reached the origin over HTTPS.

    Behind Railway's edge the app itself is spoken to over plain HTTP, so the
    forwarded scheme is what matters. ``request.url.scheme`` already reflects
    ``X-Forwarded-Proto`` when a proxy-headers middleware is installed; the
    explicit header check keeps the behaviour correct if it is not.
    """

    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        # A proxy chain sends a comma-separated list; the client-most hop leads.
        return forwarded.split(",")[0].strip().casefold() == "https"
    return request.url.scheme == "https"
