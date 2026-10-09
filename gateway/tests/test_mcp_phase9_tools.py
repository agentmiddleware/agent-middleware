"""Focused coverage for gateway/app/services/mcp_phase9_tools.py.

Tests only. Covers the tool catalog invariants, the endpoint-pointer
wrappers, the passkey challenge delegation (including provider errors),
the default preview handler, and registry round-trip.
"""

import pytest
from pydantic import BaseModel

from app.services import mcp_phase9_tools as phase9
from app.services.mcp_phase9_tools import (
    ALWAYS_GOVERNED_AWI_TOOLS,
    DEFAULT_MCP_STUB_SERVICE_IDS,
    MCP_PHASE9_TOOLS,
    PROOF_SURFACE_MCP_STUB_IDS,
    _make_default_service_handler,
    awi_dom_action_preview,
    awi_dom_bridge_session,
    awi_dom_state,
    awi_dom_sync,
    awi_memory_index,
    awi_passkey_challenge,
    awi_passkey_verify,
    awi_rag_query,
    awi_session_context,
    register_phase9_tools,
)
from app.services.service_registry import get_service_registry


@pytest.fixture
def clean_phase9_registrations():
    """Remove only the registrations this test added, leaving others alone."""
    registry = get_service_registry()
    before = {
        service_id
        for service_id in PROOF_SURFACE_MCP_STUB_IDS
        if registry.get_local(service_id) is not None
    }
    registered_flag = phase9._registered
    default_services_flag = phase9._default_services_registered
    try:
        yield registry
    finally:
        for service_id in PROOF_SURFACE_MCP_STUB_IDS:
            if service_id not in before:
                registry.unregister_local(service_id)
        phase9._registered = registered_flag
        phase9._default_services_registered = default_services_flag


class TestToolCatalog:
    def test_nine_tools_match_always_governed_set(self):
        ids = [tool["service_id"] for tool in MCP_PHASE9_TOOLS]
        assert len(ids) == 9
        assert len(set(ids)) == 9
        assert set(ids) == set(ALWAYS_GOVERNED_AWI_TOOLS)

    def test_all_tools_require_permit_with_positive_pricing(self):
        for tool in MCP_PHASE9_TOOLS:
            assert tool["require_permit"] is True
            assert tool["credits_per_unit"] > 0
            assert tool["unit_name"]
            assert tool["name"] and tool["description"]
            assert callable(tool["func"])
            assert isinstance(tool["input_model"], type)
            assert issubclass(tool["input_model"], BaseModel)

    def test_proof_surface_ids_are_union_of_both_sets(self):
        assert PROOF_SURFACE_MCP_STUB_IDS == (
            ALWAYS_GOVERNED_AWI_TOOLS | DEFAULT_MCP_STUB_SERVICE_IDS
        )
        assert DEFAULT_MCP_STUB_SERVICE_IDS == frozenset(
            {
                "data-indexer",
                "content-generator",
                "telemetry-processor",
                "semantic-search",
            }
        )


class TestWrapperPointers:
    async def test_passkey_verify_returns_endpoint_pointer(self):
        result = await awi_passkey_verify("cid-1", {"id": "cred"})
        assert result == {
            "endpoint": "/v1/awi/passkey/verify",
            "method": "POST",
            "requires": ["challenge_id", "credential"],
        }

    async def test_dom_wrappers_return_endpoint_pointers(self):
        assert await awi_dom_bridge_session("https://example.com") == {
            "endpoint": "/v1/awi/dom/session",
            "method": "POST",
            "requires": ["target_url"],
        }
        assert await awi_dom_sync("sess-1", "click") == {
            "endpoint": "/v1/awi/dom/sync",
            "method": "POST",
            "requires": ["session_id", "action"],
        }
        assert await awi_dom_state("sess-1") == {
            "endpoint": "/v1/awi/dom/state",
            "method": "GET",
            "requires": ["session_id"],
        }
        assert await awi_dom_action_preview("sess-1", "click") == {
            "endpoint": "/v1/awi/dom/preview",
            "method": "POST",
            "requires": ["session_id", "action"],
        }

    async def test_memory_wrappers_point_at_scoped_http_routes(self):
        # RAG wrappers must stay endpoint pointers so tenant scoping happens
        # in the HTTP route, never by searching a shared store here.
        assert await awi_memory_index("sess-1", "browser") == {
            "endpoint": "/v1/awi/rag/index",
            "method": "POST",
            "requires": ["session_id", "session_type"],
        }
        assert await awi_rag_query("past logins") == {
            "endpoint": "/v1/awi/rag/query",
            "method": "POST",
            "requires": ["query"],
        }
        assert await awi_session_context("sess-1") == {
            "endpoint": "/v1/awi/rag/context",
            "method": "POST",
            "requires": ["current_session_id"],
        }


