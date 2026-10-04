"""
Tests for v1.1 Production Hardening Features
============================================

Tests for:
1. Structured logging
2. Health/ready endpoint
3. Retry with backoff
4. Circuit breakers
5. MCP pagination
"""

import pytest
import asyncio

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry


class TestResilienceUtilities:
    """Tests for resilience utilities."""

    def test_retry_with_backoff_success(self):
        """Test retry decorator succeeds on first attempt."""
        from app.core.resilience import retry_with_backoff

        call_count = 0

        @retry_with_backoff(max_attempts=3, base_delay=0.1)
        async def success_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = asyncio.run(success_func())
        assert result == "success"
        assert call_count == 1

    def test_retry_with_backoff_retry(self):
        """Test retry decorator retries on failure."""
        from app.core.resilience import retry_with_backoff

        call_count = 0

        @retry_with_backoff(max_attempts=3, base_delay=0.01)
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Transient error")
            return "success"

        result = asyncio.run(flaky_func())
        assert result == "success"
        assert call_count == 3

    def test_circuit_breaker_closed_state(self):
        """Test circuit breaker starts closed."""
        from app.core.resilience import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3)
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_allowed() is True

    def test_circuit_breaker_opens_after_threshold(self):
        """Test circuit breaker opens after failures."""
        from app.core.resilience import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.is_allowed() is False

    def test_circuit_breaker_record_success(self):
        """Test circuit breaker records success."""
        from app.core.resilience import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb._failure_count == 1


_PAGINATION_PROBES = (
    ("pagination-probe-a", ServiceCategory.AGENT_COMMS),
    ("pagination-probe-b", ServiceCategory.AGENT_COMMS),
    ("pagination-probe-c", ServiceCategory.PROTOCOL_GEN),
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def pagination_probe_tools():
    """Register three local tools so GET /mcp/tools always has pages to walk."""
    registry = get_service_registry()
    for service_id, category in _PAGINATION_PROBES:
        registry.register_local(
            service_id=service_id,
            name=service_id,
            description="Pagination probe",
            category=category,
            func=lambda: {"ok": True},
        )
    try:
        yield [service_id for service_id, _ in _PAGINATION_PROBES]
    finally:
        for service_id, _ in _PAGINATION_PROBES:
            registry.unregister_local(service_id)


class TestMCPPagination:
    """GET /mcp/tools must page through the catalog by limit and offset."""

    @pytest.mark.anyio
    async def test_pages_partition_the_full_listing(
        self, client, pagination_probe_tools
    ):
        full_resp = await client.get("/mcp/tools", params={"limit": 500})
        assert full_resp.status_code == 200
        full = full_resp.json()
        ordered = [tool["name"] for tool in full["tools"]]
        total = full["total"]
        assert full["has_more"] is False, "the reference listing must be complete"
        assert full["count"] == total == len(ordered)
        assert len(ordered) == len(set(ordered)), "a tool is listed twice"
        assert set(pagination_probe_tools) <= set(ordered)

        walked: list[str] = []
        offset = 0
        while True:
            resp = await client.get("/mcp/tools", params={"limit": 2, "offset": offset})
            assert resp.status_code == 200
            page = resp.json()
            names = [tool["name"] for tool in page["tools"]]
            # Each page is exactly the next slice of the full listing, so a
            # page that ignored offset, overlapped, or skipped a tool fails.
            assert names == ordered[offset : offset + 2]
            assert page["count"] == len(names)
            assert page["total"] == total
            assert page["limit"] == 2
            assert page["offset"] == offset
            assert page["has_more"] is (offset + len(names) < total)
            walked.extend(names)
            if not page["has_more"]:
                break
            offset += 2
        assert walked == ordered

    @pytest.mark.anyio
    async def test_offset_past_the_end_is_an_empty_final_page(
        self, client, pagination_probe_tools
    ):
        total = (await client.get("/mcp/tools")).json()["total"]
        assert total >= len(pagination_probe_tools)

        resp = await client.get("/mcp/tools", params={"offset": total})
        assert resp.status_code == 200
        page = resp.json()
        assert page["tools"] == []
        assert page["count"] == 0
        assert page["total"] == total
        assert page["has_more"] is False

    @pytest.mark.anyio
    async def test_category_filter_applies_before_paging(
        self, client, pagination_probe_tools
    ):
        resp = await client.get(
            "/mcp/tools",
            params={"category": ServiceCategory.PROTOCOL_GEN.value, "limit": 500},
        )
        assert resp.status_code == 200
        page = resp.json()
        names = {tool["name"] for tool in page["tools"]}
        assert "pagination-probe-c" in names
        assert not names & {"pagination-probe-a", "pagination-probe-b"}
        assert page["total"] == len(names)

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "params",
        [{"limit": 0}, {"limit": 501}, {"offset": -1}, {"category": "not-a-category"}],
    )
    async def test_out_of_range_paging_is_rejected(self, client, params):
        resp = await client.get("/mcp/tools", params=params)
        assert resp.status_code == 422, resp.text


class TestHealthEndpoints:
    """Tests for health endpoints."""

    def test_health_endpoint_exists(self):
        """Verify /health endpoint exists."""
        from app.main import app

        from tests.conftest import iter_routes

        routes = [r.path for r in iter_routes(app.routes)]
        assert "/health" in routes

    def test_health_ready_endpoint_exists(self):
        """Verify /health/ready endpoint exists."""
        from app.main import app

        from tests.conftest import iter_routes

        routes = [r.path for r in iter_routes(app.routes)]
        assert "/health/ready" in routes


class TestStructuredLogging:
    """Tests for structured logging."""

    def test_structlog_available(self):
        """Test that structlog can be imported."""
        try:
            import structlog

            assert hasattr(structlog, "configure")
            assert hasattr(structlog, "get_logger")
        except ImportError:
            pytest.skip("structlog not installed")

    def test_resilience_logger_integration(self):
        """Test that resilience logger works without structlog."""
        from app.core.resilience import retry_with_backoff

        call_count = 0

        @retry_with_backoff(max_attempts=2, base_delay=0.01)
        async def failing_func():
            nonlocal call_count
            call_count += 1
            raise ValueError("Test error")

        with pytest.raises(ValueError):
            asyncio.run(failing_func())

        assert call_count == 2  # Initial + 1 retry
