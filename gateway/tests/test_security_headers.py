"""Baseline response-hardening headers on the API origin."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.security_headers import (
    API_CSP,
    DOCS_HTML_CSP,
    FIRST_PARTY_HTML_CSP,
    HSTS_VALUE,
    SecurityHeadersMiddleware,
)


def _client() -> TestClient:
    app = Starlette(routes=[Route("/probe", lambda request: PlainTextResponse("ok"))])
    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_baseline_headers_are_always_present() -> None:
    response = _client().get("/probe")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_json_responses_use_a_lockdown_csp() -> None:
    """JSON has nothing to execute; default-src 'none' is the whole policy."""

    response = _client().get("/probe")
    assert response.headers["content-security-policy"] == API_CSP
    assert "'unsafe-inline'" not in response.headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in response.headers["content-security-policy"]


def test_first_party_html_allows_inline_css_but_no_scripts() -> None:
    app = Starlette(
        routes=[Route("/dashboard", lambda request: HTMLResponse("<html></html>"))]
    )
    app.add_middleware(SecurityHeadersMiddleware)

    csp = TestClient(app).get("/dashboard").headers["content-security-policy"]
    assert csp == FIRST_PARTY_HTML_CSP
    assert "script-src 'none'" in csp
    assert "style-src 'unsafe-inline'" in csp


def test_swagger_and_redoc_use_the_cdn_compatible_csp() -> None:
    """FastAPI's stock docs load jsDelivr plus an inline boot script."""

    app = Starlette(
        routes=[
            Route("/docs", lambda request: HTMLResponse("<html>docs</html>")),
            Route("/redoc", lambda request: HTMLResponse("<html>redoc</html>")),
            Route("/docs/index", lambda request: PlainTextResponse("{}")),
        ]
    )
    app.add_middleware(SecurityHeadersMiddleware)
    client = TestClient(app)

    docs = client.get("/docs").headers["content-security-policy"]
    redoc = client.get("/redoc").headers["content-security-policy"]
    index = client.get("/docs/index").headers["content-security-policy"]

    assert docs == DOCS_HTML_CSP
    assert redoc == DOCS_HTML_CSP
    assert "cdn.jsdelivr.net" in docs
    # The JSON doc index is not Swagger HTML, so it stays locked down.
    assert index == API_CSP


def test_docs_csp_allows_what_redoc_actually_loads() -> None:
    """ReDoc's bundle parses in a blob: Web Worker and pulls Google Fonts.

    worker-src falls back to script-src, which allows neither, so a policy
    without these directives leaves /redoc blank rather than merely unstyled.
    """

    assert "worker-src blob:" in DOCS_HTML_CSP
    assert "https://fonts.googleapis.com" in DOCS_HTML_CSP
    assert "https://fonts.gstatic.com" in DOCS_HTML_CSP
    # Only the docs HTML pays for this; JSON responses stay locked down.
    assert "blob:" not in API_CSP
    assert "blob:" not in FIRST_PARTY_HTML_CSP


def test_sensitive_paths_are_no_store() -> None:
    app = Starlette(
        routes=[
            Route("/v1/wallets", lambda request: PlainTextResponse("denied")),
            Route("/v1/permits", lambda request: PlainTextResponse("denied")),
            Route("/mcp/messages", lambda request: PlainTextResponse("denied")),
        ]
    )
    app.add_middleware(SecurityHeadersMiddleware)
    client = TestClient(app)

    for path in ("/v1/wallets", "/v1/permits", "/mcp/messages"):
        assert client.get(path).headers["cache-control"] == "no-store"


def test_public_discovery_is_not_forced_no_store() -> None:
    """Agents may cache OpenAPI and well-known documents."""

    app = Starlette(
        routes=[
            Route("/openapi.json", lambda request: PlainTextResponse("{}")),
            Route("/v1/discover", lambda request: PlainTextResponse("{}")),
            Route(
                "/.well-known/agent.json",
                lambda request: PlainTextResponse("{}"),
            ),
        ]
    )
    app.add_middleware(SecurityHeadersMiddleware)
    client = TestClient(app)

    for path in ("/openapi.json", "/.well-known/agent.json"):
        assert "cache-control" not in client.get(path).headers