class TestPasskeyChallenge:
    async def test_delegates_to_provider_and_marks_governed(self, monkeypatch):
        seen = {}

        class FakeProvider:
            async def create_challenge(self, session_id, action):
                seen["session_id"] = session_id
                seen["action"] = action
                return {"challenge_id": "cid-1", "challenge": "abc"}

        from app.services import webauthn_provider as provider_module

        monkeypatch.setattr(
            provider_module, "get_webauthn_provider", lambda: FakeProvider()
        )
        result = await awi_passkey_challenge("sess-1", "login")
        assert result["status"] == "ok"
        assert result["governed"] is True
        assert result["tool"] == "awi_passkey_challenge"
        assert result["challenge_id"] == "cid-1"
        assert seen == {"session_id": "sess-1", "action": "login"}

    async def test_provider_error_propagates(self, monkeypatch):
        class FailingProvider:
            async def create_challenge(self, session_id, action):
                raise RuntimeError("provider down")

        from app.services import webauthn_provider as provider_module

        monkeypatch.setattr(
            provider_module, "get_webauthn_provider", lambda: FailingProvider()
        )
        with pytest.raises(RuntimeError, match="provider down"):
            await awi_passkey_challenge("sess-1", "login")


class TestDefaultServiceHandler:
    async def test_preview_payload_sorts_input_keys(self):
        service_def = {
            "service_id": "data-indexer",
            "name": "Data Indexer",
            "category": next(tool["category"] for tool in MCP_PHASE9_TOOLS),
            "unit_name": "document",
        }
        handler = _make_default_service_handler(service_def)
        result = await handler(zebra=1, apple=2)
        assert result["status"] == "preview_available"
        assert result["service_id"] == "data-indexer"
        assert result["input_keys"] == ["apple", "zebra"]

    async def test_preview_payload_with_no_inputs(self):
        service_def = {
            "service_id": "semantic-search",
            "name": "Semantic Search",
            "category": next(tool["category"] for tool in MCP_PHASE9_TOOLS),
            "unit_name": "query",
        }
        handler = _make_default_service_handler(service_def)
        result = await handler()
        assert result["input_keys"] == []
        assert result["mode"] == "agent_operations_control_plane_preview"


class TestRegistration:
    def test_register_phase9_tools_marks_require_permit(
        self, clean_phase9_registrations
    ):
        registry = clean_phase9_registrations
        register_phase9_tools()
        for tool in MCP_PHASE9_TOOLS:
            service = registry.get_local(tool["service_id"])
            assert service is not None
            assert service["require_permit"] is True
            assert service["name"] == tool["name"]

    def test_unregister_proof_surface_stubs_removes_phase9_tools(
        self, clean_phase9_registrations
    ):
        register_phase9_tools()
        phase9.unregister_proof_surface_mcp_stubs()
        registry = get_service_registry()
        for service_id in ALWAYS_GOVERNED_AWI_TOOLS:
            assert registry.get_local(service_id) is None
        # Restore entries for later tests in the same process. The fixture
        # prunes them again when they were not present before this test.
        register_phase9_tools()
