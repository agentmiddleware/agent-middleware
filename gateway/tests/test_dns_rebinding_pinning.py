"""DNS rebinding protection for agent-driven outbound fetches.

``check_outbound_url`` validates a URL once, but the sandbox HTTP proxy and
the headless browser used to resolve the hostname again at connect time. A
name that validated as public could therefore rebind to a private address
before the connection, and the connection would follow it. The sandbox now
connects to the pinned IP that passed validation (same approach as the
upstream MCP pinned transport), and browser navigation revalidates
immediately before ``page.goto``.

These tests use a resolver that returns a public address and then a private
address to prove the validated IP is the one connected to.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import httpx
from fastapi import FastAPI

from app.core import url_guard
from app.core.auth import AuthContext, get_auth_context
from app.core.config import get_settings
from app.core.url_guard import check_outbound_url, pin_outbound_url
from app.routers.awi_enhanced import router as awi_enhanced_router
from app.services import awi_session
from app.services.awi_playwright_bridge import (
    AWIPlaywrightBridge,
    BridgeSession,
    NavigationBlocked,
)
from app.services.behavioral_sandbox import BehavioralSandboxEngine

_PUBLIC = "93.184.216.34"
_PRIVATE = "10.0.0.5"


def _addrinfo(address: str):
    return [(None, None, None, None, (address, 0))]


def _rebinding_resolve_factory(monkeypatch, calls: list):
    """First lookup returns public, every later lookup returns private."""

    async def resolve(_host):
        calls.append(1)
        if len(calls) == 1:
            return _addrinfo(_PUBLIC)
        return _addrinfo(_PRIVATE)

    monkeypatch.setattr(url_guard, "_resolve_host", resolve)


@pytest.fixture
def bridge():
    return AWIPlaywrightBridge()


@pytest.fixture
def session():
    return BridgeSession(session_id="test-session", current_url="https://example.com")


@pytest.fixture
def engine():
    return BehavioralSandboxEngine(redis_url="redis://localhost:6379")


class _RecordingPage:
    """Fake Playwright page that records navigations."""

    def __init__(self):
        self.url = "https://example.com/"
        self.goto_calls: list[str] = []

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url

    async def title(self):
        return "Example"


class TestPinOutboundUrl:
    @pytest.mark.anyio
    async def test_pin_returns_first_validated_address(self):
        async def resolver(_host):
            return (_PUBLIC, "93.184.216.35")

        reason, pinned = await pin_outbound_url(
            "https://example.com/data", resolver=resolver
        )
        assert reason is None
        assert pinned == _PUBLIC

    @pytest.mark.anyio
    async def test_pin_blocks_private_resolution(self):
        async def resolver(_host):
            return (_PRIVATE,)

        reason, pinned = await pin_outbound_url(
            "https://example.com/data", resolver=resolver
        )
        assert reason == "private_address_blocked"
        assert pinned is None

    @pytest.mark.anyio
    async def test_pin_returns_literal_for_literal_host(self):
        reason, pinned = await pin_outbound_url("https://93.184.216.34/data")
        assert reason is None
        assert pinned == _PUBLIC

    @pytest.mark.anyio
    async def test_check_outbound_url_still_reports_verdict_only(self, monkeypatch):
        async def resolve(_host):
            return _addrinfo(_PUBLIC)

        monkeypatch.setattr(url_guard, "_resolve_host", resolve)
        assert await check_outbound_url("https://example.com/page") is None


class TestPrivateTargetsAllowed:
    """``ALLOW_PRIVATE_NETWORK_TARGETS`` returns no pin; callers dial by name."""

    @pytest.mark.anyio
    async def test_pin_returns_no_address_when_private_targets_allowed(
        self, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "ALLOW_PRIVATE_NETWORK_TARGETS", True)

        async def resolver(_host):
            raise AssertionError("allowed private mode must not resolve")

        result = await pin_outbound_url("http://localhost:9/mock", resolver=resolver)
        assert result == (None, None)

    @pytest.mark.anyio
    async def test_sandbox_reaches_local_mock_server_without_a_pin(
        self, engine, monkeypatch
    ):
        """The unpinned path still connects: a real localhost server answers."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        monkeypatch.setattr(get_settings(), "ALLOW_PRIVATE_NETWORK_TARGETS", True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = await engine._execute_http_proxy(
                "http_get",
                {"url": f"http://localhost:{server.server_address[1]}/mock"},
                False,
                5,
                network_access=True,
            )
        finally:
            server.shutdown()
            server.server_close()

        assert result["success"] is True, result
        assert result["output"]["status_code"] == 204