def test_tool_catalogs_are_forced_no_store() -> None:
    """Authenticated catalogs must not be CDN-cached as public discovery."""

    app = Starlette(
        routes=[
            Route("/v1/discover", lambda request: PlainTextResponse("{}")),
            Route("/mcp/tools.json", lambda request: PlainTextResponse("{}")),
            Route(
                "/.well-known/mcp/tools.json",
                lambda request: PlainTextResponse("{}"),
            ),
        ]
    )
    app.add_middleware(SecurityHeadersMiddleware)
    client = TestClient(app)

    for path in ("/v1/discover", "/mcp/tools.json", "/.well-known/mcp/tools.json"):
        assert client.get(path).headers["cache-control"] == "no-store"


def test_explicit_cache_control_is_not_overridden() -> None:
    app = Starlette(
        routes=[
            Route(
                "/v1/permits/req/card",
                lambda request: HTMLResponse(
                    "<html></html>",
                    headers={"Cache-Control": "no-store, private"},
                ),
            )
        ]
    )
    app.add_middleware(SecurityHeadersMiddleware)

    assert (
        TestClient(app).get("/v1/permits/req/card").headers["cache-control"]
        == "no-store, private"
    )


def test_hsts_is_sent_only_for_requests_that_arrived_over_tls() -> None:
    """A browser ignores HSTS over plain HTTP, so do not claim it there."""

    client = _client()

    plain = client.get("/probe")
    assert "strict-transport-security" not in plain.headers

    forwarded_http = client.get("/probe", headers={"x-forwarded-proto": "http"})
    assert "strict-transport-security" not in forwarded_http.headers

    # A proxy chain sends a comma-separated list; the client-most hop leads.
    forwarded_https = client.get("/probe", headers={"x-forwarded-proto": "https, http"})
    assert forwarded_https.headers["strict-transport-security"] == HSTS_VALUE


def test_hsts_does_not_claim_preload() -> None:
    """Preload is a slow-to-reverse, whole-subdomain-tree operator decision."""

    assert "preload" not in HSTS_VALUE
    assert "includeSubDomains" in HSTS_VALUE


def test_explicit_route_headers_are_not_overridden() -> None:
    app = Starlette(
        routes=[
            Route(
                "/framed",
                lambda request: PlainTextResponse(
                    "ok", headers={"X-Frame-Options": "DENY"}
                ),
            )
        ]
    )
    app.add_middleware(SecurityHeadersMiddleware)

    assert TestClient(app).get("/framed").headers["x-frame-options"] == "DENY"


def test_middleware_is_registered_on_the_application() -> None:
    from app.main import app

    assert any(entry.cls is SecurityHeadersMiddleware for entry in app.user_middleware)


def test_full_app_auth_gated_401_has_csp_and_no_store() -> None:
    """The reviewer's auth-gated probe should see both nits closed."""

    from fastapi.testclient import TestClient as FastAPITestClient

    from app.main import app

    response = FastAPITestClient(app).get("/v1/permits")
    assert response.status_code == 401
    assert response.headers["content-security-policy"] == API_CSP
    assert response.headers["cache-control"] == "no-store"
    # Rate limiting is on this path: 40 requests will not 429, but the
    # budget is advertised on every counted response, including 401s.
    assert "x-ratelimit-limit" in response.headers


def test_full_app_openapi_has_csp_without_no_store() -> None:
    from fastapi.testclient import TestClient as FastAPITestClient

    from app.main import app

    response = FastAPITestClient(app).get("/openapi.json")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == API_CSP
    assert "cache-control" not in response.headers


def test_full_app_docs_use_swagger_csp() -> None:
    from fastapi.testclient import TestClient as FastAPITestClient

    from app.main import app

    response = FastAPITestClient(app).get("/docs")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == DOCS_HTML_CSP
    assert response.headers["content-type"].startswith("text/html")


def test_full_app_redoc_csp_covers_every_origin_its_html_references() -> None:
    """Whatever FastAPI's stock ReDoc page loads, the policy has to allow."""

    import re

    from fastapi.testclient import TestClient as FastAPITestClient

    from app.main import app

    response = FastAPITestClient(app).get("/redoc")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert csp == DOCS_HTML_CSP

    origins = {
        f"{match.group(1)}//{match.group(2)}"
        for match in re.finditer(r"(https:)//([^/\"\' ]+)", response.text)
    }
    assert origins  # the page does reference third-party origins
    for origin in origins:
        assert origin in csp, f"{origin} is loaded by /redoc but not in the CSP"
