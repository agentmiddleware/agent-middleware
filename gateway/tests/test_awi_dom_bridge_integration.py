"""
Tests for Phase 9.2: Playwright DOM Bridge Integration
=====================================================

Tests that AWISessionManager routes to Playwright when DOM bridge is attached.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestAWISessionManagerDOMBridge:
    """Tests for AWISessionManager DOM bridge routing."""

    def test_manager_has_playwright_bridge(self):
        """Verify AWISessionManager initializes with Playwright bridge."""
        from app.services.awi_session import AWISessionManager

        manager = AWISessionManager()
        assert manager._playwright_bridge is not None
        assert hasattr(manager, "_dom_sessions")
        assert isinstance(manager._dom_sessions, dict)

    def test_manager_has_dom_attach_method(self):
        """Verify AWISessionManager has attach_dom_bridge method."""
        from app.services.awi_session import AWISessionManager

        manager = AWISessionManager()
        assert hasattr(manager, "attach_dom_bridge")
        assert callable(manager.attach_dom_bridge)

    def test_manager_has_dom_detach_method(self):
        """Verify AWISessionManager has detach_dom_bridge method."""
        from app.services.awi_session import AWISessionManager

        manager = AWISessionManager()
        assert hasattr(manager, "detach_dom_bridge")
        assert callable(manager.detach_dom_bridge)

    def test_manager_has_dom_status_method(self):
        """Verify AWISessionManager has get_dom_bridge_status method."""
        from app.services.awi_session import AWISessionManager

        manager = AWISessionManager()
        assert hasattr(manager, "get_dom_bridge_status")
        assert callable(manager.get_dom_bridge_status)

    def test_manager_has_execute_via_dom_bridge_method(self):
        """Verify AWISessionManager has _execute_via_dom_bridge method."""
        from app.services.awi_session import AWISessionManager

        manager = AWISessionManager()
        assert hasattr(manager, "_execute_via_dom_bridge")
        assert callable(manager._execute_via_dom_bridge)


class TestDOMBridgeEndpoints:
    """Tests for DOM bridge router endpoints."""

    def test_attach_dom_endpoint_exists(self):
        """Verify /dom/attach endpoint is defined (with router prefix)."""
        from app.routers.awi_enhanced import router

        routes = [r.path for r in router.routes]
        assert "/v1/awi/dom/attach" in routes

    def test_detach_dom_endpoint_exists(self):
        """Verify /dom/attach/{session_id} DELETE endpoint is defined."""
        from app.routers.awi_enhanced import router

        routes = [r.path for r in router.routes]
        assert "/v1/awi/dom/attach/{session_id}" in routes

    def test_dom_status_endpoint_exists(self):
        """Verify /dom/attach/{session_id}/status endpoint is defined."""
        from app.routers.awi_enhanced import router

        routes = [r.path for r in router.routes]
        assert "/v1/awi/dom/attach/{session_id}/status" in routes


class TestDOMBridgeRouting:
    """Tests for DOM bridge action routing logic."""

    @pytest.mark.asyncio
    async def test_execute_routes_to_dom_when_attached(self):
        """Verify execute_action routes to DOM bridge when session is attached."""
        from app.services.awi_session import AWISessionManager
        from app.services.awi_playwright_bridge import BridgeSession
        from app.schemas.awi import (
            AWISessionCreate,
            AWIStandardAction,
            AWIExecutionRequest,
        )

        manager = AWISessionManager()

        session = await manager.create_session(
            AWISessionCreate(target_url="https://example.com")
        )

        dom_session = BridgeSession(
            session_id="dom-123",
            current_url="https://example.com",
        )
        manager._dom_sessions[session.session_id] = dom_session.session_id
        # The bridge must actually know the session: previously a missing
        # bridge session raised inside _execute_via_dom_bridge and the manager
        # silently fell back to the mock logic, which is what this test was
        # (unknowingly) asserting on.
        manager._playwright_bridge._sessions[dom_session.session_id] = dom_session

        with patch.object(
            manager._playwright_bridge, "translate_action", new_callable=AsyncMock
        ) as mock_translate:
            with patch.object(
                manager._playwright_bridge, "execute_commands", new_callable=AsyncMock
            ) as mock_execute:
                mock_translate.return_value = []
                mock_execute.return_value = MagicMock(
                    success=True,
                    commands_executed=1,
                    new_url="https://example.com/results",
                    error=None,
                    duration_ms=100,
                )

                request = AWIExecutionRequest(
                    session_id=session.session_id,
                    action=AWIStandardAction.SEARCH_AND_SORT,
                    parameters={"query": "test"},
                )

                response = await manager.execute_action(request)

                assert response.status == "success"
                mock_translate.assert_called_once()
                mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_returns_typed_failure_when_dom_bridge_fails(self):
        """A raising DOM bridge yields status="error", never mock success."""
        from app.services.awi_session import AWISessionManager
        from app.schemas.awi import (
            AWISessionCreate,
            AWIStandardAction,
            AWIExecutionRequest,
        )

        manager = AWISessionManager()
        session = await manager.create_session(
            AWISessionCreate(target_url="https://example.com")
        )
        # Attached in the manager's eyes, but unknown to the bridge: the
        # bridge call raises.
        manager._dom_sessions[session.session_id] = "dom-missing"

        response = await manager.execute_action(
            AWIExecutionRequest(
                session_id=session.session_id,
                action=AWIStandardAction.NAVIGATE_TO,
                parameters={"url": "https://example.com/page2"},
            )
        )

        assert response.status == "error"
        assert response.error is not None
        assert response.error.startswith("dom_bridge_failed")
        assert response.result is None

    @pytest.mark.asyncio
    async def test_execute_uses_mock_when_not_attached(self):
        """Verify execute_action uses mock logic when no DOM bridge attached."""
        from app.services.awi_session import AWISessionManager
        from app.schemas.awi import (
            AWISessionCreate,
            AWIStandardAction,
            AWIExecutionRequest,
        )

        manager = AWISessionManager()

        session = await manager.create_session(
            AWISessionCreate(target_url="https://example.com")
        )

        request = AWIExecutionRequest(
            session_id=session.session_id,
            action=AWIStandardAction.NAVIGATE_TO,
            parameters={"url": "https://example.com/page2"},
        )

        response = await manager.execute_action(request)

        assert response.status == "success"


class TestDOMBridgeAttachDetach:
    """Tests for attach/detach DOM bridge operations."""

    @pytest.mark.asyncio
    async def test_attach_dom_creates_bridge_session(self):
        """Verify attach_dom_bridge creates a DOM bridge session."""
        from app.services.awi_session import AWISessionManager
        from app.services.awi_playwright_bridge import BridgeSession
        from app.schemas.awi import AWISessionCreate

        manager = AWISessionManager()

        session = await manager.create_session(
            AWISessionCreate(target_url="https://example.com")
        )

        with patch.object(
            manager._playwright_bridge, "create_session", new_callable=AsyncMock
        ) as mock_create:
            with patch.object(
                manager._playwright_bridge,
                "extract_state_representation",
                new_callable=AsyncMock,
            ) as mock_extract:
                mock_create.return_value = BridgeSession(
                    session_id="dom-new-123",
                    current_url="https://example.com",
                )
                mock_extract.return_value = {
                    "url": "https://example.com",
                    "page_type": "generic",
                    "interactive_elements": [],
                }

                result = await manager.attach_dom_bridge(session.session_id)

                assert result["status"] == "attached"
                assert result["dom_session_id"] == "dom-new-123"
                assert session.session_id in manager._dom_sessions

    @pytest.mark.asyncio
    async def test_detach_dom_cleans_up(self):
        """Verify detach_dom_bridge cleans up the session."""
        from app.services.awi_session import AWISessionManager
        from app.schemas.awi import AWISessionCreate

        manager = AWISessionManager()

        session = await manager.create_session(
            AWISessionCreate(target_url="https://example.com")
        )
        manager._dom_sessions[session.session_id] = "dom-to-detach"

        with patch.object(
            manager._playwright_bridge, "destroy_session", new_callable=AsyncMock
        ) as mock_destroy:
            mock_destroy.return_value = True

            result = await manager.detach_dom_bridge(session.session_id)

            assert result["status"] == "detached"
            assert session.session_id not in manager._dom_sessions
            mock_destroy.assert_called_once_with("dom-to-detach")

    @pytest.mark.asyncio
    async def test_detach_dom_when_not_attached(self):
        """Verify detach_dom_bridge handles not-attached case."""
        from app.services.awi_session import AWISessionManager
        from app.schemas.awi import AWISessionCreate

        manager = AWISessionManager()

        session = await manager.create_session(
            AWISessionCreate(target_url="https://example.com")
        )

        result = await manager.detach_dom_bridge(session.session_id)

        assert result["status"] == "not_attached"

    @pytest.mark.asyncio
    async def test_destroy_session_detaches_dom(self):
        """Verify destroy_session also detaches DOM bridge."""
        from app.services.awi_session import AWISessionManager
        from app.schemas.awi import AWISessionCreate

        manager = AWISessionManager()

        session = await manager.create_session(
            AWISessionCreate(target_url="https://example.com")
        )
        manager._dom_sessions[session.session_id] = "dom-to-cleanup"

        with patch.object(
            manager._playwright_bridge, "destroy_session", new_callable=AsyncMock
        ) as mock_destroy:
            mock_destroy.return_value = True

            await manager.destroy_session(session.session_id)

            assert session.session_id not in manager._sessions
            assert session.session_id not in manager._dom_sessions
