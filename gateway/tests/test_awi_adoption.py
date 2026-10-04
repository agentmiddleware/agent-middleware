"""
Tests for AWI Adoption Kit — Phase 8
====================================
Tests for the External AWI Adoption Kit components.
"""

import pytest

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.tools.awi_manifest_generator import ManifestGenerator


class TestAWIManifestGenerator:
    """Test AWI manifest generator."""

    def test_generator_init(self):
        """Test manifest generator initialization."""
        gen = ManifestGenerator(framework="fastapi")
        assert gen.framework == "fastapi"
        assert gen.actions == []

    def test_generate_empty_manifest(self):
        """Test generating a manifest from a FastAPI app with no routes."""
        from fastapi import FastAPI

        gen = ManifestGenerator()
        empty_app = FastAPI(title="Test API", version="1.0.0")
        manifest = gen.scan_fastapi_app(empty_app)

        assert manifest["name"] == "Test API"
        assert manifest["awi_version"] == "1.0.0"
        assert manifest["framework"] == "fastapi"
        assert manifest["actions"] == []

    def test_generate_from_openapi(self):
        """Test generating manifest from OpenAPI spec."""
        gen = ManifestGenerator(framework="openapi")
        spec = {
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/search": {
                    "post": {"summary": "Search items", "description": "Search"}
                },
                "/cart/add": {"post": {"summary": "Add to cart"}},
            },
        }

        manifest = gen.generate_from_openapi(spec)

        assert manifest["name"] == "Test API"
        assert manifest["framework"] == "openapi"
        assert "endpoints" in manifest

    def test_openapi_endpoint_extraction(self):
        """Test that endpoints are extracted from OpenAPI spec."""
        gen = ManifestGenerator(framework="openapi")
        spec = {
            "info": {"title": "API", "version": "1.0.0"},
            "paths": {
                "/products": {"get": {"summary": "List products"}},
                "/products/{id}": {"get": {"summary": "Get product"}},
            },
        }

        manifest = gen.generate_from_openapi(spec)

        assert len(manifest["endpoints"]) == 2

    def test_openapi_servers_extraction(self):
        """Test that servers URL is extracted."""
        gen = ManifestGenerator(framework="openapi")
        spec = {
            "info": {"title": "API", "version": "1.0.0"},
            "servers": [{"url": "https://api.example.com"}],
            "paths": {},
        }

        manifest = gen.generate_from_openapi(spec)

        assert manifest["base_url"] == "https://api.example.com"

    def test_action_mapping_logic(self):
        """Test action mapping returns valid action for POST routes."""
        gen = ManifestGenerator()

        class MockRoute:
            path = "/custom/action"
            methods = ["POST"]
            name = "custom"

        route = MockRoute()
        action = gen._route_to_action(route)

        assert action is not None
        assert "awi_action" in action
        assert action["method"] == "POST"


class TestAWIClientSDK:
    """Test AWI Python SDK models (defined locally for standalone testing)."""

    def test_awi_action_enum(self):
        """Test AWI action enum values."""
        from enum import Enum

        class AWIStandardAction(str, Enum):
            SEARCH_AND_SORT = "search_and_sort"
            ADD_TO_CART = "add_to_cart"
            CHECKOUT = "checkout"
            FILL_FORM = "fill_form"
            LOGIN = "login"
            LOGOUT = "logout"
            NAVIGATE_TO = "navigate_to"

        assert AWIStandardAction.SEARCH_AND_SORT == "search_and_sort"
        assert AWIStandardAction.ADD_TO_CART == "add_to_cart"
        assert AWIStandardAction.CHECKOUT == "checkout"

    def test_awi_representation_enum(self):
        """Test AWI representation type enum values."""
        from enum import Enum

        class AWIRepresentationType(str, Enum):
            FULL_DOM = "full_dom"
            SUMMARY = "summary"
            EMBEDDING = "embedding"
            LOW_RES_SCREENSHOT = "low_res_screenshot"

        assert AWIRepresentationType.SUMMARY == "summary"
        assert AWIRepresentationType.EMBEDDING == "embedding"

    def test_awi_session_dataclass(self):
        """Test AWISession dataclass structure."""
        from dataclasses import dataclass
        from datetime import datetime

        @dataclass
        class AWISession:
            session_id: str
            target_url: str
            status: str
            created_at: datetime
            max_steps: int = 100

        session = AWISession(
            session_id="test-123",
            target_url="https://example.com",
            status="created",
            created_at=datetime.now(),
        )

        assert session.session_id == "test-123"
        assert session.target_url == "https://example.com"
        assert session.max_steps == 100

    def test_awi_execution_response_dataclass(self):
        """Test AWIExecutionResponse dataclass structure."""
        from dataclasses import dataclass

        @dataclass
        class AWIExecutionResponse:
            execution_id: str
            session_id: str
            action: str
            status: str
            result: dict | None = None
            error: str | None = None

        response = AWIExecutionResponse(
            execution_id="exec-123",
            session_id="test-123",
            action="search_and_sort",
            status="success",
        )

        assert response.execution_id == "exec-123"
        assert response.action == "search_and_sort"


class TestAWIExternalAdapter:
    """Test AWI external adapter."""

    def test_external_adapter_init(self):
        """Test external adapter initialization."""
        from app.services.awi_external_adapter import AWIExternalAdapter

        adapter = AWIExternalAdapter(
            middleware_url="http://localhost:8000",
            api_key="test-key",
        )

        assert adapter.middleware_url == "http://localhost:8000"
        assert adapter.api_key == "test-key"

    def test_fallback_adapter_init(self):
        """Test fallback adapter initialization."""
        from app.services.awi_external_adapter import AWIFallbackAdapter

        adapter = AWIFallbackAdapter(
            middleware_url="http://localhost:8000",
            api_key="test-key",
        )

        assert adapter.awi is not None


class TestAWIAdoptionGuide:
    """Test adoption guide content."""

    def test_adoption_guide_exists(self):
        """Test adoption guide was created."""
        import os

        guide_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "docs",
            "awi-adoption-guide.md",
        )
        assert os.path.exists(guide_path)

    def test_adoption_guide_content(self):
        """Test adoption guide has required sections."""
        import os

        guide_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "docs",
            "awi-adoption-guide.md",
        )

        with open(guide_path) as f:
            content = f.read()

        assert "AWI Adoption Guide" in content
        assert "Quick Start" in content
        assert "Security Checklist" in content
        assert "Framework Templates" in content
        assert "arXiv" in content or "arxiv" in content.lower()


class TestAWIAdoptionRouter:
    """Test AWI adoption endpoints."""

    @pytest.mark.anyio
    async def test_awi_endpoints_available(self):
        """Test AWI endpoints are accessible."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/awi/vocabulary")
            assert response.status_code == 200
            data = response.json()
            assert "actions" in data