class TestSandboxRebindingPinning:
    @pytest.mark.anyio
    async def test_connects_to_pinned_address_despite_rebinding(
        self, engine, monkeypatch
    ):
        resolve_calls: list = []
        _rebinding_resolve_factory(monkeypatch, resolve_calls)

        connected: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            connected.append(request)
            return httpx.Response(200, text="ok")

        result = await engine._execute_http_proxy(
            "http_get",
            {"url": "https://example.com/data", "method": "GET"},
            False,
            5,
            network_access=True,
            transport=httpx.MockTransport(handler),
        )

        assert result["success"] is True
        assert result["output"]["status_code"] == 200
        # DNS was consulted exactly once: validation saw the public address
        # and the connection reused it instead of resolving again (where it
        # would have found the private rebound address).
        assert len(resolve_calls) == 1
        assert len(connected) == 1
        assert connected[0].url.host == _PUBLIC
        # HTTP Host and TLS SNI still name the original hostname.
        assert connected[0].headers["Host"] == "example.com"
        assert connected[0].extensions["sni_hostname"] == "example.com"

    @pytest.mark.anyio
    async def test_never_connects_when_validation_sees_private(
        self, engine, monkeypatch
    ):
        async def resolve(_host):
            return _addrinfo(_PRIVATE)

        monkeypatch.setattr(url_guard, "_resolve_host", resolve)

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("blocked fetch must not connect")

        result = await engine._execute_http_proxy(
            "http_get",
            {"url": "https://example.com/data"},
            False,
            5,
            network_access=True,
            transport=httpx.MockTransport(handler),
        )

        assert result["success"] is False
        assert "private_address_blocked" in result["error"]


class TestBrowserNavigationRevalidation:
    @pytest.mark.anyio
    async def test_create_session_rejects_private_preflight(self, bridge, monkeypatch):
        async def resolve(_host):
            return _addrinfo(_PRIVATE)

        monkeypatch.setattr(url_guard, "_resolve_host", resolve)
        with pytest.raises(NavigationBlocked, match="private_address_blocked"):
            await bridge.create_session("https://example.com/")
        assert bridge._sessions == {}

    @pytest.mark.anyio
    async def test_goto_refuses_after_rebinding(self, bridge, session, monkeypatch):
        resolve_calls: list = []
        _rebinding_resolve_factory(monkeypatch, resolve_calls)

        # Command build validates while DNS still answers public.
        [command] = await bridge._handle_navigate_to(
            session, {"url": "https://example.com/products"}
        )

        # By execution time DNS has rebound to private: refuse, no goto.
        page = _RecordingPage()
        session._page = page
        with pytest.raises(ValueError, match="navigation blocked"):
            await bridge._execute_single_command(session, command)
        assert page.goto_calls == []

    @pytest.mark.anyio
    async def test_navigate_to_refuses_after_rebinding(
        self, bridge, session, monkeypatch
    ):
        resolve_calls: list = []
        _rebinding_resolve_factory(monkeypatch, resolve_calls)

        # An earlier pre-flight check (create_session) saw the public address.
        assert await check_outbound_url("https://example.com/") is None

        # The last-moment check before goto sees the rebound private address.
        page = _RecordingPage()
        session._page = page
        with pytest.raises(ValueError, match="navigation blocked"):
            await bridge._navigate_to(session, "https://example.com/")
        assert page.goto_calls == []

    @pytest.mark.anyio
    async def test_navigate_to_still_runs_for_stable_public_dns(
        self, bridge, session, monkeypatch
    ):
        async def resolve(_host):
            return _addrinfo(_PUBLIC)

        monkeypatch.setattr(url_guard, "_resolve_host", resolve)

        page = _RecordingPage()
        session._page = page
        await bridge._navigate_to(session, "https://example.com/")
        assert page.goto_calls == ["https://example.com/"]

    @pytest.mark.anyio
    async def test_create_session_fails_and_unregisters_on_blocked_navigation(
        self, bridge, monkeypatch
    ):
        """A rebound target must fail create_session, not return a blank session.

        The pre-flight check sees the public address; the last-moment check in
        ``_navigate_to`` sees the rebound private one. The session used to be
        returned (and attach_dom_bridge reported "attached") with an open
        context on a blank page.
        """
        resolve_calls: list = []
        _rebinding_resolve_factory(monkeypatch, resolve_calls)
        page = _RecordingPage()
        page.close = AsyncMock()
        context = SimpleNamespace(close=AsyncMock())

        async def no_playwright():
            bridge._playwright_initialized = True

        async def fake_context(session, headless, viewport):
            session._page = page
            session._context = context
            session.browser_context_id = "fake-context"

        monkeypatch.setattr(bridge, "_init_playwright", no_playwright)
        monkeypatch.setattr(bridge, "_create_browser_context", fake_context)

        with pytest.raises(ValueError, match="navigation blocked"):
            await bridge.create_session("https://example.com/")
        assert len(resolve_calls) == 2
        assert page.goto_calls == []
        page.close.assert_awaited_once()
        context.close.assert_awaited_once()
        assert bridge._sessions == {}

    @pytest.mark.anyio
    async def test_attach_reports_blocked_navigation(self, monkeypatch):
        manager = SimpleNamespace(
            get_session=AsyncMock(return_value=SimpleNamespace(wallet_id=None)),
            attach_dom_bridge=AsyncMock(
                side_effect=NavigationBlocked(
                    "navigation blocked (private_address_blocked)"
                )
            ),
        )
        monkeypatch.setattr(awi_session, "get_awi_session_manager", lambda: manager)
        app = FastAPI()
        app.include_router(awi_enhanced_router)
        app.dependency_overrides[get_auth_context] = lambda: AuthContext(
            source="api_key", raw_key="synthetic", is_bootstrap_admin=True
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/awi/dom/attach", json={"session_id": "existing-session"}
            )

        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "navigation_blocked"
        assert manager.get_session.await_count == 1
        manager.attach_dom_bridge.assert_awaited_once_with(
            session_id="existing-session", target_url=None
        )
